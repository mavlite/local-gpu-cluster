# Live inference dashboard — design

**Date:** 2026-10-16
**Status:** approved in conversation, awaiting written-spec review
**Replaces:** the cluster monitor on `http://192.168.6.175:8888/` (`scripts/files/cluster_monitor.py`, `scripts/63-cluster-monitor.sh`, design `docs/cluster-monitor-design.md`)
**Mock-up (approved 2026-10-16):** `.superpowers/brainstorm/*/content/live-dashboard-v1.html` in the lgc-coding-engine worktree (git-ignored). The layout in §3 is the source of truth.

## 1. Purpose

The user codes during the day in opencode against local qwen3.8. They want a Strata-style live view, kept open beside the editor, showing what the GPU server is doing right now: prompt-processing and token-generation speed above all. They also want enough history to tell whether a change made real-world speed better or worse.

**Decisions taken in conversation:**

| Topic | Decision |
|---|---|
| Existing health checks | **Dropped.** This is a live inference view only (the user's choice, made knowing that RAG-freshness, backup and restart-policy warnings go away) |
| History | 15 min of 1 s samples in memory for sparklines, plus **every finished request kept 30 days** in SQLite |
| Access | **Anyone on the LAN, read-only, no token.** Numbers only, never prompt or response text |
| Build | Python **standard library only**, one service on the Proxmox host, same pattern as today. Hand-drawn SVG, no CDN, no build step |
| Layout | The approved mock-up (§3) |

**Success criteria:**
1. The page updates about once a second.
2. A request is visible from the moment a slot picks it up, with prompt progress.
3. Every finished request lands in history with llama-server's own timings.
4. A dead data source greys out its own cards and nothing else.
5. The page never shows prompt or response text, and never exposes a key.

## 2. Architecture

A replacement service on the Proxmox host on port `8888`, bound to the LAN address. It is a small package, not one large file. It lives at `scripts/files/live_dashboard/`, with the page as a separate static file.

| Module | Responsibility | Source | Cadence |
|---|---|---|---|
| `llama_live.py` | Slot busy/idle and generating state, `n_decoded` (output so far), context in use, queue (`requests_deferred`), cumulative counters → rates, MTP acceptance overall and per draft position | chat llama-server `GET /slots` and `GET /metrics` on `http://192.168.6.151:8080` (Bearer key) | 1 s |
| `llama_journal.py` | **Prefill progress** (the `prompt processing … progress=` lines), slot/task start (`launch_slot_`), exact per-request timings (prompt eval, eval, total), draft acceptance, cancels, release, and server restarts | the chat unit's journal: one long-running `pct exec 151 -- journalctl -u llamacpp-chat -f -o cat -n 0`, parsed line by line (it flushes per line) | streaming |
| `gpu.py` | Per V620: busy %, VRAM used/total, junction temperature, power average and cap, PCIe link speed and width; GPU fan duty | host sysfs, cards found by PCI bus address (0000:03:00.0, 0000:07:00.0; card/hwmon numbers drift): `device/gpu_busy_percent`, `mem_info_vram_used/total` (bytes), `hwmon*/temp2_input` (junction, m°C), `power1_average`/`power1_cap` (µW), `current_link_speed/width`. Fans are on the motherboard controller: `pwm5`/`pwm6` (0-255) of the hwmon named `nct6798`. One read of both cards: 8-20 ms | 1 s |
| `host.py` | CPU %, RAM, disk read/write MB/s, ZFS ARC size | `/proc/stat`, `/proc/meminfo`, `/proc/diskstats`, `/proc/spl/kstat/zfs/arcstats` | 1 s |
| `services.py` | Router health (chat/embed/rerank upstream), embed and rerank activity (`n_decode_total` rate), next possible chat restart | router `GET /healthz`; embed `:8082` and rerank `:8083` `/metrics`; restart timer in LXC 151 (`systemctl show llamacpp-chat-restart.timer -p NextElapseUSecRealtime` plus the stamp `/var/lib/llamacpp/last-proactive-restart`). The timer is hourly and idle-gated with a 12 h minimum, so the card shows "next possible restart" = max(stamp + 12 h, next timer fire) | 5 s (timer: 60 s) |
| `escalation.py` | Claude month and day spend, caps, calls left, last call | router `GET /escalation/budget` with the owner key, read on demand from `/etc/router.env` in LXC 153 (as the old Tavily check did) | 30 s |
| `history.py` | SQLite request store; 30-day prune; aggregates (speed by context band, by day) | file `/var/lib/live-dashboard/history.db` | on each finished request, nightly prune |
| `tracker.py` | The request state machine joining `llama_journal` (lifecycle, prefill progress, timings) and `llama_live` (generating detail) by (server run, slot, task) (§4) | — | — |
| `server.py` | HTTP: `/` (page), `/api/stream` (server-sent events, one snapshot per second), `/api/requests?limit=` and `/api/history?days=` (JSON), `/healthz` | — | — |
| `static/index.html` | The page, matching the mock-up; vanilla JS plus inline SVG | — | — |

**Secrets:**
- **llama-server API key:** read from the running chat process's command line through the host's `/proc`, since LXC processes are visible there. It is held only in memory and re-read after a 401.
- **Router owner key:** read through `pct exec` when needed.
- Neither key is ever logged, written to disk, or sent to the browser.

**Config:** `/etc/live-dashboard.json`, with defaults in code:
- URLs and the listen address;
- the GPU PCI ids;
- retention (30 days), sample window (15 min), viewer cap (8).

**Service:** `live-dashboard.service`, run as root because it needs host `/proc` and `pct`. `Restart=always`.

## 3. Page layout (from the approved mock-up)

1. **Header:** one status pill per slot (state, progress or tok/s), the model / build / slot layout pill, and "updated N s ago".
2. **Chat slots panel:** one card per slot (3). For each slot:
   - state tag;
   - progress bar (prompt processed / prompt tokens while reading; output so far while generating);
   - prefill or gen tok/s;
   - cache-reused tokens and %;
   - context fill (`n_ctx` used / 128K).

   The panel also shows the queue depth and which slot is the reserved user lane.
3. **Speed cards, each with a 15-min sparkline:**
   - generation tok/s, with the 15-min average and the bench reference 50.8;
   - prompt processing tok/s, with the 15-min average;
   - cache reuse % over 15 min;
   - MTP draft acceptance, overall and by position.
4. **Hardware row:**
   - **GPU 0 and GPU 1:** load, VRAM, junction temperature, power against the cap, a sparkline, and fan % plus what runs on the card;
   - **Host:** CPU, RAM, disk read, ZFS ARC;
   - **Service pills:** router, embed (req/min), rerank, next chat restart.
5. **Claude escalation:** month ring gauge, today's spend against the cap, calls left per alias, last call.
6. **Speed vs context (7 days):** gen and prefill tok/s by context band (0–8K, 8–32K, 32–64K, 64–96K, 96–128K), from history.
7. **Recent requests table:** time, slot, status (reading / generating / done / slow / interrupted / approx), prompt, reused, output, prefill t/s, gen t/s, duration. "Slow" = prefill of more than 5K uncached tokens, the costly re-prefill the 2026-10-15 measurements found.

**Deferred to a later version** (both need router changes): a thinking % column, and per-client labels (opencode vs AnythingLLM). v1 ships without them.

## 4. Request tracking

The tracker keeps one in-memory record per slot.

The journal drives the lifecycle; `/slots` adds the generating detail. Verified on b11578 (2026-10-16): during prefill `/slots` does not show progress (`n_prompt_tokens_processed` stays 0 and `n_prompt_tokens` shows the batch size until prefill ends), so prefill progress must come from the journal.

- **Request start:** a journal `launch_slot_: id N | task T` line. The tracker opens a row for (slot N, task T): state *reading*, start time.
- **Reading progress:** each `prompt processing … n_tokens=…, progress=…` line for that slot updates the progress bar and the live prefill tok/s.
- **Reading → generating:** at the first `/slots` poll with `n_decoded > 0` for that slot, or at the journal's prefill-complete line. While generating, each poll updates the output count and live tok/s from `n_decoded` deltas. Context in use comes from `n_prompt_tokens` on `/slots`.
- **Finish:** the journal's timing block (prompt eval tokens and ms, eval tokens and ms, total), then `release: id | task | stop processing`. These numbers are authoritative, the row is written to history, and the draft-acceptance line is attached if present.
- **Cancelled:** a `stop: cancel task` line with no timing block before `release`. The row is written as **cancelled**.
- **Approx:** the journal stream is down and the slot is seen going idle on `/slots` (`is_processing` false). The row is written from the 1 s observations and flagged **approx**.
- **Interrupted:** the journal shows a server start (`llama_server: initializing`), or `/slots` stays unreachable over 10 s, while a row is open. The open row is written as **interrupted**.
- **Ids are per server run.** The journal's timestamp prefix (time since server start) and task ids reset at every restart. Rows are keyed by (server run, slot, task); the server run is the wall-clock time the tracker saw that server start.

## 5. Failure handling

- **Isolated sources:** each source module fails independently.
  - The live snapshot carries per-source `ok` / `stale_since`.
  - The page greys out only that source's cards, with "unavailable since HH:MM:SS".
  - A source that stops updating is never shown as live.
- **Chat restarts:** these come from the idle-gated restart timer (at most every 12 h), a profile swap, or a crash. The slots panel shows "chat restarting". The key is re-read after a 401.
- **Journal stream drops:** reconnect with exponential backoff (1 s → 30 s). Meanwhile rows are written as **approx**.
- **Browser stream:** the page's `EventSource` reconnects on its own and shows "reconnecting". The server caps concurrent stream clients at 8; the next client gets HTTP 503 with a retry hint.
- **History:**
  - A single writer thread does all SQLite writes.
  - A nightly prune removes rows older than 30 days.
  - If the database can't be written, the live view keeps working and the page shows a "history paused" banner.
- **Security:**
  - The service binds to the LAN interface only.
  - It is read-only, with no endpoint that changes anything.
  - Responses carry numbers and fixed labels only, with no prompt or response text and no request bodies.
  - Keys stay in memory and out of logs.
  - All request handling is bounded: no unbounded query parameters, `limit` capped at 500, `days` capped at 30.

## 6. Testing

**Unit tests**, using recorded fixtures from the live cluster:
- the `/slots` and `/metrics` parsers;
- journal timing-line parsing for llama.cpp b11578's format;
- the sysfs and `/proc` parsers;
- the tracker state machine (idle → reading → generating → done, interrupted, approx, short request seen only in the journal);
- history aggregation (context bands), 30-day pruning and query caps.

**End-to-end with a fake cluster:** stub llama-server (slots and metrics), a stub journal feed, stub router and budget, and fake sysfs and `/proc` trees. These drive the real service. Tests check `/api/stream` snapshots, request rows, and each failure in §5.

**On the host, before cut-over:**
1. Run the new service on a spare port beside the old one for at least a day.
2. Check its counts and speeds against llama-server's own `/metrics` totals, and against the journal timings for a sample of requests.
3. Then move `:8888` over.

## 7. Rollout and rollback

- A new deploy script, `scripts/64-live-dashboard.sh`, run from a detached worktree on the host:
  1. installs the package and unit;
  2. stops and disables `cluster-monitor.service`, leaving its files in place;
  3. enables `live-dashboard.service` on `:8888`.
- **Rollback:** a generated script stops the new service and re-enables the old one.
- **Removal:** the old monitor's code (`cluster_monitor.py`, its tests, `63-cluster-monitor.sh`, the old design doc's status) is removed in a follow-up commit once the new dashboard has run cleanly for a week.

## 8. Out of scope

- **Health and freshness checks** from the old monitor: Memory Vault, AnythingLLM, MCP, RAG refresh, backups, restart policies, LXC RAM ceilings, the Tavily quota. They are dropped by choice. If silent-failure detection is wanted again, it belongs in a separate, alert-oriented tool, not in this view.
- **Thinking % and per-client attribution** (router changes).
- **Alerts and notifications**, and **actions** of any kind.
- **Monitoring the VCF lab** or the Minisforum hosts.
