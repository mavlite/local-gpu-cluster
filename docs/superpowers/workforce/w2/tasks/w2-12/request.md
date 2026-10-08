## Monitor: intermittent SQLite errors when the dashboard is open during a collection

The monitor's collector thread records check results into the SQLite `Store` while the HTTP API
thread reads snapshots from the same connection. With the dashboard open, the collector logs
intermittent `sqlite3` errors (recursive cursor use, interface errors). A dashboard request can also
see a half-written state.

**Wanted:** concurrent `record` and `snapshot` calls from different threads never raise, and they see
consistent rows. `scripts/files/tests/test_cluster_monitor.py` covers the store.
