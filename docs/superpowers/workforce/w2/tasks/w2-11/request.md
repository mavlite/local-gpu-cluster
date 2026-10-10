## Monitor: the GPU VRAM and temperature checks always fail on the host

The cluster monitor runs on the Proxmox host, and its GPU checks call `rocm-smi` directly. ROCm isn't
installed on the host; it lives in LXC 151, the GPU container. Both checks therefore fail on every
run, with `rocm-smi` not found.

**Wanted:** the GPU checks run `rocm-smi` inside the GPU container via `pct exec <ctid> --`. The
container ID comes from the monitor's config and defaults to 151. The rest of each check (thresholds,
per-card results) is unchanged. `scripts/files/tests/test_cluster_monitor.py` drives the checks
through a fake probe layer.
