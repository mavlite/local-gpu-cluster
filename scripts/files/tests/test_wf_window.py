"""wf-window.sh: the host side of a workforce measurement window (workforce spec §9-§10; Plan D).

`open` puts the chat server in the 3 x 128K layout with embed and rerank still loaded (decision H:
measured to fit on 2026-10-08) and stops everything that could restart chat or swap its profile
mid-window. `close` puts back exactly what `open` found. `stamp` records what a run's validity is
judged on: the boot id and the invocation ids of the router and the chat server.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "wf-window.sh").replace("\\", "/")
FAKE = os.path.join(HERE, "fake_pve.py").replace("\\", "/")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

PERTURBERS_HOST = ["redteam-mode-watch.service", "redteam-mode-idle.timer", "redteam-mode-precreate.service",
                   "rag-refresh.timer", "swap-webhook.service"]
PERTURBERS_AMD = ["llamacpp-chat-restart.timer", "llamacpp-fast.service"]


@pytest.fixture
def world(tmp_path):
    bindir, sbin = tmp_path / "bin", tmp_path / "sbin"
    bindir.mkdir()
    sbin.mkdir()
    py = sys.executable.replace("\\", "/")
    for tool, d in (("pct", bindir), ("systemctl", bindir),
                    ("redteam-mode-enter.sh", sbin), ("redteam-mode-exit.sh", sbin)):
        (d / tool).write_text(f'#!/usr/bin/env bash\n"{py}" "{FAKE}" {tool} "$@"\n', newline="\n")
        (d / tool).chmod(0o755)
    boot = tmp_path / "boot_id"
    boot.write_text("boot-1\n")
    w = {"capacity": 1, "boot_id": "boot-1", "rt_state": (tmp_path / "rt.state").as_posix(),
         "units": {"host": {u: "active" for u in PERTURBERS_HOST},
                   "151": {"llamacpp-chat.service": "active", "llamacpp-embed.service": "active",
                           "llamacpp-rerank.service": "active", "llamacpp-chat-restart.timer": "active",
                           "llamacpp-fast.service": "inactive"},
                   "153": {"llm-router.service": "active"}},
         "invocation": {"151": {"llamacpp-chat.service": "chat-a"}, "153": {"llm-router.service": "router-a"}}}
    w["units"]["host"]["redteam-mode-precreate.service"] = "inactive"     # a oneshot, normally done
    path = tmp_path / "world.json"
    path.write_text(json.dumps(w))
    env = dict(os.environ, FAKE_WORLD=path.as_posix(), FAKEBIN=bindir.as_posix(),
               WF_WIN_SBIN=sbin.as_posix(), WF_WIN_STATE_DIR=(tmp_path / "state").as_posix(),
               WF_WIN_RT_STATE=w["rt_state"], WF_WIN_BOOT_ID=boot.as_posix(), WF_WIN_WAIT_S="2",
               WF_WIN_RT_LAST=(tmp_path / "rt.last").as_posix())

    def run(*args, **extra):
        prog = ('fb="$FAKEBIN"; command -v cygpath >/dev/null && fb="$(cygpath -u "$FAKEBIN")"; '
                f'export PATH="$fb:$PATH"; bash "{SCRIPT}" ' + " ".join(args))
        return subprocess.run([BASH, "-c", prog], capture_output=True, text=True, env=dict(env, **extra))

    def state():
        return json.loads(path.read_text())

    def patch(**kv):
        w2 = state()
        w2.update(kv)
        path.write_text(json.dumps(w2))

    return run, state, patch, tmp_path


def test_open_gives_three_slots_with_rag_and_stops_every_perturber(world):
    run, state, _, _ = world
    r = run("open")
    assert r.returncode == 0, r.stdout + r.stderr
    w = state()
    assert w["capacity"] == 3
    assert w["units"]["151"]["llamacpp-embed.service"] == "active"
    assert w["units"]["151"]["llamacpp-rerank.service"] == "active"
    for u in PERTURBERS_HOST:
        assert w["units"]["host"].get(u) != "active", u
    for u in PERTURBERS_AMD:
        assert w["units"]["151"].get(u) != "active", u


def test_close_puts_back_exactly_what_open_found(world):
    run, state, _, tmp = world
    before = state()["units"]
    assert run("open").returncode == 0
    r = run("close")
    assert r.returncode == 0, r.stdout + r.stderr
    w = state()
    assert w["capacity"] == 1
    for place in ("host", "151"):
        for u, st in before[place].items():
            if u == "llamacpp-chat.service":
                continue
            assert w["units"][place].get(u, "inactive") == st, (place, u)
    assert not os.path.exists(w["rt_state"])
    assert not (tmp / "state" / "window.env").exists()


def test_open_refuses_while_redteam_mode_is_active(world):
    run, state, _, _ = world
    with open(state()["rt_state"], "w") as f:
        f.write("active\n")
    r = run("open")
    assert r.returncode != 0 and "redteam" in r.stderr
    assert state()["capacity"] == 1


def test_open_refuses_a_second_open(world):
    run, _, _, _ = world
    assert run("open").returncode == 0
    r = run("open")
    assert r.returncode != 0 and "already open" in r.stderr


def test_a_failed_open_can_still_be_closed(world):
    run, state, patch, _ = world
    patch(fail_start="llamacpp-embed.service")
    r = run("open")
    assert r.returncode != 0
    patch(fail_start=None)
    r = run("close")
    assert r.returncode == 0, r.stdout + r.stderr
    w = state()
    assert w["capacity"] == 1 and w["units"]["host"]["rag-refresh.timer"] == "active"


def test_stamp_records_boot_and_invocation_ids(world):
    run, _, _, tmp = world
    out = tmp / "stamps.jsonl"
    r = run("stamp", out.as_posix(), "before-run-1")
    assert r.returncode == 0, r.stdout + r.stderr
    rec = json.loads(out.read_text().splitlines()[-1])
    assert rec["label"] == "before-run-1" and rec["boot_id"] == "boot-1"
    assert rec["router_invocation"] == "router-a" and rec["chat_invocation"] == "chat-a"
    assert rec["capacity"] == 1 and rec["ts"]


def test_close_without_open_is_an_error(world):
    run, _, _, _ = world
    r = run("close")
    assert r.returncode != 0 and "not open" in r.stderr


@pytest.mark.parametrize("unit", ["rag-refresh.service", "redteam-mode-idle.service"])
def test_open_refuses_while_a_timer_started_job_is_running(world, unit):
    # Final review I3: stopping the timer does not stop a refresh already loading the GPUs.
    run, state, patch, _ = world
    w = state()
    w["units"]["host"][unit] = "active"
    patch(units=w["units"])
    r = run("open")
    assert r.returncode != 0 and unit in r.stderr
    assert state()["capacity"] == 1


def test_open_fails_if_a_perturber_survives_the_stop(world):
    run, state, patch, _ = world
    patch(fail_stop="rag-refresh.timer")
    r = run("open")
    assert r.returncode != 0 and "still active" in r.stderr


def test_open_refreshes_the_redteam_idle_stamp_before_entering(world):
    run, _, _, tmp = world
    last = tmp / "rt.last"
    r = run("open", WF_WIN_RT_LAST=last.as_posix())
    assert r.returncode == 0, r.stderr
    assert last.read_text().strip().isdigit()


def test_stamp_records_any_active_perturber(world):
    run, state, patch, tmp = world
    out = tmp / "stamps.jsonl"
    assert run("open").returncode == 0
    w = state()
    w["units"]["host"]["rag-refresh.timer"] = "active"
    patch(units=w["units"])
    assert run("stamp", out.as_posix(), "x").returncode == 0
    rec = json.loads(out.read_text().splitlines()[-1])
    assert rec["perturbers_active"] == ["rag-refresh.timer"]
