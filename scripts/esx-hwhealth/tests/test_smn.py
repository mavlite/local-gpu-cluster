import pytest

from fakes import FakeRunner
from hwhealth.runner import CommandError
from hwhealth.smn import CCD, ROOT_CFG, TCTL, SmnBusy, read_smn

GET60 = ("vsish", "-e", "get", f"{ROOT_CFG}/0x60")
GET64 = ("vsish", "-e", "get", f"{ROOT_CFG}/0x64")


def set60(v):
    return ("vsish", "-e", "set", f"{ROOT_CFG}/0x60", v)


def test_smn_reads_and_restores():
    run = FakeRunner({GET60: "0x00000000\n", set60("0x59800"): "", GET64: "0x7d3b0000\n", set60("0x0"): ""})
    assert read_smn(run, [TCTL]) == {0x59800: 0x7D3B0000}
    assert run.argvs == [GET60, set60("0x59800"), GET64, set60("0x0")]
    assert all(t == 10.0 for _, t in run.calls)


def test_smn_busy_skips_without_writes():
    run = FakeRunner({GET60: "0x00000004\n"})
    with pytest.raises(SmnBusy):
        read_smn(run, [TCTL])
    assert run.argvs == [GET60]
    assert not any(a[2] == "set" for a in run.argvs)


def test_smn_restores_index_on_error():
    err = CommandError(["vsish"], "boom")
    run = FakeRunner({GET60: "0x00000000\n", set60("0x59800"): "", GET64: err, set60("0x0"): ""})
    with pytest.raises(CommandError):
        read_smn(run, [TCTL])
    assert run.argvs[-1] == set60("0x0")


def test_smn_rejects_unlisted_address_before_any_call():
    run = FakeRunner({})
    with pytest.raises(ValueError):
        read_smn(run, [0x12345])
    assert run.calls == []


def test_smn_multiple_addresses():
    run = FakeRunner({GET60: "0x0\n", set60(hex(CCD[0])): "", set60(hex(CCD[1])): "",
                      GET64: "0x00000010\n", set60("0x0"): ""})
    assert read_smn(run, list(CCD)) == {CCD[0]: 0x10, CCD[1]: 0x10}
