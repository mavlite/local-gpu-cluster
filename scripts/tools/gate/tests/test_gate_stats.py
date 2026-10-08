import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gate_stats import cv, decide, mean_ci, p50, t95, welch_diff_ci  # noqa: E402

CFG = {"one_task_fraction": 1 / 34, "latency_factor": 1.5, "quality_task_margin": 1}


def test_t95_table_and_large_df():
    assert t95(1) == 12.706 and t95(4) == 2.776 and t95(30) == 2.042 and t95(200) == 1.96
    with pytest.raises(ValueError):
        t95(0)


def test_mean_ci_known_values():
    m, lo, hi = mean_ci([10, 12, 14, 16, 18])          # sd = 3.1623, n = 5, t = 2.776
    assert m == 14
    assert hi - m == pytest.approx(2.776 * 3.16228 / 5 ** 0.5, rel=1e-4)
    with pytest.raises(ValueError):
        mean_ci([1])


def test_welch_ci_excludes_zero_for_clear_difference():
    d, lo, hi = welch_diff_ci([10, 11, 12, 11, 10], [5, 6, 5, 6, 5])
    assert d == pytest.approx(5.4) and lo > 0


def test_welch_ci_includes_zero_for_noise():
    _, lo, hi = welch_diff_ci([10, 12, 9, 11, 13], [11, 10, 12, 9, 12])
    assert lo < 0 < hi


def test_p50_and_cv():
    assert p50([3, 1, 2]) == 2
    assert cv([10, 10, 10]) == 0


def runs(tph, passed, probe, base):
    return [{"tasks_per_hour": t, "passed": p, "probe_p50": probe, "baseline_p50": base}
            for t, p in zip(tph, passed)]


def good_quality():
    return {"coordinator_pass2": [0.60, 0.62, 0.58], "worker_pass2": [0.59, 0.60, 0.61]}


def test_build_when_all_conditions_hold():
    cap = {"A": runs([6, 6.5, 6.2], [5, 5, 6], 2.0, 1.0),
           "B": runs([9, 9.5, 9.2], [5, 5, 5], 1.2, 1.0)}
    out = decide(CFG, good_quality(), cap)
    assert out["build"] is True


def test_quality_veto_first():
    q = {"coordinator_pass2": [0.60, 0.62, 0.58], "worker_pass2": [0.30, 0.35, 0.32]}
    cap = {"A": runs([6, 6, 6], [5, 5, 5], 1, 1), "B": runs([99, 99, 99], [6, 6, 6], 1, 1)}
    assert decide(CFG, q, cap)["reason"] == "quality veto"


def test_no_build_when_capacity_ci_includes_zero():
    cap = {"A": runs([6, 9, 7], [5, 5, 5], 1, 1), "B": runs([7, 8, 6], [5, 5, 5], 1, 1)}
    assert decide(CFG, good_quality(), cap)["build"] is False


def test_no_build_when_any_b_run_slows_coordinator_beyond_factor():
    cap = {"A": runs([6, 6.5, 6.2], [5, 5, 5], 1, 1), "B": runs([9, 9.5, 9.2], [5, 5, 5], 1.0, 1.0)}
    cap["B"][1]["probe_p50"] = 1.51
    out = decide(CFG, good_quality(), cap)
    assert out["build"] is False and out["details"]["coordinator_slow_runs"] == [1]


def test_no_build_when_b_loses_more_than_one_task_in_a_run():
    cap = {"A": runs([6, 6.5, 6.2], [6, 6, 6], 1, 1), "B": runs([9, 9.5, 9.2], [6, 4, 6], 1, 1)}
    out = decide(CFG, good_quality(), cap)
    assert out["build"] is False and out["details"]["task_quality_worse_runs"] == [1]


def test_arm_a_latency_alone_never_builds():
    # rev-3 loophole: A's coordinator badly degraded but B adds no capacity -> no build
    cap = {"A": runs([6, 6, 6], [5, 5, 5], 10.0, 1.0), "B": runs([6, 6.1, 5.9], [5, 5, 5], 1.0, 1.0)}
    assert decide(CFG, good_quality(), cap)["build"] is False
