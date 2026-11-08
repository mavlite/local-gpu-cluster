import pytest

from fake_host import Clock, Host
from hwhealth.collect import collect
from hwhealth.runner import CommandError
from hwhealth.smn import CCD


def run_collect(host, clock, **kw):
    return collect(host, sleep=clock.sleep, now=clock.now, wall=lambda: "T", **kw)


def make():
    clock = Clock()
    return Host(clock), clock


def jump_on(clock, predicate, seconds=21):
    def hook(argv):
        if predicate(argv):
            clock.t += seconds
    return hook


def test_collect_full():
    host, clock = make()
    u = run_collect(host, clock)
    assert u["schema"] == 1
    cpu = u["cpu"]
    assert cpu["supported"] is True and cpu["family"] == 25 and cpu["model"] == 97
    assert cpu["tctl_c"] == pytest.approx(76.125)
    assert cpu["ccd_c"] == [pytest.approx(76.0), pytest.approx(76.0)]
    assert cpu["package_w"] == pytest.approx(150.47, abs=0.01)
    assert len(cpu["eff_mhz"]) == 32
    assert cpu["eff_mhz_summary"]["min"] == cpu["eff_mhz_summary"]["max"]
    assert len(u["nvme"]) == 3 and u["nvme"][0]["wctemp_c"] is not None
    assert u["errors"] == []
    assert u["sampled_at"] == "T" and u["duration_ms"] == 1000


def test_collect_unsupported_cpu():
    host = Host(family=25, model=33)
    u = run_collect(host, host.clock)
    assert u["cpu"]["supported"] is False
    for key in ("tctl_c", "ccd_c", "package_w", "eff_mhz"):
        assert key not in u["cpu"]
    assert len([e for e in u["errors"] if e["reading"] == "cpu"]) == 1
    assert host.msr_or_smn_calls == []


def test_collect_smn_busy():
    host, clock = make()
    host.index_reg = 4
    u = run_collect(host, clock)
    assert "tctl_c" not in u["cpu"] and "ccd_c" not in u["cpu"]
    reasons = {e["reading"]: e["reason"] for e in u["errors"]}
    assert reasons == {"tctl_c": "smn index busy", "ccd_c": "smn index busy"}
    assert "package_w" in u["cpu"] and len(u["nvme"]) == 3


def test_budget_spent_during_smn_still_restores_index():
    host, clock = make()
    host.on_call = jump_on(clock, lambda a: a[2] == "set" and a[-1] != "0x0")
    u = run_collect(host, clock)
    assert host.index == 0  # the restore is exempt from the budget
    assert {"reading": "nvme", "reason": "budget"} in u["errors"]
    assert u["nvme"] == []


def test_collect_respects_budget_per_nvme_drive():
    host, clock = make()
    host.on_call = jump_on(clock, lambda a: a[2:4] == ("device", "list"))
    u = run_collect(host, clock)
    assert u["nvme"] == []
    nvme = [e for e in u["errors"] if e["reading"].startswith("nvme[")]
    assert [e["reason"] for e in nvme] == ["budget"] * 3


def test_budget_spent_before_sampling_skips_steps():
    host, clock = make()
    host.on_call = jump_on(clock, lambda a: a[:3] == ("esxcli", "hardware", "cpu"))
    u = run_collect(host, clock)
    names = [e["reading"] for e in u["errors"] if e["reason"] == "budget"]
    assert names == ["rapl", "eff_mhz", "tctl_c", "ccd_c", "nvme"]
    assert "package_w" not in u["cpu"]


def test_invalid_ccd_keeps_slot():
    host, clock = make()
    host.smn[CCD[1]] = 1000  # valid bit clear
    u = run_collect(host, clock)
    assert u["cpu"]["ccd_c"][1] is None and u["cpu"]["ccd_c"][0] == pytest.approx(76.0)
    assert {"reading": "ccd_c[1]", "reason": "valid bit clear"} in u["errors"]


def test_counter_backwards_and_halted_core():
    host, clock = make()

    def second(p, a, m):
        if p == 0:
            return a - 5, m + 10
        if p == 1:
            return a + 10, m  # MPERF did not advance
        return a + 1000, m + 2000

    host.aperf2 = second
    u = run_collect(host, clock)
    eff = u["cpu"]["eff_mhz"]
    assert len(eff) == 32 and eff[0] is None and eff[1] is None and eff[2] is not None
    reasons = {e["reading"]: e["reason"] for e in u["errors"]}
    assert reasons["eff_mhz[0]"] == "counter went backwards"
    assert reasons["eff_mhz[1]"] == "core halted"
    summary = u["cpu"]["eff_mhz_summary"]
    assert summary["min"] == summary["max"] == eff[2]


def test_eff_summary_absent_when_no_values():
    host, clock = make()
    host.aperf2 = lambda p, a, m: (a, m)  # MPERF never advances
    u = run_collect(host, clock)
    assert u["cpu"]["eff_mhz"] == [None] * 32
    assert "eff_mhz_summary" not in u["cpu"]


def test_value_error_is_recorded_not_raised(monkeypatch):
    host, clock = make()

    def boom(run, addrs):
        raise ValueError("SMN address not allowed")

    monkeypatch.setattr("hwhealth.collect.read_smn", boom)
    u = run_collect(host, clock)
    reasons = {e["reading"]: e["reason"] for e in u["errors"]}
    assert reasons["tctl_c"] == reasons["ccd_c"] == "SMN address not allowed"
    assert "package_w" in u["cpu"]


def test_nvme_list_failure_is_recorded():
    host, clock = make()

    def failing(argv, timeout):
        if tuple(argv[:4]) == ("esxcli", "nvme", "device", "list"):
            raise CommandError(argv, "Connection failed: 503 Service Unavailable")
        return host(argv, timeout)

    u = collect(failing, sleep=clock.sleep, now=clock.now, wall=lambda: "T")
    assert u["nvme"] == []
    assert u["errors"] == [{"reading": "nvme",
                            "reason": "Connection failed: 503 Service Unavailable"}]


ROOT_ID_GET = ("vsish", "-e", "get", "/hardware/pci/seg/0/bus/0/slot/0/func/0/pciConfigReg/size/4/addr/0x0")


def _smn_calls(host):
    return [c for c in host.calls if c[0] == "vsish" and "pciConfigReg" in c[3] and c != ROOT_ID_GET]


@pytest.mark.parametrize("root_id", [0x71928086, 0x71908086, 0x14E81022, 0x0])
def test_foreign_root_complex_makes_no_smn_call(root_id):
    # R18: the nested VM passes family 25 / model 97 through; its root complex is an Intel 440BX.
    host, clock = make()
    host.root_id = root_id
    u = run_collect(host, clock)
    reasons = {e["reading"]: e["reason"] for e in u["errors"]}
    assert reasons == {"tctl_c": "root complex not supported", "ccd_c": "root complex not supported"}
    assert "tctl_c" not in u["cpu"] and "ccd_c" not in u["cpu"]
    assert ROOT_ID_GET in host.calls
    assert _smn_calls(host) == []  # no index GET, no vsish set, no data GET
    assert not [c for c in host.calls if c[:3] == ("vsish", "-e", "set")]
    assert "package_w" in u["cpu"] and len(u["nvme"]) == 3


def test_root_complex_read_failure_skips_smn_with_its_message():
    host, clock = make()

    def fail(argv):
        if tuple(argv) == ROOT_ID_GET:
            raise CommandError(argv, "Operation not permitted")
    host.on_call = fail
    u = run_collect(host, clock)
    reasons = {e["reading"]: e["reason"] for e in u["errors"]}
    assert reasons == {"tctl_c": "Operation not permitted", "ccd_c": "Operation not permitted"}
    assert _smn_calls(host) == []


def test_raphael_root_complex_is_checked_before_the_first_smn_access():
    host, clock = make()
    u = run_collect(host, clock)
    assert u["errors"] == [] and u["cpu"]["tctl_c"] == pytest.approx(76.125)
    first_smn = host.calls.index(_smn_calls(host)[0])
    assert host.calls.index(ROOT_ID_GET) < first_smn


def test_unparseable_root_complex_id_skips_smn_with_its_message():
    host, clock = make()
    host.root_id = None  # the fake then answers "0xNone"
    u = run_collect(host, clock)
    reasons = {e["reading"]: e["reason"] for e in u["errors"]}
    assert set(reasons) == {"tctl_c", "ccd_c"} and "invalid literal" in reasons["tctl_c"]
    assert _smn_calls(host) == []
