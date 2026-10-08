"""Pre-registered W1 and W3 decision rules (spec §7, §9)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import analysis  # noqa: E402


def test_one_sided_clopper_pearson_upper_bound():
    assert analysis.cp_upper(0, 30) == pytest.approx(1 - 0.05 ** (1 / 30), abs=1e-6)   # 0.0950
    u = analysis.cp_upper(1, 30)                                  # P(X <= 1 | 30, u) == 0.05
    assert (1 - u) ** 30 + 30 * u * (1 - u) ** 29 == pytest.approx(0.05, abs=1e-9)
    assert u == pytest.approx(0.1486, abs=5e-4)
    assert analysis.cp_upper(30, 30) == 1.0


def test_w1_needs_zero_loops_in_30_attempts_to_meet_ten_percent():
    assert analysis.cp_upper(0, 30) <= 0.10 < analysis.cp_upper(1, 30)


def cfg(looped, passed, wall_s, attempts=30):
    return {"attempts": attempts, "looped": looped, "passed": passed, "wall_s": wall_s}


def test_w1_picks_the_lowest_loop_rate_under_the_limit():
    r = analysis.w1_choose({"C0": cfg(1, 20, 900), "C2": cfg(0, 15, 1200), "C3": cfg(0, 18, 3000)})
    assert r["chosen"] == "C3"                      # C2 and C3 tie on loops; C3 has the higher pass rate


def test_w1_breaks_a_pass_rate_tie_on_speed():
    r = analysis.w1_choose({"C0": cfg(0, 18, 900), "C3": cfg(0, 18, 3000)})
    assert r["chosen"] == "C0"


def test_w1_stops_when_no_configuration_meets_the_limit():
    r = analysis.w1_choose({"C0": cfg(1, 25, 900), "C1": cfg(4, 25, 900)})
    assert r["chosen"] is None and "no configuration" in r["reason"]


TASKS = [f"t{i}" for i in range(16)]


def run(arm, accepted_ids, aph, probe=1.0, base=1.0, valid=True):
    return {"arm": arm, "valid": valid, "accepted": {t: t in accepted_ids for t in TASKS},
            "accepted_per_hour": aph, "probe_p50": probe, "baseline_p50": base}


def schedule(g_acc, t_acc, g_aph, t_aph, **over):
    arms = "GTTGGTTG"
    runs = []
    for i, a in enumerate(arms):
        runs.append(run(a, g_acc if a == "G" else t_acc, (g_aph if a == "G" else t_aph)[i // 2]))
    for i, r in over.items():
        runs[int(i)].update(r)
    return runs


ALL = set(TASKS)


def test_w3_builds_when_quality_holds_throughput_wins_and_the_user_is_protected():
    r = analysis.w3_decide(schedule(ALL, ALL, [10, 11, 10, 11], [15, 16, 15, 16]), TASKS)
    assert r["build"] is True and r["failed"] == []


def test_w3_quality_clause_fails_when_t_accepts_too_few_tasks():
    r = analysis.w3_decide(schedule(ALL, set(TASKS[:12]), [10, 11, 10, 11], [15, 16, 15, 16]), TASKS)
    assert r["build"] is False and "quality" in r["failed"]


def test_w3_win_clause_needs_a_ci_that_excludes_zero():
    r = analysis.w3_decide(schedule(ALL, ALL, [10, 14, 10, 14], [11, 15, 9, 13]), TASKS)
    assert r["build"] is False and r["failed"] == ["win"]


def test_w3_user_clause_fails_on_any_slow_t_run():
    runs = schedule(ALL, ALL, [10, 11, 10, 11], [15, 16, 15, 16], **{"2": {"probe_p50": 1.6}})
    r = analysis.w3_decide(runs, TASKS)
    assert r["failed"] == ["user"]


def test_w3_ignores_invalid_runs_and_needs_two_valid_per_arm():
    runs = schedule(ALL, ALL, [10, 11, 10, 11], [15, 16, 15, 16])
    for i in (1, 2, 5):                               # three of the four T runs invalid
        runs[i]["valid"] = False
    r = analysis.w3_decide(runs, TASKS)
    assert r["build"] is False and r["failed"] == ["insufficient valid runs"] and r["valid"] == {"G": 4, "T": 1}


def test_w3_bootstrap_is_reproducible():
    runs = schedule(ALL, set(TASKS[:15]), [10, 11, 10, 11], [15, 16, 15, 16])
    assert analysis.w3_decide(runs, TASKS) == analysis.w3_decide(runs, TASKS)


def test_a_t_run_without_probe_samples_fails_the_user_clause():
    # Final review I4: a dead probe cannot prove the user lane was protected, so the clause fails.
    import analysis
    runs = []
    for i, arm in enumerate("GTTGGTTG"):
        runs.append({"arm": arm, "valid": True, "accepted": {"a": True, "b": True},
                     "accepted_per_hour": 2.0 + (arm == "T") + 0.01 * i,
                     "probe_p50": None if (arm == "T" and i == 1) else 1.0, "baseline_p50": 1.0})
    out = analysis.w3_decide(runs, ["a", "b"])
    assert not out["clauses"]["user"]["ok"] and "user" in out["failed"]
