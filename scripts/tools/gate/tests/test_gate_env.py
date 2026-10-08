import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "sh", "gate_env.sh")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

AMD = {"llamacpp-chat-restart.timer": "active", "llamacpp-embed.service": "active",
       "llamacpp-rerank.service": "active", "llamacpp-fast.service": "inactive",
       "llamacpp-chat.service": "active"}
HOST = {"redteam-mode-watch.service": "active", "redteam-mode-idle.timer": "active",
        "redteam-mode-precreate.service": "inactive", "rag-refresh.timer": "active"}

# What the real /usr/local/sbin/redteam-mode-{enter,exit}.sh do to unit state (read 2026-10-05).
ENTER = """#!/usr/bin/env bash
echo active > "$GATE_REDTEAM_STATE"
pct exec 151 -- systemctl stop llamacpp-chat-restart.timer llamacpp-embed llamacpp-rerank
pct exec 151 -- systemctl start llamacpp-fast
systemctl start redteam-mode-idle.timer redteam-mode-watch.service
setcap 3
"""
EXIT = """#!/usr/bin/env bash
rm -f "$GATE_REDTEAM_STATE"
pct exec 151 -- systemctl start llamacpp-chat-restart.timer llamacpp-embed llamacpp-rerank
pct exec 151 -- systemctl stop llamacpp-fast
setcap 1
"""


def _posix(p):
    return str(p).replace("\\", "/")


@pytest.fixture
def world(tmp_path):
    bindir, sbin = tmp_path / "bin", tmp_path / "sbin"
    bindir.mkdir()
    sbin.mkdir()
    fake, py = _posix(os.path.join(HERE, "fake_host.py")), _posix(sys.executable)
    for tool in ("pct", "systemctl", "curl", "setcap"):
        (bindir / tool).write_text(f'#!/usr/bin/env bash\nexec "{py}" "{fake}" {tool} "$@"\n',
                                   newline="\n")
    (sbin / "redteam-mode-enter.sh").write_text(ENTER, newline="\n")
    (sbin / "redteam-mode-exit.sh").write_text(EXIT, newline="\n")
    for d in (bindir, sbin):
        for f in d.iterdir():
            f.chmod(0o755)
    wfile, rt = tmp_path / "w.json", tmp_path / "redteam.state"
    wfile.write_text(json.dumps({"units": {"151": dict(AMD), "host": dict(HOST),
                                           "153": {"llm-router.service": "active"}},
                                 "router_env": "ROUTER_API_KEY=x\nRATE_LIMIT_CHAT=60/minute\n",
                                 "capacity": 1}))
    env = dict(os.environ, FAKE_WORLD=str(wfile), GATE_STATE_DIR=_posix(tmp_path / "state"),
               GATE_SBIN=_posix(sbin), GATE_REDTEAM_STATE=_posix(rt))
    env["PATH"] = _posix(bindir) + os.pathsep + env["PATH"]

    def run(*args, ok=True):
        cmd = f'export PATH="{_posix(bindir)}:$PATH"; exec bash "{_posix(SCRIPT)}" ' + " ".join(args)
        r = subprocess.run([BASH, "-c", cmd], env=env, capture_output=True, text=True, timeout=120)
        if ok:
            assert r.returncode == 0, r.stdout + r.stderr
        return r

    def state():
        return json.loads(wfile.read_text())
    state.set = lambda w: wfile.write_text(json.dumps(w))
    return run, state, rt


def test_pin_stops_perturbers_and_raises_rate(world):
    run, state, _ = world
    run("pin")
    w = state()
    assert "RATE_LIMIT_CHAT=1000/minute" in w["router_env"]
    assert all(v == "inactive" for v in w["units"]["host"].values())
    for u in ("llamacpp-chat-restart.timer", "llamacpp-embed.service", "llamacpp-rerank.service",
              "llamacpp-fast.service"):
        assert w["units"]["151"][u] == "inactive"
    assert w["units"]["151"]["llamacpp-chat.service"] == "active"


def test_arm_a_then_b_then_restore_returns_exact_prior_state(world):
    run, state, rt = world
    before = state()
    run("pin")
    run("arm-a")
    w = state()
    assert w["capacity"] == 3
    assert w["units"]["host"]["redteam-mode-idle.timer"] == "inactive"      # no revert to 1 slot
    assert w["units"]["host"]["redteam-mode-watch.service"] == "inactive"
    assert w["units"]["151"]["llamacpp-fast.service"] == "inactive"          # no VRAM sharing
    run("arm-b")
    w = state()
    assert w["capacity"] == 1 and not rt.exists()
    assert w["units"]["151"]["llamacpp-embed.service"] == "inactive"         # same as arm A
    assert w["units"]["151"]["llamacpp-chat-restart.timer"] == "inactive"
    run("arm-a")
    run("restore")
    after = state()
    assert after["units"] == before["units"]
    assert after["router_env"] == before["router_env"]
    assert after["capacity"] == 1 and not rt.exists()


def test_restore_removes_rate_line_that_did_not_exist(world):
    run, state, _ = world
    w = state()
    w["router_env"] = "ROUTER_API_KEY=x\n"
    state.set(w)
    run("pin")
    assert "RATE_LIMIT_CHAT=1000/minute" in state()["router_env"]
    run("restore")
    assert state()["router_env"] == "ROUTER_API_KEY=x\n"


def test_refuses_when_redteam_active_or_already_pinned(world):
    run, _, rt = world
    rt.write_text("active\n")
    assert run("pin", ok=False).returncode != 0
    assert run("preflight", ok=False).returncode != 0
    rt.unlink()
    run("pin")
    assert run("pin", ok=False).returncode != 0
    assert run("preflight", ok=False).returncode != 0


def test_restore_without_pin_refuses(world):
    run, _, _ = world
    assert run("restore", ok=False).returncode != 0
