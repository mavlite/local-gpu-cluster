## Monitor: alert on status transitions, with a cooldown and pluggable notifiers

The monitor needs an alert engine between the checks and the notifier.

**Wanted:** `AlertEngine(notifier, cooldown_s=900)` with `evaluate(result, prev_status, now)`.
- It returns an `AlertEvent`, or `None`, and passes every event it returns to `notifier.send`.
- It **fires** when a check goes from a non-problem status to a problem status. `warn` and `fail` are
  problems.
- It **resolves** when a check goes from a problem back to `ok`.
- Steady states produce nothing.
- A fire is **suppressed** if the same check fired with the same status within the cooldown.
- Notifiers share a small `Notifier` interface. Provide a no-op notifier and one that writes to a
  logger.

`scripts/files/tests/test_cluster_monitor.py` shows the expected interface.
