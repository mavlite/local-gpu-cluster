import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gate  # noqa: E402
import task_set  # noqa: E402

TASK_ROOT = os.environ.get("GATE_TASK_ROOT", gate.TASK_ROOT)


def make_config(tmp_path, **over):
    cfg = {"thresholds": {"one_task_fraction": 1 / 34, "latency_factor": 1.5,
                          "quality_task_margin": 1, "variance_sd_tasks": 1, "variance_cv": 0.15},
           "runs_per_arm": 3, "router_url": "http://r/v1", "coordinator_alias": "qwen3.8-nothink",
           "worker_urls": ["a", "b", "c"],
           "probe": {"interval_s": 20, "baseline_samples": 30, "max_failed": 0},
           "polyglot": {"exercises": ["x"] * 34}, "worker_flags": "f",
           "task_manifest_sha256": task_set.manifest(TASK_ROOT), "large_prefill_min": 4096}
    cfg.update(over)
    p = tmp_path / "gate-config.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def test_config_loads_when_frozen(tmp_path):
    assert gate.load_config(make_config(tmp_path), TASK_ROOT)["runs_per_arm"] == 3


def test_config_refuses_changed_task_set(tmp_path):
    copy = tmp_path / "set"
    shutil.copytree(TASK_ROOT, copy)
    cfgp = make_config(tmp_path)
    first = task_set.load(str(copy))[0]["dir"]
    with open(os.path.join(first, "TASK.md"), "a") as f:
        f.write("\nedited after the gate commit\n")
    with pytest.raises(ValueError, match="void"):
        gate.load_config(cfgp, str(copy))


def test_config_refuses_missing_key(tmp_path):
    p = make_config(tmp_path)
    cfg = json.load(open(p))
    del cfg["thresholds"]
    open(p, "w").write(json.dumps(cfg))
    with pytest.raises(ValueError, match="missing"):
        gate.load_config(p, TASK_ROOT)


def run(passed2, dur):
    return {"passed2": passed2, "duration_s": dur}


def test_variance_ok_and_too_noisy():
    ok = gate.variance_verdict([run(17, 3600), run(18, 3500), run(17, 3700), run(16, 3650),
                                run(17, 3550)], 34, 1, 0.15)
    assert ok["ok"] is True and ok["pass2_sd_tasks"] <= 1
    noisy = gate.variance_verdict([run(12, 3600), run(18, 3500), run(15, 3700), run(20, 3650),
                                   run(14, 3550)], 34, 1, 0.15)
    assert noisy["ok"] is False


def write_run(root, arm, k, tph, passed, p50, **over):
    d = root / arm / f"run-{k:02d}"
    d.mkdir(parents=True)
    cap = {"A": 3, "B": 1}[arm]
    r = {"tasks_per_hour": tph, "passed": passed, "rc": 0, "timed_out": False,
         "probe": {"p50": p50, "failed": 0}, "capacity_before": cap, "capacity_after": cap}
    r.update(over)
    (d / "result.json").write_text(json.dumps(r))


def test_collect_shapes_and_pairs(tmp_path):
    for kind, vals in (("coordinator", [0.5, 0.52, 0.48]), ("worker", [0.5, 0.49, 0.51])):
        (tmp_path / "quality" / kind).mkdir(parents=True)
        for i, v in enumerate(vals):
            (tmp_path / "quality" / kind / f"r{i}.json").write_text(json.dumps({"pass2": v}))
    for arm, base in (("A", 2.0), ("B", 1.0)):
        (tmp_path / arm).mkdir()
        (tmp_path / arm / "baseline.json").write_text(json.dumps({"p50": base}))
    for k in (1, 2, 3):
        write_run(tmp_path, "A", k, 6.0 + k / 10, 5, 2.5)
        write_run(tmp_path, "B", k, 9.0 + k / 10, 5, 1.2)
    q, cap = gate.collect(str(tmp_path))
    assert q["worker_pass2"] == [0.5, 0.49, 0.51]
    assert cap["B"][0] == {"tasks_per_hour": 9.1, "passed": 5, "probe_p50": 1.2, "baseline_p50": 1.0}
    write_run(tmp_path, "B", 4, 9.0, 5, 1.0)
    with pytest.raises(ValueError, match="unpaired"):
        gate.collect(str(tmp_path))


def _bases(tmp_path):
    for arm in ("A", "B"):
        (tmp_path / arm).mkdir()
        (tmp_path / arm / "baseline.json").write_text(json.dumps({"p50": 1.0}))


@pytest.mark.parametrize("over, why", [
    ({"probe": {"p50": None, "failed": 3}}, "no successful latency probe"),
    ({"probe": {"p50": 1.0, "failed": 1}}, "1 failed latency probes"),
    ({"timed_out": True, "rc": None}, "timed out"),
    ({"rc": 1}, "opencode exit code 1"),
    ({"capacity_after": 1}, "layout changed"),
])
def test_collect_rejects_invalid_runs(tmp_path, over, why):
    _bases(tmp_path)
    write_run(tmp_path, "A", 1, 6, 5, 1.0, **over)
    with pytest.raises(ValueError, match=why):
        gate.collect(str(tmp_path))


def test_failed_probe_tolerance_is_configurable(tmp_path):
    _bases(tmp_path)
    write_run(tmp_path, "A", 1, 6, 5, 1.0, probe={"p50": 1.0, "failed": 1})
    write_run(tmp_path, "B", 1, 9, 5, 1.0)
    _, cap = gate.collect(str(tmp_path), max_probe_failed=1)
    assert len(cap["A"]) == 1
