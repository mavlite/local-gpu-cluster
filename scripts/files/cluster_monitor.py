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
