## Host: LXC 151 fails to autostart after a crash because `tank` is not imported in time

Since 2026-10-05, every host boot has left LXC 151 (the GPU container) stopped. The PVE pre-start hook
fails with ENOENT on 151's bind mount of `/tank/models`. Starting it by hand a minute later works.

Diagnosis so far:
- `/etc/zfs/zpool.cache` came back **empty** after a hard crash, so `zfs-import-cache` imported nothing.
- The old per-pool import unit still names the pool's previous name (`llm-pool`).
- Proxmox does import `tank` itself, but only about 40 s after boot, through storage activation.
  That is too late for the guests that autostart.

**Wanted:** `scripts/40-host-config.sh` phase 4.8 arranges for `tank` to be imported at boot in a way
that does not depend on the cache file being intact. `scripts/files/tests/test_host_config_zfs.py`
drives the phase with fakes.
