"""Per-run metadata for W3 (spec §9; Plan D): validity from the host stamps, the user-lane probe p50,
and the lead's chat-server time. Feeds `cli.py w3-schedule`, whose output `cli.py w3` decides on."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cli  # noqa: E402
import runmeta  # noqa: E402

BEFORE = {"ts": "2026-10-10T10:00:00Z", "label": "before-r1", "boot_id": "b1", "router_invocation": "r1",
          "chat_invocation": "c1", "capacity": 3, "window_open": True}
AFTER = dict(BEFORE, ts="2026-10-10T11:00:00Z", label="after-r1")

# Mainline llama-server timing lines (LXC 151), as gate/llama_log.py parses them.
JOURNAL = """\
slot print_timing: id  0 | task 10 | prompt eval time = 1000.00 ms /   800 tokens
       eval time =  500.00 ms /    50 tokens
slot print_timing: id  1 | task 11 | prompt eval time = 2000.00 ms /  1600 tokens
       eval time = 1500.00 ms /   100 tokens
slot print_timing: id  2 | task 12 | prompt eval time =   12.50 ms /    20 tokens
       eval time =   30.00 ms /     2 tokens
"""


def epoch(iso):
    return runmeta.iso_epoch(iso)


def test_matching_stamps_are_valid():
    assert runmeta.validity(BEFORE, AFTER) == []


@pytest.mark.parametrize("field,reason", [
    ("boot_id", "host rebooted"),
    ("router_invocation", "router restarted"),
    ("chat_invocation", "chat server restarted"),
])
def test_any_restart_between_the_stamps_invalidates_the_run(field, reason):
    assert reason in " ".join(runmeta.validity(BEFORE, dict(AFTER, **{field: "changed"})))


def test_a_run_outside_the_open_three_slot_window_is_invalid():
    reasons = " ".join(runmeta.validity(dict(BEFORE, capacity=1), dict(AFTER, window_open=False)))
    assert "3-slot" in reasons and "window" in reasons


def test_a_stamp_with_a_blank_invocation_is_invalid():
    assert runmeta.validity(BEFORE, dict(AFTER, chat_invocation=""))


def probes(*recs):
    return [dict({"ts": epoch("2026-10-10T10:30:00Z"), "ok": True, "status": 200, "prompt_ms": None,
                  "predicted_ms": None}, **r) for r in recs]


def test_probe_p50_counts_failures_at_the_timeout_value():
    recs = probes({"latency_s": 1.0}, {"latency_s": 2.0}, {"ok": False, "latency_s": 0.1},
                  {"ok": False, "latency_s": 0.1}, {"ok": False, "latency_s": 0.2})
    p50 = runmeta.probe_p50(recs, epoch(BEFORE["ts"]), epoch(AFTER["ts"]), fail_s=120.0)
    assert p50 == 120.0                         # 3 of 5 failed: the median is a failure


def test_probe_p50_only_uses_samples_inside_the_run():
    inside = probes({"latency_s": 1.0}, {"latency_s": 3.0})
    outside = probes({"ts": epoch("2026-10-10T12:00:00Z"), "latency_s": 99.0})
    assert runmeta.probe_p50(inside + outside, epoch(BEFORE["ts"]), epoch(AFTER["ts"]), 120.0) == 2.0


def test_no_probe_samples_gives_none():
    assert runmeta.probe_p50([], 0, 1, 120.0) is None


def test_gpu_ms_subtracts_the_probe_share():
    recs = probes({"latency_s": 0.1, "prompt_ms": 12.5, "predicted_ms": 30.0})
    assert runmeta.gpu_ms(JOURNAL, recs) == pytest.approx(5000.0)


def write_run(tmp_path, name, accepted=3, valid=True):
    d = tmp_path / name
    d.mkdir()
    (d / "run.json").write_text(json.dumps({"valid": valid, "accepted": accepted, "wall_s": 3600.0,
                                            "accepted_by_task": {"a": True}, "accepted_per_hour": 3.0}))
    return d


def test_run_meta_writes_meta_json_and_flags_missing_probe(tmp_path):
    d = write_run(tmp_path, "r1")
    stamps = tmp_path / "stamps.jsonl"
    stamps.write_text(json.dumps(BEFORE) + "\n" + json.dumps(AFTER) + "\n")
    probe = tmp_path / "probe.jsonl"
    probe.write_text("".join(json.dumps(r) + "\n" for r in probes({"latency_s": 2.0})))
    journal = tmp_path / "chat.log"
    journal.write_text(JOURNAL)
    m = runmeta.run_meta(str(d), str(stamps), "r1", str(probe), str(journal), fail_s=120.0)
    assert m["valid"] and m["probe_p50"] == 2.0 and m["gpu_ms"] == pytest.approx(5042.5)
    assert m["gpu_ms_per_accepted"] == pytest.approx(5042.5 / 3)
    assert json.loads((d / "meta.json").read_text()) == m
    probe.write_text("")
    m = runmeta.run_meta(str(d), str(stamps), "r1", str(probe), str(journal), fail_s=120.0)
    assert not m["valid"] and "no user-lane probe samples" in " ".join(m["reasons"])


def test_run_meta_refuses_a_label_without_both_stamps(tmp_path):
    d = write_run(tmp_path, "r1")
    stamps = tmp_path / "stamps.jsonl"
    stamps.write_text(json.dumps(BEFORE) + "\n")
    with pytest.raises(SystemExit, match="after-r1"):
        runmeta.run_meta(str(d), str(stamps), "r1", str(stamps), str(stamps), fail_s=120.0)


def test_w3_schedule_joins_meta_and_the_idle_baseline(tmp_path, capsys):
    runs = []
    for name, arm, valid in (("g1", "G", True), ("t1", "T", False)):
        d = write_run(tmp_path, name)
        (d / "meta.json").write_text(json.dumps({"valid": valid, "probe_p50": 2.0, "reasons": []}))
        runs.append(f"{arm}={d}")
    base = tmp_path / "baseline.jsonl"
    base.write_text("".join(json.dumps(r) + "\n" for r in probes({"latency_s": 1.0}, {"latency_s": 1.4})))
    out = tmp_path / "schedule.json"
    args = ["w3-schedule", "--baseline-probe", str(base), "--out", str(out)]
    for r in runs:
        args += ["--run", r]
    assert cli.main(args) == 0
    sched = json.loads(out.read_text())
    assert [s["arm"] for s in sched] == ["G", "T"]
    assert sched[0]["valid"] is True and sched[1]["valid"] is False
    assert sched[0]["baseline_p50"] == pytest.approx(1.2) and sched[0]["probe_p50"] == 2.0
    assert os.path.normpath(sched[0]["run_dir"]) == os.path.normpath(runs[0].split("=", 1)[1])


def test_w3_schedule_refuses_a_run_without_meta(tmp_path):
    d = write_run(tmp_path, "g1")
    base = tmp_path / "baseline.jsonl"
    base.write_text(json.dumps(probes({"latency_s": 1.0})[0]) + "\n")
    with pytest.raises(SystemExit, match="meta.json"):
        cli.main(["w3-schedule", "--baseline-probe", str(base), "--out", str(tmp_path / "s.json"),
                  "--run", f"G={d}"])
