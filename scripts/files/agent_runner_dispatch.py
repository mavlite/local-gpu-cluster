#!/usr/bin/env python3
"""Agent-runner work-queue dispatcher (agent-runner queue spec 2026-10-30, section 6).

Runs ON VM 178 as user `runner`, from agent-runner-dispatch.timer every 5 minutes. One task per run:

  1. flock; rotate event logs older than 30 days.
  2. If the router's loaded chat profile has no lane for scoped keys, stop quietly (nothing is claimed).
  3. Oldest open `runner:ready` issue across DISPATCH_REPOS (in that order on ties) that carries no other
     runner:* label and whose repo passes `agent-run --preflight` (sandbox entry + agent_egress table loaded;
     a repo that is not ready is skipped and its tasks stay runner:ready, untouched).
     Claim it: runner:ready -> runner:running plus a start comment.
  4. Check out the issue's "Branch / commit" in ~/work/<repo> (fetch, switch, pull --ff-only, verify sha).
  5. agent-run <dir> "<preamble + issue body>" with the issue's max wall time; raw JSON events to
     ~/runner-logs/<repo>-<issue>-<UTC>.jsonl. agent-run runs opencode as the unprivileged user `agent` in a
     systemd sandbox: the checkout read-only at /work/<repo>, a per-task results dir at /results
     (AGENT_RESULTS_DIR on the host side), private /tmp, no /home, no /etc/agent-runner, no GitHub credentials.
  6. Exit 75 (lane closed) or 76 (sandbox not ready after all): back to runner:ready with a comment. Otherwise take the report block the agent
     PRINTED (the last text event of the stream holding `task: N` and a verdict) and post it here; if there is
     none (or it is malformed), post a BLOCKED one with the redacted tail of the event log.
     Set runner:done / runner:blocked, plus needs-claude unless PASS with no deviations and a clean exit.

The dispatcher is a plain script, not an opencode session: it alone posts comments and edits labels.
Nothing it posts may contain tokens, keys or environment dumps (see redact(), which also removes the literal
values of the runner's own secret files).
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request

LANE_MODELS = {"rag-qwen3.6": "qwen3.6", "qwen3.8": "qwen3.8-nothink"}   # mirrors agent-run
LABEL_READY, LABEL_RUNNING = "runner:ready", "runner:running"
LABEL_DONE, LABEL_BLOCKED, LABEL_CLAUDE = "runner:done", "runner:blocked", "needs-claude"
# Tiers (owner decision 2026-10-30): tier:a = no servers, tier:b = staff test servers, tier:c = never auto-run.
# Exactly one tier label is required. An open issue labelled runner:halt (the pinned "Runner control" issue)
# halts that repo's tier:b tasks; tier:a still runs.
TIER_A, TIER_B, TIER_C = "tier:a", "tier:b", "tier:c"
TIERS = (TIER_A, TIER_B, TIER_C)
LABEL_HALT = "runner:halt"
HALTED_PREFIX = "runner: halted"
VERDICTS = ("PASS", "FAIL", "INDETERMINATE", "BLOCKED")
DEFAULT_TIMEOUT_MIN, MAX_TIMEOUT_MIN = 60, 720
LOG_KEEP_DAYS = 30
EXIT_LANE_CLOSED = 75
EXIT_NOT_READY = 76      # agent-run: sandbox entry or agent_egress table missing; nothing ran
PREFLIGHT_TIMEOUT_S = 60
TAIL_LINES, TAIL_LINE_CHARS, COMMENT_MAX = 50, 300, 60000


def parse_mirrors(spec):
    """'SDG-RUST:upstream,SDG-X:a' -> {'SDG-RUST': ['upstream'], 'SDG-X': ['a']}"""
    out = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        repo, _, branch = item.partition(":")
        out.setdefault(repo, []).append(branch)
    return out


def cfg():
    home = os.path.expanduser("~")
    return {
        "repos": [r.strip() for r in os.environ.get(
            "DISPATCH_REPOS", "DraconisCluster/SDG-RUST,DraconisCluster/SDG-ARMA3").split(",") if r.strip()],
        "work": os.environ.get("DISPATCH_WORK_DIR", os.path.join(home, "work")),
        "logs": os.environ.get("DISPATCH_LOG_DIR", os.path.join(home, "runner-logs")),
        "healthz": os.environ.get("DISPATCH_HEALTHZ", "http://192.168.6.153:8000/healthz"),
        "gh": shlex.split(os.environ.get("DISPATCH_GH", "gh")),
        "agent": shlex.split(os.environ.get("DISPATCH_AGENT_RUN", "agent-run")),
        "max_timeout_s": int(os.environ.get("DISPATCH_MAX_TIMEOUT_S", str(MAX_TIMEOUT_MIN * 60))),
        "results_root": os.environ.get("DISPATCH_RESULTS_ROOT", "/var/lib/agent-results"),
        "retry_sleep_s": float(os.environ.get("DISPATCH_RETRY_SLEEP_S", "5")),
        # repo name -> local branches kept equal to origin's (SDG-RUST tools/build.py reads `upstream:...`)
        "mirror_branches": parse_mirrors(os.environ.get("DISPATCH_MIRROR_BRANCHES", "SDG-RUST:upstream")),
        "secret_files": [p for p in os.environ.get(
            "DISPATCH_SECRET_FILES",
            "/etc/agent-runner/router.key,/etc/agent-runner/gh-token,/etc/agent-runner/ptero-156.env").split(",") if p],
    }


LITERAL_SECRETS = []   # exact secret values, loaded once per run by load_secrets()


def load_secrets(c):
    """Bare-value files (router.key, gh-token) contribute their content; KEY=value files (ptero-156.env)
    contribute each value. Values shorter than 8 characters are too generic to redact literally."""
    LITERAL_SECRETS.clear()
    for path in c["secret_files"]:
        try:
            with open(path) as f:
                raw = f.read()
        except OSError:
            continue
        lines = [l.strip() for l in raw.splitlines() if l.strip() and not l.strip().startswith("#")]
        if lines and all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", l) for l in lines):
            values = [l.split("=", 1)[1].strip().strip("'\"") for l in lines]
        else:
            values = [raw.strip()]
        LITERAL_SECRETS.extend(v for v in values if len(v) >= 8)
    LITERAL_SECRETS.sort(key=len, reverse=True)       # longest first, so a value inside another is not split


def log(msg):
    print(f"agent-runner-dispatch: {msg}", file=sys.stderr, flush=True)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- redaction / parsing ---
_SECRETS = [
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)\b(bearer|token|authorization)\s*[:=]?\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASS)[A-Z0-9_]*)\s*=\s*\S+"),
    re.compile(r"\b[0-9a-fA-F]{48,}\b"),          # router/API keys (64 hex); git SHAs (40) are kept
]


def redact(text):
    for v in LITERAL_SECRETS:
        text = text.replace(v, "<redacted>")
    for pat in _SECRETS:
        text = pat.sub("<redacted>", text)
    return text


def sections(body):
    """Issue-form body -> {heading: text}. Forms render each field as '### Heading' + value."""
    out, cur = {}, None
    for line in (body or "").splitlines():
        m = re.match(r"^#{2,4}\s+(.+?)\s*$", line)
        if m:
            cur = m.group(1).strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def field(secs, *names):
    for name in names:
        for k, v in secs.items():
            if k.lower().startswith(name.lower()):
                return v
    return ""


_REF = re.compile(r"^([A-Za-z0-9._/-]+?)(?:@([0-9a-fA-F]{7,40}))?$")


_SWITCH = re.compile(r"\bgit\s+(?:switch|checkout)\s+([A-Za-z0-9._/][A-Za-z0-9._/-]*)")
_SHA_EXPECT = re.compile(r"(?i)\b(?:expect(?:ed)?|sha)\b\s*[:=]?\s*([0-9a-f]{7,40})\b")
_SHA_BARE = re.compile(r"^[0-9a-fA-F]{40}$")


def parse_ref(text):
    """(branch, sha-or-None) from the Branch / commit field. Accepts, anywhere in the field (code fences ignored):
    `branch`, `branch@sha`, the spec's `git switch <branch> && git pull --ff-only` (or `git checkout <branch>`),
    and the sha to verify as `# expect: <sha>` or a bare 40-hex line. Exactly one branch must be named.
    SDG-RUST#3 (2026-10-30) used a ```shell fence and the first-line parser took `shell` as the branch."""
    branches, shas = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        bare = line.strip("`").strip()
        if _SHA_BARE.match(bare):
            shas.append(bare.lower())
            continue
        m = _SHA_EXPECT.search(line)
        if m:
            shas.append(m.group(1).lower())
            continue
        found = _SWITCH.findall(line)
        if found:
            branches += found
            continue
        m = _REF.match(bare)
        if m:
            branches.append(m.group(1))
            if m.group(2):
                shas.append(m.group(2).lower())
    distinct = sorted(set(branches))
    if len(distinct) != 1 or distinct[0].startswith("-") or ".." in distinct[0]:
        what = "no branch" if not distinct else f"branches {', '.join(distinct)}"
        raise ValueError(f"found {what}; name exactly one: `branch`, `branch@sha` or `git switch <branch>`")
    if len(set(shas)) > 1:
        raise ValueError(f"more than one sha to verify: {', '.join(sorted(set(shas)))}")
    return distinct[0], (shas[0] if shas else None)


def parse_timeout_minutes(text):
    m = re.search(r"(?i)max(?:imum)?\s+wall\s*time\D{0,10}?(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?)\b",
                  text or "")
    if not m:
        return DEFAULT_TIMEOUT_MIN
    n = float(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1)
    return int(max(1, min(n, MAX_TIMEOUT_MIN)))


def event_texts(path):
    """Text parts of an opencode --format json event stream, in order."""
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                part = ev.get("part") if isinstance(ev, dict) else None
                if ev.get("type") == "text" and isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.append(part["text"])
    except OSError:
        pass
    return out


def report_block(text, task):
    """The report block in one text, normalised to '### runner report' + a yaml fence, or None. The heading is
    what the preamble asks for; a bare `task:` ... `verdict:` yaml also counts (qwen3.6 dropped the heading and
    the fence on the first live run)."""
    m = re.search(r"(?m)^task:\s*#?(\d+)\s*$", text)
    if not m or int(m.group(1)) != int(task) or not re.search(r"(?m)^verdict:\s*\S", text):
        return None
    yaml = text[m.start():]
    yaml = yaml.split("```", 1)[0].rstrip()          # stop at the closing fence, if any
    v = re.search(r"(?m)^verdict:\s*([A-Z]+)\s*$", yaml)
    dev = re.search(r"(?m)^deviations:\s*(.*)$", yaml)
    return {"block": f"### runner report\n```yaml\n{yaml}\n```",
            "verdict": v.group(1) if v and v.group(1) in VERDICTS else None,
            "deviations": dev.group(1).strip() if dev else ""}


def find_report(texts, task):
    """The last printed report block for this task, from the agent's text events."""
    for t in reversed(texts):
        rep = report_block(t, task)
        if rep:
            return rep
    return None


def needs_claude(verdict, deviations, exit_code):
    clean = deviations.strip().strip("'\"").lower() in ("none", "no", "[]")
    return not (verdict == "PASS" and clean and exit_code == 0)


# ------------------------------------------------------------------------------------- plumbing ---
def gh(c, *args, check=True):
    r = subprocess.run(c["gh"] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


def comment(c, repo, num, text):
    gh(c, "issue", "comment", str(num), "-R", repo, "--body", redact(text)[:COMMENT_MAX])


RELABEL_TRIES = 4


def relabel(c, repo, num, remove=(), add=()):
    """GitHub can apply part of a label edit and then fail (GraphQL "Something went wrong": SDG-ARMA3#1 lost
    runner:running but never got runner:done, 2026-10-30). Retry with backoff; before each retry read the labels
    back and send only what is still missing. Raises if the last try fails."""
    remove, add = list(remove), list(add)
    for attempt in range(1, RELABEL_TRIES + 1):
        args = ["issue", "edit", str(num), "-R", repo]
        if remove:
            args += ["--remove-label", ",".join(remove)]
        if add:
            args += ["--add-label", ",".join(add)]
        try:
            gh(c, *args)
            return
        except RuntimeError as e:
            if attempt == RELABEL_TRIES:
                raise
            log(f"{repo}#{num}: label edit failed (try {attempt}/{RELABEL_TRIES}), retrying: {e}")
        time.sleep(c["retry_sleep_s"] * attempt)
        try:
            view = json.loads(gh(c, "issue", "view", str(num), "-R", repo, "--json", "labels"))
            have = {lab["name"] for lab in view["labels"]}
        except (RuntimeError, ValueError, KeyError) as e:
            log(f"{repo}#{num}: could not read labels back ({e}); resending the whole edit")
            continue
        remove = [lab for lab in remove if lab in have]
        add = [lab for lab in add if lab not in have]
        if not remove and not add:
            return


def lane_model(c):
    try:
        with urllib.request.urlopen(c["healthz"], timeout=10) as r:
            profile = json.load(r).get("active_chat_profile")
    except Exception as e:                      # router unreachable: same as a closed lane
        log(f"healthz unreadable ({e}); not claiming")
        return None
    return LANE_MODELS.get(profile)


def rotate_logs(c):
    cutoff = time.time() - LOG_KEEP_DAYS * 86400
    for name in os.listdir(c["logs"]):
        p = os.path.join(c["logs"], name)
        if (name.endswith(".jsonl") or name.endswith(".stderr")) and os.path.getmtime(p) < cutoff:
            os.remove(p)


def ready_issues(c, only):
    found = []
    for order, repo in enumerate(c["repos"]):
        if only and only[0] != repo:
            continue
        rows = json.loads(gh(c, "issue", "list", "-R", repo, "-l", LABEL_READY, "--state", "open",
                             "--json", "number,title,body,createdAt,labels", "--limit", "100") or "[]")
        for i in rows:
            names = {l["name"] for l in i.get("labels", [])}
            if only and int(only[1]) != i["number"]:
                continue
            if any(n.startswith("runner:") and n != LABEL_READY for n in names):
                log(f"{repo}#{i['number']} carries runner:ready plus another runner:* label; skipping")
                continue
            found.append((i["createdAt"], order, repo, i))
    return [(repo, i) for _, _, repo, i in sorted(found, key=lambda t: (t[0], t[1], t[3]["number"]))]


def halted(c, repo):
    """True while any OPEN issue in the repo carries runner:halt (the pinned "Runner control" issue). Keyed on the
    label alone, so a renamed or unpinned control issue still halts: the safe direction."""
    rows = json.loads(gh(c, "issue", "list", "-R", repo, "-l", LABEL_HALT, "--state", "open",
                         "--json", "number", "--limit", "5") or "[]")
    return bool(rows)


def tier_of(issue):
    names = [l["name"] for l in issue.get("labels", [])]
    tiers = [n for n in names if n in TIERS]
    return tiers[0] if len(tiers) == 1 else None


def refuse_tier(c, repo, issue):
    names = [l["name"] for l in issue.get("labels", [])]
    tiers = [n for n in names if n in TIERS]
    if not tiers:
        why = "it has no tier label"
    elif len(tiers) > 1:
        why = f"it has more than one tier label ({', '.join(tiers)})"
    else:
        why = "tier:c tasks are never auto-run"
    comment(c, repo, issue["number"],
            f"runner: refused -- {why}. The runner only runs runner:ready issues with exactly one of tier:a "
            "(no servers) or tier:b (staff test servers). Fix the labels and set runner:ready again.")
    relabel(c, repo, issue["number"], remove=[LABEL_READY], add=[LABEL_BLOCKED])
    log(f"{repo}#{issue['number']}: refused ({why})")


def note_halted(c, repo, issue):
    """Leave a tier:b task runner:ready under a halt; comment once, not every 5 minutes."""
    view = json.loads(gh(c, "issue", "view", str(issue["number"]), "-R", repo, "--json", "comments"))
    last = (view.get("comments") or [{}])[-1].get("body", "") if view.get("comments") else ""
    if not last.startswith(HALTED_PREFIX):
        comment(c, repo, issue["number"],
                f"{HALTED_PREFIX} -- {repo} has an open runner:halt issue (Runner control), so tier:b tasks wait. "
                "This stays runner:ready and runs once the halt label is removed.")
    log(f"{repo}#{issue['number']}: tier:b held by runner:halt")


def runner_ready(c, repo):
    """`agent-run --preflight` for the repo's checkout: its sandbox entry exists and agent_egress is loaded.
    Asked before claiming, so a runner that is not ready leaves the task runner:ready instead of burning it."""
    cmd = c["agent"] + ["--preflight", os.path.join(c["work"], repo.split("/")[-1])]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=PREFLIGHT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"{repo}: agent-run preflight did not run ({e}); not claiming")
        return False
    if r.returncode != 0:
        log(f"{repo}: runner not ready (preflight exit {r.returncode}: {redact(r.stderr.strip())[-300:]}); "
            "not claiming")
        return False
    return True


def pick(c, queue, ready=lambda repo: True):
    """Apply tier and halt rules in queue order: refuse what may never run, hold tier:b under a halt, skip a repo
    whose runner is not ready (`ready`, asked once per repo), and return the first runnable (repo, issue) or None.
    Refusals do not use up the cycle."""
    halt, ok = {}, {}
    for repo, issue in queue:
        tier = tier_of(issue)
        if tier is None or tier == TIER_C:
            refuse_tier(c, repo, issue)
            continue
        if tier == TIER_B:
            if repo not in halt:
                halt[repo] = halted(c, repo)
            if halt[repo]:
                note_halted(c, repo, issue)
                continue
        if repo not in ok:
            ok[repo] = ready(repo)
        if not ok[repo]:
            continue
        return repo, issue
    return None


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def update_mirrors(path, branch, mirrors):
    """Fast-forward each local mirror branch to origin's (creating it if missing). Skips the checked-out task
    branch (pull --ff-only already did it) and branches origin doesn't have. Returns an error string or None."""
    for m in mirrors:
        if m == branch or git(path, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{m}").returncode != 0:
            continue
        r = git(path, "fetch", "origin", f"refs/heads/{m}:refs/heads/{m}")      # no '+': fast-forward only
        if r.returncode != 0:
            return f"cannot fast-forward local branch {m} to origin/{m}: {(r.stderr or r.stdout).strip()[:400]}"
    return None


def prepare_checkout(path, branch, sha, mirrors=()):
    """Returns (ref_string, error). Never discards local work: a dirty tree is an error."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return None, f"no clone at {path}"
    dirty = git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        return None, "the clone has uncommitted changes to tracked files:\n" + dirty[:1000]
    for step in (("fetch", "--prune", "origin"), ("switch", branch), ("pull", "--ff-only")):
        r = git(path, *step)
        if r.returncode != 0:
            return None, f"git {' '.join(step)} failed: {(r.stderr or r.stdout).strip()[:800]}"
    err = update_mirrors(path, branch, mirrors)
    if err:
        return None, err
    head = git(path, "rev-parse", "HEAD").stdout.strip()
    if sha and not head.lower().startswith(sha.lower()):
        return None, f"HEAD {head} does not match the requested {sha} on {branch}"
    return f"{branch}@{head}", None


def preamble(repo, num, ref, started, model):
    return f"""You are the lab runner for {repo} issue #{num}. Execute the task below exactly.

Rules:
- The current directory is the checkout at {ref}. It is mounted read-only. Run only the commands in Steps, in order.
- Write every log, result or artifact under /results (writable, kept after the run); use /tmp for scratch.
- Check every Precondition first; if one fails, or anything happens that Steps do not cover, stop and report BLOCKED.
- You have no GitHub access. Do not post, comment, push or change labels: the dispatcher posts your report.
- Never write secrets or player data anywhere.
- When finished (or stopped), PRINT the block below as your final message: the line `### runner report`, then the
  ```yaml fenced block, copied as-is (heading and fence included) and filled in.

### runner report
```yaml
task: {num}
verdict: PASS | FAIL | INDETERMINATE | BLOCKED
ref: {ref}
started: {started}
finished: <UTC ISO>
model: {model}
results:
  - check: <name>
    value: <number or string>
    pass: true | false
artifacts:
  - </results/... path of each log or result you wrote>
deviations: <none | what differed from the steps>
state_left: <servers, locks, branch>
```
The verdict follows the Pass criteria literally. You never decide that a FAIL is acceptable.

---- TASK (issue #{num}) ----
"""


def run_agent(c, work_dir, message, timeout_s, log_base, results_dir):
    """Returns (exit_code or None on timeout)."""
    env = dict(os.environ, AGENT_RESULTS_DIR=results_dir, AGENT_MAX_SECONDS=str(timeout_s + 60))
    with open(log_base + ".jsonl", "w") as out, open(log_base + ".stderr", "w") as err:
        p = subprocess.Popen(c["agent"] + [work_dir, message], stdout=out, stderr=err, env=env,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            return p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            for sig, grace in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
                try:
                    os.killpg(p.pid, sig)
                    p.wait(timeout=grace)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue
            return None


def tail(path, n=TAIL_LINES):
    try:
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()[-n:]
    except OSError:
        return ""
    return "\n".join(l if len(l) <= TAIL_LINE_CHARS else l[:TAIL_LINE_CHARS] + " ..." for l in lines)


def blocked_report(num, ref, started, model, reason, log_base):
    ev = tail(log_base + ".jsonl") if log_base else ""
    er = tail(log_base + ".stderr", 20) if log_base else ""
    text = (f"### runner report\n```yaml\ntask: {num}\nverdict: BLOCKED\nref: {ref or 'unknown'}\n"
            f"started: {started}\nfinished: {utc_now()}\nmodel: {model}\nresults: []\nartifacts:\n"
            f"  - {os.path.basename(log_base) + '.jsonl' if log_base else 'none'} (runner VM ~/runner-logs)\n"
            f"deviations: posted by the dispatcher: {reason.splitlines()[0][:200]}\nstate_left: unknown\n```\n\n"
            f"**Dispatcher:** {reason}\n")
    if ev:
        text += f"\nLast {TAIL_LINES} event-log lines:\n```text\n{ev}\n```\n"
    if er:
        text += f"\nagent-run stderr (tail):\n```text\n{er}\n```\n"
    return text


def dispatch(c, repo, issue, model):
    num = issue["number"]
    name = repo.split("/")[-1]
    started = utc_now()
    relabel(c, repo, num, remove=[LABEL_READY], add=[LABEL_RUNNING])
    comment(c, repo, num, f"runner: claimed at {started} (model {model}); starting.")
    log(f"claimed {repo}#{num}")

    secs = sections(issue.get("body", ""))
    timeout_s = min(parse_timeout_minutes(field(secs, "Limits")) * 60, c["max_timeout_s"])
    log_base = None
    try:
        branch, sha = parse_ref(field(secs, "Branch / commit", "Branch", "Commit"))
    except ValueError as e:
        return finish(c, repo, num, None, blocked_report(num, None, started, model,
                                                         f"Branch / commit: {e}", None))
    ref, err = prepare_checkout(os.path.join(c["work"], name), branch, sha,
                                c["mirror_branches"].get(name, ()))
    if err:
        return finish(c, repo, num, None, blocked_report(num, None, started, model, err, None))

    stamp = started.replace("-", "").replace(":", "")
    log_base = os.path.join(c["logs"], f"{name}-{num}-{stamp}")
    results_dir = os.path.join(c["results_root"], f"{name}-{num}-{stamp}")
    message = preamble(repo, num, ref, started, model) + issue.get("body", "")
    code = run_agent(c, os.path.join(c["work"], name), message, timeout_s, log_base, results_dir)

    if code == EXIT_LANE_CLOSED:
        relabel(c, repo, num, remove=[LABEL_RUNNING], add=[LABEL_READY])
        comment(c, repo, num, "runner: lane closed (the router's chat profile has no slot for the runner); "
                              "back to runner:ready, retrying next cycle.")
        log(f"{repo}#{num}: lane closed, requeued")
        return 0
    if code == EXIT_NOT_READY:
        relabel(c, repo, num, remove=[LABEL_RUNNING], add=[LABEL_READY])
        comment(c, repo, num, "runner: the runner's sandbox was not ready (agent-run exit 76: egress table or "
                              "sandbox entry missing); nothing ran. Back to runner:ready, retrying next cycle.")
        log(f"{repo}#{num}: sandbox not ready, requeued")
        return 0
    rep = find_report(event_texts(log_base + ".jsonl"), num)
    if code is None:
        reason = f"agent-run timed out after {timeout_s}s and was stopped."
        return finish(c, repo, num, None, blocked_report(num, ref, started, model, reason, log_base))
    if rep is None or rep["verdict"] is None:
        why = "the agent printed no report block" if rep is None else "the agent's report block has no valid verdict"
        return finish(c, repo, num, None,
                      blocked_report(num, ref, started, model, f"{why} (agent-run exit {code}).", log_base))
    return finish(c, repo, num, rep, None, exit_code=code, results_dir=results_dir)


def finish(c, repo, num, rep, fallback, exit_code=0, results_dir=None):
    if fallback is not None:
        comment(c, repo, num, fallback)
        verdict, claude = "BLOCKED", True
    else:
        verdict = rep["verdict"]
        claude = needs_claude(verdict, rep["deviations"], exit_code)
        note = f"\n\nPosted by the dispatcher; `/results` is `{results_dir}` on the runner VM."
        if exit_code != 0:
            note += f" agent-run exited {exit_code}."
        comment(c, repo, num, rep["block"] + note)
    add = [LABEL_BLOCKED if verdict == "BLOCKED" else LABEL_DONE] + ([LABEL_CLAUDE] if claude else [])
    relabel(c, repo, num, remove=[LABEL_RUNNING], add=add)
    log(f"{repo}#{num}: {verdict}{' + needs-claude' if claude else ''}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", metavar="OWNER/REPO#N", help="consider just this one issue (testing)")
    a = ap.parse_args(argv)
    c = cfg()
    only = None
    if a.only:
        m = re.match(r"^([\w.-]+/[\w.-]+)#(\d+)$", a.only)
        if not m:
            ap.error("--only takes OWNER/REPO#N")
        only = (m.group(1), m.group(2))
    os.makedirs(c["logs"], exist_ok=True)
    load_secrets(c)
    if os.environ.get("DISPATCH_SELFTEST") == "1":
        # Run the preflight (a real no-op sandbox start) for every repo, from whatever unit started us, and claim
        # nothing. 80-agent-runner-dispatcher.sh selftest runs this THROUGH agent-runner-dispatch.service, so the
        # check has exactly the unit's properties (2026-10-30: NoNewPrivileges there broke every sudo).
        results = {repo: runner_ready(c, repo) for repo in c["repos"]}
        for repo, ok in results.items():
            log(f"selftest {repo}: {'ready' if ok else 'NOT READY'}")
        return 0 if all(results.values()) else 1
    with open(os.path.join(c["logs"], ".dispatch.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another dispatch is running; exiting")
            return 0
        rotate_logs(c)
        model = lane_model(c)
        if model is None:
            log("router lane closed for scoped keys; nothing claimed")
            return 0
        chosen = pick(c, ready_issues(c, only), ready=lambda repo: runner_ready(c, repo))
        if chosen is None:
            return 0
        repo, issue = chosen
        return dispatch(c, repo, issue, model)


if __name__ == "__main__":
    sys.exit(main())
