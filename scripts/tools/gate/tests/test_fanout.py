import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fanout  # noqa: E402

# One line verbatim from `opencode run --format json` (1.18.34) against stub_llm, 2026-10-05.
EVENT = ('{"type":"tool_use","timestamp":1791224598584,"sessionID":"ses_a","part":{"type":"tool",'
         '"tool":"task","callID":"call_0","state":{"status":"completed","input":{"description":'
         '"implement T1","subagent_type":"worker-1","prompt":"x"},"output":"SUMMARY: ok",'
         '"metadata":{"parentSessionId":"ses_a","sessionId":"ses_b","model":{"providerID":"w1",'
         '"modelID":"qwen3.6"},"truncated":false},"title":"implement T1","time":{"start":'
         '1791224594200,"end":1791224598525}},"id":"prt_1","sessionID":"ses_a","messageID":"msg_1"}}')


def test_parse_events_extracts_task_calls_only():
    text = "\n".join(['{"type":"step_start","part":{"type":"step-start"}}', EVENT, "not json"])
    assert fanout.parse_events(text) == [{"agent": "worker-1", "provider": "w1", "status": "completed",
                                          "start_ms": 1791224594200, "end_ms": 1791224598525}]


def test_max_overlap():
    c = lambda s, e: {"start_ms": s, "end_ms": e}  # noqa: E731
    assert fanout.max_overlap([c(0, 10), c(1, 9), c(2, 8)]) == 3
    assert fanout.max_overlap([c(0, 1), c(1, 2), c(2, 3)]) == 1          # touching is not overlap
    assert fanout.max_overlap([]) == 0


def test_prober_summarises_and_excludes_failures():
    seq = iter([(True, 1.0), (False, 9.0), (True, 3.0), (True, 2.0)])
    p = fanout.Prober("u", "m", "k", interval=0.0, probe=lambda *a: next(seq, (True, 2.0)))
    p.start()
    while len(p.samples) < 4:
        pass
    out = p.stop()
    assert out["failed"] >= 1 and out["p50"] == 2.0


def test_run_capacity_refuses_without_keys(tmp_path):
    with pytest.raises(RuntimeError):
        fanout.run_capacity("B", str(tmp_path / "r"), str(tmp_path), "http://x/v1",
                            ["a", "b", "c"], env={"PATH": os.environ.get("PATH", "")})


def test_run_capacity_refuses_wrong_layout_before_starting(tmp_path):
    import probe
    import stub_llm

    port = probe.free_ports(1)[0]
    servers = stub_llm.serve([port], stub_llm.Script(capacity=3), str(tmp_path / "req.jsonl"))
    try:
        with pytest.raises(RuntimeError, match="wrong layout"):
            fanout.run_capacity("B", str(tmp_path / "r"), str(tmp_path), f"http://127.0.0.1:{port}/v1",
                                ["a", "b", "c"], env={"GATE_ROUTER_KEY": "k", "GATE_WORKER_KEY": "k"})
    finally:
        for s in servers:
            s.shutdown()
    assert not (tmp_path / "r").exists()          # nothing was started or written


def test_chat_capacity_strips_v1(tmp_path):
    import probe
    import stub_llm

    port = probe.free_ports(1)[0]
    servers = stub_llm.serve([port], stub_llm.Script(capacity=3), str(tmp_path / "req.jsonl"))
    try:
        assert fanout.chat_capacity(f"http://127.0.0.1:{port}/v1") == 3
        assert fanout.chat_capacity(f"http://127.0.0.1:{port}") == 3
    finally:
        for s in servers:
            s.shutdown()


def _has_opencode():
    try:
        fanout.resolve_opencode()
        return True
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _has_opencode(), reason="opencode not installed")
def test_end_to_end_against_stub(tmp_path):
    import probe
    import stub_llm

    ids = ("A1", "A2", "A3")
    root = tmp_path / "set"
    for tid in ids:
        (root / tid / "seed").mkdir(parents=True)
        (root / tid / "hidden").mkdir()
        (root / tid / "TASK.md").write_text("write done.txt\n")
        (root / tid / "seed" / "m.py").write_text("")
        (root / tid / "hidden" / "test_h.py").write_text(
            "import os\n\ndef test_done():\n    assert os.path.exists('done.txt')\n")
    ports = probe.free_ports(4)
    servers = stub_llm.serve(ports, stub_llm.Script(1.0, ids), str(tmp_path / "req.jsonl"))
    try:
        res = fanout.run_capacity(
            "B", str(tmp_path / "run"), str(root), f"http://127.0.0.1:{ports[0]}/v1",
            [f"http://127.0.0.1:{p}/v1" for p in ports[1:]], timeout_s=240, probe_interval=0.5,
            env=dict(os.environ, GATE_ROUTER_KEY="k", GATE_WORKER_KEY="k"))
    finally:
        for s in servers:
            s.shutdown()
    assert res["task_calls"] == 3 and res["completed"] == 3
    assert res["providers"] == ["w1", "w2", "w3"] and res["max_overlap"] == 3
    assert res["passed"] == 0          # done.txt is not a .py file, so the grader never sees it
    assert res["probe"]["n"] >= 1 and res["probe"]["failed"] == 0
    assert res["capacity_before"] == 1 and res["capacity_after"] == 1
    assert json.load(open(tmp_path / "run" / "result.json"))["wall_s"] == res["wall_s"]


@pytest.mark.skipif(not _has_opencode(), reason="opencode not installed")
def test_mechanism_probe_parallel_and_contained():
    import probe
    out = probe.mechanism_probe(worker_sleep=3.0)
    assert out["verdict"] == "parallel", out
    assert "bash" not in out["worker_tools"] and "webfetch" not in out["worker_tools"]
    assert "task" not in out["worker_tools"]
    assert not {"bash", "edit", "write", "webfetch"} & set(out["coordinator_tools"])
    assert out["escaped"] is False
