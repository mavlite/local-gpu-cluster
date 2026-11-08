"""Sensor readers. Parse only the labels seen in the recorded fixtures."""
from dataclasses import dataclass

from .runner import CommandError, Runner

_TIMEOUT = 10.0
_MSR = "/hardware/msr/pcpu/0/addr"


@dataclass(frozen=True)
class CpuIdentity:
    vendor: str
    family: int
    model: int
    nominal_mhz: float
    pcpus: int


@dataclass(frozen=True)
class NvmeHealth:
    adapter: str
    model: str
    temp_c: int
    spare_pct: int
    used_pct: int
    critical_warning: int
    media_errors: int
    wctemp_c: int | None
    cctemp_c: int | None


def _vsish_get(path: str) -> list[str]:
    return ["vsish", "-e", "get", path]


CPU_LIST_ARGV = ["esxcli", "hardware", "cpu", "list"]
NVME_LIST_ARGV = ["esxcli", "nvme", "device", "list"]


def _nvme_smart_argv(a: str) -> list[str]:
    return ["esxcli", "nvme", "device", "log", "smart", "get", "-A", a]


def _nvme_get_argv(a: str) -> list[str]:
    return ["esxcli", "nvme", "device", "get", "-A", a]


def _fields(text: str) -> dict[str, str]:
    """First occurrence of each 'Label: value' line."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip() and label.strip() not in out:
            out[label.strip()] = value.strip()
    return out


def read_cpu_identity(run: Runner) -> CpuIdentity:
    text = run(CPU_LIST_ARGV, _TIMEOUT)
    pcpus = sum(1 for ln in text.splitlines() if ln.startswith("CPU:"))
    f = _fields(text)
    try:
        return CpuIdentity(
            vendor=f["Brand"],
            family=int(f["Family"]),
            model=int(f["Model"]),
            nominal_mhz=int(f["Core Speed"]) / 1e6,
            pcpus=pcpus,
        )
    except (KeyError, ValueError) as exc:
        raise CommandError(CPU_LIST_ARGV, f"unparseable cpu list: {exc!r}") from exc


def _msr(run: Runner, msr: str) -> int:
    argv = _vsish_get(f"{_MSR}/{msr}")
    try:
        return int(run(argv, _TIMEOUT).strip(), 16)
    except ValueError as exc:
        raise CommandError(argv, "unparseable MSR value") from exc


def read_rapl(run: Runner) -> tuple[int, int]:
    return _msr(run, "0xC0010299"), _msr(run, "0xC001029B")


def read_perf(run: Runner, pcpu: int) -> tuple[int, int]:
    argv = _vsish_get(f"/power/pcpu/{pcpu}/perf")
    text = run(argv, _TIMEOUT)
    vals: dict[str, int] = {}
    for line in text.splitlines():
        for label in ("APERF reading", "MPERF reading"):
            if line.strip().startswith(label + ":"):
                vals[label] = int(line.split(":", 1)[1].strip())
    if len(vals) != 2:
        raise CommandError(argv, "APERF/MPERF reading not found")
    return vals["APERF reading"], vals["MPERF reading"]


class _Implausible(ValueError):
    """A reading that parsed but cannot be a real temperature (0 K or below)."""


def _kelvin_to_c(raw: str) -> int:
    k = int(raw.split()[0])
    if k <= 0:
        raise _Implausible("temperature not plausible")
    return k - 273


def _threshold_c(raw: str | None) -> int | None:
    if raw is None:
        return None
    k = int(raw.split()[0])
    return k - 273 if k > 0 else None


def _read_one_nvme(run: Runner, adapter: str) -> NvmeHealth:
    smart = _fields(run(_nvme_smart_argv(adapter), _TIMEOUT))
    ident = _fields(run(_nvme_get_argv(adapter), _TIMEOUT))
    argv = _nvme_smart_argv(adapter)
    try:
        crit = any(
            smart[k] == "true"
            for k in (
                "Available Spare Space Below Threshold",
                "Temperature Warning",
                "NVM Subsystem Reliability Degradation",
                "Read Only Mode",
                "Volatile Memory Backup Device Failure",
            )
        )
        return NvmeHealth(
            adapter=adapter,
            model=ident["Model Number"],
            temp_c=_kelvin_to_c(smart["Composite Temperature"]),
            spare_pct=int(smart["Available Spare"].split()[0]),
            used_pct=int(smart["Percentage Used"].split()[0]),
            critical_warning=int(crit),
            media_errors=int(smart["Media Errors"], 16),
            wctemp_c=_threshold_c(ident.get("Warning Composite Temperature Threshold")),
            cctemp_c=_threshold_c(ident.get("Critical Composite Temperature Threshold")),
        )
    except _Implausible as exc:
        raise CommandError(argv, str(exc)) from exc
    except (KeyError, ValueError, IndexError) as exc:
        raise CommandError(argv, f"unparseable nvme output: {exc!r}") from exc


def read_nvme(run: Runner) -> tuple[list[NvmeHealth], list[dict]]:
    text = run(NVME_LIST_ARGV, _TIMEOUT)  # failure propagates to the caller
    adapters = [
        parts[0]
        for parts in (ln.split() for ln in text.splitlines())
        if parts and parts[0].startswith("vmhba")
    ]
    drives: list[NvmeHealth] = []
    errors: list[dict] = []
    for adapter in adapters:
        try:
            drives.append(_read_one_nvme(run, adapter))
        except CommandError as exc:
            errors.append({"reading": f"nvme[{adapter}]", "reason": exc.message})
    return drives, errors
