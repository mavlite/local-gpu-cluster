import os
import subprocess
import sys
import time

from scripts.delegate import runner
from scripts.delegate.config import load_config

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r",
                   "LOCAL_DELEGATE_OPENCODE_EXE": sys.executable,
                   "LOCAL_DELEGATE_JOBS_DIR": os.path.join(os.environ.get("TEMP", "."), "ld-runner-test")})
STUB = os.path.join(os.path.dirname(__file__), "stub_opencode.py")


def _spawn_stub(scenario, seen=None):
    def spawn(cmd, **kw):
        if seen is not None:
            seen.update(cmd=cmd, kw=kw)
            seen["proc"] = None
        proc = subprocess.Popen([sys.executable, STUB, scenario], **kw)
        if seen is not None:
            seen["proc"] = proc
        return proc
    return spawn


def test_sums_tokens_and_concats_text():
    res = runner.run_opencode(CFG, ".", "p", spawn=_spawn_stub("ok"))
    assert res.exit_code == 0
    assert "hello world" in res.text
    assert res.tokens["input"] == 30 and res.tokens["output"] == 7
    assert res.rejected is False


def test_rejected_permission_is_flagged_even_on_exit_zero():
    res = runner.run_opencode(CFG, ".", "p", spawn=_spawn_stub("rejected"))
    assert res.exit_code == 0
    assert res.rejected is True
    assert "auto-rejecting" in res.stderr_tail


def test_rejected_json_event_is_flagged():
    res = runner.run_opencode(CFG, ".", "p", spawn=_spawn_stub("rejected_event"))
    assert res.rejected is True


def test_timeout_kills_and_reports():
    seen = {}
    t0 = time.monotonic()
    res = runner.run_opencode(CFG, ".", "p", spawn=_spawn_stub("hang", seen), timeout_s=1)
    assert time.monotonic() - t0 < 30
    assert res.exit_code != 0
    assert seen["proc"].poll() is not None  # the hanging stub was really killed


def test_spawn_uses_devnull_stdin_command_and_scrubbed_env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("SSH_AUTH_SOCK", "x")
    seen = {}
    runner.run_opencode(CFG, "WORK", "do it", spawn=_spawn_stub("ok", seen))
    assert seen["kw"]["stdin"] == subprocess.DEVNULL
    assert seen["cmd"] == [sys.executable, "run", "--agent", "delegate", "--dir", "WORK",
                           "--format", "json", "do it"]
    env = seen["kw"]["env"]
    assert env.get("PATH") and "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert not any(k.startswith("SSH") for k in env)
    assert env["LOCAL_DELEGATE_ROUTER_TOKEN"] == "r"
    assert env["HOME"].endswith("_opencode_home")


def test_token_cache_dict_is_summed_not_crashed():
    """Real opencode emits tokens.cache as a nested {read,write} dict and a
    'total' field; the summer must handle that without TypeError (live-found bug)."""
    tok = {"input": 0, "output": 0, "reasoning": 0, "cache": 0}
    evt = {"type": "step_finish", "part": {"tokens": {
        "total": 3940, "input": 17, "output": 26, "reasoning": 0,
        "cache": {"write": 0, "read": 3897}}}}
    runner._apply_event(evt, [], tok)
    assert tok == {"input": 17, "output": 26, "reasoning": 0, "cache": 3897}
