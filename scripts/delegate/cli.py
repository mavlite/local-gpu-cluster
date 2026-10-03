"""local-delegate CLI: `wait <job_id>` (run in background) and `reap`."""
import argparse
import os
import sys
import time

from scripts.delegate.config import load_config
from scripts.delegate.gitstore import rmtree_force
from scripts.delegate.jobs import JobStore, owner_alive
from scripts.delegate.ledger import Ledger

TERMINAL = ("done", "failed", "abandoned")
POLL_S = 2.0
REAP_DAYS = 7


def wait(cfg, job_id, sleep=time.sleep, poll_s=POLL_S) -> int:
    """Block until the job is terminal; 0 on terminal, 1 if the job is unknown."""
    store = JobStore(cfg, deps=None)
    while True:
        try:
            status = store.get(job_id).get("status")
        except OSError:
            print(f"unknown job: {job_id}", file=sys.stderr)
            return 1
        except ValueError:
            status = None  # torn read of a state file mid-write: retry
        if status in TERMINAL:
            print(f"{job_id} {status}")
            return 0
        sleep(poll_s)


def _prune(cfg, store, days) -> list:
    cutoff, pruned = time.time() - days * 86400, []
    for st in store.list():
        path = store._path(st["id"])
        if st.get("status") in TERMINAL and os.path.getmtime(path) < cutoff:
            rmtree_force(os.path.join(cfg.jobs_dir, st["id"]))
            os.remove(path)
            pruned.append(st["id"])
    return pruned


def _abandon_dead(store) -> list:
    out = []
    for st in store.list("running"):
        if not owner_alive(st.get("owner_pid"), st.get("owner_started")):
            store._save({**st, "status": "abandoned"})
            out.append(st["id"])
    return out


def reap(cfg, days=REAP_DAYS) -> dict:
    """Mark dead-owner running jobs abandoned, then prune old terminal jobs.

    Deliberately does not call JobStore.reconcile(): that re-enqueues queued
    jobs and would start a worker inside this short-lived CLI process.
    """
    store = JobStore(cfg, deps=None)
    abandoned = _abandon_dead(store)
    return {"abandoned": abandoned, "pruned": _prune(cfg, store, days)}


def _arm(vals) -> dict:
    n, tot = len(vals), sum(vals)
    return {"n": n, "claude_tokens_total": tot, "claude_tokens_mean": (tot / n if n else 0)}


def report(cfg, target=30) -> dict:
    """Summarize the Phase-1 A/B measurement from the ledger's review rows.

    Rows with no measured claude_tokens (token_source 'unavailable') are counted
    but excluded from the arm means, so a missing measurement never masquerades as
    a cheap task and skews the go/no-go."""
    rows = [r for r in Ledger(cfg.ledger_path).read_all() if r.get("kind") == "review"]
    measured = [r for r in rows if r.get("claude_tokens") is not None]
    d = _arm([r["claude_tokens"] for r in measured if r.get("delegated")])
    s = _arm([r["claude_tokens"] for r in measured if not r.get("delegated")])
    savings = gate = None
    if d["n"] and s["n"] and s["claude_tokens_mean"]:
        savings = (s["claude_tokens_mean"] - d["claude_tokens_mean"]) / s["claude_tokens_mean"] * 100
        gate = savings >= 20
    return {"n_reviews": len(rows), "n_unmeasured": len(rows) - len(measured),
            "delegated": d, "direct": s,
            "savings_pct": savings, "meets_20pct_gate": gate,
            "progress": f"{len(rows)}/{target}"}


def measure(cfg, *, job_id=None, marker_id=None, session_id=None, transcript=None) -> dict:
    """Print the Claude-token cost of one task's transcript window (debug/manual use)."""
    from scripts.delegate import transcript as tx
    path = transcript or tx.find_transcript(session_id=session_id)
    if not path:
        return {"error": "no transcript found"}
    if job_id:
        return tx.measure_delegated(path, job_id)
    return tx.measure_selfdone(path, marker_id)


def _fmt_report(rep) -> str:
    lines = [f"local-delegate A/B measurement ({rep['progress']} tasks recorded)"]
    for label, key in (("delegated", "delegated"), ("self-done", "direct")):
        a = rep[key]
        lines.append(f"  {label:9s}: n={a['n']:3d}  mean Claude tokens/task={a['claude_tokens_mean']:.0f}")
    if rep["savings_pct"] is None:
        lines.append("  savings : n/a (need both arms with data)")
    else:
        verdict = "PASS (>=20% gate)" if rep["meets_20pct_gate"] else "below 20% gate"
        lines.append(f"  savings : {rep['savings_pct']:.1f}%  ({verdict})")
    return "\n".join(lines)


def main(argv=None, env=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.delegate.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wait").add_argument("job_id")
    sub.add_parser("reap").add_argument("--days", type=int, default=REAP_DAYS)
    sub.add_parser("report")
    m = sub.add_parser("measure")
    m.add_argument("--job")
    m.add_argument("--marker")
    m.add_argument("--session")
    m.add_argument("--transcript")
    args = ap.parse_args(argv)
    cfg = load_config(os.environ if env is None else env)
    if args.cmd == "wait":
        return wait(cfg, args.job_id)
    if args.cmd == "report":
        print(_fmt_report(report(cfg)))
        return 0
    if args.cmd == "measure":
        import json as _json
        print(_json.dumps(measure(cfg, job_id=args.job, marker_id=args.marker,
                                  session_id=args.session, transcript=args.transcript)))
        return 0
    print(reap(cfg, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
