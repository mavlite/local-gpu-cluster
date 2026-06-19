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
