"""40-host-config.sh phase 4.8: the 'tank' pool must be imported at boot by its own unit.

On 2026-10-05..07 every host boot left LXC 151 stopped: /etc/zfs/zpool.cache had become empty after a
hard crash, so zfs-import-cache skipped, nothing imported 'tank', and 151's bind mount of /tank/models
failed (PVE pre-start hook, ENOENT). Proxmox imported the pool ~40 s later via storage activation, which
is why a manual start worked. A per-pool zfs-import@tank.service does not depend on the cache file.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _phase(tmp_path, pool_exists):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    src = open(os.path.join(ROOT, "40-host-config.sh"), encoding="utf-8").read()
    start = src.index("phase_4_8_zfs() {")
    end = src.index("\n}\n", start) + 3
    fn = tmp_path / "phase.sh"
    fn.write_text(src[start:end], newline="\n")
    calls = tmp_path / "calls"
    runner = tmp_path / "run.sh"
    runner.write_text(
        f'CALLS="{calls.as_posix()}"\n'
        'step() { :; }; skip() { :; }; ok() { :; }; log() { :; }; die() { echo "die: $*" >&2; exit 1; }\n'
        f'zpool() {{ echo "zpool $*" >> "$CALLS"; [ "$1" = list ] && return {0 if pool_exists else 1}; return 0; }}\n'
        'zfs() { echo "zfs $*" >> "$CALLS"; [ "$1" = list ] && return 1; return 0; }\n'
        'systemctl() { echo "systemctl $*" >> "$CALLS"; return 0; }\n'
        'DATA_NVME_A=/dev/null DATA_NVME_B=/dev/null\n'
        f'. "{fn.as_posix()}"\n'
        'phase_4_8_zfs <<< "create mirror"\n', newline="\n")
    r = subprocess.run([bash, str(runner)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return calls.read_text().splitlines()


def test_tank_is_imported_at_boot_by_its_own_unit_not_the_cache_file(tmp_path):
    # The existing-pool path (the live host). The create path needs real block devices; the enable
    # runs after the create/skip branch, so both paths reach it.
    calls = _phase(tmp_path, pool_exists=True)
    assert "systemctl enable zfs-import@tank.service" in calls, calls
