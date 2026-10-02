"""local-delegate CLI: `wait <job_id>` (run in background) and `reap`."""
import argparse
import os
import sys
import time

from scripts.delegate.config import load_config
from scripts.delegate.gitstore import rmtree_force
from scripts.delegate.jobs import JobStore, owner_alive

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


def main(argv=None, env=None) -> int:
    ap = argparse.ArgumentParser(prog="scripts.delegate.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wait").add_argument("job_id")
    sub.add_parser("reap").add_argument("--days", type=int, default=REAP_DAYS)
    args = ap.parse_args(argv)
    cfg = load_config(os.environ if env is None else env)
    if args.cmd == "wait":
        return wait(cfg, args.job_id)
    print(reap(cfg, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
