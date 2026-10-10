"""Hidden checks for w2-01: the boot import is enabled once the pool exists, never via the cache file."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_host_config_zfs import _phase  # noqa: E402


def test_the_import_unit_is_enabled_after_the_pool_is_set_up(tmp_path):
    calls = _phase(tmp_path, pool_exists=True)
    assert "systemctl enable zfs-import@tank.service" in calls, calls
    enable = calls.index("systemctl enable zfs-import@tank.service")
    pool = max(i for i, c in enumerate(calls) if c.startswith(("zpool ", "zfs ")))
    assert enable > pool, calls


def test_the_fix_does_not_lean_on_the_cache_file(tmp_path):
    calls = _phase(tmp_path, pool_exists=True)
    assert not any("cachefile" in c or "zfs-import-cache" in c for c in calls), calls
