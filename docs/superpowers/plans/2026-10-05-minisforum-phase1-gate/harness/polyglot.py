"""Aider Polyglot run -> pass@1 / pass@2 (spec §5.1, §5.6 quality sub-gate).

A run directory is aider's tmp.benchmarks/<run-name>/; each exercise leaves
<lang>/exercises/practice/<name>/.aider.results.json with "tests_outcomes" (one bool per
attempt, at most 2, ending at the first pass). The denominator is the exercise list, never the
number of result files: an exercise that crashed before writing results is a FAIL, not a skip.
"""
import json
import os


def load_results(run_dir):
    """{exercise_name: results_dict} for every .aider.results.json under run_dir."""
    out = {}
    for dirpath, _, files in os.walk(run_dir):
        if ".aider.results.json" in files:
            with open(os.path.join(dirpath, ".aider.results.json"), encoding="utf-8") as f:
                d = json.load(f)
            out[d.get("testcase") or os.path.basename(dirpath)] = d
    return out


def summarize(run_dir, exercises):
    """exercises: the frozen list of exercise names (the denominator)."""
    if not exercises:
        raise ValueError("exercise list is empty")
    res = load_results(run_dir)
    unknown = sorted(set(res) - set(exercises))
    if unknown:
        raise ValueError(f"results for exercises outside the frozen set: {unknown}")
    p1 = p2 = 0
    missing, duration, malformed, exhausted = [], 0.0, 0, 0
    for name in exercises:
        d = res.get(name)
        if d is None:
            missing.append(name)
            continue
        outcomes = d.get("tests_outcomes") or []
        p1 += bool(outcomes[:1] and outcomes[0])
        p2 += any(outcomes[:2])
        duration += d.get("duration") or 0.0
        malformed += d.get("num_malformed_responses") or 0
        exhausted += d.get("num_exhausted_context_windows") or 0
    n = len(exercises)
    return {"n": n, "pass1": p1 / n, "pass2": p2 / n, "passed2": p2, "missing": missing,
            "duration_s": duration, "malformed": malformed, "exhausted_ctx": exhausted}
