"""SMN access through the root-complex PCI config index/data pair (0x60/0x64).

Safety rule: only TCTL and the two CCD addresses are ever written to the index, and only on an
AMD Raphael root complex; the index is read first and left alone if non-zero; it is always
restored to 0.
"""
from typing import Sequence

from .runner import Runner

ROOT_CFG = "/hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr"
TCTL = 0x59800
CCD = (0x59B08, 0x59B0C)
_ALLOWED = frozenset((TCTL, *CCD))
# Root complex vendor/device at PCI 0:0.0 config 0x0: AMD (0x1022) Raphael (0x14d8).
RAPHAEL_ROOT_ID = 0x14D81022
_TIMEOUT = 10.0


class SmnBusy(Exception):
    """The SMN index register was non-zero: someone else is using it."""


class SmnUnsupported(Exception):
    """The root complex is not AMD Raphael: SMN must not be touched on this part."""


def _get(run: Runner, reg: str) -> int:
    out = run(["vsish", "-e", "get", f"{ROOT_CFG}/{reg}"], _TIMEOUT)
    return int(out.strip(), 16)


def _set_index(run: Runner, value: int) -> None:
    run(["vsish", "-e", "set", f"{ROOT_CFG}/0x60", hex(value)], _TIMEOUT)


def read_smn(run: Runner, addrs: Sequence[int]) -> dict[int, int]:
    bad = [a for a in addrs if a not in _ALLOWED]
    if bad:
        raise ValueError(f"SMN address not allowed: {[hex(a) for a in bad]}")
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
        _set_index(run, 0)
    return result
