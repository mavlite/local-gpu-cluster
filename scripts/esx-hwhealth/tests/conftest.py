import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import contextlib  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _free_smn_lock(monkeypatch):
    """The real SMN lock is a flock in /var/run (POSIX only). Tests get a free in-memory lock;
    tests/test_smn_lock.py exercises flock_lock itself with a fake fcntl."""
    from hwhealth import smn
    monkeypatch.setattr(smn, "default_lock", contextlib.nullcontext)
