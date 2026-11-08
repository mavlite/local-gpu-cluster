"""M2: one exclusive, non-blocking lock serialises every SMN reader (sampler and --stdout)."""
import errno
from contextlib import contextmanager

import pytest

from fake_host import Clock, Host
from fakes import FakeRunner
from hwhealth import smn
from hwhealth.collect import collect
from hwhealth.smn import LOCK_PATH, SmnLocked, TCTL, flock_lock, read_smn


def held_lock():
    raise SmnLocked("smn locked by another reader")


@contextmanager
def free_lock(log):
    log.append("acquired")
    yield
    log.append("released")


def test_lock_file_lives_in_var_run():
    assert LOCK_PATH.startswith("/var/run/")


def test_read_smn_with_lock_held_makes_no_call():
    run = FakeRunner({})
    with pytest.raises(SmnLocked):
        read_smn(run, [TCTL], lock=held_lock)
    assert run.calls == []


def test_read_smn_holds_the_lock_from_the_first_read_through_the_restore():
    log = []
    host = Host()
    inner = host.__call__

    def run(argv, timeout):
        log.append(tuple(argv)[2])
        return inner(argv, timeout)
    raw = read_smn(run, [TCTL], lock=lambda: free_lock(log))
    assert raw == {TCTL: host.smn[TCTL]}
    assert log[0] == "acquired" and log[-1] == "released"
    assert log[-2] == "set" and host.index == 0  # restore happened inside the lock


def test_collect_with_lock_held_skips_temperatures_without_any_smn_call():
    clock = Clock()
    host = Host(clock)
    u = collect(host, sleep=clock.sleep, now=clock.now, wall=lambda: "T", smn_lock=held_lock)
    reasons = {e["reading"]: e["reason"] for e in u["errors"]}
    assert reasons == {"tctl_c": "smn locked by another reader",
                       "ccd_c": "smn locked by another reader"}
    smn_calls = [c for c in host.calls if c[0] == "vsish" and c[3].endswith(("/0x60", "/0x64"))]
    assert smn_calls == []
    assert "tctl_c" not in u["cpu"] and "package_w" in u["cpu"] and len(u["nvme"]) == 3


def test_collect_with_lock_free_is_unchanged():
    clock = Clock()
    host = Host(clock)
    log = []
    u = collect(host, sleep=clock.sleep, now=clock.now, wall=lambda: "T",
                smn_lock=lambda: free_lock(log))
    assert u["errors"] == [] and u["cpu"]["tctl_c"] == pytest.approx(76.125)
    assert log == ["acquired", "released"]


def test_default_lock_is_used_when_none_is_injected(monkeypatch):
    log = []
    monkeypatch.setattr(smn, "default_lock", lambda: free_lock(log))
    read_smn(Host(), [TCTL])
    assert log == ["acquired", "released"]


class FakeFcntl:
    LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8

    def __init__(self, error=None):
        self.error = error
        self.ops = []

    def flock(self, fd, op):
        self.ops.append(op)
        if self.error is not None and op != self.LOCK_UN:
            raise self.error


def test_flock_lock_free_takes_exclusive_non_blocking_and_unlocks(tmp_path):
    f = FakeFcntl()
    with flock_lock(str(tmp_path / "smn.lock"), fcntl_mod=f):
        assert f.ops == [f.LOCK_EX | f.LOCK_NB]
    assert f.ops[-1] == f.LOCK_UN
    assert (tmp_path / "smn.lock").exists()  # never deleted: unlinking a lock file races


def test_flock_lock_held_raises_without_waiting(tmp_path):
    f = FakeFcntl(BlockingIOError(errno.EWOULDBLOCK, "busy"))
    with pytest.raises(SmnLocked, match="^smn locked by another reader$"):
        with flock_lock(str(tmp_path / "smn.lock"), fcntl_mod=f):
            pytest.fail("entered a held lock")
    assert f.ops == [f.LOCK_EX | f.LOCK_NB]


def test_flock_lock_other_failure_fails_closed(tmp_path):
    f = FakeFcntl(OSError(errno.ENOLCK, "no locks"))
    with pytest.raises(SmnLocked, match="smn lock unavailable"):
        with flock_lock(str(tmp_path / "smn.lock"), fcntl_mod=f):
            pytest.fail("entered a failed lock")


def test_flock_lock_unopenable_file_fails_closed(tmp_path):
    with pytest.raises(SmnLocked, match="smn lock unavailable"):
        with flock_lock(str(tmp_path / "missing-dir" / "smn.lock"), fcntl_mod=FakeFcntl()):
            pytest.fail("entered without a lock file")


def test_flock_lock_without_fcntl_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(smn, "_import_fcntl", lambda: None)
    with pytest.raises(SmnLocked, match="smn lock unavailable"):
        with flock_lock(str(tmp_path / "smn.lock")):
            pytest.fail("entered without fcntl")
