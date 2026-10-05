"""Statistics and the pre-registered decision rule for the Phase 1 gate (spec §5.7).

Stdlib only. Small-sample t intervals (two-sided 95%), Welch for differences.
"""
import math
import statistics

# Two-sided 95% Student-t critical values, df 1..30; df > 30 uses 1.96.
_T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]


def t95(df):
    if df < 1:
        raise ValueError("df must be >= 1")
    return _T95[int(df) - 1] if df <= 30 else 1.96


def mean_ci(xs):
    """(mean, lo, hi) two-sided 95% t interval. Needs >= 2 values."""
    if len(xs) < 2:
        raise ValueError("need at least 2 values")
    m, s = statistics.mean(xs), statistics.stdev(xs)
    h = t95(len(xs) - 1) * s / math.sqrt(len(xs))
    return m, m - h, m + h


def welch_diff_ci(a, b):
    """(mean(a)-mean(b), lo, hi) two-sided 95% Welch interval. Needs >= 2 values each."""
    if len(a) < 2 or len(b) < 2:
        raise ValueError("need at least 2 values per group")
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    d = statistics.mean(a) - statistics.mean(b)
    se = math.sqrt(va + vb)
    if se == 0:
        return d, d, d
    df = (va + vb) ** 2 / ((va ** 2) / (len(a) - 1) + (vb ** 2) / (len(b) - 1))
    h = t95(max(1, math.floor(df))) * se
    return d, d - h, d + h


def p50(xs):
    if not xs:
        raise ValueError("no samples")
    return statistics.median(xs)


def cv(xs):
    m = statistics.mean(xs)
    return statistics.stdev(xs) / m if m else float("inf")


def decide(cfg, q, cap):
    """Apply spec §5.7 exactly.

    cfg: {"one_task_fraction": float, "latency_factor": 1.5, "quality_task_margin": 1}
    q:   {"coordinator_pass2": [...], "worker_pass2": [...]}          per-run pass@2 fractions
    cap: {"A": [run, ...], "B": [run, ...]} with run =
         {"tasks_per_hour": float, "passed": int, "probe_p50": float, "baseline_p50": float}
    Returns {"build": bool, "reason": str, "details": {...}}.
    """
    details = {}
    _, w_lo, _ = mean_ci(q["worker_pass2"])
    c_mean = statistics.mean(q["coordinator_pass2"])
    details["quality"] = {"worker_ci_lo": w_lo, "coordinator_mean": c_mean}
    if w_lo < c_mean - cfg["one_task_fraction"]:
        return {"build": False, "reason": "quality veto", "details": details}

    A, B = cap["A"], cap["B"]
    d, lo, hi = welch_diff_ci([r["tasks_per_hour"] for r in B], [r["tasks_per_hour"] for r in A])
    details["capacity_diff_B_minus_A"] = {"diff": d, "lo": lo, "hi": hi}
    if lo <= 0:
        return {"build": False, "reason": "no additive capacity (CI includes 0)", "details": details}

    slow = [i for i, r in enumerate(B) if r["probe_p50"] > cfg["latency_factor"] * r["baseline_p50"]]
    details["coordinator_slow_runs"] = slow
    if slow:
        return {"build": False, "reason": "coordinator latency over limit in arm B", "details": details}

    worse = [i for i, (a, b) in enumerate(zip(A, B))
             if b["passed"] < a["passed"] - cfg["quality_task_margin"]]
    details["task_quality_worse_runs"] = worse
    if worse:
        return {"build": False, "reason": "arm B task quality worse than A - 1", "details": details}
    return {"build": True, "reason": "all §5.7 conditions hold", "details": details}
