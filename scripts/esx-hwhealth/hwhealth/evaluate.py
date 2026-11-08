"""Status thresholds: the report's 'unstructured' dict in, (status, reasons) out.

Thresholds are the builder's defaults from the "Thresholds" table in
docs/superpowers/specs/2026-11-06-esx-hardware-health-design.md. Clock speed is not
evaluated: v1 charts it but never alerts on it.
"""

OK, WARNING, ERROR = 0, 1, 2

CPU_TEMP_WARNING_C = 90
CPU_TEMP_ERROR_C = 95
# NVMe thresholds: derived from the drives' documentation, 2026-11-08 (spec, "Thresholds").
NVME_TEMP_WARNING_C = 70            # vendor operating maximum (Crucial P310, Samsung PM981)
NVME_TEMP_ERROR_DEFAULT_C = 80      # used only when the drive reports no WCTEMP
NVME_SPARE_THRESHOLD_DEFAULT_PCT = 10  # used only when the drive reports no spare threshold
NVME_SPARE_WARNING_MARGIN_PCT = 10  # WARNING within this many points above the drive's threshold
NVME_USED_WARNING_PCT = 80
NVME_USED_ERROR_PCT = 100           # NVMe: 100 = estimated rated endurance consumed
# NVMe Critical Warning bits graded ERROR; any other set bit is a WARNING, never silent.
NVME_CRITICAL_WARNING_BITS = {
    0x01: "spare below threshold",
    0x02: "temperature",
    0x04: "reliability degraded",
    0x08: "read-only",
    0x10: "volatile backup failed",
}


def _over(value, warning, error, label):
    """Temperature-style check: alert when the reading reaches the threshold."""
    if value >= error:
        return ERROR, f"{label} {value} >= {error}"
    if value >= warning:
        return WARNING, f"{label} {value} >= {warning}"
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


def _nvme_temp_check(temp, wctemp, label):
    """WARNING at the operating maximum, ERROR at the drive's own warning temperature."""
    error = NVME_TEMP_ERROR_DEFAULT_C if wctemp is None else wctemp
    if temp >= error:
        return ERROR, f"{label} temp_c {temp} >= {error}"
    if temp >= NVME_TEMP_WARNING_C:
        return WARNING, f"{label} temp_c {temp} >= {NVME_TEMP_WARNING_C}"
    return OK, None


def _nvme_critical_checks(critical, label):
    checks = []
    for bit, name in NVME_CRITICAL_WARNING_BITS.items():
        if critical & bit:
            checks.append((ERROR, f"{label} critical_warning {name}"))
    unknown = critical & ~sum(NVME_CRITICAL_WARNING_BITS)
    if unknown:
        checks.append((WARNING, f"{label} critical_warning unknown bits {unknown:#04x}"))
    return checks


def _nvme_spare_check(spare, threshold, label):
    """ERROR below the drive's own spare threshold, WARNING within the margin above it."""
    floor = NVME_SPARE_THRESHOLD_DEFAULT_PCT if threshold is None else threshold
    if spare < floor:
        return ERROR, f"{label} spare_pct {spare} < {floor}"
    warning = floor + NVME_SPARE_WARNING_MARGIN_PCT
    if spare <= warning:
        return WARNING, f"{label} spare_pct {spare} <= {warning}"
    return OK, None


def _nvme_checks(index, drive):
    label = f"nvme[{drive.get('adapter') or index}]"
    checks = []

    temp = drive.get("temp_c")
    if temp is not None:
        checks.append(_nvme_temp_check(temp, drive.get("wctemp_c"), label))

    checks.extend(_nvme_critical_checks(drive.get("critical_warning") or 0, label))

    spare = drive.get("spare_pct")
    if spare is not None:
        checks.append(_nvme_spare_check(spare, drive.get("spare_threshold_pct"), label))

    used = drive.get("used_pct")
    if used is not None:
        checks.append(_over(used, NVME_USED_WARNING_PCT, NVME_USED_ERROR_PCT, f"{label} used_pct"))

    media = drive.get("media_errors")
    if media:
        checks.append((ERROR, f"{label} media_errors {media} > 0"))

    return [c for c in checks if c[1] is not None]


def _nvme_error_checks(errors):
    """A drive (or the whole NVMe step) that could not be read is a WARNING, never silent."""
    checks = []
    for entry in errors:
        reading = str((entry or {}).get("reading") or "")
        if reading.startswith("nvme"):
            checks.append((WARNING, f"{reading} unreadable: {entry.get('reason')}"))
    return checks


def evaluate(u: dict) -> tuple[int, list[str]]:
    """Return the overall status and a human-readable reason for each alerting reading.

    The input is only read, never mutated. Status is the highest level of any check.
    """
    checks = _cpu_checks(u.get("cpu") or {})
    for index, drive in enumerate(u.get("nvme") or []):
        checks.extend(_nvme_checks(index, drive))
    checks.extend(_nvme_error_checks(u.get("errors") or []))

    status = max((level for level, _ in checks), default=OK)
    reasons = [reason for _, reason in checks]
    return status, reasons
