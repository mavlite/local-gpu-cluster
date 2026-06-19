# Memory Vault Shared Memory Service — Design Spec

- **Date:** 2026-06-17
- **Status:** Approved (design) — pending implementation plan
- **Scope:** Deploy [MihaiBuilds/memory-vault](https://github.com/MihaiBuilds/memory-vault) as a shared,
  self-hosted persistent-memory service on the local GPU cluster, consumed by both OpenCode and the
  Claude Code client.

## 1. Background

Memory Vault is a self-hosted persistent-memory layer for LLMs (MIT, v1.0 released 2026-05-07,
single maintainer). It stores "memories" in **PostgreSQL 16 + pgvector**, retrieves them with
**hybrid search** (vector similarity + full-text, fused via Reciprocal Rank Fusion), and exposes
`remember` / `recall` / `forget` / `memory_status` as MCP tools plus a full REST API and a web
dashboard. Embeddings (`all-MiniLM-L6-v2`, 384-dim) and spaCy NER run **CPU-only — no GPU, no cloud,
no API keys**.

It is complementary to, not a replacement for, the AnythingLLM RAG corpora: Memory Vault is
*agent working memory* (decisions, context, learnings — short, conversational, high-value), while
the AnythingLLM workspaces are large static reference document sets. The two keep separate vector
stores and must not be merged. (Note: `all-MiniLM-L6-v2` caps at ~256 input tokens, far below the
cluster's 16384-token RAG embedding pipeline — fine for short memories, unsuitable for long docs.)

### The integration problem

The cluster's entire MCP architecture is **remote SSE-over-HTTP** (e.g. `mcp-sdg` on
`http://<LXC-155>:3004/sse`, consumed remotely by OpenCode). Memory Vault's MCP server is
**stdio-only** — designed for a client and server co-located on one machine. Bridging that mismatch
is the central design decision.

## 2. Decisions (locked)

| Decision | Choice |
| --- | --- |
| Transport bridging | **Approach A** — shared LXC service + systemd FastMCP SSE bridge |
| v1 automation scope | **Service first**; manual `remember`/`recall` from both clients. Auto-hooks designed, deferred to phase 2 |
| Memory partitioning | **Per-project namespaces** — one memory space per repo, shared by both clients on that repo |
| Local-llama Claude Code | **First-class deliverable** in this spec |
| Local CC endpoint | **Extend the router** (LXC 153) with an Anthropic `/v1/messages` passthrough — unified admission control |

### Approaches considered

- **A (chosen):** Shared LXC 156 running Memory Vault via `docker compose`, plus a separate systemd
  FastMCP SSE bridge that translates MCP↔REST. Both clients connect as a remote SSE MCP. Smallest
  delta from the proven `mcp-sdg` pattern; embeddings server-side; clients need no local Python/repo.
- **B (rejected):** REST-only with bespoke slash commands — loses the native `remember`/`recall` MCP
  tools and is off-pattern.
- **C (rejected):** Run the upstream stdio MCP on each client machine against a shared Postgres —
  every client must clone the repo, run the Python MCP process, and install sentence-transformers;
  embeddings client-side; violates the LXC-for-all-services model.

## 3. System overview

```
 OpenCode  ─┐                                  ┌─ remote SSE MCP (:3005, ?space=<repo>)
            ├─ memory-vault-bridge (LXC 156) ──┤
 Claude Code┘   FastMCP SSE → REST (bearer)    └─ POST /remember /recall /forget /memory_status
                          │
                          ▼
              Memory Vault app + Postgres16/pgvector  (docker compose, LXC 156)
              dashboard :8000 (LAN-only), data on tank/memory-vault

 Claude Code (local LLM) ── ANTHROPIC_BASE_URL ──▶ router (LXC 153) /v1/messages passthrough
                                                   └─ admission gate ─▶ llama-server (LXC 151) :8080/v1/messages
```

## 4. Components

### 4.1 LXC 156 (`memory-vault`, `192.168.6.156`)
- Unprivileged container, `features=nesting=1,keyctl=1`, Docker CE — same provisioning shape as
  `55-lxc-mcp.sh`.
- Sizing: **4 cores / 8192 MB / 32 GB rootfs**. Justification: Postgres HNSW index builds
  (`maintenance_work_mem` default 1 GB, tune to ~2 GB), the sentence-transformers model, and spaCy
  `en_core_web_sm`, all CPU-resident. No V620/GPU contention.
- Postgres data lives on a new **`tank/memory-vault`** ZFS dataset, bind-mounted into the container
  (mirrors `tank/mcp`), so it is covered by ZFS snapshots and survives container rebuilds.

### 4.2 Memory Vault stack (docker compose)
- Upstream `docker-compose.yml` (app + Postgres 16/pgvector), cloned to `/opt/memory-vault`.
- Dashboard + REST API on `:8000`, **bound to the LAN only**.
- REST **bearer token generated at provision time** (via the project's CLI), stored in
  `/etc/memory-vault.env` (mode 600). Never committed to git.
- DB credentials set via env; Postgres listens only on the container's internal network.

### 4.3 `memory-vault-bridge` (systemd FastMCP SSE service)
- `/opt/memory-vault-bridge/server.py`, listening on **port 3005**, FastMCP over SSE.
- Modeled almost verbatim on `scripts/files/mcp-sdg-server.py` + `scripts/58-mcp-sdg.sh`
  (venv + `mcp>=1.2` + `httpx`, `/etc/...env` mode 600, systemd unit with `Restart=on-failure`).
- **Tools exposed:** `remember(text)`, `recall(query, top_n)`, `forget(chunk_id)`, `memory_status()`.
- **Memory space is scoped per SSE connection** via the `?space=<slug>` query param on the SSE URL.
  The bridge reads the space at connection time, so the tool signatures carry no space argument — the
  connection *is* the scope. A configurable `MEMVAULT_DEFAULT_SPACE` covers connections that omit it.
- Calls the Memory Vault REST API using the bearer token from its env file.

### 4.4 Router enhancement (LXC 153)
- Add an Anthropic-Messages passthrough route to `scripts/files/router-app.py`: accept
  `POST /v1/messages` (and `/v1/messages/count_tokens`), apply the existing `CHAT_CONCURRENCY`
  admission gate, rate limiting, and SSE keepalive, and forward to `V620_URL/v1/messages`.
- llama-server already speaks the Anthropic Messages API natively (llama.cpp PR #17570; cluster build
  b9584 is well past it), so this is a **gated proxy, not a format translator**.
- Redeployed through the existing router provisioning script.

## 5. Client configuration

### OpenCode (`opencode.json`)
```jsonc
"mcp": {
  "memory": { "type": "remote", "url": "http://192.168.6.156:3005/mcp?space=local-gpu-cluster" }
}
```

### Claude Code
- Memory MCP — `.mcp.json` `http` (Streamable HTTP) entry, identical URL/space pattern.
- Local-LLM backend — shipped as a small `.env` / helper template:
```bash
ANTHROPIC_BASE_URL=http://192.168.6.153:8000   # router listens on :8000 (uvicorn, LXC 153)
ANTHROPIC_AUTH_TOKEN=<ROUTER_API_KEY>
ANTHROPIC_MODEL=<llama alias, e.g. rag-qwen3.6>
```
(Router `:8000` is on LXC 153; Memory Vault's dashboard `:8000` is on LXC 156 — different hosts, no conflict.)

### Memory-space convention
Space name = **project/repo slug** (e.g. `local-gpu-cluster`). Pinned per-repo in each client's MCP
URL. Both clients on the same repo share its space; different repos stay isolated.

## 6. Data flow

- **remember:** client → bridge `:3005` (space from URL) → `POST /remember` (bearer) → CPU embed +
  store in Postgres (with spaCy entity extraction).
- **recall:** client → bridge → `POST /recall` → hybrid search (vector + FTS, RRF) → top chunks →
  returned to the model with similarity scores.
- **local CC chat:** Claude Code → router `/v1/messages` → admission gate → llama-server
  `:8080/v1/messages` → streamed response.

## 7. New / changed artifacts

| Artifact | Purpose |
| --- | --- |
| `scripts/61-lxc-memory-vault.sh` | Create LXC 156, `tank/memory-vault` dataset + bind mount, Docker CE, clone repo, generate bearer token, `docker compose up -d` |
| `scripts/62-memory-vault-bridge.sh` | venv + `mcp`/`httpx`, push `server.py`, write `/etc/memory-vault-bridge.env`, install + enable systemd unit (mirrors `58-mcp-sdg.sh`) |
| `scripts/files/memory-vault-bridge.py` | The FastMCP SSE bridge |
| `scripts/files/router-app.py` | Add `/v1/messages` + `/v1/messages/count_tokens` gated passthrough |
| `scripts/config.env.example` | `MEMVAULT_VMID=156`, `MEMVAULT_IP`, sizing vars, `MEMVAULT_BRIDGE_PORT=3005`, `MEMVAULT_DEFAULT_SPACE` |
| Client config templates + `docs` section | OpenCode + Claude Code wiring, local-LLM `.env` |
| `scripts/60-verify.sh` | Add memory-vault smoke checks |
| Nightly backup timer | `pg_dump` → `tank/backups` (systemd timer, matches cluster convention) |

## 8. Security & persistence

- **LAN-only** across the board (`192.168.6.0/24`). Postgres reachable only inside LXC 156.
- REST bearer token in `/etc/memory-vault.env`, mode 600, generated at provision time, never committed.
- The SSE bridge is **unauthenticated on the LAN** — consistent with the existing `mcp-sdg` service.
  Accepted posture for a single-user trusted LAN, not a new gap; revisit if the LAN trust boundary changes.
- **Backups:** nightly `pg_dump` to `tank/backups` via systemd timer; ZFS snapshots cover
  `tank/memory-vault`.

## 9. Verification

- SSE handshake on `:3005` returns the `event: endpoint` line.
- REST `/memory_status` returns healthy stats.
- Full `remember` → `recall` round-trip: store a known chunk, recall it by paraphrase, confirm it
  returns with a sensible score.
- Router `/v1/messages` returns a streamed completion from the local model.
- Dashboard loads on `:8000`.
- Checks added to `scripts/60-verify.sh`.

## 10. Phase 2 (designed, deferred)

Deterministic, model-independent auto-memory driven by harness lifecycle hooks calling the REST API:

- **OpenCode plugin** (TypeScript, `.opencode/plugins/`): `session.created` → `recall` + inject;
  `experimental.session.compacting` → `remember` working state; `session.compacted` → re-inject.
  Pin the OpenCode version — the compaction hook is experimental and its API may shift.
- **Claude Code hooks**: `SessionStart` → `recall`; `PreCompact` → `remember`. Both hit the REST API
  directly so persistence does not depend on model cooperation.

## 11. Out of scope (YAGNI)

- Memory Vault PRO / multi-tenant features.
- Memory Vault's bundled LM Studio chat (the cluster has its own LLMs).
- Heavy reliance on the knowledge-graph / NER features (English-only; not load-bearing here).

**Accepted risk:** Memory Vault is v1.0, single-maintainer, ~6 weeks old. Mitigated by LAN isolation,
nightly backups, and low blast radius (isolated LXC, no dependency from other services).
