## Monitor: a failed check leaves a FAIL tile that never clears

After the host rebooted on 2026-08-21, three tiles on the cluster-monitor dashboard stayed at FAIL for
about 41 hours. In the same period every per-card GPU check and every router upstream check read ok.
Someone had to delete those rows from `check_state` by hand.

The checks involved are the router `/healthz` check and the per-card GPU VRAM and temperature checks.
When they succeed they report one result per upstream or per card. When they fail (router unreachable,
an answer that is not JSON, `rocm-smi` failing or returning no cards), the results they produce are
never replaced by a later successful run, because the monitor stores results by check ID.

**Wanted:** a failed run reports under the same check IDs a successful run would use, so the next
good run clears it. The number of GPU cards should be configurable, defaulting to 2.

`scripts/files/tests/test_cluster_monitor.py` shows the expected IDs.
