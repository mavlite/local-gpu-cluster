## Monitor: persist check results with last-ok time and a sample window

The cluster monitor needs a small SQLite store for check results.

**Wanted:** a `Store(path)` with these operations:
- **`record(result, now)`** upserts the check's current row (status, detail, value, unit, suggested
  action, `updated_at`). It returns the **previous** status, or `""` the first time.
  - It keeps `last_ok_at`, the last time the check was ok. A non-ok record never clears it.
  - It appends a `(ts, value)` sample only when the result has a value.
- **`snapshot(sample_window_s, now)`** returns every check's row, including `last_ok_at` and the
  samples within the window.
- **`prune(now, retention_s)`** deletes samples older than the retention and returns how many.
- **`close()`**.

`scripts/files/tests/test_cluster_monitor.py` shows the expected interface.
