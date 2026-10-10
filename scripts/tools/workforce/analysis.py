"""Pre-registered decision rules (workforce spec §7 W1, §9 W3). Stdlib only, deterministic.

W1 bound: one-sided 95% Clopper-Pearson upper bound on the per-attempt loop rate. With 30 attempts
the bound for 0 loops is 9.5% and for 1 loop 14.9%, so "upper bound <= 10%" means "no loop in 30
attempts". (A two-sided bound would be 11.6% even for 0/30: no configuration could ever pass.)

W3 'accepted' per task and run is supplied by the caller (pipeline records: lead verdict accepted
or lead-fixed, AND the grade passed). The quality clause uses it; the win clause uses the run's
accepted_per_hour (worker-/implementer-accepted only).
"""
import math
import random
import statistics

W1_LIMIT = 0.10
W3_LATENCY_FACTOR = 1.5
_T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]


def _binom_cdf(k, n, p):
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def cp_upper(k, n, conf=0.95):
    """One-sided Clopper-Pearson upper bound for k events in n trials."""
    if k >= n:
        return 1.0
    lo, hi, alpha = k / n, 1.0, 1 - conf
    for _ in range(200):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def w1_choose(results, limit=W1_LIMIT):
    """results: {config: {"attempts", "looped", "passed", "wall_s"}} -> {"chosen", "reason", "table"}.
    Lowest loop rate among configs whose upper bound <= limit; ties on pass rate, then speed."""
    table = {}
    for cid, r in results.items():
        n = r["attempts"]
        if n == 0:                                   # no valid attempts: nothing to choose on
            table[cid] = {"loop_rate": None, "loop_upper": 1.0, "pass_rate": None, "s_per_attempt": None}
            continue
        table[cid] = {"loop_rate": r["looped"] / n, "loop_upper": cp_upper(r["looped"], n),
                      "pass_rate": r["passed"] / n, "s_per_attempt": r["wall_s"] / n}
    eligible = [c for c, t in table.items() if t["loop_upper"] <= limit]
    if not eligible:
        return {"chosen": None, "table": table,
                "reason": f"no configuration has a loop-rate upper bound <= {limit:.0%}; W3 does not run"}
    best = min(eligible, key=lambda c: (table[c]["loop_rate"], -table[c]["pass_rate"],
                                        table[c]["s_per_attempt"]))
    return {"chosen": best, "table": table, "reason": "lowest loop rate under the limit"}


def _welch_diff_ci(a, b):
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    d, se = statistics.mean(a) - statistics.mean(b), math.sqrt(va + vb)
    if se == 0:
        return d, d, d
    df = (va + vb) ** 2 / (va ** 2 / (len(a) - 1) + vb ** 2 / (len(b) - 1))
    df = max(1, math.floor(df))
    h = (_T95[df - 1] if df <= 30 else 1.96) * se
    return d, d - h, d + h


def w3_decide(runs, tasks, latency_factor=W3_LATENCY_FACTOR, n_boot=10000, seed=20261007):
    """runs: [{"arm": "G"|"T", "valid", "accepted": {task: bool}, "accepted_per_hour",
    "probe_p50", "baseline_p50"}]. Invalid runs are excluded and counted."""
    by = {a: [r for r in runs if r["arm"] == a and r["valid"]] for a in ("G", "T")}
    valid = {a: len(v) for a, v in by.items()}
    if min(valid.values()) < 2:
        return {"build": False, "failed": ["insufficient valid runs"], "valid": valid, "clauses": {}}
    clauses = {}

    rate = {a: [statistics.mean(1.0 if r["accepted"].get(t) else 0.0 for r in by[a]) for t in tasks]
            for a in by}
    diffs = [t - g for t, g in zip(rate["T"], rate["G"])]
    rng = random.Random(seed)
    boots = sorted(statistics.mean(rng.choice(diffs) for _ in diffs) for _ in range(n_boot))
    lower = boots[int(0.025 * n_boot)]
    clauses["quality"] = {"mean_diff": statistics.mean(diffs), "lower95": lower,
                          "threshold": -1 / len(tasks), "ok": lower >= -1 / len(tasks)}

    d, lo, hi = _welch_diff_ci([r["accepted_per_hour"] for r in by["T"]],
                               [r["accepted_per_hour"] for r in by["G"]])
    clauses["win"] = {"diff": d, "lo": lo, "hi": hi, "ok": lo > 0}

    # A T run without probe samples cannot show the user lane was protected: it counts as slow.
    slow = [i for i, r in enumerate(by["T"])
            if r["probe_p50"] is None or r["probe_p50"] > latency_factor * r["baseline_p50"]]
    clauses["user"] = {"slow_t_runs": slow, "ok": not slow}

    failed = [c for c in ("quality", "win", "user") if not clauses[c]["ok"]]
    return {"build": not failed, "failed": failed, "valid": valid, "clauses": clauses}


R2_BAR, R2_MARGINAL = 1.5, 1.2            # round 2 §5.3: the chosen bars on the pooled T / max(S, G) ratio
R2_CAPPED_LIMIT = 2                        # more capped reviews than this in any arm: inconclusive


def _pool(runs):
    """One arm's pooled figures: accepted per hour over the summed clock, makespan as the mean run."""
    hours = sum(r["wall_s"] for r in runs) / 3600
    acc = sum(r["accepted_per_hour"] * r["wall_s"] / 3600 for r in runs)   # the count each run's rate counted
    gpu_h = sum(r.get("gpu_ms") or 0 for r in runs) / 3.6e6
    worker = [r["worker_s"] for r in runs if r.get("worker_s") is not None]
    return {"n_runs": len(runs),
            "accepted_per_hour": acc / hours if hours else None,
            "makespan_s": statistics.mean(r["wall_s"] for r in runs) if runs else None,
            "accepted_per_run": statistics.mean(sum(1 for v in r["accepted"].values() if v) for r in runs)
            if runs else None,
            "gpu_hours_per_accepted": gpu_h / acc if acc and gpu_h else None,
            "worker_hours_per_accepted": sum(worker) / 3600 / acc if worker and acc else None,
            "review_capped": sum(r.get("review_capped") or 0 for r in runs)}


def _bottleneck_ci(by, other, tasks, n_boot, seed):
    """Paired bootstrap over tasks of T's bottleneck seconds minus `other`'s: worker seconds / 3 for T,
    the task's own (GPU) seconds for S and G, each the mean over the arm's runs; only tasks accepted in
    every run of both arms are paired."""
    def secs(arm, t):
        vals = [r["task_s"][t] for r in by[arm] if t in r.get("task_s", {})]
        return statistics.mean(vals) / (3 if arm == "T" else 1) if vals else None

    diffs = []
    for t in tasks:
        if all(r["accepted"].get(t) for r in by["T"] + by[other]):
            a, b = secs("T", t), secs(other, t)
            if a is not None and b is not None:
                diffs.append(a - b)
    if not diffs:
        return {"vs": other, "n_tasks": 0, "mean_diff": None, "lo": None, "hi": None}
    rng = random.Random(seed)
    boots = sorted(statistics.mean(rng.choice(diffs) for _ in diffs) for _ in range(n_boot))
    return {"vs": other, "n_tasks": len(diffs), "mean_diff": statistics.mean(diffs),
            "lo": boots[int(0.025 * n_boot)], "hi": boots[int(0.975 * n_boot) - 1]}


def round2_decide(runs, tasks, bar=R2_BAR, marginal=R2_MARGINAL, n_boot=10000, seed=20261010):
    """Round 2 §5.3. runs: [{"arm": "S"|"G"|"T", "valid", "accepted": {task: bool}, "accepted_per_hour",
    "wall_s", "gpu_ms", "worker_s" (T, may be None), "review_capped", "task_s": {task: seconds}}].
    Bands: build | marginal | no | inconclusive (reviewer health) | invalid (an arm without a valid run)."""
    by = {a: [r for r in runs if r["arm"] == a and r["valid"]] for a in ("S", "G", "T")}
    arms = {a: _pool(v) for a, v in by.items()}
    acceptance = {t: {a: (statistics.mean(1.0 if r["accepted"].get(t) else 0.0 for r in by[a]) if by[a] else None)
                      for a in by} for t in tasks}
    out = {"band": None, "ratio": None, "reason": "", "arms": arms, "acceptance": acceptance,
           "bottleneck_ci": None, "bar": bar, "marginal": marginal}
    missing = [a for a, v in by.items() if not v]
    if missing:
        out.update(band="invalid", reason=f"no valid run for arm(s) {', '.join(missing)}")
        return out
    other = max(("S", "G"), key=lambda a: arms[a]["accepted_per_hour"])
    best_other = arms[other]["accepted_per_hour"]
    ratio = arms["T"]["accepted_per_hour"] / best_other if best_other else None
    out["ratio"] = ratio
    out["bottleneck_ci"] = _bottleneck_ci(by, other, tasks, n_boot, seed)
    makespan = {a: arms[a]["makespan_s"] for a in arms}
    shortest = min(makespan, key=makespan.get)
    best_acc = max(arms[a]["accepted_per_run"] for a in arms)
    capped = {a: arms[a]["review_capped"] for a in ("G", "T")}
    if any(v > R2_CAPPED_LIMIT for v in capped.values()):
        out.update(band="inconclusive", reason=f"capped reviews per arm {capped}: fix the reviewer and rerun all arms")
    elif ratio is not None and ratio >= bar and shortest == "T" and arms["T"]["accepted_per_run"] >= best_acc - 1:
        out.update(band="build", reason=f"T is {ratio:.2f}x {other} with the shortest makespan and acceptance "
                                        f"within one task of the best arm")
    elif shortest == "S" and arms["S"]["accepted_per_run"] >= arms["T"]["accepted_per_run"]:
        out.update(band="no", reason="run the GPU implementer alone: S has the shortest makespan at equal "
                                     "acceptance; keep the review loop only as an optional quality gate")
    elif ratio is not None and (marginal <= ratio < bar or makespan["T"] <= 1.1 * makespan[shortest]):
        out.update(band="marginal", reason=f"T is {ratio:.2f}x {other} (makespan {makespan['T']:.0f} s vs "
                                           f"{makespan[shortest]:.0f} s for {shortest}): the user decides, "
                                           f"GPU-hours per accepted in front of them")
    else:
        out.update(band="no", reason=f"T is {ratio:.2f}x {other}" if ratio is not None else "no accepted tasks")
    return out
