"""Tests for the agent-runner work-queue dispatcher (agent-runner queue spec, section 6).

The dispatcher runs against a fake `gh` (tests/fake_gh.py, state in a JSON file), a fake `agent-run`
(tests/fake_agent_run.py), real git repositories and a file:// stand-in for the router's /healthz.
"""
import datetime
import fcntl
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

import agent_runner_dispatch as d  # noqa: E402

REPO = "DraconisCluster/SDG-ARMA3"
OTHER = "DraconisCluster/SDG-RUST"
PY = sys.executable


def body(ref="main", limits="Max wall time: 10 minutes. Leave main checked out."):
    return ("### Goal\n\nCheck the runner end to end.\n\n"
            f"### Branch / commit\n\n{ref}\n\n"
            "### Preconditions\n\nNone.\n\n"
            "### Steps\n\n1. `git log -1` -> prints the head commit\n\n"
            "### Pass criteria\n\ngit log -1 exits 0\n\n"
            f"### Limits\n\n{limits}\n\n"
            "### Report block\n\n(section 5)\n\n"
            "### Escalate when\n\nAnything not listed in Steps.\n")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=origin)
    work = tmp_path / "work"
    work.mkdir()
    for name in ("SDG-ARMA3", "SDG-RUST"):
        git("clone", "-q", str(origin), str(work / name))
    state = tmp_path / "gh.json"
    state.write_text(json.dumps({"user": "mavlite", "issues": {}}))
    healthz = tmp_path / "healthz.json"
    healthz.write_text(json.dumps({"active_chat_profile": "rag-qwen3.6"}))
    gh_cmd = f"{PY} {os.path.join(HERE, 'fake_gh.py')}"
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    monkeypatch.setenv("FAKE_GH_CMD", gh_cmd)
    monkeypatch.setenv("FAKE_REPO", REPO)
    monkeypatch.setenv("FAKE_TASK", "7")
    monkeypatch.setenv("FAKE_AGENT_MODE", "pass")
    monkeypatch.setenv("DISPATCH_REPOS", f"{OTHER},{REPO}")
    monkeypatch.setenv("DISPATCH_WORK_DIR", str(work))
    monkeypatch.setenv("DISPATCH_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DISPATCH_HEALTHZ", healthz.as_uri())
    monkeypatch.setenv("DISPATCH_GH", gh_cmd)
    monkeypatch.setenv("DISPATCH_AGENT_RUN", f"{PY} {os.path.join(HERE, 'fake_agent_run.py')}")
    monkeypatch.delenv("DISPATCH_MAX_TIMEOUT_S", raising=False)
    monkeypatch.setenv("DISPATCH_RETRY_SLEEP_S", "0")
    monkeypatch.delenv("FAKE_GH_TOKEN_EXPIRY", raising=False)
    monkeypatch.setenv("DISPATCH_MIRROR_BRANCHES", "SDG-ARMA3:upstream")
    monkeypatch.delenv("FAKE_GH_EDIT_FAIL", raising=False)
    secret = tmp_path / "router.key"
    secret.write_text("rk_live_" + "Q" * 30 + "\n")        # not hex, not a gh token: only a literal match catches it
    monkeypatch.setenv("FAKE_SECRET_FILE", str(secret))
    monkeypatch.setenv("DISPATCH_SECRET_FILES", f"{secret},{tmp_path / 'absent-token'}")
    monkeypatch.setenv("DISPATCH_RESULTS_ROOT", str(tmp_path / "results"))

    class E:
        pass
    e = E()
    e.tmp, e.state, e.healthz, e.work, e.origin = tmp_path, state, healthz, work, origin
    e.logs = tmp_path / "logs"
    e.secret = secret.read_text().strip()
    e.results = tmp_path / "results"

    def add_issue(num, repo=REPO, labels=("runner:ready", "kind:check", "tier:a"), created="2026-10-30T10:00:00Z", **kw):
        st = json.loads(state.read_text())
        st["issues"][f"{repo}#{num}"] = {"number": num, "title": kw.get("title", f"task {num}"),
                                         "body": kw.get("b", body()), "createdAt": created,
                                         "labels": list(labels), "comments": [], "state": kw.get("state", "OPEN")}
        state.write_text(json.dumps(st))
    e.add_issue = add_issue
    e.issue = lambda num, repo=REPO: json.loads(state.read_text())["issues"][f"{repo}#{num}"]
    e.run = lambda *a: d.main(list(a))
    return e


def reports(issue):
    return [c["body"] for c in issue["comments"] if "### runner report" in c["body"]]


def test_pass_report_marks_done_without_needs_claude(env):
    env.add_issue(7)
    assert env.run() == 0
    i = env.issue(7)
    assert set(i["labels"]) == {"runner:done", "kind:check", "tier:a"}
    assert any("claimed" in c["body"] for c in i["comments"])
    assert len(reports(i)) == 1
    logs = list(env.logs.glob("SDG-ARMA3-7-*.jsonl"))
    assert len(logs) == 1 and "step_start" in logs[0].read_text()


def test_dispatcher_posts_the_printed_report_and_names_the_results_dir(env):
    # Hardening 2026-10-30: the agent has no GitHub credentials; it prints the block, the dispatcher posts it.
    env.add_issue(7)
    env.run()
    rep = reports(env.issue(7))
    assert len(rep) == 1 and rep[0].startswith("### runner report") and "verdict: PASS" in rep[0]
    assert "Done." not in rep[0]                       # only the block, not the agent's chatter
    dirs = list(env.results.glob("SDG-ARMA3-7-*"))
    assert len(dirs) == 1 and (dirs[0] / "probe.txt").exists() and dirs[0].name in rep[0]


def test_literal_secret_values_are_redacted(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "leak")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    rep = reports(i)
    assert len(rep) == 1 and "note: <redacted>" in rep[0]
    assert env.secret not in json.dumps(i["comments"])


def test_values_from_env_style_secret_files_are_redacted(env, monkeypatch):
    # /etc/agent-runner/ptero-156.env holds KEY=value lines; each VALUE must be stripped, not the whole file.
    envfile = env.tmp / "ptero-156.env"
    api = "ptla_" + "Zx9" * 12
    envfile.write_text(f"SDG_PTERO_SFTP_USER=lab\nSDG_PTERO_SFTP_PASSWORD=hunter2-but-longer\nSDG_PTERO_API_KEY={api}\n")
    monkeypatch.setenv("DISPATCH_SECRET_FILES", f"{envfile}")
    monkeypatch.setenv("FAKE_AGENT_MODE", "leak")
    monkeypatch.setenv("FAKE_LEAK_VALUE", f"{api} and hunter2-but-longer")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    posted = json.dumps(i["comments"])
    assert len(reports(i)) == 1
    assert api not in posted and "hunter2-but-longer" not in posted


def test_default_secret_files_include_the_rust_lab_secrets(monkeypatch):
    monkeypatch.delenv("DISPATCH_SECRET_FILES", raising=False)
    assert "/etc/agent-runner/ptero-156.env" in d.cfg()["secret_files"]


def test_headerless_report_is_accepted_and_posted_with_heading(env, monkeypatch):
    # Live 2026-10-30: the agent wrote the yaml without '### runner report' or a fence.
    monkeypatch.setenv("FAKE_AGENT_MODE", "headerless")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    assert set(i["labels"]) == {"runner:done", "kind:check", "tier:a"}
    rep = reports(i)
    assert len(rep) == 1 and "```yaml\ntask: 7" in rep[0] and "verdict: BLOCKED" not in rep[0]


def test_start_comment_is_not_mistaken_for_a_report(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "noreport")
    env.add_issue(7)
    env.run()
    assert "runner:blocked" in env.issue(7)["labels"]


def test_preamble_says_print_the_block_and_write_only_to_results():
    p = d.preamble("o/r", 5, "main@abc", "2026-10-30T00:00:00Z", "qwen3.6")
    assert "### runner report" in p and "```yaml" in p
    assert "/results" in p and "read-only" in p
    assert "gh issue comment" not in p and "do not post" in p.lower()


def test_fail_report_marks_done_and_needs_claude(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "fail")
    env.add_issue(7)
    env.run()
    assert set(env.issue(7)["labels"]) == {"runner:done", "needs-claude", "kind:check", "tier:a"}


def test_missing_report_posts_redacted_blocked_report(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "noreport")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    assert set(i["labels"]) == {"runner:blocked", "needs-claude", "kind:check", "tier:a"}
    rep = reports(i)
    assert len(rep) == 1 and "verdict: BLOCKED" in rep[0] and "I forgot the report" in rep[0]
    everything = json.dumps(i["comments"])
    assert "ghp_" + "A" * 36 not in everything and "abc.def.ghi" not in everything


def test_closed_lane_claims_nothing(env):
    env.healthz.write_text(json.dumps({"active_chat_profile": "devstral"}))
    env.add_issue(7)
    assert env.run() == 0
    i = env.issue(7)
    assert i["labels"] == ["runner:ready", "kind:check", "tier:a"] and i["comments"] == []


def test_exit_75_puts_the_task_back(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit75")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    assert set(i["labels"]) == {"runner:ready", "kind:check", "tier:a"}
    assert any("lane closed" in c["body"] for c in i["comments"]) and not reports(i)


def test_timeout_kills_the_agent_and_blocks(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "hang")
    monkeypatch.setenv("DISPATCH_MAX_TIMEOUT_S", "2")
    env.add_issue(7)
    t0 = datetime.datetime.now()
    env.run()
    assert (datetime.datetime.now() - t0).total_seconds() < 60
    i = env.issue(7)
    assert "runner:blocked" in i["labels"] and "timed out" in reports(i)[0]


def test_unparseable_ref_blocks_without_running_the_agent(env):
    env.add_issue(7, b=body(ref="please test whatever is newest"))
    env.run()
    i = env.issue(7)
    assert "runner:blocked" in i["labels"] and "Branch / commit" in reports(i)[0]
    assert not (env.work / "SDG-ARMA3" / "probe.txt").exists()


def test_sha_mismatch_blocks(env):
    env.add_issue(7, b=body(ref="main@0123456789abcdef"))
    env.run()
    i = env.issue(7)
    assert "runner:blocked" in i["labels"] and "does not match" in reports(i)[0]


def test_one_task_per_run_oldest_first_across_repos(env):
    env.add_issue(7, created="2026-10-30T09:00:00Z")
    env.add_issue(3, repo=OTHER, created="2026-10-30T11:00:00Z")
    env.run()
    assert "runner:done" in env.issue(7)["labels"]
    assert env.issue(3, repo=OTHER)["labels"] == ["runner:ready", "kind:check", "tier:a"]


def test_issue_with_two_runner_labels_is_left_alone(env):
    env.add_issue(7, labels=("runner:ready", "runner:running", "kind:check", "tier:a"))
    env.run()
    i = env.issue(7)
    assert i["labels"] == ["runner:ready", "runner:running", "kind:check", "tier:a"] and i["comments"] == []


def test_held_lock_exits_without_touching_anything(env):
    env.add_issue(7)
    env.logs.mkdir(parents=True, exist_ok=True)
    with open(env.logs / ".dispatch.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert env.run() == 0
    assert env.issue(7)["labels"] == ["runner:ready", "kind:check", "tier:a"]


def test_nonzero_exit_after_pass_still_needs_claude(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "crash-after-pass")
    env.add_issue(7)
    env.run()
    assert set(env.issue(7)["labels"]) == {"runner:done", "needs-claude", "kind:check", "tier:a"}


def test_only_flag_restricts_to_one_issue(env):
    env.add_issue(7, created="2026-10-30T09:00:00Z")
    env.add_issue(8, created="2026-10-30T08:00:00Z")
    env.run("--only", f"{REPO}#7")
    assert "runner:done" in env.issue(7)["labels"]
    assert env.issue(8)["labels"] == ["runner:ready", "kind:check", "tier:a"]


def test_old_logs_are_rotated(env):
    env.logs.mkdir(parents=True, exist_ok=True)
    old = env.logs / "SDG-RUST-1-20260901T000000Z.jsonl"
    old.write_text("{}")
    stamp = (datetime.datetime.now() - datetime.timedelta(days=31)).timestamp()
    os.utime(old, (stamp, stamp))
    env.run()
    assert not old.exists()


@pytest.mark.parametrize("text,expected", [
    ("main", ("main", None)),
    ("`feat/perception-plan2`", ("feat/perception-plan2", None)),
    ("feat/v26@abc1234", ("feat/v26", "abc1234")),
    # the spec's own form (section 4): the switch command, then the sha to verify
    ("git switch feat/v26 && git pull --ff-only", ("feat/v26", None)),
    # SDG-RUST#3 verbatim (2026-10-30): a fenced block -- the dispatcher read the fence's "shell" as the branch
    ("```shell\ngit switch feat/v26 && git pull --ff-only\ngit log -1 --format=%H\n"
     "# expect: 22fb1c04d16ba94f956d128cb72e5d839f03f4c8\n```\nThe dispatcher does the switch and pull.",
     ("feat/v26", "22fb1c04d16ba94f956d128cb72e5d839f03f4c8")),
    ("git checkout main\n22fb1c04d16ba94f956d128cb72e5d839f03f4c8", ("main", "22fb1c04d16ba94f956d128cb72e5d839f03f4c8")),
    ("```\nfeat/x\n```", ("feat/x", None)),
    ("git switch a && git switch b", None),               # two different branches: ambiguous, refuse
    ("```shell\n```", None),
    ("please test whatever is newest", None),
    ("", None),
])
def test_parse_ref(text, expected):
    if expected is None:
        with pytest.raises(ValueError):
            d.parse_ref(text)
    else:
        assert d.parse_ref(text) == expected


@pytest.mark.parametrize("text,minutes", [
    ("Max wall time: 45 minutes", 45), ("max wall time 2 h", 120), ("Max wall time: 90m.", 90),
    ("leave the server stopped", 60), ("Max wall time: 48 hours", 720),
    # SDG-RUST#1 (2026-10-30) wrote it without "Max" and was killed at the 60-minute default
    ("- Wall time: 6 h at most.", 360), ("Wall-time: 20 minutes", 20),
])
def test_parse_timeout_minutes(text, minutes):
    assert d.parse_timeout_minutes(text) == minutes


# ---- tiers and the per-repo halt flag (owner decision 2026-10-30) ------------------------------------------
def halt(env, repo=REPO, num=99, state="OPEN"):
    env.add_issue(num, repo=repo, labels=("runner:halt",), title="Runner control", state=state)


def ran(env, num, repo=REPO):
    return "runner:done" in env.issue(num, repo)["labels"]


@pytest.mark.parametrize("labels,why", [
    (("runner:ready", "kind:check"), "no tier"),
    (("runner:ready", "kind:check", "tier:c"), "tier:c"),
    (("runner:ready", "kind:check", "tier:a", "tier:b"), "two tiers"),
])
def test_untiered_tier_c_and_ambiguous_tasks_are_refused_and_blocked(env, labels, why):
    env.add_issue(7, labels=labels)
    env.run()
    i = env.issue(7)
    assert "runner:blocked" in i["labels"] and "runner:ready" not in i["labels"], why
    assert not ran(env, 7) and not reports(i)
    assert any("tier" in c["body"] and "refus" in c["body"] for c in i["comments"])
    assert not list(env.logs.glob("SDG-ARMA3-7-*.jsonl"))        # the agent never ran


def test_halt_holds_tier_b_ready_with_one_halted_comment(env):
    halt(env)
    env.add_issue(7, labels=("runner:ready", "kind:check", "tier:b"))
    env.run()
    env.run()                                                    # every cycle re-checks; comments only once
    i = env.issue(7)
    assert i["labels"] == ["runner:ready", "kind:check", "tier:b"] and not reports(i)
    assert sum("halted" in c["body"] for c in i["comments"]) == 1


def test_halt_still_runs_tier_a(env, monkeypatch):
    monkeypatch.setenv("FAKE_TASK", "8")                         # the fake agent reports for the task it runs
    halt(env)
    env.add_issue(7, labels=("runner:ready", "kind:check", "tier:b"), created="2026-10-30T08:00:00Z")
    env.add_issue(8, labels=("runner:ready", "kind:check", "tier:a"), created="2026-10-30T09:00:00Z")
    env.run()
    assert ran(env, 8) and not ran(env, 7)


def test_tier_b_runs_when_not_halted(env):
    env.add_issue(7, labels=("runner:ready", "kind:check", "tier:b"))
    env.run()
    assert ran(env, 7)


def test_halt_is_per_repo(env):
    halt(env, repo=OTHER)
    env.add_issue(7, labels=("runner:ready", "kind:check", "tier:b"))
    env.run()
    assert ran(env, 7)


def test_closed_halt_issue_does_not_halt(env):
    halt(env, state="CLOSED")
    env.add_issue(7, labels=("runner:ready", "kind:check", "tier:b"))
    env.run()
    assert ran(env, 7)


def test_refusals_do_not_use_up_the_cycle(env):
    # an untiered issue is refused, then the oldest eligible one still runs in the same cycle
    env.add_issue(6, labels=("runner:ready", "kind:check"), created="2026-10-30T08:00:00Z")
    env.add_issue(7, labels=("runner:ready", "kind:check", "tier:a"), created="2026-10-30T09:00:00Z")
    env.run()
    assert "runner:blocked" in env.issue(6)["labels"] and ran(env, 7)


# Readiness (2026-10-30, after SDG-ARMA3#1 and SDG-RUST#3 were claimed while agent_egress was not loaded and
# ended BLOCKED): `agent-run --preflight` is asked before claiming; a runner that is not ready claims nothing.

def test_not_ready_runner_claims_nothing(env, monkeypatch):
    monkeypatch.setenv("FAKE_PREFLIGHT_FAIL", "all")
    env.add_issue(7)
    assert env.run() == 0
    i = env.issue(7)
    assert i["labels"] == ["runner:ready", "kind:check", "tier:a"] and i["comments"] == []


def test_preflight_is_per_repo(env, monkeypatch):
    # the older task's repo is not ready: it stays untouched and the other repo's task still runs this cycle
    monkeypatch.setenv("FAKE_PREFLIGHT_FAIL", "SDG-RUST")
    env.add_issue(3, repo=OTHER, created="2026-10-30T08:00:00Z")
    env.add_issue(7, created="2026-10-30T09:00:00Z")
    env.run()
    o = env.issue(3, repo=OTHER)
    assert o["labels"] == ["runner:ready", "kind:check", "tier:a"] and o["comments"] == []
    assert "runner:done" in env.issue(7)["labels"]


def test_exit_76_after_claim_puts_the_task_back(env, monkeypatch):
    # the sandbox went away between preflight and the run: back to runner:ready, not BLOCKED
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit76")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    assert set(i["labels"]) == {"runner:ready", "kind:check", "tier:a"}
    assert any("not ready" in c["body"] for c in i["comments"]) and not reports(i)


# ---- selftest through the real unit, and the unit itself (2026-10-30: NoNewPrivileges broke every sudo) -------
def test_selftest_runs_the_preflight_for_every_repo_and_claims_nothing(env, monkeypatch):
    monkeypatch.setenv("DISPATCH_SELFTEST", "1")
    env.add_issue(7)
    assert env.run() == 0
    i = env.issue(7)
    assert i["labels"] == ["runner:ready", "kind:check", "tier:a"] and i["comments"] == []


def test_selftest_fails_when_any_repo_is_not_ready(env, monkeypatch):
    monkeypatch.setenv("DISPATCH_SELFTEST", "1")
    monkeypatch.setenv("FAKE_PREFLIGHT_FAIL", "SDG-RUST")
    env.add_issue(7)
    assert env.run() == 1
    assert env.issue(7)["comments"] == []


def test_dispatch_unit_allows_sudo_and_orders_after_the_egress_table():
    unit = open(os.path.join(HERE, "..", "agent-runner-dispatch.service")).read()
    live = [l.strip() for l in unit.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert not any(l.startswith("NoNewPrivileges") for l in live)
    after = next(l for l in live if l.startswith("After="))
    assert "agent-runner-egress.service" in after


def test_partial_label_edit_failure_is_retried_and_ends_runner_done(env, monkeypatch):
    # SDG-ARMA3#1, 2026-10-30: GitHub removed runner:running, then failed before adding runner:done; the
    # dispatcher crashed and left the issue with no runner:* label at all.
    env.add_issue(7)
    # the claim succeeds; the finishing edit then fails once, after applying its removal
    real = d.relabel
    calls = {"n": 0}

    def flaky(c, repo, num, remove=(), add=()):
        calls["n"] += 1
        if calls["n"] == 2:
            monkeypatch.setenv("FAKE_GH_EDIT_FAIL", "1")
        return real(c, repo, num, remove=remove, add=add)
    monkeypatch.setattr(d, "relabel", flaky)
    assert env.run() == 0
    labels = env.issue(7)["labels"]
    assert "runner:done" in labels and "runner:running" not in labels
    assert len(reports(env.issue(7))) == 1


def test_label_edit_that_keeps_failing_still_raises(env, monkeypatch):
    env.add_issue(7)
    c = d.cfg()
    monkeypatch.setenv("FAKE_GH_EDIT_FAIL", "99")
    with pytest.raises(RuntimeError):
        d.relabel(c, REPO, 7, remove=["runner:ready"], add=["runner:running"])
    edits = [a for a in json.loads(env.state.read_text())["calls"] if a[:2] == ["issue", "edit"]]
    assert len(edits) == d.RELABEL_TRIES


def test_relabel_retry_sends_only_what_is_still_missing(env, monkeypatch):
    env.add_issue(7, labels=("runner:running", "kind:check", "tier:a"))
    monkeypatch.setenv("FAKE_GH_EDIT_FAIL", "1")
    d.relabel(d.cfg(), REPO, 7, remove=["runner:running"], add=["runner:done"])
    assert env.issue(7)["labels"] == ["kind:check", "tier:a", "runner:done"]
    edits = [a for a in json.loads(env.state.read_text())["calls"] if a[:2] == ["issue", "edit"]]
    assert "--remove-label" not in edits[-1] and edits[-1][-2:] == ["--add-label", "runner:done"]


def _commit(cwd, msg):
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", msg, cwd=cwd)


def test_mirror_branch_is_created_and_fast_forwarded_from_origin(env, monkeypatch):
    # SDG-RUST#1: tools/build.py reads `upstream:plugin/...`, a LOCAL branch the clone never had; the dispatcher
    # keeps it equal to origin/upstream on every run.
    git("branch", "upstream", cwd=env.origin)
    env.add_issue(7)
    env.run()
    clone = env.work / "SDG-ARMA3"
    assert git("rev-parse", "upstream", cwd=clone) == git("rev-parse", "upstream", cwd=env.origin)
    git("switch", "-q", "upstream", cwd=env.origin)
    _commit(env.origin, "upstream moves")
    git("switch", "-q", "main", cwd=env.origin)
    env.add_issue(8)
    monkeypatch.setenv("FAKE_TASK", "8")      # the fake agent reports for this task number
    env.run()
    assert git("rev-parse", "upstream", cwd=clone) == git("rev-parse", "upstream", cwd=env.origin)
    assert "runner:done" in env.issue(8)["labels"]


def test_mirror_branch_missing_on_origin_does_not_block(env):
    env.add_issue(7)                       # origin has no `upstream` branch at all
    env.run()
    assert "runner:done" in env.issue(7)["labels"]


def test_task_on_the_mirror_branch_itself_still_runs(env):
    git("branch", "upstream", cwd=env.origin)
    env.add_issue(7, b=body(ref="upstream"))
    env.run()
    i = env.issue(7)
    assert "runner:done" in i["labels"], reports(i)


def _expiry_in(days):
    t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%d %H:%M:%S UTC")


def _expiry_checks(env):
    return [a for a in json.loads(env.state.read_text())["calls"] if a == ["api", "-i", "rate_limit"]]


def test_token_expiring_within_14_days_is_warned_once_a_day(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_GH_TOKEN_EXPIRY", _expiry_in(5))
    env.run()
    env.run()
    err = capsys.readouterr().err
    assert err.count("WARNING: the runner's GitHub token expires in 4 days") == 1
    assert len(_expiry_checks(env)) == 1          # the second run inside 24 h does not ask again


def test_token_far_from_expiry_is_not_warned(env, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_GH_TOKEN_EXPIRY", _expiry_in(60))
    env.run()
    assert "WARNING" not in capsys.readouterr().err


def test_selftest_reports_token_days_left_without_failing(env, monkeypatch, capsys):
    monkeypatch.setenv("DISPATCH_SELFTEST", "1")
    monkeypatch.setenv("FAKE_GH_TOKEN_EXPIRY", "2027-01-25 14:09:35 UTC")
    assert env.run() == 0
    assert "selftest GitHub token: expires 2027-01-25 14:09:35 UTC" in capsys.readouterr().err


def test_token_without_expiry_header_is_reported_unknown_not_an_error(env, monkeypatch, capsys):
    monkeypatch.setenv("DISPATCH_SELFTEST", "1")
    assert env.run() == 0
    assert "selftest GitHub token: expiry unknown" in capsys.readouterr().err
