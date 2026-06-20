#!/usr/bin/env python3
"""cluster_monitor.py — read-only health/metrics dashboard for the local GPU
cluster. Runs as a systemd service on the Proxmox host (see
scripts/63-cluster-monitor.sh and docs/cluster-monitor-design.md).

Python 3 standard library ONLY. v1 observes and suggests; it never mutates.
"""
from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

# ─────────────────────────── 1. Core types ───────────────────────────

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_INFO = "info"


@dataclass(frozen=True)
class CheckResult:
    id: str
    group: str
    status: str
    detail: str
    value: float | None = None
    unit: str | None = None
    suggested_action: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class CmdResult:
    rc: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class HttpResult:
    status: int          # HTTP status code; 0 == transport failure
    body: str
    error: str | None = None


# ─────────────────────────── 2. Probes (IO seam) ───────────────────────────


class Probes:
    """All cluster IO funnels through here so checks are testable. Never
    raises for expected failures — returns a non-zero CmdResult / status==0
    HttpResult instead, so one broken endpoint can't crash a collection cycle.
    """

    def cmd(self, args: list[str], timeout: float = 10.0) -> CmdResult:
        try:
            p = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout, check=False,
            )
            return CmdResult(p.returncode, p.stdout, p.stderr)
        except FileNotFoundError as e:
            return CmdResult(127, "", str(e))
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", f"timeout after {timeout}s: {' '.join(args)}")
        except OSError as e:  # noqa: BLE001 - any spawn failure becomes a result
            return CmdResult(125, "", str(e))

    def http(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json_body: dict | None = None,
        timeout: float = 5.0,
    ) -> HttpResult:
        data = None
        hdrs = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return HttpResult(resp.status, body)
        except urllib.error.HTTPError as e:
            # A 4xx/5xx is a real HTTP status, not a transport failure.
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            return HttpResult(e.code, body)
        except (urllib.error.URLError, OSError) as e:
            return HttpResult(0, "", str(getattr(e, "reason", e)))


# ─────────────────────────── 3. Store (SQLite) ───────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS check_state (
    id TEXT PRIMARY KEY,
    grp TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    value REAL,
    unit TEXT,
    suggested_action TEXT,
    updated_at REAL NOT NULL,
    last_ok_at REAL
);
CREATE TABLE IF NOT EXISTS samples (
    id TEXT NOT NULL,
    ts REAL NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_id_ts ON samples(id, ts);
"""


class Store:
    """Persists latest CheckResult per id, last-ok timestamps, and a rolling
    window of numeric samples for sparklines. Survives the monitor's restart.
    """

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(self, result: CheckResult, now: float) -> str:
        cur = self._db.execute(
            "SELECT status, last_ok_at FROM check_state WHERE id=?", (result.id,)
        )
        row = cur.fetchone()
        prev_status = row[0] if row else ""
        prev_last_ok = row[1] if row else None
        last_ok = now if result.status == STATUS_OK else prev_last_ok
        self._db.execute(
            "INSERT INTO check_state"
            " (id, grp, status, detail, value, unit, suggested_action,"
            "  updated_at, last_ok_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  grp=excluded.grp, status=excluded.status, detail=excluded.detail,"
            "  value=excluded.value, unit=excluded.unit,"
            "  suggested_action=excluded.suggested_action,"
            "  updated_at=excluded.updated_at, last_ok_at=excluded.last_ok_at",
            (result.id, result.group, result.status, result.detail, result.value,
             result.unit, result.suggested_action, now, last_ok),
        )
        if result.value is not None:
            self._db.execute(
                "INSERT INTO samples (id, ts, value) VALUES (?,?,?)",
                (result.id, now, float(result.value)),
            )
        self._db.commit()
        return prev_status

    def prune(self, now: float, retention_s: float) -> int:
        cur = self._db.execute(
            "DELETE FROM samples WHERE ts < ?", (now - retention_s,)
        )
        self._db.commit()
        return cur.rowcount

    def snapshot(self, sample_window_s: float, now: float) -> list[dict]:
        out: list[dict] = []
        rows = self._db.execute(
            "SELECT id, grp, status, detail, value, unit, suggested_action,"
            " updated_at, last_ok_at FROM check_state ORDER BY grp, id"
        ).fetchall()
        for r in rows:
            cid = r[0]
            samples = self._db.execute(
                "SELECT ts, value FROM samples WHERE id=? AND ts>=? ORDER BY ts",
                (cid, now - sample_window_s),
            ).fetchall()
            out.append({
                "id": cid, "group": r[1], "status": r[2], "detail": r[3],
                "value": r[4], "unit": r[5], "suggested_action": r[6],
                "updated_at": r[7], "last_ok_at": r[8],
                "samples": [[s[0], s[1]] for s in samples],
            })
        return out

    def close(self) -> None:
        self._db.close()


# ─────────────────────────── 4. Alert engine ───────────────────────────

_PROBLEM = frozenset({STATUS_WARN, STATUS_FAIL})


@dataclass(frozen=True)
class AlertEvent:
    check_id: str
    kind: str          # "fired" | "resolved"
    status: str
    detail: str
    ts: float


class Notifier:
    def send(self, event: AlertEvent) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class NoopNotifier(Notifier):
    def send(self, event: AlertEvent) -> None:
        return None


class LogNotifier(Notifier):
    """Delivers to a stdlib logger -> journald under systemd."""

    def __init__(self, logger):
        self._log = logger

    def send(self, event: AlertEvent) -> None:
        if event.kind == "resolved":
            self._log.info("RESOLVED %s: %s", event.check_id, event.detail)
        else:
            self._log.warning(
                "ALERT %s [%s]: %s", event.check_id, event.status, event.detail
            )


class AlertEngine:
    """Diffs new vs previous status and fires on transitions, with per-check
    cooldown + dedup. Pure-ish: caller supplies prev_status and now.
    """

    def __init__(self, notifier: Notifier, cooldown_s: float = 900.0):
        self._notifier = notifier
        self._cooldown_s = cooldown_s
        self._last_fire: dict[tuple[str, str], float] = {}  # (id, status) -> ts

    def evaluate(self, result: CheckResult, prev_status: str, now: float):
        is_problem = result.status in _PROBLEM
        was_problem = prev_status in _PROBLEM
        event = None
        if is_problem and not was_problem:
            if self._suppressed(result.id, result.status, now):
                return None
            self._last_fire[(result.id, result.status)] = now
            event = AlertEvent(result.id, "fired", result.status, result.detail, now)
        elif was_problem and result.status == STATUS_OK:
            event = AlertEvent(result.id, "resolved", result.status, result.detail, now)
        if event is not None:
            self._notifier.send(event)
        return event

    def _suppressed(self, check_id: str, status: str, now: float) -> bool:
        last_ts = self._last_fire.get((check_id, status))
        if last_ts is None:
            return False
        return (now - last_ts) < self._cooldown_s


# ─────────────────────── 5. Check helpers + parsers ───────────────────────

_PROM_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<val>[-+0-9.eE]+)\s*$')


def parse_prom(text: str) -> dict[str, list[tuple[dict, float]]]:
    out: dict[str, list[tuple[dict, float]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _PROM_LINE.match(line)
        if not m:
            continue
        labels: dict[str, str] = {}
        if m.group("labels"):
            for pair in m.group("labels").split(","):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    labels[k.strip()] = v.strip().strip('"')
        try:
            val = float(m.group("val"))
        except ValueError:
            continue
        out.setdefault(m.group("name"), []).append((labels, val))
    return out


@dataclass(frozen=True)
class Check:
    id: str
    group: str
    fn: "Callable[[Probes, dict], list[CheckResult]]"


def status_for(value: float, warn: float, fail: float, *,
               higher_is_worse: bool = True) -> str:
    if higher_is_worse:
        if value >= fail:
            return STATUS_FAIL
        if value >= warn:
            return STATUS_WARN
        return STATUS_OK
    if value <= fail:
        return STATUS_FAIL
    if value <= warn:
        return STATUS_WARN
    return STATUS_OK


def http_alive(probes, url: str, *, ok_statuses=None,
               alive_any_http: bool = False) -> tuple[str, str]:
    """Returns (status, detail). alive_any_http=True => any HTTP reply is ok."""
    res = probes.http("GET", url)
    if res.status == 0:
        return STATUS_FAIL, f"unreachable: {res.error}"
    if alive_any_http:
        return STATUS_OK, f"HTTP {res.status}"
    ok_statuses = ok_statuses or range(200, 400)
    if res.status in ok_statuses:
        return STATUS_OK, f"HTTP {res.status}"
    return STATUS_FAIL, f"HTTP {res.status}"


def _rocm_find(d: dict, needle: str):
    for k, v in d.items():
        if needle.lower() in k.lower():
            return v
    return None


def parse_rocm_vram_json(text: str) -> list[tuple[int, float, float]]:
    data = json.loads(text)
    rows: list[tuple[int, float, float]] = []
    for key, card in sorted(data.items()):
        if not key.startswith("card") or not isinstance(card, dict):
            continue
        total = _rocm_find(card, "VRAM Total Memory (B)")
        used = _rocm_find(card, "VRAM Total Used Memory")
        if total is None or used is None:
            continue
        idx = int(key.replace("card", "") or 0)
        rows.append((idx, float(used) / 1048576.0, float(total) / 1048576.0))
    return rows


def parse_rocm_temp_json(text: str) -> list[tuple[int, float]]:
    data = json.loads(text)
    rows: list[tuple[int, float]] = []
    for key, card in sorted(data.items()):
        if not key.startswith("card") or not isinstance(card, dict):
            continue
        t = _rocm_find(card, "junction")
        if t is None:
            t = _rocm_find(card, "Temperature")
        if t is None:
            continue
        idx = int(key.replace("card", "") or 0)
        rows.append((idx, float(t)))
    return rows


def parse_meminfo(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                out[name.strip()] = float(parts[0])
            except ValueError:
                pass
    return out


# ─────────────────────── 6. Checks + REGISTRY ───────────────────────

REGISTRY: list[Check] = []


def check_router_healthz(probes, cfg) -> list[CheckResult]:
    res = probes.http("GET", f"{cfg['router_url']}/healthz")
    if res.status == 0:
        return [CheckResult(
            "router_up", "health", STATUS_FAIL,
            f"router unreachable: {res.error}",
            suggested_action="restart_unit(153, llm-router)")]
    try:
        data = json.loads(res.body)
    except ValueError:
        return [CheckResult("router_up", "health", STATUS_FAIL,
                            f"router /healthz non-JSON (HTTP {res.status})")]
    out: list[CheckResult] = []
    upstreams = data.get("upstream", {})
    for name in ("chat", "embed", "rerank"):
        up = bool(upstreams.get(name))
        out.append(CheckResult(
            f"router_{name}_upstream", "health",
            STATUS_OK if up else STATUS_FAIL,
            f"{name} upstream {'reachable' if up else 'DOWN'}",
            suggested_action=None if up else f"restart_unit(151, llamacpp-{name})"))
    profile = str(data.get("active_chat_profile", "unknown"))
    out.append(CheckResult("loaded_chat_profile", "freshness", STATUS_INFO, profile))
    ssc = data.get("seconds_since_chat")
    if ssc is not None:
        out.append(CheckResult(
            "last_chat_completion", "freshness",
            status_for(float(ssc), warn=3600, fail=86400),
            f"{int(ssc)}s since last chat completion",
            value=float(ssc), unit="s"))
    return out


def check_anythingllm(probes, cfg) -> list[CheckResult]:
    status, detail = http_alive(probes, f"{cfg['anythingllm_url']}/")
    return [CheckResult("anythingllm", "health", status, detail,
                        suggested_action=None if status == STATUS_OK
                        else "restart_unit(154, anythingllm)")]


def check_mcp_sdg(probes, cfg) -> list[CheckResult]:
    status, detail = http_alive(probes, f"{cfg['mcp_sdg_url']}/", alive_any_http=True)
    return [CheckResult("mcp_sdg", "health", status, detail)]


def check_memvault_rest(probes, cfg) -> list[CheckResult]:
    status, detail = http_alive(probes, f"{cfg['memvault_rest_url']}/api/health")
    return [CheckResult("memvault_rest", "health", status, detail,
                        suggested_action=None if status == STATUS_OK
                        else "compose_up(156)")]


def check_memvault_bridge(probes, cfg) -> list[CheckResult]:
    status, detail = http_alive(probes, cfg["memvault_bridge_url"], alive_any_http=True)
    return [CheckResult("memvault_bridge", "health", status, detail,
                        suggested_action=None if status == STATUS_OK
                        else "restart_unit(156, memory-vault-bridge)")]


def check_gpu_vram(probes, cfg) -> list[CheckResult]:
    res = probes.cmd(["rocm-smi", "--showmeminfo", "vram", "--json"])
    if res.rc != 0:
        return [CheckResult("gpu_vram", "metrics", STATUS_FAIL,
                            f"rocm-smi failed: {res.stderr.strip() or res.rc}")]
    out: list[CheckResult] = []
    for idx, used, total in parse_rocm_vram_json(res.stdout):
        pct = (used / total * 100.0) if total else 0.0
        out.append(CheckResult(
            f"gpu_vram_{idx}", "metrics",
            status_for(pct, cfg["gpu_vram_warn_pct"], cfg["gpu_vram_fail_pct"]),
            f"card {idx}: {used:.0f}/{total:.0f} MiB ({pct:.0f}%)",
            value=round(pct, 1), unit="%"))
    return out or [CheckResult("gpu_vram", "metrics", STATUS_FAIL,
                               "no cards parsed from rocm-smi")]


def check_gpu_temp(probes, cfg) -> list[CheckResult]:
    res = probes.cmd(["rocm-smi", "--showtemp", "--json"])
    if res.rc != 0:
        return [CheckResult("gpu_temp", "metrics", STATUS_FAIL,
                            f"rocm-smi failed: {res.stderr.strip() or res.rc}")]
    out: list[CheckResult] = []
    for idx, temp in parse_rocm_temp_json(res.stdout):
        out.append(CheckResult(
            f"gpu_temp_{idx}", "metrics",
            status_for(temp, cfg["gpu_temp_warn_c"], cfg["gpu_temp_fail_c"]),
            f"card {idx} junction {temp:.0f}C", value=temp, unit="C"))
    return out or [CheckResult("gpu_temp", "metrics", STATUS_FAIL,
                               "no temps parsed from rocm-smi")]


def check_host_mem(probes, cfg) -> list[CheckResult]:
    res = probes.cmd(["cat", "/proc/meminfo"])
    if res.rc != 0:
        return [CheckResult("host_mem", "metrics", STATUS_FAIL, "no /proc/meminfo")]
    m = parse_meminfo(res.stdout)
    total, avail = m.get("MemTotal", 0.0), m.get("MemAvailable", 0.0)
    pct_avail = (avail / total * 100.0) if total else 0.0
    return [CheckResult(
        "host_mem", "metrics",
        status_for(pct_avail, cfg["host_mem_warn_pct"], cfg["host_mem_fail_pct"],
                   higher_is_worse=False),
        f"{avail/1048576:.1f} GiB avail of {total/1048576:.1f} GiB ({pct_avail:.0f}%)",
        value=round(pct_avail, 1), unit="%")]


def check_host_cpu(probes, cfg) -> list[CheckResult]:
    load = probes.cmd(["cat", "/proc/loadavg"])
    nproc = probes.cmd(["nproc"])
    if load.rc != 0:
        return [CheckResult("host_cpu", "metrics", STATUS_FAIL, "no /proc/loadavg")]
    one = float(load.stdout.split()[0])
    cores = int(nproc.stdout.strip() or "1") if nproc.rc == 0 else 1
    ratio = one / cores if cores else one
    return [CheckResult(
        "host_cpu", "metrics",
        status_for(ratio, warn=1.0, fail=2.0),
        f"load1 {one:.2f} over {cores} cores ({ratio:.2f}/core)",
        value=one, unit="load1")]


def check_zfs_arc(probes, cfg) -> list[CheckResult]:
    res = probes.cmd(["cat", "/proc/spl/kstat/zfs/arcstats"])
    if res.rc != 0:
        return [CheckResult("zfs_arc", "metrics", STATUS_INFO, "no ARC stats (no ZFS?)")]
    size = c_max = 0.0
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "size":
            size = float(parts[2])
        elif len(parts) == 3 and parts[0] == "c_max":
            c_max = float(parts[2])
    pct = (size / c_max * 100.0) if c_max else 0.0
    return [CheckResult("zfs_arc", "metrics", STATUS_INFO,
                        f"ARC {size/1073741824:.1f} GiB of {c_max/1073741824:.1f} GiB cap",
                        value=round(pct, 1), unit="%")]


def check_lxc_mem(probes, cfg) -> list[CheckResult]:
    out: list[CheckResult] = []
    for vmid in cfg.get("lxc_ids", []):
        conf = probes.cmd(["pct", "config", str(vmid)])
        if conf.rc != 0:
            out.append(CheckResult(f"lxc_mem_{vmid}", "metrics", STATUS_FAIL,
                                   f"pct config {vmid} failed"))
            continue
        ceiling_mib = 0.0
        for line in conf.stdout.splitlines():
            if line.startswith("memory:"):
                ceiling_mib = float(line.split(":", 1)[1].strip() or 0)
        mi = probes.cmd(["pct", "exec", str(vmid), "--", "cat", "/proc/meminfo"])
        if mi.rc != 0:
            out.append(CheckResult(f"lxc_mem_{vmid}", "metrics", STATUS_WARN,
                                   f"ceiling {ceiling_mib:.0f} MiB; usage unavailable"))
            continue
        m = parse_meminfo(mi.stdout)
        total_kb, avail_kb = m.get("MemTotal", 0.0), m.get("MemAvailable", 0.0)
        used_mib = (total_kb - avail_kb) / 1024.0
        pct = (used_mib / ceiling_mib * 100.0) if ceiling_mib else 0.0
        out.append(CheckResult(
            f"lxc_mem_{vmid}", "metrics",
            status_for(pct, warn=85, fail=95),
            f"LXC {vmid}: {used_mib:.0f} MiB used of {ceiling_mib:.0f} MiB ceiling ({pct:.0f}%)",
            value=round(pct, 1), unit="%"))
    return out


def check_rag_refresh(probes, cfg, now: float | None = None) -> list[CheckResult]:
    import time as _t
    now = _t.time() if now is None else now
    res = probes.cmd(["cat", cfg["rag_metrics_path"]])
    if res.rc != 0:
        # No metrics file => the refresh has never run / timer not installed.
        return [CheckResult(
            "rag_refresh", "freshness", STATUS_WARN,
            "no RAG refresh metrics found (timer not installed or never run)",
            suggested_action="run scripts/58-rag-refresh-timer.sh on host")]
    m = parse_prom(res.stdout)
    last = m.get("rag_refresh_last_run_timestamp", [({}, 0.0)])[0][1]
    age = now - last
    errors = sum(v for lbl, v in m.get("rag_refresh_run_total", [])
                 if lbl.get("status") in ("error", "halted"))
    status = STATUS_OK
    detail = f"last run {age/3600:.1f}h ago"
    if errors > 0:
        status, detail = STATUS_WARN, f"{int(errors)} source(s) errored; last run {age/3600:.1f}h ago"
    if age > cfg["rag_stale_after_s"]:
        status = STATUS_WARN
        detail = f"stale: last successful run {age/3600:.1f}h ago"
    out = [CheckResult("rag_refresh", "freshness", status, detail,
                       value=round(age / 3600.0, 2), unit="h")]
    return out


def check_backup_timer(probes, cfg) -> list[CheckResult]:
    res = probes.cmd(["systemctl", "is-active", cfg["backup_timer_name"]])
    active = res.stdout.strip() == "active"
    return [CheckResult(
        "backup_timer", "freshness",
        STATUS_OK if active else STATUS_WARN,
        f"{cfg['backup_timer_name']} {res.stdout.strip() or 'inactive'}",
        suggested_action=None if active else "enable_timer(memory-vault-backup.timer)")]


def check_restart_policies(probes, cfg) -> list[CheckResult]:
    vmid = cfg["memvault_vmid"]
    res = probes.cmd([
        "pct", "exec", str(vmid), "--", "bash", "-lc",
        "docker inspect --format '{{.Name}} {{.HostConfig.RestartPolicy.Name}}'"
        " $(docker ps -aq)"])
    if res.rc != 0:
        return [CheckResult("restart_policies", "freshness", STATUS_WARN,
                            f"could not inspect containers on LXC {vmid}")]
    bad = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "unless-stopped":
            bad.append(parts[0].lstrip("/"))
    if bad:
        return [CheckResult(
            "restart_policies", "freshness", STATUS_FAIL,
            f"not unless-stopped: {', '.join(bad)}",
            suggested_action=f"docker update --restart unless-stopped {' '.join(bad)}")]
    return [CheckResult("restart_policies", "freshness", STATUS_OK,
                        "all Memory Vault containers restart unless-stopped")]


def check_lxc_ram_ceilings(probes, cfg) -> list[CheckResult]:
    out: list[CheckResult] = []
    for vmid, expected in sorted(cfg.get("lxc_ram_ceilings", {}).items()):
        conf = probes.cmd(["pct", "config", str(vmid)])
        if conf.rc != 0:
            out.append(CheckResult(f"lxc_ram_ceiling_{vmid}", "freshness", STATUS_WARN,
                                   f"pct config {vmid} failed"))
            continue
        actual = None
        for line in conf.stdout.splitlines():
            if line.startswith("memory:"):
                actual = int(line.split(":", 1)[1].strip() or 0)
        if actual == int(expected):
            out.append(CheckResult(f"lxc_ram_ceiling_{vmid}", "freshness", STATUS_OK,
                                   f"LXC {vmid} ceiling {actual} MiB (expected)"))
        else:
            out.append(CheckResult(
                f"lxc_ram_ceiling_{vmid}", "freshness", STATUS_FAIL,
                f"LXC {vmid} ceiling {actual} MiB, expected {expected} MiB",
                suggested_action=f"pct_set_mem({vmid}, {expected})"))
    return out


def check_tavily_proxy(probes, cfg) -> list[CheckResult]:
    # Read ROUTER_API_KEY at runtime from the router LXC; never persist it.
    keyres = probes.cmd([
        "pct", "exec", str(cfg["router_vmid"]), "--",
        "bash", "-lc",
        f"set -a; . {cfg['router_env_path']} 2>/dev/null; printf %s \"$ROUTER_API_KEY\""])
    key = keyres.stdout.strip()
    if not key:
        return [CheckResult("tavily_proxy", "freshness", STATUS_WARN,
                            "ROUTER_API_KEY unreadable; skipping Tavily probe")]
    res = probes.http(
        "POST", f"{cfg['router_url']}/v1/tavily/search",
        headers={"Authorization": f"Bearer {key}"},
        json_body={"query": cfg["tavily_query"], "max_results": 1})
    if res.status == 200:
        return [CheckResult("tavily_proxy", "freshness", STATUS_OK, "Tavily proxy 200")]
    if res.status == 503:
        return [CheckResult("tavily_proxy", "freshness", STATUS_FAIL,
                            "Tavily 503 (key invalid or upstream down)",
                            suggested_action="check TAVILY_API_KEY in /etc/router.env")]
    return [CheckResult("tavily_proxy", "freshness", STATUS_WARN,
                        f"Tavily proxy HTTP {res.status or 'unreachable'}")]


REGISTRY.extend([
    Check("router_healthz", "health", check_router_healthz),
    Check("anythingllm", "health", check_anythingllm),
    Check("mcp_sdg", "health", check_mcp_sdg),
    Check("memvault_rest", "health", check_memvault_rest),
    Check("memvault_bridge", "health", check_memvault_bridge),
    Check("gpu_vram", "metrics", check_gpu_vram),
    Check("gpu_temp", "metrics", check_gpu_temp),
    Check("host_mem", "metrics", check_host_mem),
    Check("host_cpu", "metrics", check_host_cpu),
    Check("zfs_arc", "metrics", check_zfs_arc),
    Check("lxc_mem", "metrics", check_lxc_mem),
    Check("rag_refresh", "freshness", check_rag_refresh),
    Check("backup_timer", "freshness", check_backup_timer),
    Check("restart_policies", "freshness", check_restart_policies),
    Check("lxc_ram_ceilings", "freshness", check_lxc_ram_ceilings),
    Check("tavily_proxy", "freshness", check_tavily_proxy),
])

# ─────────────────────────── 7. Collector ───────────────────────────

_LOG = logging.getLogger("cluster_monitor")


class Collector:
    def __init__(self, checks, store: Store, probes, alert_engine: AlertEngine,
                 cfg: dict, now_fn=time.time):
        self._checks = checks
        self._store = store
        self._probes = probes
        self._alerts = alert_engine
        self._cfg = cfg
        self._now_fn = now_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run: dict[str, float] = {}

    def run_checks(self, groups, now: float) -> list[CheckResult]:
        results: list[CheckResult] = []
        for check in self._checks:
            if groups is not None and check.group not in groups:
                continue
            results.extend(self._run_one(check, now))
        return results

    def _run_one(self, check: Check, now: float) -> list[CheckResult]:
        try:
            produced = check.fn(self._probes, self._cfg)
        except Exception as e:  # noqa: BLE001 - one bad check must not kill the cycle
            _LOG.exception("check %s raised", check.id)
            produced = [CheckResult(check.id, check.group, STATUS_FAIL,
                                    f"check raised: {e}")]
        for r in produced:
            prev = self._store.record(r, now)
            self._alerts.evaluate(r, prev, now)
        return produced

    def run_once(self) -> list[CheckResult]:
        return self.run_checks(groups=None, now=self._now_fn())

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="collector",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        intervals = self._cfg["intervals"]
        retention = self._cfg["sample_retention_s"]
        while not self._stop.is_set():
            now = self._now_fn()
            due = {g for g, iv in intervals.items()
                   if now - self._last_run.get(g, 0.0) >= iv}
            if due:
                self.run_checks(groups=due, now=now)
                for g in due:
                    self._last_run[g] = now
                self._store.prune(now, retention)
            # Wake at the finest cadence; cheap no-op cycles otherwise.
            self._stop.wait(min(intervals.values()))


# ─────────────────────────── 8. HTTP server ───────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Cluster Monitor</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background:#0f1115; color:#e6e6e6; }
  header { padding: 12px 20px; background:#171a21; border-bottom:1px solid #262b36;
           display:flex; justify-content:space-between; align-items:center; }
  h1 { font-size: 16px; margin:0; font-weight:600; }
  #updated { font-size:12px; color:#8a93a6; }
  .group { padding: 8px 20px 0; }
  .group h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
              color:#8a93a6; margin:14px 0 6px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:10px; }
  .tile { border:1px solid #262b36; border-radius:8px; padding:10px 12px; background:#141821; }
  .tile .id { font-size:13px; font-weight:600; }
  .tile .detail { font-size:12px; color:#aab2c5; margin-top:4px; }
  .tile .meta { font-size:11px; color:#6f7891; margin-top:6px; }
  .tile .action { font-size:11px; color:#d8a657; margin-top:4px; }
  .tile.ok { border-left:4px solid #4caf50; }
  .tile.warn { border-left:4px solid #e0a800; }
  .tile.fail { border-left:4px solid #e05260; }
  .tile.info { border-left:4px solid #4f86c6; }
  .val { float:right; font-variant-numeric:tabular-nums; color:#cfd6e4; }
  svg { display:block; margin-top:8px; }
  .down { color:#e05260; padding:20px; }
</style>
</head>
<body>
<header><h1>Cluster Monitor</h1><span id="updated">connecting...</span></header>
<div id="root"></div>
<script>
const GROUPS = ["health","metrics","freshness"];
function rel(ts){ if(!ts) return "never"; const s=Math.floor(Date.now()/1000-ts);
  if(s<60) return s+"s ago"; if(s<3600) return Math.floor(s/60)+"m ago";
  if(s<86400) return Math.floor(s/3600)+"h ago"; return Math.floor(s/86400)+"d ago"; }
function spark(samples){
  if(!samples || samples.length<2) return "";
  const vs=samples.map(s=>s[1]); const min=Math.min(...vs), max=Math.max(...vs);
  const w=240,h=28,span=(max-min)||1;
  const pts=samples.map((s,i)=>{const x=i/(samples.length-1)*w;
    const y=h-((s[1]-min)/span)*h; return x.toFixed(1)+","+y.toFixed(1);}).join(" ");
  return '<svg width="'+w+'" height="'+h+'"><polyline fill="none" stroke="#4f86c6" '+
         'stroke-width="1.5" points="'+pts+'"/></svg>';
}
function tile(c){
  const v = (c.value!==null && c.value!==undefined) ?
    '<span class="val">'+c.value+(c.unit||"")+'</span>' : "";
  const act = c.suggested_action ? '<div class="action">fix: '+c.suggested_action+'</div>' : "";
  return '<div class="tile '+c.status+'"><div class="id">'+c.id+v+'</div>'+
    '<div class="detail">'+(c.detail||"")+'</div>'+
    '<div class="meta">last ok: '+rel(c.last_ok_at)+'</div>'+act+spark(c.samples)+'</div>';
}
async function refresh(){
  try{
    const r = await fetch("/api/status", {cache:"no-store"});
    if(!r.ok){ document.getElementById("root").innerHTML =
      '<div class="down">API '+r.status+'</div>'; return; }
    const data = await r.json();
    const byGroup = {}; GROUPS.forEach(g=>byGroup[g]=[]);
    data.checks.forEach(c=>{ (byGroup[c.group]=byGroup[c.group]||[]).push(c); });
    let html="";
    Object.keys(byGroup).forEach(g=>{ if(!byGroup[g].length) return;
      html += '<div class="group"><h2>'+g+'</h2><div class="grid">'+
        byGroup[g].map(tile).join("")+'</div></div>'; });
    document.getElementById("root").innerHTML = html;
    document.getElementById("updated").textContent = "updated "+new Date().toLocaleTimeString();
  }catch(e){ document.getElementById("updated").textContent = "offline"; }
}
refresh(); setInterval(refresh, 15000);
</script>
</body>
</html>
"""


def route(path: str, auth_header, store: Store, cfg: dict):
    """Pure request router -> (http_status, content_type, body)."""
    if path == "/healthz":
        return 200, "application/json", json.dumps({"status": "ok"})
    if path == "/" or path == "/index.html":
        return 200, "text/html", DASHBOARD_HTML
    if path == "/api/status":
        token = cfg.get("bearer_token", "")
        if token:
            expected = f"Bearer {token}"
            if auth_header != expected:
                return 401, "application/json", json.dumps({"error": "unauthorized"})
        snap = store.snapshot(cfg["sample_window_s"], now=time.time())
        payload = {"title": cfg.get("dashboard_title", "Cluster Monitor"),
                   "generated_at": time.time(), "checks": snap}
        return 200, "application/json", json.dumps(payload)
    return 404, "application/json", json.dumps({"error": "not found"})


def make_handler(store: Store, cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            code, ctype, body = route(
                self.path.split("?", 1)[0], self.headers.get("Authorization"),
                store, cfg)
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # quiet; journald handles logging
            _LOG.debug("http %s", fmt % args)

    return Handler


def build_server(store: Store, cfg: dict) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        (cfg["bind_host"], int(cfg["bind_port"])), make_handler(store, cfg))


# ─────────────────────────── 9. Config + main ───────────────────────────

DEFAULT_CONFIG: dict = {
    # endpoints
    "router_url": "http://192.168.6.153:8000",
    "anythingllm_url": "http://192.168.6.154:3001",
    "mcp_sdg_url": "http://192.168.6.155:3004",
    "memvault_rest_url": "http://192.168.6.223:8000",
    "memvault_bridge_url": "http://192.168.6.223:3005/mcp",
    "memvault_vmid": 156,
    "router_vmid": 153,
    "router_env_path": "/etc/router.env",
    "tavily_query": "cluster monitor reachability probe",
    # server
    "bind_host": "127.0.0.1",
    "bind_port": 8888,
    "bearer_token": "",
    "dashboard_title": "Cluster Monitor",
    "sample_window_s": 3600,
    "sample_retention_s": 86400,
    "db_path": "/var/lib/cluster-monitor/state.db",
    # collector
    "intervals": {"health": 20, "metrics": 60, "freshness": 300},
    "alert_cooldown_s": 900,
    # checks
    "lxc_ids": [151, 153, 154, 155, 156],
    "lxc_ram_ceilings": {"151": 32768},
    "gpu_vram_warn_pct": 90, "gpu_vram_fail_pct": 98,
    "gpu_temp_warn_c": 95, "gpu_temp_fail_c": 105,
    "host_mem_warn_pct": 10, "host_mem_fail_pct": 3,
    "rag_metrics_path": "/var/lib/rag-refresh/metrics.prom",
    "rag_stale_after_s": 93600,
    "rag_timer_name": "rag-refresh.timer",
    "backup_timer_name": "memory-vault-backup.timer",
}


def load_config(path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except (ValueError, OSError, TypeError) as e:
            _LOG.warning("config %s unreadable (%s); using defaults", path, e)
    return cfg


def format_once_table(results: list[CheckResult]) -> str:
    width = max((len(r.id) for r in results), default=4)
    lines = []
    for r in sorted(results, key=lambda x: (x.group, x.id)):
        lines.append(f"{r.status.upper():5} {r.id:<{width}}  {r.detail}")
    return "\n".join(lines)


def _build(cfg: dict):
    store = Store(cfg["db_path"])
    notifier = LogNotifier(_LOG)
    engine = AlertEngine(notifier, cooldown_s=cfg["alert_cooldown_s"])
    collector = Collector(REGISTRY, store, Probes(), engine, cfg)
    return store, collector


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="Local GPU cluster monitor")
    ap.add_argument("--config", default="/etc/cluster-monitor.json")
    ap.add_argument("--once", action="store_true",
                    help="run all checks once, print a table, exit")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.host:
        cfg["bind_host"] = args.host
    if args.port:
        cfg["bind_port"] = args.port

    if args.once:
        # --once uses an in-memory store so it never touches the service DB.
        cfg = dict(cfg, db_path=":memory:")
        store, collector = _build(cfg)
        results = collector.run_once()
        print(format_once_table(results))
        store.close()
        return 1 if any(r.status == STATUS_FAIL for r in results) else 0

    store, collector = _build(cfg)
    collector.start()
    server = None
    try:
        server = build_server(store, cfg)
        _LOG.info("serving on http://%s:%s", cfg["bind_host"], cfg["bind_port"])
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        if server is not None:
            server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
