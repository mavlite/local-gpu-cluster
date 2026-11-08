"""Collector: run every reader under a time budget and build the report's 'unstructured' dict.

Order: cpu identity, RAPL+perf #1, sleep, RAPL+perf #2, SMN temperatures, NVMe. A reading that
fails is recorded in errors[] and left out of the result; the run itself never raises.
"""
import time
from dataclasses import asdict
from datetime import datetime, timezone

from .decode import ccd_c, effective_mhz, energy_delta_j, rapl_energy_unit_j, tctl_c
from .readers import read_cpu_identity, read_nvme, read_perf, read_rapl
from .runner import CommandError, Runner
from .smn import CCD, ROOT_CFG, TCTL, SmnBusy, read_smn

SCHEMA = 1
SUPPORTED = ("AuthenticAMD", 25, 97)  # vendor, family, model
_SAMPLE_GAP_S = 1.0
# Root complex vendor/device at PCI 0:0.0 config 0x0: AMD (0x1022) Raphael (0x14d8). SMN is only
# touched on this part; a VM can pass family 25 / model 97 through on a virtual (Intel) root complex.
RAPHAEL_ROOT_ID = 0x14D81022
_ROOT_ID_TIMEOUT_S = 10.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err(reading: str, reason: str) -> dict:
    return {"reading": reading, "reason": reason}


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc)


def _budgeted(run: Runner, now, start: float, budget_s: float) -> Runner:
    """Wrap run so that once the budget is spent every call raises CommandError('budget').

    The SMN index restore (vsish set ... 0x0) is exempt: it must always run so the index is
    never left pointing at an SMN address.
    """
    def guarded(argv, timeout):
        restore = list(argv[:3]) == ["vsish", "-e", "set"] and argv[-1] == "0x0"
        if not restore and now() - start >= budget_s:
            raise CommandError(argv, "budget")
        return run(argv, timeout)
    return guarded


def _eff_mhz(nominal, first, second):
    """Per-core effective MHz from two (aperf, mperf) snapshots -> (slots, errors)."""
    slots, errors = [], []
    for n, ((a1, m1), (a2, m2)) in enumerate(zip(first, second)):
        if a2 < a1 or m2 < m1:
            slots.append(None)
            errors.append(_err(f"eff_mhz[{n}]", "counter went backwards"))
            continue
        mhz = effective_mhz(nominal, a2 - a1, m2 - m1)
        if mhz is None:
            slots.append(None)
            errors.append(_err(f"eff_mhz[{n}]", "core halted"))
        else:
            slots.append(round(mhz, 1))
    return slots, errors


def _eff_summary(slots):
    vals = [v for v in slots if v is not None]
    if not vals:
        return None
    return {"min": min(vals), "avg": round(sum(vals) / len(vals), 1), "max": max(vals)}


def _root_complex_problem(run):
    """None if the root complex is AMD Raphael, else the reason to skip every SMN access."""
    try:
        out = run(["vsish", "-e", "get", f"{ROOT_CFG}/0x0"], _ROOT_ID_TIMEOUT_S)
        root_id = int(out.strip(), 16)
    except (CommandError, ValueError) as exc:
        return _reason(exc)
    return None if root_id == RAPHAEL_ROOT_ID else "root complex not supported"


def _smn_temps(run):
    raw = read_smn(run, [TCTL, *CCD])
    ccds, errors = [], []
    for n, addr in enumerate(CCD):
        temp = ccd_c(raw[addr])
        if temp is None:
            errors.append(_err(f"ccd_c[{n}]", "valid bit clear"))
            ccds.append(None)
        else:
            ccds.append(round(temp, 3))
    return round(tctl_c(raw[TCTL]), 3), ccds, errors


def _sample(run, pcpus, now):
    """One RAPL + perf snapshot. The timestamp is taken right after the energy read, so the
    power window is the time between the two RAPL reads, not skewed by the perf reads."""
    unit_raw, energy = read_rapl(run)
    energy_at = now()
    perf = [read_perf(run, p) for p in range(pcpus)]
    return unit_raw, energy, perf, energy_at


def collect(run: Runner, sleep=time.sleep, now=time.monotonic, wall=_utc_now,
            budget_s: float = 20.0) -> dict:
    start = now()
    run = _budgeted(run, now, start, budget_s)
    spent = lambda: now() - start >= budget_s  # noqa: E731
    errors: list[dict] = []
    cpu: dict = {}
    nvme: list[dict] = []

    def skipped(*names):
        errors.extend(_err(n, "budget") for n in names)

    ident = None
    try:
        ident = read_cpu_identity(run)
    except (CommandError, ValueError) as exc:
        errors.append(_err("cpu", _reason(exc)))
        cpu = {"supported": False}
    if ident is not None:
        cpu = {"family": ident.family, "model": ident.model,
               "supported": (ident.vendor, ident.family, ident.model) == SUPPORTED}
        if not cpu["supported"]:
            errors.append(_err("cpu", f"unsupported cpu {ident.vendor} family "
                                      f"{ident.family} model {ident.model}"))

    if cpu.get("supported"):
        _cpu_readings(run, ident, cpu, errors, sleep, now, spent, skipped)

    try:
        if spent():
            skipped("nvme")
        else:
            drives, nvme_errors = read_nvme(run)
            nvme = [asdict(d) for d in drives]
            errors.extend(nvme_errors)
    except (CommandError, ValueError) as exc:
        errors.append(_err("nvme", _reason(exc)))

    return {"schema": SCHEMA, "cpu": cpu, "nvme": nvme, "errors": errors,
            "sampled_at": wall(), "duration_ms": int((now() - start) * 1000)}


def _cpu_readings(run, ident, cpu, errors, sleep, now, spent, skipped):
    if spent():
        skipped("rapl", "eff_mhz")
    else:
        _power_and_clocks(run, ident, cpu, errors, sleep, now, spent, skipped)

    if spent():
        skipped("tctl_c", "ccd_c")
        return
    problem = _root_complex_problem(run)
    if problem is not None:
        errors.extend([_err("tctl_c", problem), _err("ccd_c", problem)])
        return
    try:
        cpu["tctl_c"], cpu["ccd_c"], ccd_errors = _smn_temps(run)
        errors.extend(ccd_errors)
    except SmnBusy:
        errors.extend([_err("tctl_c", "smn index busy"), _err("ccd_c", "smn index busy")])
    except (CommandError, ValueError) as exc:
        errors.extend([_err("tctl_c", _reason(exc)), _err("ccd_c", _reason(exc))])


def _power_and_clocks(run, ident, cpu, errors, sleep, now, spent, skipped):
    try:
        first = _sample(run, ident.pcpus, now)
        sleep(_SAMPLE_GAP_S)
        if spent():
            skipped("rapl", "eff_mhz")
            return
        second = _sample(run, ident.pcpus, now)
    except (CommandError, ValueError) as exc:
        errors.extend([_err("rapl", _reason(exc)), _err("eff_mhz", _reason(exc))])
        return

    elapsed = second[3] - first[3]
    if elapsed > 0:
        unit_j = rapl_energy_unit_j(first[0])
        cpu["package_w"] = round(energy_delta_j(first[1], second[1], unit_j) / elapsed, 2)
    else:
        errors.append(_err("rapl", "no elapsed time between samples"))
    slots, eff_errors = _eff_mhz(ident.nominal_mhz, first[2], second[2])
    errors.extend(eff_errors)
    cpu["eff_mhz"] = slots
    summary = _eff_summary(slots)
    if summary is not None:
        cpu["eff_mhz_summary"] = summary
