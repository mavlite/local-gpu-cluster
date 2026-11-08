import copy

from hwhealth.evaluate import OK, WARNING, ERROR, evaluate


def _sample():
    """A fresh report in the spec's 'unstructured' shape, healthy by default."""
    return {
        "schema": 1,
        "cpu": {
            "family": 25, "model": 97, "supported": True,
            "tctl_c": 76.1, "ccd_c": [64.9, 76.5],
            "package_w": 75.2,
            "eff_mhz": [4810, 4795],
            "eff_mhz_summary": {"min": 3900, "avg": 4800, "max": 5300},
        },
        "nvme": [{
            "adapter": "vmhba2", "model": "CT4000P310", "temp_c": 41,
            "spare_pct": 100, "used_pct": 1, "critical_warning": 0, "media_errors": 0,
        }],
        "errors": [],
        "sampled_at": "2026-11-06T15:40:00Z",
        "duration_ms": 1300,
    }


def _nvme(**fields):
    drive = {"adapter": "vmhba0", "model": "TEST", "temp_c": 41,
             "spare_pct": 100, "used_pct": 1, "critical_warning": 0, "media_errors": 0}
    drive.update(fields)
    return drive


def test_ok():
    assert evaluate(_sample()) == (OK, [])


def test_cpu_warning_at_90():
    u = _sample()
    u["cpu"]["tctl_c"] = 90.0
    status, reasons = evaluate(u)
    assert status == WARNING
    assert any("tctl_c" in r and "90.0" in r for r in reasons)


def test_cpu_error_at_95():
    u = _sample()
    u["cpu"]["ccd_c"] = [64.9, 95.0]
    status, reasons = evaluate(u)
    assert status == ERROR
    assert any("ccd_c[1]" in r and "95.0" in r for r in reasons)


def _status(drive):
    u = _sample()
    u["nvme"] = [drive]
    return evaluate(u)


def test_nvme_temp_warns_at_70_whatever_the_drive_reports():
    status, reasons = _status(_nvme(temp_c=70, wctemp_c=86, cctemp_c=88))
    assert status == WARNING
    assert any("temp_c 70 >= 70" in r for r in reasons)
    assert _status(_nvme(temp_c=69, wctemp_c=86, cctemp_c=88)) == (OK, [])


def test_nvme_temp_error_at_the_drive_wctemp():
    status, reasons = _status(_nvme(temp_c=86, wctemp_c=86, cctemp_c=88))
    assert status == ERROR
    assert any("temp_c 86 >= 86" in r for r in reasons)
    assert _status(_nvme(temp_c=85, wctemp_c=86, cctemp_c=88))[0] == WARNING
    # Samsung PM981: WCTEMP 81 C
    assert _status(_nvme(temp_c=81, wctemp_c=81, cctemp_c=82))[0] == ERROR


def test_nvme_temp_error_defaults_to_80_without_wctemp():
    assert _status(_nvme(temp_c=79))[0] == WARNING
    status, reasons = _status(_nvme(temp_c=80, wctemp_c=None))
    assert status == ERROR
    assert any("temp_c 80 >= 80" in r for r in reasons)


def test_nvme_each_known_critical_warning_bit_is_an_error_and_named():
    names = {1: "spare below threshold", 2: "temperature", 4: "reliability degraded",
             8: "read-only", 16: "volatile backup failed"}
    for bit, name in names.items():
        status, reasons = _status(_nvme(critical_warning=bit))
        assert status == ERROR, bit
        assert any(f"critical_warning {name}" in r for r in reasons), (bit, reasons)


def test_nvme_unknown_critical_warning_bit_warns():
    status, reasons = _status(_nvme(critical_warning=32))
    assert status == WARNING
    assert any("critical_warning unknown bits 0x20" in r for r in reasons)


def test_nvme_several_critical_warning_bits_each_reported():
    status, reasons = _status(_nvme(critical_warning=4 | 8 | 64))
    assert status == ERROR
    assert sum("critical_warning" in r for r in reasons) == 3


def test_nvme_spare_uses_the_drives_own_threshold():
    # Crucial P310: threshold 5 % -> WARNING at <= 15 %, ERROR below 5 %
    assert _status(_nvme(spare_pct=16, spare_threshold_pct=5)) == (OK, [])
    status, reasons = _status(_nvme(spare_pct=15, spare_threshold_pct=5))
    assert status == WARNING
    assert any("spare_pct 15 <= 15" in r for r in reasons)
    assert _status(_nvme(spare_pct=5, spare_threshold_pct=5))[0] == WARNING
    status, reasons = _status(_nvme(spare_pct=4, spare_threshold_pct=5))
    assert status == ERROR
    assert any("spare_pct 4 < 5" in r for r in reasons)
    # Samsung PM981: threshold 10 % -> WARNING at <= 20 %
    assert _status(_nvme(spare_pct=20, spare_threshold_pct=10))[0] == WARNING


def test_nvme_spare_falls_back_to_a_10_pct_threshold():
    assert _status(_nvme(spare_pct=21)) == (OK, [])
    assert _status(_nvme(spare_pct=20, spare_threshold_pct=None))[0] == WARNING
    assert _status(_nvme(spare_pct=9))[0] == ERROR


def test_nvme_endurance_used():
    assert _status(_nvme(used_pct=79)) == (OK, [])
    status, reasons = _status(_nvme(used_pct=80))
    assert status == WARNING
    assert any("used_pct 80 >= 80" in r for r in reasons)
    status, reasons = _status(_nvme(used_pct=100))
    assert status == ERROR
    assert any("used_pct 100 >= 100" in r for r in reasons)
    assert _status(_nvme(used_pct=255))[0] == ERROR  # the field saturates at 255


def test_nvme_any_media_error_is_an_error():
    assert _status(_nvme(media_errors=0)) == (OK, [])
    status, reasons = _status(_nvme(media_errors=1))
    assert status == ERROR
    assert any("media_errors 1 > 0" in r for r in reasons)


def test_unsupported_or_missing_cpu_warns():
    u = _sample()
    u["cpu"]["supported"] = False
    status, reasons = evaluate(u)
    assert status == WARNING
    assert any("unsupported" in r for r in reasons)

    u = _sample()
    del u["cpu"]["tctl_c"]
    del u["cpu"]["ccd_c"]
    status, reasons = evaluate(u)
    assert status == WARNING
    assert any("missing" in r for r in reasons)


def test_status_is_max():
    u = _sample()
    u["cpu"]["supported"] = False
    u["nvme"] = [_nvme(critical_warning=4)]
    status, reasons = evaluate(u)
    assert status == ERROR
    assert len(reasons) == 2
    assert any("unsupported" in r for r in reasons)
    assert any("critical_warning" in r for r in reasons)


def test_ccd_none_entries_are_skipped_not_zero():
    u = _sample()
    u["cpu"]["ccd_c"] = [None, 64.9]
    assert evaluate(u) == (OK, [])

    u["cpu"]["ccd_c"] = [None, None]
    assert evaluate(u) == (OK, [])  # tctl_c still present, so readings are not all missing

    u["cpu"]["tctl_c"] = None
    status, reasons = evaluate(u)
    assert status == WARNING
    assert any("missing" in r for r in reasons)


def test_nvme_missing_fields_are_not_evaluated():
    u = _sample()
    u["nvme"] = [{"adapter": "vmhba3", "critical_warning": 0}]
    assert evaluate(u) == (OK, [])

    u["nvme"] = [_nvme(temp_c=None, spare_pct=None)]
    assert evaluate(u) == (OK, [])


def test_input_is_not_mutated():
    u = _sample()
    u["cpu"]["ccd_c"] = [None, 95.0]
    u["nvme"] = [_nvme(temp_c=85, spare_pct=10, critical_warning=1)]
    before = copy.deepcopy(u)
    evaluate(u)
    assert u == before


def test_one_unreadable_nvme_drive_warns_with_its_reason():
    u = _sample()
    u["errors"] = [{"reading": "nvme[vmhba1]", "reason": "temperature not plausible"}]
    status, reasons = evaluate(u)
    assert status >= WARNING
    assert "nvme[vmhba1] unreadable: temperature not plausible" in reasons


def test_step_level_nvme_budget_error_warns():
    u = _sample()
    u["nvme"] = []
    u["errors"] = [{"reading": "nvme", "reason": "budget"}]
    assert evaluate(u) == (WARNING, ["nvme unreadable: budget"])


def test_non_nvme_errors_do_not_change_the_status():
    u = _sample()
    u["errors"] = [{"reading": "eff_mhz[3]", "reason": "core halted"}]
    assert evaluate(u) == (OK, [])


def test_nvme_error_does_not_lower_an_error_status():
    u = _sample()
    u["nvme"][0]["critical_warning"] = 1
    u["errors"] = [{"reading": "nvme[vmhba1]", "reason": "budget"}]
    status, reasons = evaluate(u)
    assert status == ERROR and "nvme[vmhba1] unreadable: budget" in reasons
