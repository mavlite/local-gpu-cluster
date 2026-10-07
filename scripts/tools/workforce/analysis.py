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

    slow = [i for i, r in enumerate(by["T"]) if r["probe_p50"] > latency_factor * r["baseline_p50"]]
    clauses["user"] = {"slow_t_runs": slow, "ok": not slow}

    failed = [c for c in ("quality", "win", "user") if not clauses[c]["ok"]]
    return {"build": not failed, "failed": failed, "valid": valid, "clauses": clauses}
