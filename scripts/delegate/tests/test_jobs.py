import json
import os

from scripts.delegate import ids, jobs
from scripts.delegate.config import load_config


def test_ids_are_sorted_and_unique():
    xs = [ids.new_id() for _ in range(100)]
    assert len(set(xs)) == 100 and xs == sorted(xs)


def _cfg(tmp_path):
    return load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r",
                        "LOCAL_DELEGATE_JOBS_DIR": str(tmp_path / "jobs")})


def _write_job(cfg, state):
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    with open(os.path.join(cfg.jobs_dir, f"{state['id']}.json"), "w") as f:
        json.dump(state, f)


def test_single_worker_never_runs_two_at_once(tmp_path):
    store = jobs.JobStore(_cfg(tmp_path), deps=jobs.make_test_deps(sleep=0.2))
    a = store.submit({"task": "t1", "repo": ".", "base_ref": "HEAD", "checks": []})
    b = store.submit({"task": "t2", "repo": ".", "base_ref": "HEAD", "checks": []})
    store.join()
    assert store.deps.max_concurrent == 1
    assert store.deps.acquired == 2 and store.deps.lease_held == 0
    assert store.get(a)["status"] in ("done", "failed")
    assert store.get(b)["status"] in ("done", "failed")


def test_pipeline_order_and_base_after_overlay(tmp_path):
    store = jobs.JobStore(_cfg(tmp_path), deps=jobs.make_test_deps())
    jid = store.submit({"task": "t", "repo": ".", "checks": [["pytest"]]})
    store.join()
    names = [c[0] for c in store.deps.calls]
    assert names == ["validate_repo", "resolve_ref", "export", "strip", "overlay",
                     "commit_work", "base_sha", "run_opencode", "commit_work",
                     "inspect_diff", "extract_patch", "run_checks"]
    st = store.get(jid)
    assert st["status"] == "done" and st["diff"] == "DIFF" and st["tokens"] == {"output": 5}
    assert st["checks"][0]["exit_code"] == 0
    assert st["owner_pid"] == os.getpid()


def test_rejected_or_failed_runs_are_recorded_not_merged(tmp_path):
    deps = jobs.make_test_deps(exit_code=1, gate_rejected=True)
    store = jobs.JobStore(_cfg(tmp_path), deps=deps)
    jid = store.submit({"task": "t", "repo": ".", "checks": []})
    store.join()
    st = store.get(jid)
    assert st["status"] == "failed" and st["gate"]["rejected"] is True
    assert st["diff"] == "DIFF"


def test_pipeline_exception_marks_failed_and_releases_lease(tmp_path):
    deps = jobs.make_test_deps(raise_in="export")
    store = jobs.JobStore(_cfg(tmp_path), deps=deps)
    jid = store.submit({"task": "t", "repo": ".", "checks": []})
    store.join()
    st = store.get(jid)
    assert st["status"] == "failed" and "boom" in st["error"]
    assert deps.lease_held == 0


def test_list_filters_by_status(tmp_path):
    store = jobs.JobStore(_cfg(tmp_path), deps=jobs.make_test_deps())
    store.submit({"task": "t", "repo": ".", "checks": []})
    store.join()
    assert len(store.list("done")) == 1 and store.list("queued") == []


def test_running_job_with_live_owner_not_abandoned(tmp_path):
    cfg = _cfg(tmp_path)
    jid = ids.new_id()
    _write_job(cfg, {"id": jid, "status": "running", "owner_pid": os.getpid(),
                     "owner_started": jobs.proc_start(os.getpid())})
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps())
    store.reconcile()
    assert store.get(jid)["status"] == "running"


def test_running_job_with_dead_owner_abandoned(tmp_path):
    cfg = _cfg(tmp_path)
    jid = ids.new_id()
    _write_job(cfg, {"id": jid, "status": "running", "owner_pid": 999999999,
                     "owner_started": 0.0})
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps())
    store.reconcile()
    assert store.get(jid)["status"] == "abandoned"


def test_pid_reuse_detected_via_start_time(tmp_path):
    cfg = _cfg(tmp_path)
    jid = ids.new_id()
    _write_job(cfg, {"id": jid, "status": "running", "owner_pid": os.getpid(),
                     "owner_started": 1.0})  # same PID, different create time
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps())
    store.reconcile()
    if jobs.proc_start(os.getpid()) is not None:
        assert store.get(jid)["status"] == "abandoned"
