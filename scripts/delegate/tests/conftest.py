"""Register the opt-in `live` marker and deselect it unless explicitly requested."""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs the real router/GPU; opt in with -m live")


def pytest_collection_modifyitems(config, items):
    if "live" in (config.getoption("-m") or ""):
        return
    kept = [i for i in items if "live" not in i.keywords]
    dropped = [i for i in items if "live" in i.keywords]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept
