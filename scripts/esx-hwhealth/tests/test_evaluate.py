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


def test_nvme_uses_wctemp_cctemp():
    u = _sample()
    u["nvme"] = [_nvme(temp_c=76, wctemp_c=75, cctemp_c=85)]
    status, reasons = evaluate(u)
    assert status == WARNING
    assert any("temp_c" in r and "76" in r for r in reasons)

    u["nvme"] = [_nvme(temp_c=85, wctemp_c=75, cctemp_c=85)]
    status, reasons = evaluate(u)
    assert status == ERROR
    assert any("temp_c" in r and "85" in r for r in reasons)


def test_nvme_threshold_defaults():
    u = _sample()
    u["nvme"] = [_nvme(temp_c=70)]
    status, reasons = evaluate(u)
    assert status == WARNING
    assert any("temp_c" in r for r in reasons)

    u["nvme"] = [_nvme(temp_c=80)]
    status, reasons = evaluate(u)
    assert status == ERROR
    assert any("temp_c" in r for r in reasons)


def test_nvme_critical_warning_and_spare():
    u = _sample()
    u["nvme"] = [_nvme(critical_warning=1)]
    status, reasons = evaluate(u)
    assert status == ERROR
    assert any("critical_warning" in r for r in reasons)

    u["nvme"] = [_nvme(spare_pct=20)]
    status, reasons = evaluate(u)
    assert status == WARNING
    assert any("spare_pct" in r and "20" in r for r in reasons)

    u["nvme"] = [_nvme(spare_pct=10)]
    status, reasons = evaluate(u)
    assert status == ERROR
    assert any("spare_pct" in r and "10" in r for r in reasons)


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
