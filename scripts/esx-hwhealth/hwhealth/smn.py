"""SMN access through the root-complex PCI config index/data pair (0x60/0x64).

Safety rule: only TCTL and the two CCD addresses are ever written to the index, and only on an
AMD Raphael root complex; the index is read first and left alone if non-zero; it is always
restored to 0. Our own readers are serialised by an exclusive, non-blocking lock file.
"""
import os
import signal
from contextlib import contextmanager
from typing import Sequence

from .runner import Runner

ROOT_CFG = "/hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr"
TCTL = 0x59800
CCD = (0x59B08, 0x59B0C)
_ALLOWED = frozenset((TCTL, *CCD))
# Root complex vendor/device at PCI 0:0.0 config 0x0: AMD (0x1022) Raphael (0x14d8).
RAPHAEL_ROOT_ID = 0x14D81022
_TIMEOUT = 10.0
# One lock for every SMN reader on the host (the boot sampler and a manual --stdout run): the
# superDom sampler and a root shell can both write /var/run; the healthd plugin never does SMN.
LOCK_PATH = "/var/run/lab-hwhealth-smn.lock"
LOCKED_REASON = "smn locked by another reader"


class SmnBusy(Exception):
    """The SMN index register was non-zero: someone else is using it."""


class SmnUnsupported(Exception):
    """The root complex is not AMD Raphael: SMN must not be touched on this part."""


class SmnLocked(Exception):
    """Another reader of ours holds the SMN lock, or the lock cannot be taken: skip, never wait."""


def _import_fcntl():
    """fcntl is POSIX-only; import it lazily so the package still imports on Windows."""
    try:
        import fcntl
    except ImportError:
        return None
    return fcntl


@contextmanager
def flock_lock(path: str = LOCK_PATH, fcntl_mod=None):
    """Exclusive, non-blocking flock on path. Raise SmnLocked if it is held or unavailable.

    The lock file is never deleted: unlinking a lock file lets two readers lock different files.
    """
    fcntl_mod = fcntl_mod or _import_fcntl()
    if fcntl_mod is None:
        raise SmnLocked("smn lock unavailable: no fcntl")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        raise SmnLocked(f"smn lock unavailable: {exc.strerror or exc}") from exc
    try:
        try:
            fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
        except BlockingIOError as exc:
            raise SmnLocked(LOCKED_REASON) from exc
        except OSError as exc:
            raise SmnLocked(f"smn lock unavailable: {exc.strerror or exc}") from exc
        try:
            yield
        finally:
            fcntl_mod.flock(fd, fcntl_mod.LOCK_UN)
    finally:
        os.close(fd)  # closing also drops the lock if the unlock above failed


def default_lock():
    return flock_lock()


@contextmanager
def _sigterm_deferred():
    """Hold SIGTERM for the duration of the block, then deliver it to the original handler
    (or re-raise it under the default disposition).

    The sampler turns SIGTERM into SystemExit; raised inside the restore's vsish call it would
    abandon the write and leave the index non-zero. signal.signal only works on the main thread;
    elsewhere the block runs undeferred, as before.
    """
    pending = []
    try:
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: pending.append(signum))
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
    if not pending:
        return
    if callable(previous):
        previous(pending[0], None)
    elif previous == signal.SIG_DFL:
        signal.raise_signal(pending[0])  # default action (terminate) now that the index is 0


def _get(run: Runner, reg: str) -> int:
    out = run(["vsish", "-e", "get", f"{ROOT_CFG}/{reg}"], _TIMEOUT)
    return int(out.strip(), 16)


def _set_index(run: Runner, value: int) -> None:
    run(["vsish", "-e", "set", f"{ROOT_CFG}/0x60", hex(value)], _TIMEOUT)


def read_smn(run: Runner, addrs: Sequence[int], lock=None) -> dict[int, int]:
    """Read addrs through the index/data pair. lock() returns the context manager that
    serialises our own readers (default: the flock on LOCK_PATH); it raises SmnLocked if held."""
    bad = [a for a in addrs if a not in _ALLOWED]
    if bad:
        raise ValueError(f"SMN address not allowed: {[hex(a) for a in bad]}")
    with (lock if lock is not None else default_lock)():
        # Defence in depth: collect() checks this too, but no write may happen on another part.
        if _get(run, "0x0") != RAPHAEL_ROOT_ID:
            raise SmnUnsupported("root complex not supported")
        if _get(run, "0x60") != 0:
            raise SmnBusy("SMN index register is in use")
        result: dict[int, int] = {}
        try:
            for addr in addrs:
                _set_index(run, addr)
                result[addr] = _get(run, "0x64")
        finally:
            with _sigterm_deferred():
                _set_index(run, 0)
    return result
