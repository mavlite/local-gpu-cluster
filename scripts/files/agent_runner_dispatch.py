#!/usr/bin/env python3
"""Agent-runner work-queue dispatcher (agent-runner queue spec 2026-10-30, section 6).

Runs ON VM 178 as user `runner`, from agent-runner-dispatch.timer every 5 minutes. One task per run:

  1. flock; rotate event logs older than 30 days.
  2. If the router's loaded chat profile has no lane for scoped keys, stop quietly (nothing is claimed).
  3. Oldest open `runner:ready` issue across DISPATCH_REPOS (in that order on ties) that carries no other
     runner:* label. Claim it: runner:ready -> runner:running plus a start comment.
  4. Check out the issue's "Branch / commit" in ~/work/<repo> (fetch, switch, pull --ff-only, verify sha).
  5. agent-run <dir> "<preamble + issue body>" with the issue's max wall time; raw JSON events to
     ~/runner-logs/<repo>-<issue>-<UTC>.jsonl. agent-run runs opencode as the unprivileged user `agent` in a
     systemd sandbox: the checkout read-only at /work/<repo>, a per-task results dir at /results
     (AGENT_RESULTS_DIR on the host side), private /tmp, no /home, no /etc/agent-runner, no GitHub credentials.
  6. Exit 75 (lane closed): back to runner:ready with a comment. Otherwise take the report block the agent
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
VERDICTS = ("PASS", "FAIL", "INDETERMINATE", "BLOCKED")
DEFAULT_TIMEOUT_MIN, MAX_TIMEOUT_MIN = 60, 720
LOG_KEEP_DAYS = 30
EXIT_LANE_CLOSED = 75
TAIL_LINES, TAIL_LINE_CHARS, COMMENT_MAX = 50, 300, 60000


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
        "secret_files": [p for p in os.environ.get(
            "DISPATCH_SECRET_FILES", "/etc/agent-runner/router.key,/etc/agent-runner/gh-token").split(",") if p],
    }


LITERAL_SECRETS = []   # exact secret values, loaded once per run by load_secrets()


def load_secrets(c):
    LITERAL_SECRETS.clear()
    for path in c["secret_files"]:
        try:
            with open(path) as f:
                v = f.read().strip()
        except OSError:
            continue
        if len(v) >= 8:
            LITERAL_SECRETS.append(v)


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


def parse_ref(text):
    """'branch' or 'branch@sha' (backticks allowed) from the first line of the Branch / commit field."""
    first = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    first = first.strip("`").strip()
    m = _REF.match(first)
    if not m or first.startswith("-") or ".." in first:
        raise ValueError(f"cannot read a ref from {first!r}; write `branch` or `branch@sha` on the first line")
    return m.group(1), m.group(2)


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


def relabel(c, repo, num, remove=(), add=()):
    args = ["issue", "edit", str(num), "-R", repo]
    if remove:
        args += ["--remove-label", ",".join(remove)]
    if add:
        args += ["--add-label", ",".join(add)]
    gh(c, *args)


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


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def prepare_checkout(path, branch, sha):
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
    head = git(path, "rev-parse", "HEAD").stdout.strip()
    if sha and not head.lower().startswith(sha.lower()):
        return None, f"HEAD {head} does not match the requested {sha} on {branch}"
    return f"{branch}@{head}", None


def preamble(repo, num, ref, started, model):
    return f"""You are the lab runner for {repo} issue #{num}. Execute the task below exactly.

Rules:
- The current directory is the checkout at {ref}. It is READ-ONLY. Run only the commands in Steps, in order.
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
    ref, err = prepare_checkout(os.path.join(c["work"], name), branch, sha)
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
        queue = ready_issues(c, only)
        if not queue:
            return 0
        repo, issue = queue[0]
        return dispatch(c, repo, issue, model)


if __name__ == "__main__":
    sys.exit(main())
