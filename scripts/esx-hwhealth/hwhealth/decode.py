"""Pure decoders: raw MSR and counter values in, degrees C, joules and MHz out.

Rules follow the Linux k10temp driver for family 19h and the RAPL/APERF definitions
in docs/superpowers/specs/2026-11-06-esx-hardware-health-design.md ("Decode rules").
"""

CUR_TEMP_RANGE_SEL_BIT = 1 << 19
CCD_VALID_BIT = 1 << 11
ENERGY_COUNTER_MOD = 2 ** 32


def tctl_c(raw: int) -> float:
    """Decode the Tctl temperature from a F19h THERMAL_CONTROL register value."""
    temp = ((raw >> 21) & 0x7FF) * 0.125
    if raw & CUR_TEMP_RANGE_SEL_BIT:
        temp -= 49
    return temp


def ccd_c(raw: int) -> float | None:
    """Decode a CCD temperature; None when the CCD's valid bit is clear."""
    if not raw & CCD_VALID_BIT:
        return None
    return (raw & 0x7FF) * 0.125 - 49


def rapl_energy_unit_j(unit_raw: int) -> float:
    """Joules per RAPL energy count, from the MSR_RAPL_POWER_UNIT register value."""
    return 2 ** -((unit_raw >> 8) & 0x1F)


def energy_delta_j(before: int, after: int, unit_j: float) -> float:
    """Energy consumed between two reads of the 32-bit RAPL energy counter."""
    return ((after - before) % ENERGY_COUNTER_MOD) * unit_j


def effective_mhz(nominal_mhz: float, aperf_delta: int, mperf_delta: int) -> float | None:
    """Effective clock from APERF/MPERF deltas; None when MPERF did not advance."""
    if mperf_delta == 0:
        return None
    return nominal_mhz * aperf_delta / mperf_delta
