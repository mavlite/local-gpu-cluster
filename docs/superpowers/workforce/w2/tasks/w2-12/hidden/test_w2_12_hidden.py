"""Hidden checks for w2-12: many writers and readers at once, no errors, every check present."""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm  # noqa: E402


def test_several_writers_and_readers_never_raise_and_end_consistent():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = cm.Store(path)
    errors = []

    def writer(w):
        for i in range(300):
            try:
                store.record(cm.CheckResult(f"w{w}_c{i % 4}", "metrics", cm.STATUS_OK, "d", value=float(i)),
                             now=1000.0 + i)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    def reader():
        for _ in range(300):
            try:
                store.snapshot(3600.0, now=5000.0)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = store.snapshot(3600.0, now=5000.0)
    store.close()
    os.unlink(path)
    assert errors == []
    assert len(rows) == 12
