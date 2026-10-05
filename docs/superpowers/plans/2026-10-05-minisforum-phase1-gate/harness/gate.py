"""Phase-1 measurement gate CLI (spec §5). Every subcommand writes JSON; nothing here decides by hand.

  python scripts/tools/gate/gate.py tasks                                  §5.0.1 validate + manifest
  python scripts/tools/gate/gate.py probe                                  §5.2 opencode concurrency
  python scripts/tools/gate/gate.py baseline --arm A --out F               §5.3 idle probe p50
  python scripts/tools/gate/gate.py capacity --arm B --run 1 --out-root R  §5.6 one fan-out run
  python scripts/tools/gate/gate.py scrape --log F --out F                 §5.6 re-prefill / cache_n
  python scripts/tools/gate/gate.py polyglot --run-dir D --out F           §5.1/§5.6 pass@2
  python scripts/tools/gate/gate.py variance --runs F...                   §5.1 sizing verdict
  python scripts/tools/gate/gate.py decide --results R                     §5.7 decision
Keys come from the environment only (GATE_ROUTER_KEY, GATE_WORKER_KEY), never argv.
"""
import argparse
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fanout  # noqa: E402
import gate_stats  # noqa: E402
import llama_log  # noqa: E402
import polyglot  # noqa: E402
import task_set  # noqa: E402

REPO = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
GATE_DIR = os.path.join(REPO, "docs", "superpowers", "gate")
CONFIG = os.path.join(GATE_DIR, "gate-config.json")
TASK_ROOT = os.path.join(GATE_DIR, "worker-tasks")

REQUIRED = ("thresholds", "runs_per_arm", "router_url", "coordinator_alias", "worker_urls",
            "probe", "polyglot", "worker_flags", "task_manifest_sha256", "large_prefill_min")


def load_config(path=CONFIG, task_root=TASK_ROOT):
    """Frozen gate config. Refuses if a key is missing or the task set no longer matches its hash."""
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise ValueError(f"gate config missing {missing}")
    actual = task_set.manifest(task_root)
    if actual != cfg["task_manifest_sha256"]:
        raise ValueError(f"task set changed since the gate commit ({actual} != "
                         f"{cfg['task_manifest_sha256']}) -- the gate is void")
    if len(cfg["polyglot"]["exercises"]) == 0:
        raise ValueError("polyglot exercise list is empty")
    return cfg


def _write(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    print(json.dumps(obj, indent=1)[:4000])


def variance_verdict(runs, n_exercises, sd_tasks_max, cv_max):
    """§5.1: runs = polyglot summaries of back-to-back runs on the coordinator alias."""
    passed = [r["passed2"] for r in runs]
    wall = [r["duration_s"] for r in runs]
    sd = statistics.stdev(passed)
    wcv = gate_stats.cv(wall)
    _, lo, hi = gate_stats.mean_ci([p / n_exercises for p in passed])
    return {"runs": len(runs), "pass2_mean": statistics.mean(passed) / n_exercises,
            "pass2_sd_tasks": sd, "wall_cv": wcv, "ci_half_width": (hi - lo) / 2,
            "one_task": 1 / n_exercises,
            "ok": sd <= sd_tasks_max and wcv <= cv_max}


def run_problems(r, arm, max_probe_failed=0):
    """Why a capacity run is invalid (empty list = valid). An invalid run is rerun, never dropped
    silently and never counted: a timeout, a nonzero exit, a failed latency probe (a failure is
    the slowest sample, so excluding it would understate latency) or the wrong layout."""
    out = []
    if r.get("timed_out"):
        out.append("timed out")
    if r.get("rc") != 0:
        out.append(f"opencode exit code {r.get('rc')}")
    if r["probe"]["p50"] is None:
        out.append("no successful latency probe")
    if r["probe"].get("failed", 0) > max_probe_failed:
        out.append(f"{r['probe']['failed']} failed latency probes")
    want = fanout.ARM_CAPACITY[arm]
    if r.get("capacity_before") != want or r.get("capacity_after") != want:
        out.append(f"layout changed: capacity {r.get('capacity_before')}->{r.get('capacity_after')}, "
                   f"arm {arm} needs {want}")
    return out


def collect(results_root, max_probe_failed=0):
    """Gather per-run files into decide()'s q and cap shapes. Refuses any invalid run."""
    def pass2(kind):
        return [json.load(open(p))["pass2"]
                for p in sorted(glob.glob(os.path.join(results_root, "quality", kind, "*.json")))]
    q = {"coordinator_pass2": pass2("coordinator"), "worker_pass2": pass2("worker")}
    cap = {}
    for arm in ("A", "B"):
        base = json.load(open(os.path.join(results_root, arm, "baseline.json")))["p50"]
        runs = []
        for p in sorted(glob.glob(os.path.join(results_root, arm, "run-*", "result.json"))):
            r = json.load(open(p))
            problems = run_problems(r, arm, max_probe_failed)
            if problems:
                raise ValueError(f"{p}: invalid run ({'; '.join(problems)}) -- rerun it")
            runs.append({"tasks_per_hour": r["tasks_per_hour"], "passed": r["passed"],
                         "probe_p50": r["probe"]["p50"], "baseline_p50": base})
        cap[arm] = runs
    if len(cap["A"]) != len(cap["B"]):
        raise ValueError(f"unpaired runs: A={len(cap['A'])} B={len(cap['B'])}")
    return q, cap


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tasks")
    sub.add_parser("probe")
    b = sub.add_parser("baseline")
    b.add_argument("--arm", choices="AB", required=True)
    b.add_argument("--out", required=True)
    c = sub.add_parser("capacity")
    c.add_argument("--arm", choices="AB", required=True)
    c.add_argument("--run", type=int, required=True)
    c.add_argument("--out-root", required=True)
    s = sub.add_parser("scrape")
    s.add_argument("--log", required=True)
    s.add_argument("--out", required=True)
    pg = sub.add_parser("polyglot")
    pg.add_argument("--run-dir", required=True)
    pg.add_argument("--out", required=True)
    v = sub.add_parser("variance")
    v.add_argument("--runs", nargs="+", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--results", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "tasks":
        problems = task_set.validate(TASK_ROOT)
        _write(os.path.join(GATE_DIR, "tasks-validation.json"),
               {"tasks": [t["id"] for t in task_set.load(TASK_ROOT)],
                "manifest_sha256": task_set.manifest(TASK_ROOT), "problems": problems})
        return 1 if problems else 0
    if a.cmd == "probe":
        import probe
        out = probe.mechanism_probe()
        out.pop("events")
        print(json.dumps(out, indent=1))
        return 0 if out["verdict"] == "parallel" and not out["escaped"] else 1

    cfg = load_config()
    if a.cmd == "baseline":
        key = os.environ.get("GATE_ROUTER_KEY") or sys.exit("GATE_ROUTER_KEY not set")
        n, interval = cfg["probe"]["baseline_samples"], cfg["probe"]["interval_s"]
        samples = []
        for i in range(n):
            ok, secs = fanout.probe_once(cfg["router_url"], cfg["coordinator_alias"], key)
            samples.append({"ok": ok, "s": secs})
            if i + 1 < n:
                import time
                time.sleep(interval)
        good = [x["s"] for x in samples if x["ok"]]
        _write(a.out, {"arm": a.arm, "n": n, "failed": n - len(good),
                       "p50": statistics.median(good) if good else None, "samples": samples})
        return 0 if good and len(good) == n else 1
    if a.cmd == "capacity":
        workers = cfg["worker_urls"] if a.arm == "B" else ()
        res = fanout.run_capacity(a.arm, os.path.join(a.out_root, a.arm, f"run-{a.run:02d}"),
                                  TASK_ROOT, cfg["router_url"], workers,
                                  timeout_s=cfg.get("capacity_timeout_s", 14400),
                                  probe_interval=cfg["probe"]["interval_s"])
        print(json.dumps({k: res[k] for k in ("arm", "wall_s", "completed", "passed",
                                              "tasks_per_hour", "max_overlap", "probe")}, indent=1))
        problems = run_problems(res, a.arm, cfg["probe"]["max_failed"])
        print("VALID" if not problems else "INVALID -- rerun: " + "; ".join(problems))
        return 0 if not problems else 1
    if a.cmd == "scrape":
        with open(a.log, encoding="utf-8", errors="replace") as f:
            recs = llama_log.parse(f.read())
        _write(a.out, llama_log.summarize(recs, cfg["large_prefill_min"]))
        return 0
    if a.cmd == "polyglot":
        _write(a.out, polyglot.summarize(a.run_dir, cfg["polyglot"]["exercises"]))
        return 0
    if a.cmd == "variance":
        runs = [json.load(open(p)) for p in a.runs]
        th = cfg["thresholds"]
        out = variance_verdict(runs, len(cfg["polyglot"]["exercises"]),
                               th["variance_sd_tasks"], th["variance_cv"])
        print(json.dumps(out, indent=1))
        return 0 if out["ok"] else 1
    if a.cmd == "decide":
        q, cap = collect(a.results, cfg["probe"]["max_failed"])
        out = gate_stats.decide(cfg["thresholds"], q, cap)
        _write(os.path.join(a.results, "decision.json"), out)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
