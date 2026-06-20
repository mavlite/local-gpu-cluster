#!/usr/bin/env python3
"""cluster_monitor.py — read-only health/metrics dashboard for the local GPU
cluster. Runs as a systemd service on the Proxmox host (see
scripts/63-cluster-monitor.sh and docs/cluster-monitor-design.md).

Python 3 standard library ONLY. v1 observes and suggests; it never mutates.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
import sqlite3
import subprocess
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


# ─────────────────────── 6. Checks + REGISTRY ───────────────────────

REGISTRY: list[Check] = []
