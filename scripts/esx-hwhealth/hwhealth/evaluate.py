"""Status thresholds: the report's 'unstructured' dict in, (status, reasons) out.

Thresholds are the builder's defaults from the "Thresholds" table in
docs/superpowers/specs/2026-11-06-esx-hardware-health-design.md. Clock speed is not
evaluated: v1 charts it but never alerts on it.
"""

OK, WARNING, ERROR = 0, 1, 2

CPU_TEMP_WARNING_C = 90
CPU_TEMP_ERROR_C = 95
NVME_TEMP_WARNING_DEFAULT_C = 70
NVME_TEMP_ERROR_DEFAULT_C = 80
NVME_SPARE_WARNING_PCT = 20
NVME_SPARE_ERROR_PCT = 10


def _over(value, warning, error, label):
    """Temperature-style check: alert when the reading reaches the threshold."""
    if value >= error:
        return ERROR, f"{label} {value} >= {error}"
    if value >= warning:
        return WARNING, f"{label} {value} >= {warning}"
    return OK, None


def _under(value, warning, error, label):
    """Spare-style check: alert when the reading falls to the threshold."""
    if value <= error:
        return ERROR, f"{label} {value} <= {error}"
    if value <= warning:
        return WARNING, f"{label} {value} <= {warning}"
    return OK, None


def _cpu_checks(cpu):
    if not cpu.get("supported"):
        return [(WARNING, "cpu unsupported")]

    readings = []
    tctl = cpu.get("tctl_c")
    if tctl is not None:
        readings.append(_over(tctl, CPU_TEMP_WARNING_C, CPU_TEMP_ERROR_C, "tctl_c"))
    for i, ccd in enumerate(cpu.get("ccd_c") or []):
        if ccd is not None:  # an invalid CCD is None; never treat it as 0
            readings.append(_over(ccd, CPU_TEMP_WARNING_C, CPU_TEMP_ERROR_C, f"ccd_c[{i}]"))

    if not readings:
        return [(WARNING, "cpu readings missing")]
    return [r for r in readings if r[1] is not None]


def _nvme_checks(index, drive):
    label = f"nvme[{drive.get('adapter') or index}]"
    checks = []

    temp = drive.get("temp_c")
    if temp is not None:
        warning = drive.get("wctemp_c")
        error = drive.get("cctemp_c")
        warning = NVME_TEMP_WARNING_DEFAULT_C if warning is None else warning
        error = NVME_TEMP_ERROR_DEFAULT_C if error is None else error
        checks.append(_over(temp, warning, error, f"{label} temp_c"))

    critical = drive.get("critical_warning")
    if critical:
        checks.append((ERROR, f"{label} critical_warning {critical}"))

    spare = drive.get("spare_pct")
    if spare is not None:
        checks.append(_under(spare, NVME_SPARE_WARNING_PCT, NVME_SPARE_ERROR_PCT,
                             f"{label} spare_pct"))

    return [c for c in checks if c[1] is not None]


def evaluate(u: dict) -> tuple[int, list[str]]:
    """Return the overall status and a human-readable reason for each alerting reading.

    The input is only read, never mutated. Status is the highest level of any check.
    """
    checks = _cpu_checks(u.get("cpu") or {})
    for index, drive in enumerate(u.get("nvme") or []):
        checks.extend(_nvme_checks(index, drive))

    status = max((level for level, _ in checks), default=OK)
    reasons = [reason for _, reason in checks]
    return status, reasons
