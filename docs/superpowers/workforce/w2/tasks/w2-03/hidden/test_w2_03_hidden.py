"""Hidden checks for w2-03: 143 is a success inside the unit's [Service] section; the fail-safe stays."""
import os
import re

INSTALLER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "56-fan-control.sh")


def service_section():
    text = open(INSTALLER, encoding="utf-8").read()
    unit = text[text.index("[Unit]\nDescription=V620"):]
    return unit[unit.index("[Service]"):unit.index("[Install]")]


def test_success_exit_status_143_is_in_the_service_section():
    sec = service_section()
    assert re.search(r"^SuccessExitStatus=.*\b143\b", sec, re.M), sec


def test_the_fail_safe_on_stop_is_kept():
    assert "ExecStopPost=/usr/local/bin/v620-fan-bridge.sh --failsafe" in service_section()
