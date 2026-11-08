import subprocess
import sys

import pytest

from fakes import FakeRunner, fixture
from hwhealth.readers import (NvmeHealth, read_cpu_identity, read_nvme, read_perf, read_rapl)
from hwhealth.runner import CommandError, subprocess_runner

NOT_SUPPORTED = fixture("vsish_not_supported.txt")
LIST = ("esxcli", "nvme", "device", "list")


def smart(a):
    return ("esxcli", "nvme", "device", "log", "smart", "get", "-A", a)


def get(a):
    return ("esxcli", "nvme", "device", "get", "-A", a)


def nvme_responses():
    r = {LIST: fixture("nvme_device_list.txt")}
    for a in ("vmhba0", "vmhba1", "vmhba2"):
        r[smart(a)] = fixture(f"nvme_smart_{a}.txt")
        r[get(a)] = fixture(f"nvme_device_get_{a}.txt")
    return r


def test_cpu_identity_from_fixture():
    cmd = ("esxcli", "hardware", "cpu", "list")
    run = FakeRunner({cmd: fixture("cpu_list.txt")})
    ident = read_cpu_identity(run)
    assert ident.vendor == "AuthenticAMD"
    assert ident.family == 25
    assert ident.model == 97
    assert ident.pcpus == 32
    assert ident.nominal_mhz == pytest.approx(2495.3, abs=0.1)
    assert run.calls[0][1] == 10.0


def test_read_rapl_from_fixture():
    base = "/hardware/msr/pcpu/0/addr/"
    run = FakeRunner({("vsish", "-e", "get", base + "0xC0010299"): fixture("msr_c0010299.txt"),
                      ("vsish", "-e", "get", base + "0xC001029B"): fixture("msr_c001029b.txt")})
    assert read_rapl(run) == (0xA1000, 0x8647A7CC)


def test_read_perf_from_fixture():
    run = FakeRunner({("vsish", "-e", "get", "/power/pcpu/0/perf"): fixture("pcpu0_perf.txt")})
    assert read_perf(run, 0) == (565115497405, 270057413775)


def test_vsish_error_text_is_missing_not_zero():
    script_err = f"import sys; sys.stderr.write({NOT_SUPPORTED!r}); sys.exit(2)"
    with pytest.raises(CommandError) as ei:
        subprocess_runner([sys.executable, "-c", script_err], 10.0)
    assert "Not supported" in ei.value.message
    script_out = f"import sys; sys.stdout.write({NOT_SUPPORTED!r})"
    with pytest.raises(CommandError):
        subprocess_runner([sys.executable, "-c", script_out], 10.0)


def test_subprocess_runner_ok_and_timeout():
    assert subprocess_runner([sys.executable, "-c", "print('hi')"], 10.0).strip() == "hi"
    with pytest.raises(CommandError) as ei:
        subprocess_runner([sys.executable, "-c", "import time; time.sleep(5)"], 0.3)
    assert ei.value.message == "timeout"


def test_nvme_from_fixtures():
    drives, errors = read_nvme(FakeRunner(nvme_responses()))
    assert errors == []
    assert [d.adapter for d in drives] == ["vmhba0", "vmhba1", "vmhba2"]
    d0 = drives[0]
    assert d0 == NvmeHealth(adapter="vmhba0", model="CT4000P310SSD8", temp_c=41, spare_pct=100,
                            used_pct=1, critical_warning=0, media_errors=0, wctemp_c=86, cctemp_c=88)
    d1 = drives[1]
    assert (d1.model, d1.temp_c, d1.used_pct, d1.wctemp_c, d1.cctemp_c) == (
        "SAMSUNG MZVLB512HAJQ-000L7", 41, 11, 81, 82)
    assert (drives[2].temp_c, drives[2].model) == (38, "CT1000P310SSD2")


def test_nvme_zero_threshold_is_none():
    r = nvme_responses()
    r[get("vmhba0")] = r[get("vmhba0")].replace("Threshold: 359", "Threshold: 0")
    drives, _ = read_nvme(FakeRunner(r))
    assert drives[0].wctemp_c is None and drives[0].cctemp_c == 88


def test_nvme_one_drive_fails():
    r = nvme_responses()
    r[smart("vmhba1")] = CommandError(list(smart("vmhba1")), "budget")
    drives, errors = read_nvme(FakeRunner(r))
    assert [d.adapter for d in drives] == ["vmhba0", "vmhba2"]
    assert errors == [{"reading": "nvme[vmhba1]", "reason": "budget"}]


def test_nvme_list_failure_propagates():
    with pytest.raises(CommandError):
        read_nvme(FakeRunner({LIST: CommandError(list(LIST), "x")}))


@pytest.mark.parametrize("kelvin", ["0 K", "-5 K"])
def test_nvme_non_positive_kelvin_temperature_is_an_error_not_a_value(kelvin):
    r = nvme_responses()
    r[smart("vmhba1")] = r[smart("vmhba1")].replace(
        "Composite Temperature: 314 K", f"Composite Temperature: {kelvin}")
    assert f"Composite Temperature: {kelvin}" in r[smart("vmhba1")]
    drives, errors = read_nvme(FakeRunner(r))
    assert [d.adapter for d in drives] == ["vmhba0", "vmhba2"]
    assert errors == [{"reading": "nvme[vmhba1]", "reason": "temperature not plausible"}]


def test_nvme_negative_threshold_is_none():
    r = nvme_responses()
    r[get("vmhba0")] = r[get("vmhba0")].replace("Threshold: 359", "Threshold: -1")
    drives, _ = read_nvme(FakeRunner(r))
    assert drives[0].wctemp_c is None and drives[0].cctemp_c == 88


def test_nvme_one_critical_warning_flag_sets_critical_warning():
    r = nvme_responses()
    r[smart("vmhba0")] = r[smart("vmhba0")].replace(
        "Read Only Mode: false", "Read Only Mode: true")
    assert "Read Only Mode: true" in r[smart("vmhba0")]
    drives, errors = read_nvme(FakeRunner(r))
    assert errors == [] and drives[0].critical_warning == 1
    assert [d.critical_warning for d in drives[1:]] == [0, 0]
