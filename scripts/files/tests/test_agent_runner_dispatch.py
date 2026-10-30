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

    def add_issue(num, repo=REPO, labels=("runner:ready", "kind:check"), created="2026-10-30T10:00:00Z", **kw):
        st = json.loads(state.read_text())
        st["issues"][f"{repo}#{num}"] = {"number": num, "title": f"task {num}", "body": kw.get("b", body()),
                                         "createdAt": created, "labels": list(labels), "comments": []}
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
    assert set(i["labels"]) == {"runner:done", "kind:check"}
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
    assert set(i["labels"]) == {"runner:done", "kind:check"}
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
    assert set(env.issue(7)["labels"]) == {"runner:done", "needs-claude", "kind:check"}


def test_missing_report_posts_redacted_blocked_report(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "noreport")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    assert set(i["labels"]) == {"runner:blocked", "needs-claude", "kind:check"}
    rep = reports(i)
    assert len(rep) == 1 and "verdict: BLOCKED" in rep[0] and "I forgot the report" in rep[0]
    everything = json.dumps(i["comments"])
    assert "ghp_" + "A" * 36 not in everything and "abc.def.ghi" not in everything


def test_closed_lane_claims_nothing(env):
    env.healthz.write_text(json.dumps({"active_chat_profile": "devstral"}))
    env.add_issue(7)
    assert env.run() == 0
    i = env.issue(7)
    assert i["labels"] == ["runner:ready", "kind:check"] and i["comments"] == []


def test_exit_75_puts_the_task_back(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "exit75")
    env.add_issue(7)
    env.run()
    i = env.issue(7)
    assert set(i["labels"]) == {"runner:ready", "kind:check"}
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
    assert env.issue(3, repo=OTHER)["labels"] == ["runner:ready", "kind:check"]


def test_issue_with_two_runner_labels_is_left_alone(env):
    env.add_issue(7, labels=("runner:ready", "runner:running", "kind:check"))
    env.run()
    i = env.issue(7)
    assert i["labels"] == ["runner:ready", "runner:running", "kind:check"] and i["comments"] == []


def test_held_lock_exits_without_touching_anything(env):
    env.add_issue(7)
    env.logs.mkdir(parents=True, exist_ok=True)
    with open(env.logs / ".dispatch.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert env.run() == 0
    assert env.issue(7)["labels"] == ["runner:ready", "kind:check"]


def test_nonzero_exit_after_pass_still_needs_claude(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "crash-after-pass")
    env.add_issue(7)
    env.run()
    assert set(env.issue(7)["labels"]) == {"runner:done", "needs-claude", "kind:check"}


def test_only_flag_restricts_to_one_issue(env):
    env.add_issue(7, created="2026-10-30T09:00:00Z")
    env.add_issue(8, created="2026-10-30T08:00:00Z")
    env.run("--only", f"{REPO}#7")
    assert "runner:done" in env.issue(7)["labels"]
    assert env.issue(8)["labels"] == ["runner:ready", "kind:check"]


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
    ("git switch feat/v26 && git pull --ff-only", None),
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
])
def test_parse_timeout_minutes(text, minutes):
    assert d.parse_timeout_minutes(text) == minutes
