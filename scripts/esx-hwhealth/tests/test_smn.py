import signal

import pytest

from fakes import FakeRunner
from hwhealth.runner import CommandError
from hwhealth.smn import CCD, ROOT_CFG, TCTL, SmnBusy, SmnUnsupported, read_smn

GET0 = ("vsish", "-e", "get", f"{ROOT_CFG}/0x0")
RAPHAEL = "0x14d81022\n"
GET60 = ("vsish", "-e", "get", f"{ROOT_CFG}/0x60")
GET64 = ("vsish", "-e", "get", f"{ROOT_CFG}/0x64")


def set60(v):
    return ("vsish", "-e", "set", f"{ROOT_CFG}/0x60", v)


def test_smn_reads_and_restores():
    run = FakeRunner({GET0: RAPHAEL, GET60: "0x00000000\n", set60("0x59800"): "", GET64: "0x7d3b0000\n", set60("0x0"): ""})
    assert read_smn(run, [TCTL]) == {0x59800: 0x7D3B0000}
    assert run.argvs == [GET0, GET60, set60("0x59800"), GET64, set60("0x0")]
    assert all(t == 10.0 for _, t in run.calls)


def test_smn_busy_skips_without_writes():
    run = FakeRunner({GET0: RAPHAEL, GET60: "0x00000004\n"})
    with pytest.raises(SmnBusy):
        read_smn(run, [TCTL])
    assert run.argvs == [GET0, GET60]
    assert not any(a[2] == "set" for a in run.argvs)


def test_smn_restores_index_on_error():
    err = CommandError(["vsish"], "boom")
    run = FakeRunner({GET0: RAPHAEL, GET60: "0x00000000\n", set60("0x59800"): "", GET64: err, set60("0x0"): ""})
    with pytest.raises(CommandError):
        read_smn(run, [TCTL])
    assert run.argvs[-1] == set60("0x0")


def test_smn_rejects_unlisted_address_before_any_call():
    run = FakeRunner({})
    with pytest.raises(ValueError):
        read_smn(run, [0x12345])
    assert run.calls == []


def test_smn_multiple_addresses():
    run = FakeRunner({GET0: RAPHAEL, GET60: "0x0\n", set60(hex(CCD[0])): "", set60(hex(CCD[1])): "",
                      GET64: "0x00000010\n", set60("0x0"): ""})
    assert read_smn(run, list(CCD)) == {CCD[0]: 0x10, CCD[1]: 0x10}


@pytest.mark.parametrize("exc", [SystemExit(143), KeyboardInterrupt()])
def test_smn_restores_index_when_the_process_is_told_to_exit(exc):
    # I1: the sampler's SIGTERM handler raises SystemExit; the finally must still restore 0.
    run = FakeRunner({GET0: RAPHAEL, GET60: "0x00000000\n", set60("0x59800"): "", GET64: exc, set60("0x0"): ""})
    with pytest.raises(type(exc)):
        read_smn(run, [TCTL])
    assert run.argvs[-1] == set60("0x0")


@pytest.mark.parametrize("root_id", ["0x71928086\n", "0x14e81022\n", "0x00000000\n"])
def test_smn_refuses_a_foreign_root_complex_before_any_write(root_id):
    # M4: defence in depth; collect() checks too, but read_smn must never write on another part.
    run = FakeRunner({GET0: root_id})
    with pytest.raises(SmnUnsupported):
        read_smn(run, [TCTL, *CCD])
    assert run.argvs == [GET0]
    assert not any(a[2] == "set" for a in run.argvs)


def test_smn_unsupported_is_not_mistaken_for_busy():
    assert not issubclass(SmnUnsupported, SmnBusy)



class _TermAt(FakeRunner):
    """Delivers SIGTERM, through whatever handler is installed, as the `at` call starts.
    Records whether the restore write (set 0x60 to 0x0) ran to completion."""

    def __init__(self, responses, at):
        super().__init__(responses)
        self.at = at
        self.restore_completed = False

    def __call__(self, argv, timeout):
        if tuple(argv) == self.at:
            handler = signal.getsignal(signal.SIGTERM)
            if callable(handler):
                handler(signal.SIGTERM, None)
        out = super().__call__(argv, timeout)
        if tuple(argv) == set60("0x0"):
            self.restore_completed = True
        return out


NL = chr(10)
OK_READ = {GET0: RAPHAEL, GET60: "0x00000000"+NL, set60("0x59800"): "", GET64: "0x7d3b0000"+NL, set60("0x0"): ""}


def _raise_exit(signum, frame):
    raise SystemExit(128 + signum)


@pytest.fixture
def exit_handler():
    previous = signal.signal(signal.SIGTERM, _raise_exit)
    yield
    signal.signal(signal.SIGTERM, previous)


@pytest.mark.parametrize("at", [set60("0x59800"), GET64, set60("0x0")], ids=["index-write", "data-read", "restore"])
def test_sigterm_inside_the_write_window_is_held_until_index_is_zero(exit_handler, at):
    run = _TermAt(OK_READ, at)
    with pytest.raises(SystemExit) as exc:
        read_smn(run, [TCTL])
    assert run.restore_completed
    assert run.argvs[-1] == set60("0x0")
    assert exc.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is _raise_exit


def test_held_sigterm_is_delivered_even_when_the_restore_fails(exit_handler):
    responses = {**OK_READ, set60("0x0"): CommandError(["vsish"], "timeout")}
    run = _TermAt(responses, GET64)
    with pytest.raises(SystemExit):
        read_smn(run, [TCTL])
    assert signal.getsignal(signal.SIGTERM) is _raise_exit


def test_no_signal_keeps_handlers_and_result(exit_handler):
    run = FakeRunner(OK_READ)
    assert read_smn(run, [TCTL]) == {0x59800: 0x7D3B0000}
    assert signal.getsignal(signal.SIGTERM) is _raise_exit


def test_sigint_is_held_too():
    previous = signal.getsignal(signal.SIGINT)
    seen = []

    class _IntAt(FakeRunner):
        def __call__(self, argv, timeout):
            if tuple(argv) == set60("0x0"):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            out = super().__call__(argv, timeout)
            seen.append(tuple(argv))
            return out

    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            read_smn(_IntAt(OK_READ), [TCTL])
        assert seen[-1] == set60("0x0")
    finally:
        signal.signal(signal.SIGINT, previous)


def test_sigterm_with_default_handler_is_re_raised_after_restore(monkeypatch):
    previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        run = _TermAt(OK_READ, GET64)
        raised = []
        monkeypatch.setattr(signal, "raise_signal",
                            lambda signum: raised.append((signum, run.restore_completed)))
        read_smn(run, [TCTL])
        assert raised == [(signal.SIGTERM, True)]
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, previous)
