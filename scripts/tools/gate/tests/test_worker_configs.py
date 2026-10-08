"""W1 worker configurations C0-C3 (workforce spec §7; Plan D). `start` alone keeps the gate's frozen
flags; `start <config>` selects one of the four W1 configurations. Every configuration runs presence
penalty 0 (spec §7 table) and the Qwen model-card sampling for its thinking mode."""
import pytest

from test_scripts_static import BASH, SPEC_FLAGS, _run_worker

pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

BASE = "-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 --min-p 0"
IK_MTP = "--spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs"
NOTHINK = "--temp 0.7 --top-p 0.8 --top-k 20 --presence-penalty 0"
THINK = "--temp 1.0 --top-p 0.95 --top-k 20 --presence-penalty 0"


def started(tmp_path, *args, env=""):
    r, calls = _run_worker(tmp_path, *args, env=env)
    assert r.returncode == 0, r.stderr
    run = [c for c in calls.splitlines() if c.startswith("systemd-run ")]
    assert len(run) == 1, calls
    return run[0], calls


def test_c0_is_ik_with_mtp_non_thinking_and_no_presence_penalty(tmp_path):
    run, _ = started(tmp_path, "start", "C0")
    assert "/opt/bench/src/ik/build/bin/llama-server" in run
    assert BASE in run and IK_MTP in run and NOTHINK in run and "-rtr" in run
    assert "--presence-penalty 1.5" not in run
    assert '{"enable_thinking":false}' in run


def test_c2_is_c0_without_mtp(tmp_path):
    run, _ = started(tmp_path, "start", "C2")
    assert BASE in run and NOTHINK in run
    assert "--spec-type" not in run and "-mtprot" not in run


def test_c3_thinks_and_preserves_thinking_with_thinking_sampling(tmp_path):
    run, _ = started(tmp_path, "start", "C3")
    assert IK_MTP in run and THINK in run
    assert '{"enable_thinking":true,"preserve_thinking":true}' in run


def test_c1_is_mainline_without_ik_only_flags(tmp_path):
    run, _ = started(tmp_path, "start", "C1")
    assert "/opt/bench/src/llama.cpp/build/bin/llama-server" in run
    assert BASE in run and NOTHINK in run
    for ik_only in ("-rtr", "-mtprot", "-ctx-ckpt", "mtp:n_max"):
        assert ik_only not in run
    assert "--spec-type" not in run                      # MTP only when the build supports it


def test_c1_uses_mainline_mtp_when_declared_supported(tmp_path):
    run, _ = started(tmp_path, "start", "C1", env="GATE_C1_MTP=1")
    assert "--spec-type draft-mtp --spec-draft-n-max 3" in run


def test_start_without_a_config_keeps_the_frozen_gate_flags(tmp_path):
    run, _ = started(tmp_path, "start")
    assert SPEC_FLAGS in run and "--presence-penalty 1.5" in run


def test_the_running_config_is_recorded(tmp_path):
    _, calls = started(tmp_path, "start", "C2")
    rec = [c for c in calls.splitlines() if c.startswith("tee ")]
    assert any(c.endswith("/config") or "config" in c for c in rec), calls


def test_an_unknown_config_starts_nothing(tmp_path):
    r, calls = _run_worker(tmp_path, "start", "C9")
    assert r.returncode != 0 and "unknown config" in r.stderr
    assert "systemd-run" not in calls
