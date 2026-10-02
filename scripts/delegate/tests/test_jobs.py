import json
import os
import threading

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
    calls = store.deps.calls
    names = [c[0] for c in calls]
    assert names == ["validate_repo", "resolve_ref", "export", "strip", "overlay",
                     "commit_work", "base_sha", "run_opencode", "commit_work",
                     "inspect_diff", "extract_patch", "run_checks"]
    by = {}
    for c in calls:
        by.setdefault(c[0], []).append(c[1:])
    work = os.path.join(store.cfg.jobs_dir, jid, "work")
    gitdir = os.path.join(store.cfg.jobs_dir, jid, "gitdir")
    assert by["export"] == [(".", "HEAD", work, gitdir)]
    assert by["strip"] == [(work,)] and by["overlay"] == [(work,)]
    assert by["commit_work"] == [(gitdir, work)] * 2
    assert by["base_sha"] == [(gitdir,)]
    assert by["run_opencode"] == [(work,)]
    assert by["inspect_diff"] == [(gitdir, work, "BASE")]
    assert by["extract_patch"] == [(gitdir, "BASE", work)]
    assert by["run_checks"] == [(work,)]
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


def test_checks_skipped_on_gate_rejection_or_failed_run(tmp_path):
    for kw, why in ((dict(gate_rejected=True), "gate rejected"), (dict(exit_code=1), "run failed")):
        deps = jobs.make_test_deps(**kw)
        store = jobs.JobStore(_cfg(tmp_path / why.replace(" ", "")), deps=deps)
        jid = store.submit({"task": "t", "repo": ".", "checks": [["pytest"]]})
        store.join()
        st = store.get(jid)
        assert st["checks"] == [] and why in st["checks_skipped"]
        assert "run_checks" not in [c[0] for c in deps.calls]


def test_worker_survives_unexpected_exception(tmp_path):
    store = jobs.JobStore(_cfg(tmp_path), deps=jobs.make_test_deps())
    real, state = store._run_job, {"first": True}

    def flaky(jid):
        if state["first"]:
            state["first"] = False
            raise OSError("disk")
        real(jid)

    store._run_job = flaky
    a = store.submit({"task": "t", "repo": ".", "checks": []})
    b = store.submit({"task": "t", "repo": ".", "checks": []})
    store.join()
    assert store.get(a)["status"] == "failed" and "disk" in store.get(a)["error"]
    assert store.get(b)["status"] == "done"


def test_ledger_failure_does_not_lose_result(tmp_path):
    deps = jobs.make_test_deps()

    class BadLedger:
        def append(self, rec):
            raise OSError("x")

    deps.ledger = BadLedger()
    store = jobs.JobStore(_cfg(tmp_path), deps=deps)
    jid = store.submit({"task": "t", "repo": ".", "checks": []})
    store.join()
    assert store.get(jid)["status"] == "done"


def test_concurrent_submits_start_one_worker(tmp_path):
    store = jobs.JobStore(_cfg(tmp_path), deps=jobs.make_test_deps(sleep=0.05))
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        store.submit({"task": "t", "repo": ".", "checks": []})

    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    store.join()
    assert store._worker_starts == 1
    assert store.deps.max_concurrent == 1
    assert len(store.list("done")) == 8


def test_reconcile_requeues_leftover_queued_jobs(tmp_path):
    cfg = _cfg(tmp_path)
    jid = ids.new_id()
    _write_job(cfg, {"id": jid, "status": "queued",
                     "spec": {"task": "t", "repo": ".", "checks": []}})
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps())
    store.reconcile()
    store.join()
    assert store.get(jid)["status"] == "done"


def test_corrupt_job_file_does_not_break_list_or_reconcile(tmp_path):
    cfg = _cfg(tmp_path)
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    with open(os.path.join(cfg.jobs_dir, "junk.json"), "w") as f:
        f.write("{not json")
    jid = ids.new_id()
    _write_job(cfg, {"id": jid, "status": "running", "owner_pid": 999999999})
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps())
    store.reconcile()
    assert [s["id"] for s in store.list()] == [jid]
    assert store.get(jid)["status"] == "abandoned"
