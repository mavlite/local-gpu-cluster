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


def r2(arm, wall_s, acc_ids=None, capped=0, gpu_ms=None, task_s=None, valid=True):
    acc_ids = ALL if acc_ids is None else acc_ids
    acc = {t: t in acc_ids for t in TASKS}
    return {"arm": arm, "valid": valid, "accepted": acc, "accepted_per_hour": len(acc_ids) / (wall_s / 3600),
            "wall_s": wall_s, "gpu_ms": gpu_ms if gpu_ms is not None else wall_s * 1000,
            "worker_s": 3 * wall_s if arm == "T" else None, "review_capped": capped,
            "task_s": task_s or {t: wall_s / 16 for t in TASKS}}


def r2_runs(s_wall, g_wall, t_wall, **over):
    runs = [r2("S", s_wall), r2("G", g_wall), r2("T", t_wall), r2("T", t_wall), r2("G", g_wall), r2("S", s_wall)]
    for i, patch in over.items():
        runs[int(i)].update(patch)
    return runs


def test_round2_builds_at_1_5x_with_the_shortest_makespan_and_equal_acceptance():
    # 16 tasks: S 5/h (3.2 h), G 4/h (4 h), T 9/h (1.78 h)
    r = analysis.round2_decide(r2_runs(16 / 5 * 3600, 16 / 4 * 3600, 16 / 9 * 3600), TASKS)
    assert r["band"] == "build" and r["ratio"] == pytest.approx(1.8)
    assert r["arms"]["T"]["accepted_per_hour"] == pytest.approx(9) and r["arms"]["S"]["accepted_per_hour"] == pytest.approx(5)
    assert r["arms"]["T"]["makespan_s"] == pytest.approx(16 / 9 * 3600) and r["arms"]["T"]["n_runs"] == 2
    assert r["arms"]["G"]["gpu_hours_per_accepted"] == pytest.approx(4 / 16)
    assert r["arms"]["T"]["worker_hours_per_accepted"] == pytest.approx(3 * (16 / 9) / 16)
    assert r["acceptance"]["t0"] == {"S": 1.0, "G": 1.0, "T": 1.0}
    assert r["bottleneck_ci"]["n_tasks"] == 16 and r["bottleneck_ci"]["hi"] < 0     # T is faster per task


def test_round2_is_marginal_between_1_2x_and_1_5x():
    r = analysis.round2_decide(r2_runs(16 / 5 * 3600, 16 / 4 * 3600, 16 / 6.5 * 3600), TASKS)
    assert r["band"] == "marginal" and 1.2 <= r["ratio"] < 1.5


def test_round2_says_no_when_s_alone_has_the_shortest_makespan_at_equal_acceptance():
    r = analysis.round2_decide(r2_runs(16 / 9 * 3600, 16 / 4 * 3600, 16 / 8 * 3600), TASKS)
    assert r["band"] == "no" and r["ratio"] < 1 and "GPU implementer alone" in r["reason"]


def test_round2_build_needs_acceptance_within_one_task_of_the_best_arm():
    runs = r2_runs(16 / 5 * 3600, 16 / 4 * 3600, 16 / 9 * 3600)
    for i in (2, 3):
        runs[i] = r2("T", 16 / 9 * 3600, acc_ids=set(TASKS[:13]))
    r = analysis.round2_decide(runs, TASKS)
    assert r["band"] != "build" and r["arms"]["T"]["accepted_per_run"] == 13


def test_round2_is_inconclusive_when_any_arm_has_more_than_two_capped_reviews():
    r = analysis.round2_decide(r2_runs(16 / 5 * 3600, 16 / 4 * 3600, 16 / 9 * 3600, **{"1": {"review_capped": 3}}), TASKS)
    assert r["band"] == "inconclusive" and r["arms"]["G"]["review_capped"] == 3


def test_round2_skips_invalid_runs_and_needs_one_valid_run_per_arm():
    runs = r2_runs(16 / 5 * 3600, 16 / 4 * 3600, 16 / 9 * 3600, **{"2": {"valid": False}, "3": {"valid": False}})
    r = analysis.round2_decide(runs, TASKS)
    assert r["band"] == "invalid" and r["arms"]["T"]["n_runs"] == 0
