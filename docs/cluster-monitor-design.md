# Cluster Monitor — Design Spec

- **Date:** 2026-06-19
- **Status:** Approved (design) — pending implementation plan
- **Scope:** An always-on, read-only health/metrics dashboard for the local GPU cluster
  (Proxmox host + GPUs + LXCs 151/153/154/155/156 + their services), running as a
  systemd service on the Proxmox host. Surfaces silent failures fast and tracks
  freshness/config-drift. Architected so remediation **actions** can be added later
  without a rewrite, but v1 executes nothing.

## 1. Background & motivation

On 2026-06-19 the cluster suffered a cascade of **silent** failures whose only
symptoms were downstream breakage, each costing significant debugging time:

- `llamacpp-chat` crash-looped (LXC 151 RAM ceiling had been cut to 12 GB → SDMA
  page faults on model load) — surfaced only when a TAM web query failed.
- Memory Vault's Postgres `db` container didn't restart after a host reboot (no
  restart policy) — surfaced only when MCP tool calls returned `ReadError`.
- The router's `TAVILY_API_KEY` was at risk on redeploy; the chat upstream was
  `unreachable` for ~50 min with no signal but a failing downstream report.
- LXC services (mcp-sdg) and the GPU state needed manual `pct`/`rocm-smi`/`docker`
  spelunking to assess.

Every one of these would have been a red tile (or a stale "last-successful"
timestamp) on a dashboard. The goal is to **make cluster state observable at a
glance and alert on regressions** — especially the "didn't come back after a
reboot / a redeploy silently changed state" class.

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Form | Always-on status service (web UI + JSON API) | Continuous visibility + alerting, vs. point-in-time `60-verify.sh` |
| Placement | **systemd service on the Proxmox host** | Native `rocm-smi`/`pct`/`docker`/`dmesg`/`systemctl` access; enables full future in-container remediation. (A LXC can't run `pct`/host-`systemctl`/sibling-`docker` even when privileged — those are host-scoped.) Trade-off accepted: not container-portable. |
| Stack | **Zero external deps** — Python 3 stdlib (`http.server`, `sqlite3`, `subprocess`) | A tool that watches for fragility must not itself depend on a venv that can rot. Host already has system `python3`. |
| Persistence | SQLite at `/var/lib/cluster-monitor/state.db` | "Last-successful" timestamps + short metric history (sparklines) must survive the monitor's own restart. |
| Alerting | Pluggable sink; v1 ships log/no-op | Real channel (Discord/ntfy/webhook) deferred; engine (transitions, cooldown, dedup, resolve) built now. |
| Mutation | **Read-only in v1**, action-ready seam | Privileged host service = sensitive; v1 only observes and *suggests* fixes. |

## 3. Architecture

Single host process `cluster-monitor.service`, four cooperating parts:

1. **Collector** — a background thread that runs a registry of **checks** on
   per-group intervals: health ~20 s, metrics ~60 s, freshness/config ~300 s.
   Each check is a pure function `() -> CheckResult` (see §4), so it's testable
   in isolation with mocked command output.
2. **Store** (`sqlite3`) — persists: latest `CheckResult` per check, `last_ok_at`
   per check, and a rolling window of numeric samples (for sparklines). Bounded
   retention (e.g. last 24 h or N samples) to keep the DB tiny.
3. **HTTP server** (`http.server`, GET-only in v1) — serves:
   - `GET /api/status` → JSON snapshot: every check's status/detail/value +
     `last_ok_at` + recent samples.
   - `GET /` → a single static dashboard page (HTML/CSS/JS, no framework) that
     polls `/api/status` every ~15 s and renders tiles + sparklines.
   - `GET /healthz` → the monitor's own liveness.
   Binds the host LAN IP only; optional bearer token (`MONITOR_TOKEN`).
4. **Alert engine** — on each collection cycle, diffs new vs. previous state and
   fires on transitions (`ok→fail`, `fresh→stale`) and recoveries (`fail→ok`),
   with per-check cooldown + dedup. Delivers via a `Notifier` interface; v1 wires
   a `LogNotifier` (journald/file) and `NoopNotifier`.

```
                 ┌─────────── cluster-monitor.service (host) ───────────┐
 rocm-smi ─┐     │  Collector(thread) ─► Store(sqlite) ─► HTTP(GET /api) │──► browser dashboard
 pct       ─┼──► │      │                                   ▲            │
 docker    ─┤    │      └─► Alert engine ─► Notifier(sink) ─┘            │──► (future) Discord/ntfy
 systemctl ─┤    │                                                       │
 curl/HTTP ─┘    └───────────────────────────────────────────────────────┘
```

## 4. Checks (the registry)

Each returns `CheckResult{ id, group, status: ok|warn|fail, detail, value?, unit?,
suggested_action? }`. `suggested_action` is a **descriptor only in v1** (shown as
text on the tile; not executable) — the seam for §7.

### 4a. Service health (group `health`, ~20 s)
- `router_healthz` — `GET :8000/healthz`; parses `upstream.chat/embed/rerank`,
  `active_chat_profile`, `seconds_since_chat`. Each upstream becomes its own tile.
- `router_up` — router process/port reachable.
- `anythingllm` — `GET :3001/` 2xx/3xx.
- `mcp_sdg` — `:3004` listening + `GET /` returns any HTTP status (404 = alive).
- `memvault_rest` — `GET :8000/api/health` on LXC 156 (200). Failing = db or app down.
- `memvault_bridge` — `GET :3005/mcp` returns HTTP (307/400/406 = alive).
- `tavily_proxy` — `POST :8000/v1/tavily/search` (router) → 200 vs 503 (key state).

### 4b. Host + GPU (group `metrics`, ~60 s)
- `gpu_vram` — per V620: used/total MiB (`rocm-smi --showmeminfo vram`). warn/fail thresholds.
- `gpu_temp` — per V620 junction temp (`rocm-smi` / existing `/var/lib/v620-temps`).
  warn near the known airflow ceiling (~95 °C).
- `host_mem` / `host_cpu` — host RAM + load (`/proc`).
- `zfs_arc` — ARC size vs cap.
- `lxc_mem` — per LXC: used vs **ceiling** (`pct config` + `pct exec free`); warn
  near ceiling. (Would have caught the 12 GB 151 ceiling.)

### 4c. Freshness + config-drift (group `freshness`, ~300 s)
- `rag_refresh` — **last successful refresh time + per-source status** from the
  `scripts/rag` state (via `lib/state.py`), plus the refresh timer's last/next run.
  Also surfaces **if the refresh job/timer is absent** (today's open finding).
- `backup_timer` — `memory-vault-backup.timer` last run; warn if stale.
- `last_chat_completion` — from `seconds_since_chat` and/or the router access log.
- `restart_policies` — `docker inspect` Memory Vault containers; **fail if any is
  not `unless-stopped`** (would have caught the db).
- `lxc_ram_ceilings` — `pct config` vs an expected table (e.g. 151 must be 32768);
  warn on drift.
- `loaded_chat_profile` — `active_chat_profile`; informational tile so the operator
  always knows which model is loaded.

The exact data sources for `rag_refresh` (state file path/schema, timer name) are
**to be located during implementation** — the 2026-06-19 timer sweep found no RAG
refresh timer, so confirming where/whether it runs is part of this work.

## 5. Configuration

`/etc/cluster-monitor.yaml` (or `.env`): bind address/port, bearer token, poll
intervals per group, per-check warn/fail thresholds, expected `lxc_ram_ceilings`
table, endpoint URLs + how to read keys (e.g. `ROUTER_API_KEY` from
`/etc/router.env` via `pct exec 153`), alert sink selection + cooldown. Ships with
sane defaults so it runs with an empty config.

## 6. Deployment

Follows the repo's existing pattern:
- `scripts/files/cluster-monitor.py` — the service (collector + store + server +
  alert engine). Dashboard HTML served from an embedded string or a sibling asset.
- `scripts/63-cluster-monitor.sh` — idempotent installer: copies the service,
  writes `/etc/cluster-monitor.yaml` (if absent), installs + enables
  `cluster-monitor.service`, creates `/var/lib/cluster-monitor`.
- `cluster-monitor.service` — `Restart=on-failure`, `Type=simple`, runs as a
  dedicated low-priv user where possible (note: GPU/`pct` access may require root;
  document the privilege footprint).
- A `--once` CLI mode runs every check once and prints a table (cron-able snapshot;
  overlaps with and can eventually subsume parts of `60-verify.sh`).

## 7. Future: action seam (NOT in v1)

- Each check's `suggested_action` descriptor names a parameterized remediation
  (`restart_unit(151, llamacpp-chat)`, `compose_up(156)`, `swap_profile(coder)`,
  `pct_set_mem(151, 32768)`). v1 renders these as suggested text only.
- v2 adds `POST /api/action/<id>` behind: bearer auth **+ explicit per-action
  confirmation + an append-only audit log**. The HTTP layer is structured (GET-only
  router today) so adding a guarded POST route + an executor is additive.
- **Security requirement (hard):** this service runs on the host with full
  privilege; the action layer is a privileged remote-exec surface. It MUST ship
  with authn/authz, confirmation, audit logging, and an allow-list of actions —
  never a generic shell. This requirement is recorded here so it can't be bolted
  on carelessly.

## 8. Security (v1)

- Read-only (GET-only); no mutation endpoints.
- Bind host LAN IP only; optional bearer token on `/api/*`.
- Secrets (router/Tavily keys) are read at runtime from existing host/LXC env
  files; never written into the dashboard, the JSON API, or the SQLite store.
- Document the privilege footprint (root vs. group membership for `rocm-smi`/`pct`/
  `docker`).

## 9. Testing

- **Checks**: pure functions over injected command output → unit tests with
  recorded `rocm-smi`/`pct`/`curl` fixtures (incl. failure cases: chat down, db
  missing, ceiling drifted, Tavily 503).
- **Store**: insert/read, retention bounding, `last_ok_at` semantics.
- **Alert engine**: transition matrix (ok→fail→ok, cooldown, dedup, stale) against
  a fake notifier.
- **`--once`** smoke run on the host as an acceptance step.

## 10. Out of scope (v1)

- Executing any remediation (read-only only — see §7).
- Time-series beyond a short sparkline window (no Prometheus/Grafana).
- Multi-host / clustering (single Proxmox host).
- Real alert channels (engine + log/no-op sink only; channel deferred by choice).
