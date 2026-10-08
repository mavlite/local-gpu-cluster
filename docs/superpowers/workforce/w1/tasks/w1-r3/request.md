## Monitor alerts: a status change wipes out another status's cooldown

The alert engine fires when a check goes from ok to a problem status (`warn` or `fail`). Each check
then has a cooldown (900 s by default) so a flapping check does not page repeatedly.

The cooldown is lost when a check alternates between problem statuses. For example: `fail` fires at
t=100, a `warn` fires at t=200, and then `fail` fires **again** at t=300. That is only 200 s after
the first `fail`, well inside its cooldown, so the second `fail` should have been suppressed.

**Wanted:** each problem status has its own cooldown per check, so firing one status never resets or
erases another's. `scripts/files/tests/test_cluster_monitor.py` covers the alert engine.
