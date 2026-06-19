#!/usr/bin/env python3
"""cluster_monitor.py — read-only health/metrics dashboard for the local GPU
cluster. Runs as a systemd service on the Proxmox host (see
scripts/63-cluster-monitor.sh and docs/cluster-monitor-design.md).

Python 3 standard library ONLY. v1 observes and suggests; it never mutates.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

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
