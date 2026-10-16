"""Pure parsers for the live dashboard. Formats recorded on the live cluster 2026-10-16 (llama.cpp b11578 with
--log-prefix; amdgpu sysfs; Linux /proc). No I/O here except the sysfs readers, which take a directory."""
import glob
import math
import os
import re
from datetime import datetime, timezone

_PREFIX = re.compile(r"^(?P<m>\d+)\.(?P<s>\d{2})\.(?P<ms>\d{3})\.(?P<us>\d{3}) [IWE] ")
_ID = r"id\s+(?P<slot>\d+) \| task (?P<task>-?\d+) \| "
_RX = (
    ("start", re.compile(r"slot launch_slot_: " + _ID + r"processing task")),
    ("prefill", re.compile(r"slot print_timing: " + _ID + r"prompt processing, n_tokens =\s+(?P<n>\d+), "
                           r"progress = (?P<progress>[\d.]+), t =\s+[\d.]+ s / (?P<tok_s>[\d.]+) tokens per second")),
    ("gen", re.compile(r"slot print_timing: " + _ID + r"n_gen =\s+(?P<n>\d+), tg =\s+(?P<tok_s>[\d.]+) t/s")),
    ("prompt_eval", re.compile(r"print_timing: " + _ID + r"prompt eval time =\s+(?P<ms>[\d.]+) ms /\s+(?P<n>\d+) tokens")),
    ("eval", re.compile(r"print_timing: " + _ID + r"\s+eval time =\s+(?P<ms>[\d.]+) ms /\s+(?P<n>\d+) tokens")),
    ("total", re.compile(r"print_timing: " + _ID + r"\s+total time =\s+(?P<ms>[\d.]+) ms /\s+(?P<n>\d+) tokens")),
    ("draft", re.compile(r"print_timing: " + _ID + r"draft acceptance = (?P<rate>[\d.]+) \(\s*(?P<accepted>\d+) "
                         r"accepted /\s*(?P<generated>\d+) generated\)")),
    ("release", re.compile(r"slot\s+release: " + _ID + r"stop processing: n_tokens = (?P<n>\d+)")),
)
_CANCEL = re.compile(r"srv\s+stop: cancel task, id_task = (?P<task>\d+)")
_SERVER_START = re.compile(r"llama_server: initializing \.\.\.")
_SERVER_READY = re.compile(r"llama_server: listening on ")
_INTS = {"slot", "task", "n", "accepted", "generated"}
_FLOATS = {"progress", "tok_s", "ms", "rate"}


def parse_journal_line(line: str):
    """One chat-journal line -> event dict, or None for anything the tracker does not use."""
    m = _PREFIX.match(line or "")
    if not m:
        return None
    since = int(m["m"]) * 60 + int(m["s"]) + int(m["ms"]) / 1e3 + int(m["us"]) / 1e6
    body = line[m.end():]
    if _SERVER_START.search(body):
        return {"kind": "server_start", "since_start_s": since}
    if _SERVER_READY.search(body):
        return {"kind": "server_ready", "since_start_s": since}
    c = _CANCEL.search(body)
    if c:
        return {"kind": "cancel", "since_start_s": since, "task": int(c["task"])}
    for kind, rx in _RX:
        r = rx.search(body)
        if r:
            ev = {"kind": kind, "since_start_s": since}
            for k, v in r.groupdict().items():
                ev[k] = int(v) if k in _INTS else float(v) if k in _FLOATS else v
            return ev
    return None


def parse_prometheus(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            v = float(value)
        except ValueError:
            continue
        if math.isfinite(v):                 # NaN/inf would make the snapshot invalid JSON for the browser
            out[name] = v
    return out


def parse_slots(data) -> list:
    out = []
    for s in data or []:
        nt = (s.get("next_token") or [{}])[0] if isinstance(s.get("next_token"), list) else {}
        out.append({"id": int(s["id"]), "n_ctx": s.get("n_ctx"), "is_processing": bool(s.get("is_processing")),
                    "id_task": s.get("id_task"), "n_prompt_tokens": s.get("n_prompt_tokens"),
                    "processed": s.get("n_prompt_tokens_processed"), "cache": s.get("n_prompt_tokens_cache"),
                    "n_decoded": int(nt.get("n_decoded") or 0)})
    return out


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def find_gpu_dirs(drm_root: str, buses: list) -> dict:
    """bus address -> card device dir, matched by PCI_SLOT_NAME in device/uevent (card numbers drift)."""
    found = {}
    for dev in sorted(glob.glob(os.path.join(drm_root, "card*", "device"))):
        try:
            for line in _read(os.path.join(dev, "uevent")).splitlines():
                if line.startswith("PCI_SLOT_NAME=") and line.split("=", 1)[1] in buses:
                    found[line.split("=", 1)[1]] = os.path.realpath(dev)
        except (OSError, ValueError):
            continue
    return found


def gpu_dir_matches(device_dir: str, bus: str) -> bool:
    """True while device_dir's uevent still names this bus (False after a driver reload renumbers the cards)."""
    try:
        return f"PCI_SLOT_NAME={bus}" in _read(os.path.join(device_dir, "uevent")).splitlines()
    except (OSError, ValueError):
        return False


def read_gpu(device_dir: str) -> dict:
    """One card's live stats; a missing/unreadable file nulls only its field and is named in `error`."""
    errors = []

    def num(rel, scale=1.0):
        try:
            return float(_read(os.path.join(device_dir, rel))) * scale
        except (OSError, ValueError):
            errors.append(rel)
            return None

    hw = sorted(glob.glob(os.path.join(device_dir, "hwmon", "hwmon*")))
    h = os.path.relpath(hw[0], device_dir) if hw else "hwmon/missing"
    used, total = num("mem_info_vram_used"), num("mem_info_vram_total")
    try:
        link = f"{_read(os.path.join(device_dir, 'current_link_speed')).replace(' PCIe', '')} x" \
               f"{_read(os.path.join(device_dir, 'current_link_width'))}"
    except OSError:
        errors.append("current_link_speed")
        link = None
    return {"load_pct": num("gpu_busy_percent"),
            "vram_used": int(used) if used is not None else None,
            "vram_total": int(total) if total is not None else None,
            "junction_c": num(os.path.join(h, "temp2_input"), 1e-3),
            "power_w": num(os.path.join(h, "power1_average"), 1e-6),
            "power_cap_w": num(os.path.join(h, "power1_cap"), 1e-6),
            "link": link, "error": ", ".join(errors) or None}


def read_fans(hwmon_root: str, chip: str = "nct6798") -> dict:
    for d in sorted(glob.glob(os.path.join(hwmon_root, "hwmon*"))):
        try:
            if _read(os.path.join(d, "name")) != chip:
                continue
            return {f"{p}_pct": round(float(_read(os.path.join(d, p))) * 100 / 255, 3) for p in ("pwm5", "pwm6")}
        except (OSError, ValueError):
            continue                                # one unreadable hwmon dir must not hide the fan chip
    return {"pwm5_pct": None, "pwm6_pct": None}


def cpu_times(stat_text: str):
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            total = sum(v[:8])
            return total - idle, total
    raise ValueError("no aggregate cpu line")


def mem_from_meminfo(text: str):
    kv = {}
    for line in text.splitlines():
        k, _, rest = line.partition(":")
        if rest.strip():
            kv[k] = int(rest.split()[0]) * 1024
    return kv["MemTotal"] - kv["MemAvailable"], kv["MemTotal"]


def disk_sectors(diskstats_text: str, devices: list):
    r = w = 0
    for line in diskstats_text.splitlines():
        f = line.split()
        if len(f) >= 10 and f[2] in devices:
            r += int(f[5])
            w += int(f[9])
    return r, w


def arc_from_arcstats(text: str):
    vals = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 3 and f[0] in ("size", "c_max") and f[2].isdigit():
            vals[f[0]] = int(f[2])
    return vals["size"], vals["c_max"]


def parse_next_elapse(text: str):
    v = text.strip().partition("=")[2].strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
