# Cluster Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an always-on, read-only health/metrics dashboard for the local GPU cluster that runs as a systemd service on the Proxmox host, surfaces silent failures at a glance, tracks freshness/config-drift, and fires alerts on regressions.

**Architecture:** A single host Python process (`cluster_monitor.py`) with four cooperating parts — a Collector thread that runs a registry of pure-ish check functions on per-group intervals, a SQLite Store for latest state + last-ok timestamps + sparkline samples, a GET-only HTTP server (JSON API + embedded dashboard), and an Alert engine that diffs state transitions and delivers via a pluggable Notifier. All cluster IO (`rocm-smi`, `pct`, `docker`, `systemctl`, HTTP) is funnelled through one injectable `Probes` seam so every check is unit-testable with recorded fixtures. v1 executes no remediation; each check carries a `suggested_action` descriptor (text only) as the seam for a future guarded action layer.

**Tech Stack:** Python 3 standard library ONLY — `http.server`, `sqlite3`, `subprocess`, `urllib.request`, `json`, `threading`, `logging`, `argparse`, `unittest`. No pip, no venv. Deployed via the repo's existing `scripts/NN-*.sh` + `scripts/files/` pattern. Spec: `docs/cluster-monitor-design.md`.

## Global Constraints

- **Python 3 stdlib ONLY** — no `pip install`, no third-party imports, no venv. A tool that watches for fragility must not itself rot. (Spec §2 "Stack".)
- **Read-only in v1** — HTTP server serves GET only; no check executes a mutating command. `suggested_action` is descriptor text, never run. (Spec §2 "Mutation", §7, §8.)
- **Immutability** — all result/event records are `@dataclass(frozen=True)`; never mutate an existing object, always construct a new one. (User standard.)
- **Secrets never persisted** — router/Tavily/DB keys are read at runtime from host/LXC env files and never written to the SQLite store, the JSON API, the dashboard HTML, or any log line. (Spec §8.)
- **Config format is JSON** at `/etc/cluster-monitor.json` (deviation from spec §5's "yaml": YAML is not in the stdlib, and the config has nested tables — ceilings, thresholds — so stdlib `json` is the zero-dep choice). Ships with baked-in defaults so it runs with no config file.
- **Source filename uses an underscore** — `cluster_monitor.py` (not the hyphen in spec §6) so the `unittest` suite can `import cluster_monitor`. Installed on the host at `/opt/cluster-monitor/cluster_monitor.py`.
- **File size** target <800 lines, functions <50 lines (user standard). The module is internally sectioned with banner comments; if it approaches the ceiling during build, that is expected for a self-contained single-file service (matches repo pattern: `router-app.py`, `mcp-sdg-server.py`).
- **Time is injected** — functions that need "now" take a `now: float` parameter (default `time.time()` at the call site) so the Store and Alert engine are deterministically testable.
- **All bind addresses default to the host LAN IP**, never `0.0.0.0` without explicit config. Optional bearer token gates `/api/*`. (Spec §8.)

**Cluster topology referenced by checks** (defaults; all overridable via config):
- Router: `192.168.6.153:8000` (`/healthz` unauthed; `/v1/tavily/search` bearer `ROUTER_API_KEY` from `/etc/router.env` in LXC 153).
- AnythingLLM: `192.168.6.154:3001`.
- MCP stack (mcp-sdg): `192.168.6.155:3004`.
- Memory Vault: LXC 156, REST `:8000/api/health`, MCP bridge `:3005/mcp` (dynamic IP, currently `192.168.6.223`).
- GPUs: 2× AMD V620 (gfx1030) on the host via `rocm-smi`.
- LXCs: 151 (chat, RAM ceiling MUST be 32768), 153, 154, 155, 156.
- RAG refresh: host systemd `rag-refresh.timer`; metrics at `/var/lib/rag-refresh/metrics.prom` (Prometheus textfile); per-source state `/tank/rag-state/<id>/manifest.json`.
- Backup: host systemd `memory-vault-backup.timer`.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/files/cluster_monitor.py` | The whole service: core types, Probes IO seam, check registry + check functions, Store, Alert engine, Collector, HTTP server, dashboard HTML, config loader, `main()`/`--once` CLI. |
| `scripts/files/tests/test_cluster_monitor.py` | `unittest` suite: parser fixtures, check functions via `FakeProbes`, Store semantics, Alert transition matrix, config merge. |
| `scripts/63-cluster-monitor.sh` | Idempotent installer (host): pushes the module, writes default `/etc/cluster-monitor.json` if absent, installs+enables `cluster-monitor.service`, creates `/var/lib/cluster-monitor` and `/opt/cluster-monitor`. |
| `scripts/files/cluster-monitor.service` | systemd unit template (`Type=simple`, `Restart=on-failure`). |
| `docs/cluster-monitor-design.md` | (exists) the approved design spec. |

The module is organized top-to-bottom in these sections, built up across the tasks below:
`1. Core types` → `2. Probes (IO seam)` → `3. Store` → `4. Alert engine` → `5. Check helpers + parsers` → `6. Checks (health/metrics/freshness) + REGISTRY` → `7. Collector` → `8. HTTP server + dashboard` → `9. Config + main/CLI`.

---

### Task 1: Module scaffold + core types + test harness

**Files:**
- Create: `scripts/files/cluster_monitor.py`
- Create: `scripts/files/tests/test_cluster_monitor.py`
- Create: `scripts/files/tests/__init__.py` (empty)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `CheckResult(id:str, group:str, status:str, detail:str, value:float|None=None, unit:str|None=None, suggested_action:str|None=None)` — frozen dataclass; `status ∈ {"ok","warn","fail","info"}`. `.to_dict() -> dict`.
  - `CmdResult(rc:int, stdout:str, stderr:str)` — frozen.
  - `HttpResult(status:int, body:str, error:str|None)` — frozen; `status==0` means transport failure (see `error`).
  - `STATUS_OK="ok"`, `STATUS_WARN="warn"`, `STATUS_FAIL="fail"`, `STATUS_INFO="info"` constants.

- [ ] **Step 1: Write the failing test**

Create `scripts/files/tests/__init__.py` as an empty file, then `scripts/files/tests/test_cluster_monitor.py`:

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cluster_monitor as cm


class TestCoreTypes(unittest.TestCase):
    def test_checkresult_to_dict_roundtrip(self):
        r = cm.CheckResult(
            id="gpu_vram_0", group="metrics", status=cm.STATUS_WARN,
            detail="card 0 at 91%", value=91.0, unit="%",
            suggested_action="none",
        )
        d = r.to_dict()
        self.assertEqual(d["id"], "gpu_vram_0")
        self.assertEqual(d["status"], "warn")
        self.assertEqual(d["value"], 91.0)
        self.assertEqual(d["unit"], "%")
        self.assertEqual(d["suggested_action"], "none")

    def test_checkresult_is_frozen(self):
        r = cm.CheckResult(id="x", group="health", status=cm.STATUS_OK, detail="")
        with self.assertRaises(Exception):
            r.status = cm.STATUS_FAIL  # frozen -> FrozenInstanceError

    def test_optional_fields_default_none(self):
        r = cm.CheckResult(id="x", group="health", status=cm.STATUS_OK, detail="ok")
        self.assertIsNone(r.value)
        self.assertIsNone(r.unit)
        self.assertIsNone(r.suggested_action)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor -v` (from `scripts/files/`)
Expected: FAIL — `ModuleNotFoundError: No module named 'cluster_monitor'`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/files/cluster_monitor.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor -v` (from `scripts/files/`)
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/__init__.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): core types + test harness scaffold"
```

---

### Task 2: Probes IO seam (real) + FakeProbes (tests)

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add section 2)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `CmdResult`, `HttpResult` (Task 1).
- Produces:
  - `Probes` with `cmd(args:list[str], timeout:float=10.0) -> CmdResult` and `http(method:str, url:str, *, headers:dict|None=None, json_body:dict|None=None, timeout:float=5.0) -> HttpResult`.
  - `FakeProbes(cmd_map:dict[str,CmdResult]|None=None, http_map:dict[tuple,HttpResult]|None=None)` — test double with the same interface; `cmd_map` keyed by `" ".join(args)`, `http_map` keyed by `(method, url)`. `FakeProbes` is defined in the **test file** (not the module).

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py` (top-level, reused by later tasks):

```python
class FakeProbes:
    """Test double for cluster_monitor.Probes."""
    def __init__(self, cmd_map=None, http_map=None):
        self._cmd = cmd_map or {}
        self._http = http_map or {}

    def cmd(self, args, timeout=10.0):
        return self._cmd.get(" ".join(args), cm.CmdResult(127, "", "fake: not found"))

    def http(self, method, url, *, headers=None, json_body=None, timeout=5.0):
        return self._http.get((method, url), cm.HttpResult(0, "", "fake: no route"))


class TestProbes(unittest.TestCase):
    def test_real_cmd_runs_echo(self):
        p = cm.Probes()
        r = p.cmd(["python3", "-c", "print('hi')"])
        self.assertEqual(r.rc, 0)
        self.assertIn("hi", r.stdout)

    def test_real_cmd_missing_binary_is_nonzero_not_raises(self):
        p = cm.Probes()
        r = p.cmd(["definitely-not-a-real-binary-xyz"])
        self.assertNotEqual(r.rc, 0)
        self.assertTrue(r.stderr)

    def test_fake_cmd_lookup(self):
        fp = FakeProbes(cmd_map={"pct config 151": cm.CmdResult(0, "memory: 32768", "")})
        self.assertEqual(fp.cmd(["pct", "config", "151"]).stdout, "memory: 32768")
        self.assertEqual(fp.cmd(["unknown"]).rc, 127)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestProbes -v`
Expected: FAIL — `AttributeError: module 'cluster_monitor' has no attribute 'Probes'`.

- [ ] **Step 3: Write minimal implementation**

Append section 2 to `cluster_monitor.py` (add imports `import json`, `import subprocess`, `import urllib.error`, `import urllib.request` to the import block):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestProbes -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): Probes IO seam + FakeProbes test double"
```

---

### Task 3: Store (SQLite)

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add section 3)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 1).
- Produces:
  - `Store(path:str)` — opens/creates SQLite at `path` (`":memory:"` in tests).
  - `Store.record(result:CheckResult, now:float) -> str` — upserts latest state; sets `last_ok_at=now` when `status==ok`; appends a sample row when `result.value is not None`; **returns the previous status** for that id (`""` if first sight). Used by the Alert engine.
  - `Store.prune(now:float, retention_s:float) -> int` — deletes samples older than `now-retention_s`; returns rows deleted.
  - `Store.snapshot(sample_window_s:float, now:float) -> list[dict]` — per check: its latest state dict + `last_ok_at` + `samples` (list of `[ts, value]` within window, ascending).
  - `Store.close()`.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = cm.Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _res(self, status, value=None):
        return cm.CheckResult(id="gpu_vram_0", group="metrics",
                              status=status, detail="d", value=value, unit="%")

    def test_first_record_returns_empty_prev_status(self):
        prev = self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.assertEqual(prev, "")

    def test_record_returns_previous_status(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        prev = self.store.record(self._res(cm.STATUS_FAIL, 99.0), now=101.0)
        self.assertEqual(prev, "ok")

    def test_last_ok_at_only_set_on_ok(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.store.record(self._res(cm.STATUS_FAIL, 99.0), now=200.0)
        snap = {s["id"]: s for s in self.store.snapshot(3600.0, now=250.0)}
        self.assertEqual(snap["gpu_vram_0"]["last_ok_at"], 100.0)
        self.assertEqual(snap["gpu_vram_0"]["status"], "fail")

    def test_samples_recorded_and_windowed(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.store.record(self._res(cm.STATUS_OK, 20.0), now=160.0)
        snap = {s["id"]: s for s in self.store.snapshot(120.0, now=200.0)}
        vals = [v for _, v in snap["gpu_vram_0"]["samples"]]
        self.assertEqual(vals, [10.0, 20.0])
        # Narrow window drops the old sample.
        snap2 = {s["id"]: s for s in self.store.snapshot(50.0, now=200.0)}
        self.assertEqual([v for _, v in snap2["gpu_vram_0"]["samples"]], [20.0])

    def test_value_none_records_no_sample(self):
        self.store.record(self._res(cm.STATUS_OK, None), now=100.0)
        snap = {s["id"]: s for s in self.store.snapshot(3600.0, now=200.0)}
        self.assertEqual(snap["gpu_vram_0"]["samples"], [])

    def test_prune_deletes_old_samples(self):
        self.store.record(self._res(cm.STATUS_OK, 10.0), now=100.0)
        self.store.record(self._res(cm.STATUS_OK, 20.0), now=1000.0)
        deleted = self.store.prune(now=1000.0, retention_s=500.0)
        self.assertEqual(deleted, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestStore -v`
Expected: FAIL — `AttributeError: module 'cluster_monitor' has no attribute 'Store'`.

- [ ] **Step 3: Write minimal implementation**

Append section 3 to `cluster_monitor.py` (add `import sqlite3` to imports):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestStore -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): SQLite store with last-ok + sample window"
```

---

### Task 4: Alert engine + Notifiers

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add section 4)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `CheckResult` (Task 1).
- Produces:
  - `AlertEvent(check_id:str, kind:str, status:str, detail:str, ts:float)` — frozen; `kind ∈ {"fired","resolved"}`.
  - `Notifier` base with `send(event:AlertEvent) -> None`; `LogNotifier(logger)` and `NoopNotifier`.
  - `AlertEngine(notifier:Notifier, cooldown_s:float=900.0)` with `evaluate(result:CheckResult, prev_status:str, now:float) -> AlertEvent | None`. Fires on `ok/info/"" -> warn/fail` and resolves on `warn/fail -> ok`. Per-id cooldown suppresses duplicate *fired* events for the same status within `cooldown_s`. Returns the event it delivered, or `None`.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class RecordingNotifier(cm.Notifier):
    def __init__(self):
        self.events = []
    def send(self, event):
        self.events.append(event)


class TestAlertEngine(unittest.TestCase):
    def setUp(self):
        self.n = RecordingNotifier()
        self.eng = cm.AlertEngine(self.n, cooldown_s=900.0)

    def _r(self, status):
        return cm.CheckResult(id="router_chat_upstream", group="health",
                              status=status, detail="d")

    def test_ok_to_fail_fires(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=100.0)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.kind, "fired")
        self.assertEqual(self.n.events[-1].status, "fail")

    def test_fail_to_ok_resolves(self):
        self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=100.0)
        ev = self.eng.evaluate(self._r(cm.STATUS_OK), prev_status="fail", now=200.0)
        self.assertEqual(ev.kind, "resolved")

    def test_steady_state_no_event(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_OK), prev_status="ok", now=100.0)
        self.assertIsNone(ev)

    def test_cooldown_suppresses_repeat_fire(self):
        self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=100.0)
        # Flap back then fail again inside the cooldown window -> suppressed.
        self.eng.evaluate(self._r(cm.STATUS_OK), prev_status="fail", now=150.0)
        ev = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=200.0)
        self.assertIsNone(ev)
        # After cooldown elapses, it fires again.
        ev2 = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="ok", now=1200.0)
        self.assertIsNotNone(ev2)

    def test_first_sight_fail_fires(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_FAIL), prev_status="", now=100.0)
        self.assertEqual(ev.kind, "fired")

    def test_warn_counts_as_problem(self):
        ev = self.eng.evaluate(self._r(cm.STATUS_WARN), prev_status="ok", now=100.0)
        self.assertEqual(ev.kind, "fired")


class TestNotifiers(unittest.TestCase):
    def test_noop_send_is_silent(self):
        cm.NoopNotifier().send(
            cm.AlertEvent("x", "fired", "fail", "d", 1.0))  # must not raise

    def test_log_notifier_writes(self):
        import logging
        logger = logging.getLogger("test_cm_alert")
        with self.assertLogs(logger, level="WARNING") as caught:
            cm.LogNotifier(logger).send(
                cm.AlertEvent("router_chat_upstream", "fired", "fail", "down", 1.0))
        self.assertTrue(any("router_chat_upstream" in m for m in caught.output))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestAlertEngine tests.test_cluster_monitor.TestNotifiers -v`
Expected: FAIL — `AttributeError: ... 'AlertEngine'`.

- [ ] **Step 3: Write minimal implementation**

Append section 4 to `cluster_monitor.py`:

```python
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
        self._last_fire: dict[str, tuple[str, float]] = {}  # id -> (status, ts)

    def evaluate(self, result: CheckResult, prev_status: str, now: float):
        is_problem = result.status in _PROBLEM
        was_problem = prev_status in _PROBLEM
        event = None
        if is_problem and not was_problem:
            if self._suppressed(result.id, result.status, now):
                return None
            self._last_fire[result.id] = (result.status, now)
            event = AlertEvent(result.id, "fired", result.status, result.detail, now)
        elif was_problem and result.status == STATUS_OK:
            event = AlertEvent(result.id, "resolved", result.status, result.detail, now)
        if event is not None:
            self._notifier.send(event)
        return event

    def _suppressed(self, check_id: str, status: str, now: float) -> bool:
        last = self._last_fire.get(check_id)
        if last is None:
            return False
        last_status, last_ts = last
        return last_status == status and (now - last_ts) < self._cooldown_s
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestAlertEngine tests.test_cluster_monitor.TestNotifiers -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): alert engine with transitions + cooldown + notifiers"
```

---

### Task 5: Check helpers (thresholds, HTTP-liveness, registry types)

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add section 5 + registry skeleton in section 6)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `CheckResult`, `Probes`/`FakeProbes`, `HttpResult` (Tasks 1–2).
- Produces:
  - `Check(id:str, group:str, fn:Callable[[Probes, dict], list[CheckResult]])` — frozen dataclass; `group ∈ {"health","metrics","freshness"}` (drives cadence; an emitted result may carry a *different* `group` for display).
  - `status_for(value:float, warn:float, fail:float, *, higher_is_worse:bool=True) -> str` — maps a metric to ok/warn/fail.
  - `http_alive(probes, url, *, ok_statuses=None, alive_any_http=False) -> tuple[str,str]` — returns `(status, detail)`; `alive_any_http=True` treats *any* HTTP response (incl. 4xx) as alive (for endpoints like the MCP bridge that 307/406 when probed plainly).
  - `REGISTRY: list[Check]` — initially empty; populated by Tasks 6–8.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class TestCheckHelpers(unittest.TestCase):
    def test_status_for_higher_is_worse(self):
        self.assertEqual(cm.status_for(50, warn=90, fail=98), cm.STATUS_OK)
        self.assertEqual(cm.status_for(92, warn=90, fail=98), cm.STATUS_WARN)
        self.assertEqual(cm.status_for(99, warn=90, fail=98), cm.STATUS_FAIL)

    def test_status_for_lower_is_worse(self):
        # e.g. free memory percent: low is bad
        self.assertEqual(
            cm.status_for(5, warn=10, fail=3, higher_is_worse=False), cm.STATUS_WARN)
        self.assertEqual(
            cm.status_for(2, warn=10, fail=3, higher_is_worse=False), cm.STATUS_FAIL)
        self.assertEqual(
            cm.status_for(50, warn=10, fail=3, higher_is_worse=False), cm.STATUS_OK)

    def test_http_alive_2xx_ok(self):
        fp = FakeProbes(http_map={("GET", "http://h:3001/"): cm.HttpResult(200, "")})
        status, _ = cm.http_alive(fp, "http://h:3001/")
        self.assertEqual(status, cm.STATUS_OK)

    def test_http_alive_transport_fail(self):
        fp = FakeProbes()  # no route -> status 0
        status, detail = cm.http_alive(fp, "http://h:3001/")
        self.assertEqual(status, cm.STATUS_FAIL)
        self.assertIn("no route", detail)

    def test_http_alive_any_http_treats_4xx_as_alive(self):
        fp = FakeProbes(http_map={("GET", "http://h:3005/mcp"): cm.HttpResult(406, "")})
        status, _ = cm.http_alive(fp, "http://h:3005/mcp", alive_any_http=True)
        self.assertEqual(status, cm.STATUS_OK)

    def test_registry_exists_and_is_list(self):
        self.assertIsInstance(cm.REGISTRY, list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestCheckHelpers -v`
Expected: FAIL — `AttributeError: ... 'status_for'`.

- [ ] **Step 3: Write minimal implementation**

Append section 5 + the registry skeleton to `cluster_monitor.py` (add `from typing import Callable` to imports):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestCheckHelpers -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): check helpers + registry skeleton"
```

---

### Task 6: Health checks (router, anythingllm, mcp-sdg, memory vault)

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add to section 6)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `Check`, `REGISTRY`, `http_alive`, `STATUS_*`, `CheckResult` (Tasks 1,5); `cfg` dict keys `router_url`, `anythingllm_url`, `mcp_sdg_url`, `memvault_rest_url`, `memvault_bridge_url`.
- Produces (appended to `REGISTRY`, group `health`):
  - `check_router_healthz(probes, cfg) -> list[CheckResult]` — GET `{router_url}/healthz`. On transport failure emits one `router_up=fail`. On success parses JSON and emits `router_chat_upstream`, `router_embed_upstream`, `router_rerank_upstream` (ok/fail per upstream), `loaded_chat_profile` (group=`freshness`, status=info, detail=profile), `last_chat_completion` (group=`freshness`, value=`seconds_since_chat`, warn>3600 fail>86400).
  - `check_anythingllm`, `check_mcp_sdg`, `check_memvault_rest`, `check_memvault_bridge`.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
import json as _json


class TestHealthChecks(unittest.TestCase):
    CFG = {
        "router_url": "http://r:8000",
        "anythingllm_url": "http://a:3001",
        "mcp_sdg_url": "http://m:3004",
        "memvault_rest_url": "http://v:8000",
        "memvault_bridge_url": "http://v:3005/mcp",
    }

    def test_router_healthz_parses_upstreams(self):
        body = _json.dumps({
            "upstream": {"chat": True, "embed": True, "rerank": False},
            "active_chat_profile": "qwen3-coder",
            "seconds_since_chat": 42,
        })
        fp = FakeProbes(http_map={
            ("GET", "http://r:8000/healthz"): cm.HttpResult(200, body)})
        out = {r.id: r for r in cm.check_router_healthz(fp, self.CFG)}
        self.assertEqual(out["router_chat_upstream"].status, cm.STATUS_OK)
        self.assertEqual(out["router_rerank_upstream"].status, cm.STATUS_FAIL)
        self.assertEqual(out["loaded_chat_profile"].detail, "qwen3-coder")
        self.assertEqual(out["last_chat_completion"].value, 42.0)

    def test_router_healthz_unreachable_one_fail_tile(self):
        fp = FakeProbes()
        out = cm.check_router_healthz(fp, self.CFG)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].id, "router_up")
        self.assertEqual(out[0].status, cm.STATUS_FAIL)

    def test_anythingllm_ok(self):
        fp = FakeProbes(http_map={("GET", "http://a:3001/"): cm.HttpResult(200, "")})
        self.assertEqual(cm.check_anythingllm(fp, self.CFG)[0].status, cm.STATUS_OK)

    def test_mcp_sdg_any_http_alive(self):
        fp = FakeProbes(http_map={("GET", "http://m:3004/"): cm.HttpResult(404, "")})
        self.assertEqual(cm.check_mcp_sdg(fp, self.CFG)[0].status, cm.STATUS_OK)

    def test_memvault_rest_down(self):
        fp = FakeProbes()
        self.assertEqual(cm.check_memvault_rest(fp, self.CFG)[0].status, cm.STATUS_FAIL)

    def test_memvault_bridge_alive_on_406(self):
        fp = FakeProbes(http_map={("GET", "http://v:3005/mcp"): cm.HttpResult(406, "")})
        self.assertEqual(cm.check_memvault_bridge(fp, self.CFG)[0].status, cm.STATUS_OK)

    def test_health_checks_registered(self):
        ids = {c.id for c in cm.REGISTRY}
        for cid in ("router_healthz", "anythingllm", "mcp_sdg",
                    "memvault_rest", "memvault_bridge"):
            self.assertIn(cid, ids)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestHealthChecks -v`
Expected: FAIL — `AttributeError: ... 'check_router_healthz'`.

- [ ] **Step 3: Write minimal implementation**

Append to section 6 of `cluster_monitor.py`:

```python
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


REGISTRY.extend([
    Check("router_healthz", "health", check_router_healthz),
    Check("anythingllm", "health", check_anythingllm),
    Check("mcp_sdg", "health", check_mcp_sdg),
    Check("memvault_rest", "health", check_memvault_rest),
    Check("memvault_bridge", "health", check_memvault_bridge),
])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestHealthChecks -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): service health checks (router/allm/mcp/memvault)"
```

---

### Task 7: Host + GPU metrics checks

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add to section 6 + a parsers block in section 5)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `Check`, `REGISTRY`, `status_for`, `CmdResult` (Tasks 1,5); `cfg` keys `gpu_vram_warn_pct`, `gpu_vram_fail_pct`, `gpu_temp_warn_c`, `gpu_temp_fail_c`, `host_mem_warn_pct`, `host_mem_fail_pct`, `lxc_ids` (list of int).
- Produces (group `metrics`):
  - `parse_rocm_vram_json(text) -> list[tuple[int,float,float]]` → `(card_index, used_mib, total_mib)`.
  - `parse_rocm_temp_json(text) -> list[tuple[int,float]]` → `(card_index, junction_c)`.
  - `parse_meminfo(text) -> dict[str,float]` (kB values).
  - `check_gpu_vram`, `check_gpu_temp`, `check_host_mem`, `check_host_cpu`, `check_zfs_arc`, `check_lxc_mem`.

Note on `rocm-smi --json`: it emits `{"card0": {"VRAM Total Memory (B)": "...", "VRAM Total Used Memory (B)": "...", ...}, "card1": {...}}` for `--showmeminfo vram --json`, and `{"card0": {"Temperature (Sensor junction) (C)": "..."}, ...}` for `--showtemp --json`. Parsers tolerate missing keys (skip that card) and varying key spellings via substring match.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class TestMetricsParsers(unittest.TestCase):
    def test_parse_rocm_vram_json(self):
        text = _json.dumps({
            "card0": {"VRAM Total Memory (B)": "34359738368",
                      "VRAM Total Used Memory (B)": "17179869184"},
            "card1": {"VRAM Total Memory (B)": "34359738368",
                      "VRAM Total Used Memory (B)": "1073741824"},
        })
        rows = cm.parse_rocm_vram_json(text)
        self.assertEqual(len(rows), 2)
        idx, used, total = rows[0]
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(used, 16384.0, places=0)   # MiB
        self.assertAlmostEqual(total, 32768.0, places=0)

    def test_parse_rocm_temp_json(self):
        text = _json.dumps({
            "card0": {"Temperature (Sensor junction) (C)": "95.0"},
            "card1": {"Temperature (Sensor junction) (C)": "60.0"},
        })
        rows = dict(cm.parse_rocm_temp_json(text))
        self.assertEqual(rows[0], 95.0)
        self.assertEqual(rows[1], 60.0)

    def test_parse_meminfo(self):
        text = "MemTotal:       65809920 kB\nMemAvailable:    6580992 kB\n"
        d = cm.parse_meminfo(text)
        self.assertEqual(d["MemTotal"], 65809920.0)
        self.assertEqual(d["MemAvailable"], 6580992.0)


class TestMetricsChecks(unittest.TestCase):
    CFG = {
        "gpu_vram_warn_pct": 90, "gpu_vram_fail_pct": 98,
        "gpu_temp_warn_c": 95, "gpu_temp_fail_c": 105,
        "host_mem_warn_pct": 10, "host_mem_fail_pct": 3,
        "lxc_ids": [151],
    }

    def test_gpu_vram_per_card_tiles(self):
        text = _json.dumps({
            "card0": {"VRAM Total Memory (B)": "34359738368",
                      "VRAM Total Used Memory (B)": "33500000000"},  # ~97.5%
        })
        fp = FakeProbes(cmd_map={
            "rocm-smi --showmeminfo vram --json": cm.CmdResult(0, text, "")})
        out = cm.check_gpu_vram(fp, self.CFG)
        self.assertEqual(out[0].id, "gpu_vram_0")
        self.assertEqual(out[0].status, cm.STATUS_WARN)  # >=90 <98
        self.assertEqual(out[0].unit, "%")

    def test_gpu_temp_fail_high(self):
        text = _json.dumps({"card0": {"Temperature (Sensor junction) (C)": "106"}})
        fp = FakeProbes(cmd_map={
            "rocm-smi --showtemp --json": cm.CmdResult(0, text, "")})
        out = cm.check_gpu_temp(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_FAIL)

    def test_host_mem_low_available_warns(self):
        text = "MemTotal: 1000 kB\nMemAvailable: 80 kB\n"  # 8% avail
        fp = FakeProbes(cmd_map={"cat /proc/meminfo": cm.CmdResult(0, text, "")})
        out = cm.check_host_mem(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_WARN)

    def test_host_cpu_value(self):
        fp = FakeProbes(cmd_map={
            "cat /proc/loadavg": cm.CmdResult(0, "1.50 1.0 0.9 1/100 1234", ""),
            "nproc": cm.CmdResult(0, "8\n", "")})
        out = cm.check_host_cpu(fp, self.CFG)
        self.assertEqual(out[0].value, 1.5)

    def test_zfs_arc_value(self):
        arcstats = "name type data\nsize 4 8589934592\nc_max 4 17179869184\n"
        fp = FakeProbes(cmd_map={
            "cat /proc/spl/kstat/zfs/arcstats": cm.CmdResult(0, arcstats, "")})
        out = cm.check_zfs_arc(fp, self.CFG)
        self.assertEqual(out[0].id, "zfs_arc")
        self.assertAlmostEqual(out[0].value, 50.0, places=0)  # 8G of 16G cap

    def test_lxc_mem_uses_ceiling_and_used(self):
        fp = FakeProbes(cmd_map={
            "pct config 151": cm.CmdResult(0, "memory: 32768\ncores: 8\n", ""),
            "pct exec 151 -- cat /proc/meminfo":
                cm.CmdResult(0, "MemTotal: 33554432 kB\nMemAvailable: 16777216 kB\n", "")})
        out = cm.check_lxc_mem(fp, self.CFG)
        self.assertEqual(out[0].id, "lxc_mem_151")
        self.assertEqual(out[0].unit, "%")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestMetricsParsers tests.test_cluster_monitor.TestMetricsChecks -v`
Expected: FAIL — `AttributeError: ... 'parse_rocm_vram_json'`.

- [ ] **Step 3: Write minimal implementation**

Add the parsers to section 5 and the checks to section 6 of `cluster_monitor.py`:

```python
# parsers (section 5)

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
        total = _rocm_find(card, "VRAM Total Memory")
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
```

```python
# checks (section 6)

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


REGISTRY.extend([
    Check("gpu_vram", "metrics", check_gpu_vram),
    Check("gpu_temp", "metrics", check_gpu_temp),
    Check("host_mem", "metrics", check_host_mem),
    Check("host_cpu", "metrics", check_host_cpu),
    Check("zfs_arc", "metrics", check_zfs_arc),
    Check("lxc_mem", "metrics", check_lxc_mem),
])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestMetricsParsers tests.test_cluster_monitor.TestMetricsChecks -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): host + GPU metric checks (vram/temp/mem/cpu/arc/lxc)"
```

---

### Task 8: Freshness + config-drift checks

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add to sections 5 + 6)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: Tasks 1,5; `cfg` keys `rag_metrics_path` (default `/var/lib/rag-refresh/metrics.prom`), `rag_stale_after_s` (default 93600 = 26h), `backup_timer_name`, `rag_timer_name`, `memvault_vmid` (156), `lxc_ram_ceilings` (dict `{"151":32768,...}`), `router_url`, `tavily_query`, `router_env_path` (`/etc/router.env` in LXC 153), `router_vmid` (153).
- Produces (group `freshness`):
  - `parse_prom(text) -> dict[str, list[tuple[dict,float]]]` — metric name → list of `(labels, value)`.
  - `check_rag_refresh`, `check_backup_timer`, `check_restart_policies`, `check_lxc_ram_ceilings`, `check_tavily_proxy`.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class TestPromParser(unittest.TestCase):
    def test_parse_prom_labels_and_values(self):
        text = (
            "# HELP rag_refresh_last_run_timestamp x\n"
            "# TYPE rag_refresh_last_run_timestamp gauge\n"
            "rag_refresh_last_run_timestamp 1718764500\n"
            'rag_refresh_run_total{status="ok"} 5\n'
            'rag_refresh_run_total{status="error"} 1\n'
        )
        m = cm.parse_prom(text)
        self.assertEqual(m["rag_refresh_last_run_timestamp"][0][1], 1718764500.0)
        totals = {lbl["status"]: v for lbl, v in m["rag_refresh_run_total"]}
        self.assertEqual(totals["error"], 1.0)


class TestFreshnessChecks(unittest.TestCase):
    CFG = {
        "rag_metrics_path": "/var/lib/rag-refresh/metrics.prom",
        "rag_stale_after_s": 93600,
        "rag_timer_name": "rag-refresh.timer",
        "backup_timer_name": "memory-vault-backup.timer",
        "memvault_vmid": 156,
        "lxc_ram_ceilings": {"151": 32768},
        "router_url": "http://r:8000",
        "router_vmid": 153,
        "router_env_path": "/etc/router.env",
        "tavily_query": "site reachability probe",
    }

    def test_rag_refresh_fresh_ok(self):
        prom = "rag_refresh_last_run_timestamp 2000000\nrag_refresh_run_total{status=\"ok\"} 3\n"
        fp = FakeProbes(cmd_map={
            "cat /var/lib/rag-refresh/metrics.prom": cm.CmdResult(0, prom, ""),
            "systemctl list-timers rag-refresh.timer --no-pager":
                cm.CmdResult(0, "NEXT LEFT LAST PASSED UNIT\nx x x x rag-refresh.timer\n", "")})
        out = {r.id: r for r in cm.check_rag_refresh(fp, self.CFG, now=2000300.0)}
        self.assertEqual(out["rag_refresh"].status, cm.STATUS_OK)

    def test_rag_refresh_stale_warns(self):
        prom = "rag_refresh_last_run_timestamp 1000000\n"
        fp = FakeProbes(cmd_map={
            "cat /var/lib/rag-refresh/metrics.prom": cm.CmdResult(0, prom, ""),
            "systemctl list-timers rag-refresh.timer --no-pager": cm.CmdResult(0, "", "")})
        out = {r.id: r for r in cm.check_rag_refresh(fp, self.CFG, now=2000000.0)}
        self.assertEqual(out["rag_refresh"].status, cm.STATUS_WARN)

    def test_rag_refresh_missing_metrics_warns_with_action(self):
        fp = FakeProbes()  # cat fails (127)
        out = {r.id: r for r in cm.check_rag_refresh(fp, self.CFG, now=2000000.0)}
        self.assertEqual(out["rag_refresh"].status, cm.STATUS_WARN)
        self.assertIn("58-rag-refresh-timer", out["rag_refresh"].suggested_action)

    def test_restart_policies_fail_when_not_unless_stopped(self):
        docker_out = "/memory-vault-db-1 no\n/memory-vault-app-1 unless-stopped\n"
        fp = FakeProbes(cmd_map={
            "pct exec 156 -- docker inspect --format {{.Name}} {{.HostConfig.RestartPolicy.Name}} $(docker ps -aq)":
                cm.CmdResult(0, docker_out, "")})
        out = cm.check_restart_policies(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_FAIL)
        self.assertIn("memory-vault-db-1", out[0].detail)

    def test_lxc_ram_ceilings_drift_fails(self):
        fp = FakeProbes(cmd_map={
            "pct config 151": cm.CmdResult(0, "memory: 12288\n", "")})
        out = {r.id: r for r in cm.check_lxc_ram_ceilings(fp, self.CFG)}
        self.assertEqual(out["lxc_ram_ceiling_151"].status, cm.STATUS_FAIL)
        self.assertIn("pct_set_mem(151, 32768)",
                      out["lxc_ram_ceiling_151"].suggested_action)

    def test_lxc_ram_ceilings_match_ok(self):
        fp = FakeProbes(cmd_map={
            "pct config 151": cm.CmdResult(0, "memory: 32768\n", "")})
        out = {r.id: r for r in cm.check_lxc_ram_ceilings(fp, self.CFG)}
        self.assertEqual(out["lxc_ram_ceiling_151"].status, cm.STATUS_OK)

    def test_backup_timer_present_ok(self):
        fp = FakeProbes(cmd_map={
            "systemctl is-active memory-vault-backup.timer": cm.CmdResult(0, "active\n", "")})
        out = cm.check_backup_timer(fp, self.CFG)
        self.assertEqual(out[0].status, cm.STATUS_OK)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestPromParser tests.test_cluster_monitor.TestFreshnessChecks -v`
Expected: FAIL — `AttributeError: ... 'parse_prom'`.

- [ ] **Step 3: Write minimal implementation**

Add `parse_prom` to section 5 and the checks to section 6 (add `import re` to imports):

```python
# parser (section 5)

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
```

```python
# checks (section 6)

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
        "pct", "exec", str(vmid), "--", "docker", "inspect",
        "--format", "{{.Name}} {{.HostConfig.RestartPolicy.Name}}",
        "$(docker ps -aq)"])
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
    Check("rag_refresh", "freshness", check_rag_refresh),
    Check("backup_timer", "freshness", check_backup_timer),
    Check("restart_policies", "freshness", check_restart_policies),
    Check("lxc_ram_ceilings", "freshness", check_lxc_ram_ceilings),
    Check("tavily_proxy", "freshness", check_tavily_proxy),
])
```

Note: `check_rag_refresh` takes an optional `now` for testability but the registry calls it with the 2-arg `(probes, cfg)` signature — the Collector (Task 9) invokes every check as `fn(probes, cfg)`, so `now` defaults to `time.time()` in production. Tests pass `now=` explicitly.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestPromParser tests.test_cluster_monitor.TestFreshnessChecks -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): freshness + config-drift checks (rag/backup/restart/ceilings/tavily)"
```

---

### Task 9: Collector (run-once + threaded loop)

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add section 7)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `REGISTRY`/`Check`, `Store`, `Probes`, `AlertEngine`, `CheckResult` (Tasks 1–8).
- Produces:
  - `Collector(checks:list[Check], store:Store, probes, alert_engine:AlertEngine, cfg:dict, now_fn=time.time)`.
  - `Collector.run_checks(groups:set[str]|None, now:float) -> list[CheckResult]` — runs checks whose `group` is in `groups` (all if `None`); each check wrapped so an exception becomes a `fail` CheckResult (id = check id) instead of crashing the cycle; records each result in the Store and feeds the Alert engine.
  - `Collector.run_once() -> list[CheckResult]` — runs every check once (for `--once`).
  - `Collector.start()` / `Collector.stop()` — background thread driving per-group intervals from `cfg["intervals"]` (`{"health":20,"metrics":60,"freshness":300}`); prunes samples each cycle using `cfg["sample_retention_s"]`.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class TestCollector(unittest.TestCase):
    def _cfg(self):
        return dict(TestHealthChecks.CFG, **{
            "intervals": {"health": 20, "metrics": 60, "freshness": 300},
            "sample_retention_s": 86400,
        })

    def test_run_checks_records_and_returns(self):
        store = cm.Store(":memory:")
        fp = FakeProbes(http_map={
            ("GET", "http://a:3001/"): cm.HttpResult(200, "")})
        eng = cm.AlertEngine(cm.NoopNotifier())
        only = [c for c in cm.REGISTRY if c.id == "anythingllm"]
        col = cm.Collector(only, store, fp, eng, self._cfg())
        results = col.run_checks(groups={"health"}, now=100.0)
        self.assertEqual(results[0].id, "anythingllm")
        snap = store.snapshot(3600, now=100.0)
        self.assertEqual(snap[0]["status"], "ok")
        store.close()

    def test_check_exception_becomes_fail_not_crash(self):
        store = cm.Store(":memory:")
        def boom(probes, cfg):
            raise RuntimeError("kaboom")
        col = cm.Collector([cm.Check("boom", "health", boom)], store,
                           FakeProbes(), cm.AlertEngine(cm.NoopNotifier()), self._cfg())
        results = col.run_checks(groups=None, now=100.0)
        self.assertEqual(results[0].id, "boom")
        self.assertEqual(results[0].status, cm.STATUS_FAIL)
        self.assertIn("kaboom", results[0].detail)
        store.close()

    def test_run_once_runs_all_groups(self):
        store = cm.Store(":memory:")
        col = cm.Collector(
            [cm.Check("a", "health", lambda p, c: [cm.CheckResult("a", "health", cm.STATUS_OK, "")]),
             cm.Check("b", "freshness", lambda p, c: [cm.CheckResult("b", "freshness", cm.STATUS_OK, "")])],
            store, FakeProbes(), cm.AlertEngine(cm.NoopNotifier()), self._cfg())
        ids = {r.id for r in col.run_once()}
        self.assertEqual(ids, {"a", "b"})
        store.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestCollector -v`
Expected: FAIL — `AttributeError: ... 'Collector'`.

- [ ] **Step 3: Write minimal implementation**

Append section 7 to `cluster_monitor.py` (add `import logging`, `import threading`, `import time` to imports):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestCollector -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): collector (run-once + threaded per-group loop)"
```

---

### Task 10: HTTP server + JSON API + bearer auth

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add section 8a)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `Store` (Task 3), `cfg` keys `bind_host`, `bind_port`, `bearer_token` (`""`=disabled), `sample_window_s`, `dashboard_title`.
- Produces:
  - `make_handler(store:Store, cfg:dict) -> type` — returns a `BaseHTTPRequestHandler` subclass serving GET `/api/status`, `/healthz`, `/` (dashboard, Task 11). `/api/*` requires `Authorization: Bearer <token>` when `bearer_token` set → else 401. `/healthz` and `/` are never gated.
  - `build_server(store, cfg) -> ThreadingHTTPServer`.

For testing without a socket, the handler delegates to a pure helper `route(path:str, auth_header:str|None, store, cfg) -> tuple[int, str, str]` returning `(status, content_type, body)`. The unit tests target `route`; `make_handler` is a thin adapter.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class TestRouting(unittest.TestCase):
    def _cfg(self, token=""):
        return {"bearer_token": token, "sample_window_s": 3600,
                "dashboard_title": "Cluster Monitor"}

    def _store_with_one(self):
        s = cm.Store(":memory:")
        s.record(cm.CheckResult("anythingllm", "health", cm.STATUS_OK, "ok"), now=100.0)
        return s

    def test_healthz_ungated(self):
        st = self._store_with_one()
        code, ctype, body = cm.route("/healthz", None, st, self._cfg(token="secret"))
        self.assertEqual(code, 200)
        self.assertIn("ok", body)
        st.close()

    def test_api_status_returns_json(self):
        st = self._store_with_one()
        code, ctype, body = cm.route("/api/status", None, st, self._cfg())
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "application/json")
        data = _json.loads(body)
        self.assertEqual(data["checks"][0]["id"], "anythingllm")
        st.close()

    def test_api_status_401_without_token(self):
        st = self._store_with_one()
        code, _, _ = cm.route("/api/status", None, st, self._cfg(token="secret"))
        self.assertEqual(code, 401)
        st.close()

    def test_api_status_ok_with_token(self):
        st = self._store_with_one()
        code, _, _ = cm.route("/api/status", "Bearer secret", st, self._cfg(token="secret"))
        self.assertEqual(code, 200)
        st.close()

    def test_unknown_path_404(self):
        st = self._store_with_one()
        code, _, _ = cm.route("/nope", None, st, self._cfg())
        self.assertEqual(code, 404)
        st.close()

    def test_dashboard_root_served(self):
        st = self._store_with_one()
        code, ctype, body = cm.route("/", None, st, self._cfg())
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "text/html")
        self.assertIn("<html", body.lower())
        st.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestRouting -v`
Expected: FAIL — `AttributeError: ... 'route'`. (`test_dashboard_root_served` will also fail until Task 11 supplies `DASHBOARD_HTML`; that's expected — it passes after Task 11.)

- [ ] **Step 3: Write minimal implementation**

Append section 8a to `cluster_monitor.py` (add `from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer` to imports). Reference `DASHBOARD_HTML` which Task 11 defines; add a temporary placeholder `DASHBOARD_HTML = "<html><body>dashboard pending</body></html>"` now and replace it in Task 11.

```python
# ─────────────────────────── 8. HTTP server ───────────────────────────

DASHBOARD_HTML = "<html><body>dashboard pending</body></html>"  # replaced in Task 11


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestRouting -v`
Expected: PASS (6 tests — the dashboard test passes against the placeholder HTML, which already contains `<html`).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): GET-only HTTP router + JSON API + bearer auth"
```

---

### Task 11: Embedded dashboard page

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (replace `DASHBOARD_HTML`)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: `/api/status` JSON shape from Task 10 (`{title, generated_at, checks:[{id,group,status,detail,value,unit,last_ok_at,samples,suggested_action}]}`).
- Produces: `DASHBOARD_HTML` — a single self-contained HTML string (no external assets/CDN) that polls `/api/status` every 15s, groups tiles by `group`, colors by `status`, shows `detail`, `last_ok_at` (relative), `suggested_action`, and an inline-SVG sparkline from `samples`.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
class TestDashboardHTML(unittest.TestCase):
    def test_html_is_self_contained(self):
        html = cm.DASHBOARD_HTML
        self.assertIn("<html", html.lower())
        self.assertIn("/api/status", html)        # polls the API
        self.assertNotIn("http://", html)         # no external assets
        self.assertNotIn("https://", html)
        self.assertIn("setInterval", html)        # auto-refresh

    def test_html_references_status_classes(self):
        html = cm.DASHBOARD_HTML
        for token in ("ok", "warn", "fail"):
            self.assertIn(token, html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestDashboardHTML -v`
Expected: FAIL — `test_html_is_self_contained` fails (placeholder lacks `/api/status` and `setInterval`).

- [ ] **Step 3: Write minimal implementation**

Replace the placeholder `DASHBOARD_HTML` in `cluster_monitor.py` with the full page (use a normal triple-quoted string; the JS braces are fine since it is NOT an f-string):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestDashboardHTML tests.test_cluster_monitor.TestRouting -v`
Expected: PASS (all — `TestRouting.test_dashboard_root_served` now serves the real page).

- [ ] **Step 5: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): self-contained dashboard page (tiles + sparklines)"
```

---

### Task 12: Config loading + main() + `--once` CLI

**Files:**
- Modify: `scripts/files/cluster_monitor.py` (add section 9)
- Test: `scripts/files/tests/test_cluster_monitor.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `DEFAULT_CONFIG: dict` — every key used by checks/server/collector with a sane default (topology from Global Constraints; `bind_host` default `127.0.0.1`; `bearer_token` `""`; intervals 20/60/300; `sample_window_s` 3600; `sample_retention_s` 86400; `rag_metrics_path`; `lxc_ram_ceilings={"151":32768}`; `lxc_ids=[151,153,154,155,156]`; thresholds).
  - `load_config(path:str|None) -> dict` — returns `DEFAULT_CONFIG` shallow-merged with JSON at `path` (missing/empty file → defaults; nested dicts replaced wholesale, documented).
  - `format_once_table(results:list[CheckResult]) -> str` — aligned text table for `--once`.
  - `main(argv=None) -> int` — argparse: `--config PATH`, `--once`, `--port`, `--host`. `--once` runs all checks once, prints the table, returns `0` if no `fail`, else `1`. Without `--once`, starts Store+Collector+HTTP and serves forever.

- [ ] **Step 1: Write the failing test**

Add to `test_cluster_monitor.py`:

```python
import tempfile


class TestConfigAndCli(unittest.TestCase):
    def test_default_config_has_required_keys(self):
        cfg = cm.DEFAULT_CONFIG
        for k in ("router_url", "bind_host", "bind_port", "intervals",
                  "sample_window_s", "lxc_ram_ceilings", "rag_metrics_path"):
            self.assertIn(k, cfg)
        self.assertEqual(cfg["lxc_ram_ceilings"]["151"], 32768)

    def test_load_config_missing_returns_defaults(self):
        cfg = cm.load_config("/nonexistent/path.json")
        self.assertEqual(cfg["router_url"], cm.DEFAULT_CONFIG["router_url"])

    def test_load_config_overrides(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(_json.dumps({"bind_port": 9999, "bearer_token": "x"}))
            path = f.name
        cfg = cm.load_config(path)
        os.unlink(path)
        self.assertEqual(cfg["bind_port"], 9999)
        self.assertEqual(cfg["bearer_token"], "x")
        self.assertEqual(cfg["router_url"], cm.DEFAULT_CONFIG["router_url"])

    def test_format_once_table(self):
        results = [cm.CheckResult("anythingllm", "health", cm.STATUS_OK, "HTTP 200"),
                   cm.CheckResult("gpu_vram_0", "metrics", cm.STATUS_FAIL, "99%")]
        table = cm.format_once_table(results)
        self.assertIn("anythingllm", table)
        self.assertIn("FAIL", table)

    def test_main_once_returns_1_on_fail(self):
        # Point at a config whose endpoints all fail -> at least one FAIL.
        rc = cm.main(["--once", "--config", "/nonexistent.json"])
        self.assertEqual(rc, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cluster_monitor.TestConfigAndCli -v`
Expected: FAIL — `AttributeError: ... 'DEFAULT_CONFIG'`.

- [ ] **Step 3: Write minimal implementation**

Append section 9 to `cluster_monitor.py` (add `import argparse`, `import os`, `import sys`):

```python
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
        except (ValueError, OSError) as e:
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
    server = build_server(store, cfg)
    _LOG.info("serving on http://%s:%s", cfg["bind_host"], cfg["bind_port"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop()
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_cluster_monitor.TestConfigAndCli -v`
Expected: PASS (5 tests). Note `test_main_once_returns_1_on_fail` runs real checks against unreachable defaults (no router/GPU in the test env), so several checks return `fail` → rc 1.

- [ ] **Step 5: Run the FULL suite**

Run: `python3 -m unittest discover -s tests -v` (from `scripts/files/`)
Expected: PASS (all tests across Tasks 1–12).

- [ ] **Step 6: Commit**

```bash
git add scripts/files/cluster_monitor.py scripts/files/tests/test_cluster_monitor.py
git commit -m "feat(cluster-monitor): config loader + main loop + --once CLI"
```

---

### Task 13: Deployment — installer script + systemd unit

**Files:**
- Create: `scripts/files/cluster-monitor.service`
- Create: `scripts/63-cluster-monitor.sh`
- Test: manual idempotency check (shell, on host) — see Step 4.

**Interfaces:**
- Consumes: `scripts/lib/common.sh` helpers (`require_root`, `require_pve_host`, `load_config`, `step`, `ok`, `skip`, `write_file_if_changed`); `cluster_monitor.py`.
- Produces on host: `/opt/cluster-monitor/cluster_monitor.py`, `/etc/cluster-monitor.json` (only if absent), `/var/lib/cluster-monitor/`, enabled `cluster-monitor.service`.

- [ ] **Step 1: Write the systemd unit**

Create `scripts/files/cluster-monitor.service`:

```ini
[Unit]
Description=Local GPU cluster monitor (read-only health/metrics dashboard)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Runs as root: rocm-smi, pct, and pct-exec docker inspection are host-scoped
# and require it. The service is READ-ONLY (no mutating commands) — see
# docs/cluster-monitor-design.md sections 7-8 for the privilege footprint and
# the hard security requirements before any future action layer is added.
ExecStart=/usr/bin/python3 /opt/cluster-monitor/cluster_monitor.py --config /etc/cluster-monitor.json
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Write the installer**

Create `scripts/63-cluster-monitor.sh`:

```bash
#!/usr/bin/env bash
# 63-cluster-monitor.sh — install the read-only cluster monitor as a host
# systemd service. See docs/cluster-monitor-design.md.
#
# Installs:
#   /opt/cluster-monitor/cluster_monitor.py   — the service (stdlib-only)
#   /etc/cluster-monitor.json                 — config (written only if absent)
#   /var/lib/cluster-monitor/                 — SQLite state dir
#   /etc/systemd/system/cluster-monitor.service
#
# Why on the PVE host: needs native rocm-smi / pct / pct-exec-docker /
# systemctl access, all host-scoped. Idempotent — re-running updates the code
# and unit but never clobbers an existing /etc/cluster-monitor.json.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

SRC="${LGC_DIR}/files/cluster_monitor.py"
UNIT_SRC="${LGC_DIR}/files/cluster-monitor.service"
INSTALL_DIR="/opt/cluster-monitor"
STATE_DIR="/var/lib/cluster-monitor"
CONFIG_PATH="/etc/cluster-monitor.json"
MON_BIND_HOST="${MON_BIND_HOST:-127.0.0.1}"
MON_BIND_PORT="${MON_BIND_PORT:-8888}"

[[ -f "$SRC" ]] || die "cluster_monitor.py not found at $SRC"
require_cmd python3

step "63.1 — Smoke-test the module (syntax + --once)"
python3 "$SRC" --once --config /nonexistent.json >/dev/null 2>&1 || \
  log "(--once returned non-zero, expected when endpoints are unreachable)"
python3 -c "import py_compile,sys; py_compile.compile('$SRC', doraise=True)" \
  || die "cluster_monitor.py failed to compile"

step "63.2 — Install code + state dir"
mkdir -p "$INSTALL_DIR" "$STATE_DIR"
chmod 0755 "$INSTALL_DIR" "$STATE_DIR"
install -m 0755 "$SRC" "$INSTALL_DIR/cluster_monitor.py"
ok "Installed $INSTALL_DIR/cluster_monitor.py"

step "63.3 — Write default config (only if absent)"
if [[ -f "$CONFIG_PATH" ]]; then
  skip "$CONFIG_PATH exists — leaving as-is"
else
  cat > "$CONFIG_PATH" <<EOF
{
  "bind_host": "$MON_BIND_HOST",
  "bind_port": $MON_BIND_PORT,
  "bearer_token": "",
  "lxc_ram_ceilings": {"151": 32768}
}
EOF
  chmod 0644 "$CONFIG_PATH"
  ok "Wrote $CONFIG_PATH"
fi

step "63.4 — Install + enable systemd unit"
install -m 0644 "$UNIT_SRC" /etc/systemd/system/cluster-monitor.service
systemctl daemon-reload
systemctl enable --now cluster-monitor.service

step "63.5 — Smoke-check"
sleep 2
systemctl is-active cluster-monitor.service || die "service not active"
curl -fsS "http://${MON_BIND_HOST}:${MON_BIND_PORT}/healthz" >/dev/null \
  && ok "healthz reachable" || warn "healthz not reachable yet"

ok "Phase 63 complete."
echo
echo "Dashboard:     http://${MON_BIND_HOST}:${MON_BIND_PORT}/"
echo "JSON API:      http://${MON_BIND_HOST}:${MON_BIND_PORT}/api/status"
echo "One-shot scan: python3 $INSTALL_DIR/cluster_monitor.py --once"
echo "Logs:          journalctl -u cluster-monitor.service -f"
echo "Config:        $CONFIG_PATH (edit + 'systemctl restart cluster-monitor')"
```

- [ ] **Step 3: Make the installer executable**

Run: `chmod +x scripts/63-cluster-monitor.sh`

- [ ] **Step 4: Lint the installer**

Run: `bash -n scripts/63-cluster-monitor.sh` (syntax check; expected: no output, rc 0).
If `shellcheck` is available: `shellcheck scripts/63-cluster-monitor.sh` (expected: clean, matching the other phase scripts' style).

- [ ] **Step 5: Commit**

```bash
git add scripts/63-cluster-monitor.sh scripts/files/cluster-monitor.service
git commit -m "feat(cluster-monitor): host installer (63-*.sh) + systemd unit"
```

---

### Task 14: README entry + host acceptance run

**Files:**
- Modify: `scripts/README.md` (add a phase 63 row/section matching existing entries)
- Test: host acceptance (manual).

**Interfaces:** none (documentation + acceptance).

- [ ] **Step 1: Read the existing README phase list**

Run: `python3 -c "import sys; print(open('scripts/README.md').read())" | head -120` and locate where phases 58–62 are documented; mirror that format.

- [ ] **Step 2: Add the phase 63 entry**

Add an entry to `scripts/README.md` consistent with the surrounding lines (exact wording follows the file's existing pattern; include: what it installs, that it is host-scoped + read-only, the dashboard URL `http://<host>:8888/`, and the `--once` one-shot). Keep it to the same length/shape as the phase 62 entry.

- [ ] **Step 3: Commit the docs**

```bash
git add scripts/README.md
git commit -m "docs(cluster-monitor): document phase 63 installer in scripts/README"
```

- [ ] **Step 4: Host acceptance — copy + run the one-shot**

On the Proxmox host (manual; from the repo checkout on the host, or after `git pull`):

```bash
python3 scripts/files/cluster_monitor.py --once
```

Expected: a table of all checks. Real cluster state should show mostly `OK`/`INFO`; investigate any `FAIL`. Crucially verify the previously-silent classes now surface:
- `lxc_ram_ceiling_151` = OK (32768) — the 12 GB drift would show FAIL.
- `restart_policies` = OK — a missing `unless-stopped` would show FAIL.
- `rag_refresh` — shows last-run age, or WARN "timer not installed" if 58-*.sh hasn't run on the host.

- [ ] **Step 5: Host acceptance — install + verify the service**

On the host:

```bash
sudo bash scripts/63-cluster-monitor.sh
systemctl status cluster-monitor.service --no-pager
curl -fsS http://127.0.0.1:8888/api/status | python3 -m json.tool | head -40
```

Expected: service `active (running)`; `/api/status` returns JSON with a `checks` array; opening `http://<host-LAN-ip>:8888/` (after setting `bind_host` in `/etc/cluster-monitor.json` to the LAN IP and restarting) renders the dashboard with grouped tiles and sparklines.

- [ ] **Step 6: Final commit (if any host-driven config/doc tweaks)**

```bash
git add -A
git commit -m "chore(cluster-monitor): acceptance fixups from host run"
```

---

## Self-Review

**1. Spec coverage** (against `docs/cluster-monitor-design.md`):

| Spec section | Covered by |
|---|---|
| §2 always-on service | Task 9 (Collector thread) + Task 10 (HTTP) + Task 13 (systemd) |
| §2 host placement / native tool access | Task 13 unit runs as root on host; checks shell `rocm-smi`/`pct`/`docker` |
| §2 zero external deps | Global Constraints; stdlib-only enforced throughout; Task 13 §63.1 `py_compile` |
| §2 SQLite persistence | Task 3 |
| §2 pluggable alert sink, log/no-op v1 | Task 4 (`LogNotifier`/`NoopNotifier`) |
| §2 read-only v1, action-ready seam | `suggested_action` descriptors in Tasks 6–8; GET-only router Task 10 |
| §3 four parts (collector/store/http/alerts) | Tasks 3,4,9,10 |
| §4a service health | Task 6 |
| §4b host + GPU | Task 7 |
| §4c freshness + config-drift | Task 8 (incl. `rag_refresh`, `restart_policies`, `lxc_ram_ceilings`, `loaded_chat_profile`, `last_chat_completion` from Task 6) |
| §4 `rag_refresh` source "to be located" | RESOLVED in plan: `/var/lib/rag-refresh/metrics.prom` + `rag-refresh.timer` (Task 8) |
| §5 configuration | Task 12 (`DEFAULT_CONFIG`/`load_config`) + Task 13 (`/etc/cluster-monitor.json`); format changed yaml→json (documented in Global Constraints) |
| §6 deployment (files, installer, --once) | Tasks 12 (`--once`), 13 (installer + unit) |
| §7 action seam (descriptors only) | `suggested_action` in every actionable check; no executor built |
| §8 security v1 | GET-only (Task 10), bearer token (Task 10), bind LAN-only default (Task 12), secrets read at runtime never stored (Task 8 `check_tavily_proxy`) |
| §9 testing | unittest suite across Tasks 1–12; `--once` acceptance Task 14 |
| §10 out of scope | No remediation executor, no Prometheus/Grafana, single-host, log/noop sink only — honored |

No gaps.

**2. Placeholder scan:** The only intentional placeholder is `DASHBOARD_HTML` in Task 10, explicitly replaced in Task 11 (test in Task 10 passes against it; the real test is in Task 11). No `TBD`/`TODO`/"add error handling"/"similar to" left in code steps; every code step shows complete code.

**3. Type consistency:**
- `CheckResult` field order/names identical everywhere (`id, group, status, detail, value, unit, suggested_action`).
- `Probes.cmd(args)` and `Probes.http(method, url, ...)` signatures match `FakeProbes` and all call sites.
- `Store.record(result, now) -> prev_status:str` consumed correctly by `Collector._run_one`.
- `AlertEngine.evaluate(result, prev_status, now)` matches Collector call.
- `route(path, auth_header, store, cfg) -> (int, str, str)` matches `make_handler` adapter and tests.
- `check_rag_refresh` 3rd `now` arg is keyword-defaulted, so registry's 2-arg invocation in `Collector._run_one` is valid (verified against `_run_one` calling `check.fn(self._probes, self._cfg)`).
- Registry `Check.group` values ∈ {health, metrics, freshness} match `intervals` keys in `DEFAULT_CONFIG`.

Consistent. Plan ready for execution.
