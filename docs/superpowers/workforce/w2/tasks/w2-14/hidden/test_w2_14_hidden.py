"""Hidden checks for w2-14: last_ok_at survives a failure; prune counts and keeps recent samples."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm  # noqa: E402


def make():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return cm.Store(path), path


def test_last_ok_survives_a_later_failure():
    store, path = make()
    store.record(cm.CheckResult("disk", "metrics", cm.STATUS_OK, "fine", value=10.0), now=10.0)
    assert store.record(cm.CheckResult("disk", "metrics", cm.STATUS_FAIL, "full", value=99.0), now=20.0) == cm.STATUS_OK
    row = {r["id"]: r for r in store.snapshot(3600.0, now=30.0)}["disk"]
    assert row["status"] == cm.STATUS_FAIL and row["last_ok_at"] == 10.0 and row["updated_at"] == 20.0
    store.close()
    os.unlink(path)


def test_prune_returns_the_count_and_keeps_recent_samples():
    store, path = make()
    for t in (10.0, 20.0, 80.0, 90.0):
        store.record(cm.CheckResult("cpu", "metrics", cm.STATUS_OK, "d", value=t), now=t)
    assert store.prune(now=100.0, retention_s=50.0) == 2
    row = store.snapshot(3600.0, now=100.0)[0]
    assert [s[0] for s in row["samples"]] == [80.0, 90.0]
    store.close()
    os.unlink(path)
