"""The opencode process wrapper: argv, session id, final text, timeout kills the whole tree."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import oc  # noqa: E402

FAKE = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_opencode.py")]


def runner(tmp_path, **env):
    return oc.Opencode(FAKE, dict(os.environ, **env))


def test_run_builds_argv_and_reads_session_and_final_text(tmp_path):
    argv_log = tmp_path / "argv.jsonl"
    r = runner(tmp_path, FAKE_OC_MODE="ok", FAKE_OC_ARGV=str(argv_log)).run(
        "impl-1", str(tmp_path), "do the task", 30, str(tmp_path / "ev.jsonl"),
        session="ses_prev", attach=str(tmp_path / "packet.md"))
    assert r.rc == 0 and not r.timed_out and r.session_id == "ses_fake1"
    assert r.text == "REVISE: add a test\nfor negatives"         # text after the last tool call only
    argv = json.loads(argv_log.read_text().splitlines()[0])
    assert argv[:2] == ["run", "--pure"] and argv[argv.index("--agent") + 1] == "impl-1"
    assert argv[argv.index("--session") + 1] == "ses_prev"
    # the message must come BEFORE --file: --file is an array option and swallows what follows
    assert argv.index("do the task") < argv.index("--file") and argv[-1].endswith("packet.md")


def test_timeout_kills_opencode_and_its_children(tmp_path):
    child_file = tmp_path / "child.pid"
    t0 = time.monotonic()
    r = runner(tmp_path, FAKE_OC_MODE="sleep", FAKE_OC_CHILD=str(child_file)).run(
        "impl-1", str(tmp_path), "x", 3, str(tmp_path / "ev.jsonl"))
    assert r.timed_out and r.rc is None and time.monotonic() - t0 < 20
    time.sleep(1)
    assert not oc.pid_alive(int(child_file.read_text()))


def test_export_skips_the_status_line(tmp_path):
    doc = runner(tmp_path).export("ses_abc")
    assert doc["info"]["id"] == "ses_abc"


def test_parse_events_without_text_or_session():
    assert oc.parse_events("") == (None, "")
    assert oc.parse_events("not json\n" + json.dumps({"type": "text", "sessionID": "s",
                                                      "part": {"text": "ACCEPT"}})) == ("s", "ACCEPT")


def test_systemd_scope_launcher_runs_as_the_agent_user_in_its_own_scope_and_kills_the_scope(tmp_path):
    calls = []
    launcher = oc.SystemdScopeLauncher("wfagent", memory_max="8G", run=lambda argv: calls.append(argv))
    argv = launcher.argv(["/usr/bin/opencode", "run", "x"], "wf-t1-impl-r0", "impl-1")
    assert argv[:3] == ["systemd-run", "--scope", "--quiet"]
    assert "--uid=wfagent-impl-1" in argv and "--gid=wfagent-impl-1" in argv and "--unit=wf-t1-impl-r0" in argv
    assert "--uid=wfagent-lead" in launcher.argv(["x"], "n", "lead")
    with pytest.raises(ValueError):
        launcher.argv(["x"], "n", None)                       # never fall back to a shared user
    assert "MemoryMax=8G" in argv and argv[argv.index("--") + 1:] == ["/usr/bin/opencode", "run", "x"]
    launcher.kill("wf-t1-impl-r0", proc=None)
    assert calls == [["systemctl", "kill", "--signal=SIGKILL", "wf-t1-impl-r0.scope"],
                     ["systemctl", "stop", "wf-t1-impl-r0.scope"]]


def test_opencode_passes_a_unique_unit_name_to_the_launcher(tmp_path):
    seen = []

    class Recorder(oc.DirectLauncher):
        def argv(self, argv, name, role=None):
            seen.append(name)
            return argv

    o = oc.Opencode(FAKE, dict(os.environ, FAKE_OC_MODE="ok"), launcher=Recorder())
    o.run("impl-1", str(tmp_path), "x", 30, str(tmp_path / "a.jsonl"))
    o.run("impl-1", str(tmp_path), "x", 30, str(tmp_path / "b.jsonl"))
    assert len(seen) == 2 and seen[0] != seen[1] and all(n.startswith("wf-") for n in seen)
