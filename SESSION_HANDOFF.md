# Session handoff — latest: 2026-05-26 (chat profiles + RAG automation)

Latest session at the top. Prior sessions preserved below for historical
context. Nothing here assumes you have the prior conversation cached.

---

## 2026-05-26 — Chat-model swap workflow + RAG automation (~22 commits)

A multi-theme session that started with a chat-model swap script and ended with the cluster fully characterized: three production-tuned chat profiles, generalized stability testing, dynamic router model discovery, full RAG automation. All commits pushed to `origin/main`.

### Cluster end-state

**Three chat profiles**, all load-tested under the T3a/T3b runaway-detection methodology (validates buffer behavior across consecutive 80K+ prefills):

| Profile | Quant | Split | Ctx | Cache-reuse | Idle | Peak | Verdict |
|---|---|---|---|---|---|---|---|
| `qwen3.6` (default) | UD-Q4_K_M (~22 GB) | 1,1 | 256K | 1024† | 75 / 44 | 84 / 52 | OK |
| `qwen3.6-hi` | UD-Q5_K_M (~26.5 GB) | 1,1.5 | 256K | 1024† | 73 / 60 | 81 / 68 | OK |
| `coder` | UD-IQ4_XS (~38 GB) | 1,1.5 | 128K | 0 | 84 / 77 | 92 / 85 | OK |

†llama.cpp auto-disables `cache_reuse` for all Q*_K_M + q8_0 KV configs in this build — runtime warning emitted, effective behavior is 0 across the board.

**Universal pattern across all three profiles:** one-time +8pp activation-buffer allocation on first heavy prefill, then bounded (T3b matches T3a). Intrinsic to this llama.cpp build on V620+ROCm, documented in [day-2-ops § 4.4](./day-2-ops.md#-44-vram-budget-template).

**Eight router aliases**, dynamically filtered by `/v1/models` based on currently-loaded profile (only aliases whose `backend` field matches what the chat unit is serving). `/healthz` now exposes `active_chat_profile` for monitoring.

### Daily-use commands

```bash
# Profile swap (auto-detects current, prints VRAM characterization + description before swap)
./scripts/swap-chat-model.sh --status
./scripts/swap-chat-model.sh qwen3.6        # 22 GB Q4, RAG/general default
./scripts/swap-chat-model.sh qwen3.6-hi     # 26.5 GB Q5, deep research / precision
./scripts/swap-chat-model.sh coder          # 38 GB IQ4, coding agent
./scripts/swap-chat-model.sh --follow coder # also stream chat-unit journal during the swap

# Stability test the loaded profile (auto-detects MODEL_ALIAS from chat unit)
./scripts/tools/stability-test-coder.sh --follow

# Capture coder summary to a file (cleaner than scrolling 50K-char terminal):
./scripts/tools/stability-test-coder.sh --follow 2>&1 | tee /tmp/stab.log
sed -n '/-- summary --/,$p' /tmp/stab.log
```

Workstation wrapper (PowerShell, in [`day-2-ops § 4.4`](./day-2-ops.md#-44-vram-budget-template)):
```powershell
function Swap-ClusterChatModel { param([Parameter(Mandatory)][ValidateSet("coder","qwen3.6","qwen3.6-hi","--status")][string]$Target) ssh root@<pve-host> "/root/local-gpu-cluster/scripts/swap-chat-model.sh $Target" }
Set-Alias swap Swap-ClusterChatModel
```

### Commits, grouped by theme

**Initial swap workflow + per-profile tuning (8 commits):**
| Commit | Topic |
|---|---|
| `a809563` | `scripts/swap-chat-model.sh` — atomic profile flip, idempotency, `--force`, `--status` |
| `ec86f3a` | Per-profile `LLAMA_TENSOR_SPLIT` (`qwen3.6=1,1`, `coder=1,1.5`) |
| `c3c10ad` | Doc: measured tuning numbers in `day-2-ops.md § 4.4` |
| `f5bd9f3` | `scripts/tools/stability-test-coder.sh` (three escalating prefills, pass/fail) |
| `77549ae` | Stability test reads chat-unit `--hf-repo` (authoritative) instead of `/v1/models` |
| `8199b81` | Per-profile `LLAMA_CTX` (`qwen3.6=262144`, `coder=131072` to fix 98%->0.6GB headroom) |
| `2fe9589` | Per-profile `LLAMA_CACHE_REUSE` (`coder=0` to work around llama.cpp cache-reuse abort) |
| `71256d8` | Doc: final measured values for coder profile |

**Doc audit + currency fixes (2 commits):**
| `07517a5` | Top-level README + scripts/README + SESSION_HANDOFF + scripts/tools/README updated for the swap workflow |
| `eba2c0d` | Audit-driven currency fixes (PowerShell ValidateSet, --follow doc, dynamic /v1/models doc, setup-runbook cross-ref) |

**Third profile + rebalance (4 commits):**
| `ccedb6e` | New `qwen3.6-hi` profile (UD-Q5_K_M, ~26.5 GB) wired up across swap script + router ALIAS_MAP + docs |
| `2b22f86` | qwen3.6-hi initial VRAM measurement (1,1 split -> 82/51) |
| `81908ec` | Rebalanced qwen3.6-hi to 1,1.5 split (predicted 73/60) |
| `5f7df91` | Locked in 1,1.5 split + measured exactly matches prediction (73/60) |

**UX improvements (2 commits):**
| `9194185` | `--follow` flag on both swap-chat-model.sh and stability-test-coder.sh; dynamic `/v1/models` filters by loaded profile |
| `6c4ad9c` | `--follow` rewrite (cursor approach was fragile; switched to `--since` timestamp + line count); `identify_profile` matches on repo+quant not just repo (fixes `qwen3.6-hi -> qwen3.6` misreporting) |

**Stability test refinements (3 commits):**
| `9f5aa0f` | T1+T2 bundle: generalized stability test (auto-detect MODEL_ALIAS), threshold sanity (3x->5x slowdown, post-settle remeasure), swap UX (descriptions + VRAM characterization in `--status`), `/healthz` adds `active_chat_profile` |
| `949cce9` | Distinguish bounded one-time buffers (info) from runaway drift (fail) |
| `c3bac2f` | T3a+T3b pair for proper runaway detection (was comparing T2->T3 which conflated first-allocation with compounding) |

**Full profile validation (1 commit):**
| `326b401` | All three profiles load-tested with T3a/T3b methodology, measurements locked in |

**Tier 3 backlog (2 commits):**
| `9f56d09` | Spec-decode HF re-check (MTP variant found — see deferred item below) + `--approve <proposal>` workflow with drift detection + archive |
| `9acf8d8` | RAG Phase 3 — `58-rag-refresh-timer.sh` provisions daily systemd timer + Prometheus textfile metrics |

### Tooling shipped this session

| Path | Purpose |
|---|---|
| [`scripts/swap-chat-model.sh`](./scripts/swap-chat-model.sh) | Atomic profile flip between `qwen3.6` / `qwen3.6-hi` / `coder`. `--status`, `--force`, `--follow`. Auto-warns about notable llama.cpp warnings after each swap. |
| [`scripts/tools/stability-test-coder.sh`](./scripts/tools/stability-test-coder.sh) | Generalized load test (auto-detects active profile). T1/T2/T3a/T3b methodology. `--follow`, `--model`, `--keep-tmp`. |
| [`scripts/58-rag-refresh-timer.sh`](./scripts/58-rag-refresh-timer.sh) | Provisions systemd timer for daily RAG refresh + Prometheus textfile metrics at `/var/lib/rag-refresh/metrics.prom`. |
| [`scripts/rag/refresh.py --approve PROPOSAL`](./scripts/rag/refresh.py) | Apply a halted safety proposal. Drift-aware (halts again with `-drift.json` if source changed); archives to `_proposals/applied/` on success. |

### Discovered upstream bugs / behavior

1. **llama.cpp `cache_reuse` auto-disable is universal** (not Coder-Next-specific). Every Q*_K_M + q8_0 KV config trips `cache_reuse is not supported by this context, it will be disabled` at load. Our setting `CACHE_REUSE=1024` on `qwen3.6` and `qwen3.6-hi` has been a no-op the whole time.
2. **Activation-buffer allocation is bounded but persistent.** First heavy prefill (~80K+ tokens) allocates ~+8pp VRAM that doesn't release post-request. Second consecutive heavy prefill stays at the same level — bounded, not runaway. Verified by T3a vs T3b delta = 0pp on all three profiles.
3. **llama.cpp cache-reuse abort** on Coder-Next exact-match cached prompt replay. `common.cpp:1489: failed to remove sequence ... p1=-1` -> `status=6/ABRT`. Worked around per-profile (`coder` has `LLAMA_CACHE_REUSE=0`). Worth reporting upstream once minimal repro is built.

### Tier 3 deferred items (full implementation outlines preserved)

- **MTP (Multi-Token Prediction) integration** — HF probe surfaced `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` (updated 2026-05-20), an MTP-trained variant of our chat model. ~1.5-2x faster inference claimed, drop-in tokenizer-compatible. Implementation outline in [day-2-ops § 4.5](./day-2-ops.md#-45-enabling-spec-decode-when-a-compatible-draft-ships). Wiring needs: `51-lxc-amd.sh` to learn `--spec-type draft-mtp`, new profile field for spec type, ~22.7 GB download, re-stability-test. ~half-day work.
- **`hugo_sitemap` / `url_list_hashed` handler stubs** — intentionally not built (no current source needs them; YAGNI). Implement when a real source requires Last-Modified caching or hand-curated URL lists.
- **Monitoring + alerting** — `/var/lib/rag-refresh/metrics.prom` is now ready for a Prometheus scraper, but node_exporter isn't deployed. Carry-over from 2026-05-24's list of standing items.
- **Security hardening checklist** — carry-over from 2026-05-24.
- **`scripts/tools/benchmark-coder-vs-rag.py`** — still broken (HF Inference Providers auth blocks it). Either provision a paid HF token or delete the script.

### Repo state snapshot (2026-05-26)

**Branch:** `main`. **Tracking:** `origin/main`. **HEAD:** `9acf8d8`.

Latest 10 commits:
```
9acf8d8  T3: RAG Phase 3 — scheduled refresh + Prometheus textfile metrics
9f56d09  T3: spec-decode HF re-check + --approve workflow
326b401  all three profiles load-tested + measurements locked in
c3bac2f  stability test: T3a + T3b pair for proper runaway-buffer detection
949cce9  stability test: distinguish bounded one-time buffers from runaway drift
9f5aa0f  T1 + T2: stability test generalization, threshold sanity, swap UX, /healthz
eba2c0d  docs: audit-driven currency fixes for the swap workflow
5f7df91  qwen3.6-hi: lock in 1,1.5 split with measured numbers
81908ec  qwen3.6-hi: rebalance tensor-split 1,1 -> 1,1.5
2b22f86  day-2-ops § 4.4: record qwen3.6-hi initial VRAM measurement
```

Working tree clean at session end. No uncommitted changes.

### How to verify the cluster on a new PC

```bash
# 0. SSH to the Proxmox host
ssh gpu-cluster

# 1. Pull the latest
cd /root/local-gpu-cluster && git pull

# 2. Cluster health
./scripts/swap-chat-model.sh --status
pct exec 151 -- systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank
pct exec 153 -- systemctl is-active llm-router
pct exec 154 -- docker ps | grep anythingllm

# 3. Router /healthz now includes active_chat_profile
curl -s http://192.168.6.153:8000/healthz | jq

# 4. /v1/models is profile-aware
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -sf -H "Authorization: Bearer $ROUTER_KEY" http://192.168.6.153:8000/v1/models | jq -r '.data[].id'

# 5. If phase 58 was deployed, check the RAG refresh timer
systemctl list-timers rag-refresh.timer --no-pager 2>/dev/null
cat /var/lib/rag-refresh/metrics.prom 2>/dev/null
```

If phase 58 hasn't been deployed yet:
```bash
./scripts/58-rag-refresh-timer.sh
```

---

# Session handoff — 2026-05-24 (prior session, kept for context)

Continuation notes for picking this work up on another PC. Read top-to-bottom;
nothing in here assumes you have the prior conversation cached.

---

## TL;DR (2026-05-24)

- **GPU cluster:** running, stable on Qwen3.6-35B-A3B. CORS works. Tavily proxy live.
- **RAG Phase 2** (openzfs / blog split / keycloak fixes) — resolved 2026-05-25 (commit 4f2b30e); full sdg-documentation wipe-loop baseline clean across all 5 sources.
- **Docs refresh complete (2026-05-25, commits 5d3ab61 + cf4f277):** all docs aligned with live cluster state; new [`day-2-ops.md`](./day-2-ops.md) operations guide (14 sections, 1543 lines).
- **All previously-deferred decisions resolved (2026-05-25):** (1) Coder-Next take 2 -> `UD-IQ4_XS` (38.4 GB) recommended in [day-2-ops.md § 4.4 worked example](./day-2-ops.md#-44-vram-budget-template); (2) router-side tool execution shipped in commits e79add8 (v1) + af935c4 (v2 fixes). Server-side OpenAI tools/tool_calls multi-turn loop with 5 tools (tavily_search / extract / crawl / map + web_fetch), end-to-end-verified — model executed 3 iterations and returned a cited answer about TrueNAS 26-BETA.1.

---

## Cluster — what's live right now

| LXC | Role | Model / process | Notes |
|---|---|---|---|
| 151 (`llamacpp-amd`) | chat / embed / rerank | `Qwen3.6-35B-A3B-UD-Q4_K_M` (alias `rag-qwen3.6`), `Qwen3-Embedding-0.6B-Q8_0`, `bge-reranker-v2-m3-Q4_K_M` | 32 GB LXC mem, `--mlock` on, 256K ctx, `--parallel 1`. Model files in HF cache layout under `/tank/models/.cache/` (host) -> `/opt/models/.cache/` (LXC bind mount). |
| 153 (`llm-router`) | FastAPI router on :8000 | `/opt/llm-router/app.py` (uvicorn) | Bearer auth, CORS middleware, Tavily proxy at `/v1/tavily/search`, slowapi rate limits, Prometheus middleware. |
| 154 (`anythingllm`) | AnythingLLM container | Docker compose at `/opt/anythingllm/docker-compose.yml` | Talks to router at `http://192.168.6.153:8000/v1` using `ROUTER_API_KEY` Bearer. Model alias = `rag-qwen3.6`. Token limit 131072. |
| 155 (`mcp-stack`) | MCP servers | Not heavily used this session | Same provisioning script `55-lxc-mcp.sh`. |

**Endpoints exposed at `http://192.168.6.153:8000`:**
- `POST /v1/chat/completions` — chat
- `POST /v1/embeddings` — embed
- `POST /v1/rerank` — rerank
- `POST /v1/tavily/search` — Tavily search proxy (NEW this session)
- `GET /v1/models` — model list (`rag-qwen3.6`, `qwen3.6-think`, `qwen3.6`, `qwen3-embed`, `bge-rerank`)
- `GET /healthz` — unauthed health probe
- `GET /metrics` — Prometheus (IP-allowlist gated)

**Secrets you need to know exist (DO NOT commit; they live ONLY on the LXCs):**

| Var | Where | Generated by |
|---|---|---|
| `ROUTER_API_KEY` | `/etc/router.env` on LXC 153 (mode 600) | `scripts/53-lxc-router.sh` (openssl rand -hex 32) |
| `LLAMACPP_API_KEY` | `/etc/llamacpp.env` on LXC 151 + mirrored to `/etc/router.env` on 153 | `scripts/51-lxc-amd.sh` |
| `TAVILY_API_KEY` | `/etc/router.env` on LXC 153 + `scripts/config.env` on Proxmox host | You pasted it into `config.env` then ran 53-lxc-router.sh |
| `ALLM_API_KEY` | AnythingLLM UI -> Settings -> API Keys (after first signup) | AnythingLLM itself |

To recover the keys on the new PC's terminal sessions:
```bash
# On Proxmox host (ssh to gpu-cluster)
pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env
pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env
pct exec 153 -- awk -F= '/^TAVILY_API_KEY=/{print $2}' /etc/router.env
```

---

## Repo state

**Branch:** `main`. **Tracking:** `origin/main` (github-lgc:mavlite/local-gpu-cluster).

**Recent commits (newest first):**
```
83b2d95 router updates                                      <-- latest pushed
13cea45 fixing router
0c032e4 rolling back to qwen3.6
461b9d4 changing models                                     <-- failed swap to Coder-Next
657aa66 rag: heartbeat during embedding pass
b99e042 rag: harden refresh.py against interruption
...
```

Recommend adding to `.gitignore`:
- `scripts/config.env` (contains `TAVILY_API_KEY` on the Proxmox host)
- Any `*.env` you might generate

---

## Cluster / RAG — work done this session

### CORS fix (committed: 83b2d95 / 13cea45)

- Added `CORSMiddleware` to `scripts/files/router-app.py` with configurable `CORS_ALLOW_ORIGINS` (default `*`).
- Fixed auth middleware: now skips `OPTIONS` requests so CORS preflight from `file://` origins gets answered before auth runs.
- Wired through `scripts/53-lxc-router.sh` -> persists in `/etc/router.env`.
- Verified: browsers loaded from `file://` can now POST to `http://192.168.6.153:8000` without CORS errors.

### Tavily search proxy (committed: 83b2d95)

- New endpoint `POST /v1/tavily/search` in `scripts/files/router-app.py`.
- Server holds the Tavily key (in `/etc/router.env`), so browser-side clients never see it. Bearer auth still required.
- Whitelisted request fields. Defaults block `raw_content`/`images` to keep responses small.
- Rate-limited separately: `RATE_LIMIT_TAVILY=30/minute`.

### Model swap attempt + rollback (committed: 461b9d4 -> 0c032e4)

- **Tried:** swap chat model from Qwen3.6-35B-A3B (22 GB) to Qwen3-Coder-Next UD-Q4_K_XL (49.6 GB).
- **What happened:** OOM on V620 #0 at weight allocation (24.2 GB single alloc failed). Model is too big for 2x V620 (32 GB each, 64 GB total) when embed+rerank are co-resident.
- **Decision:** rolled back to Qwen3.6-35B. Scripts reverted. Cluster running stable since.
- **Lesson saved:** future Coder-Next attempt needs proper VRAM-budget exercise BEFORE downloading the 50 GB GGUF.

### Documentation fixes (committed in 83b2d95)

- `scripts/config.env.example`: corrected "read-only mount" to "read-write bind mount" (the actual usage needs RW for HF cache).
- `scripts/README.md`: added "Where models live" subsection documenting `/tank/models/` <-> `/opt/models/` bind-mount and HF cache layout.

### RAG Phase 2 — RESOLVED 2026-05-25 (commit 4f2b30e)

All three diagnosed issues fixed plus full sdg-documentation wipe-loop completed.

1. **openzfs-docs `/en/` URL shape** — wiped state, `refresh.py --force` yielded 76 clean ADDs. The expected 59 orphans had already been cleaned in an earlier session (verified: 0 docs with `/en/` in sourceURL).
2. **truenas-scale-docs blog pollution** — new `scripts/rag/split_truenas_blog.py` migrated 595 blog URLs into a parked `truenas-blog` source (handler: rss, enabled: false) preserving `allm_doc_path`. Source declared in `sources.yaml`; stays disabled until the rss handler stub is implemented.
3. **keycloak handler bugs** — (a) `file_exclude_regex` updated to `(?:[^/]+/)?topics` to catch section-level topics partials; (b) `github_repo.py` `collect()` rewritten as gather-then-emit, merging content when multiple source files collapse to one citation URL (adds `merged_count` to metadata when merging).

Post-wipe baseline (all sdg-documentation sources):

| Source | state docs | workspace match | dup× | orphans |
|---|---|---|---|---|
| keycloak-docs | 6 | 12 | 2.00 | 0 |
| openzfs-docs | 76 | 150 | 1.97 | 0 |
| truenas-api-v27 | 1079 | 2159 | 2.00 | 0 |
| opnsense-docs | 418 | 798 | 1.91 | 0 |
| truenas-scale-docs | 452 | 904 | 2.00 | 0 |

One stale `raw-recovered-` sidecar from 2026-05-20 was swept from truenas-api-v27 separately. Consistent ~2.0x workspace-list dup factor confirms an AnythingLLM `/workspace/{slug}` quirk (two rows per document) — non-blocking, known.

---

## How to resume on another PC

1. **Clone the repo** (already up to date with `origin/main`):
   ```bash
   git clone <your-github-remote> local-gpu-cluster
   cd local-gpu-cluster
   ```
2. **Verify cluster health** from the new PC:
   ```bash
   ssh gpu-cluster   # or wherever your Proxmox host is reachable
   pct exec 153 -- systemctl status llm-router
   pct exec 151 -- systemctl status llamacpp-chat llamacpp-embed llamacpp-rerank
   pct exec 154 -- docker ps
   ```
3. **Read the open items** below. Pick what to resume.

---

## Open items (prioritized)

### Soon (RAG Phase 2) — DONE 2026-05-25

All five items shipped in commit 4f2b30e and the full sdg-documentation workspace was wipe-rebuilt clean. See the "RAG Phase 2 — RESOLVED" section above for the post-wipe baseline.

### Later (separate planning sessions)

- [x] Coder-Next take 2 — **analysis done 2026-05-25, deploy deferred**. Analysis added as worked example in [day-2-ops.md § 4.4](./day-2-ops.md#-44-vram-budget-template). Recommendation if/when deployed: `UD-IQ4_XS` (38.4 GB) — Q4-class Unsloth Dynamic quant, fits with ~10 GB per-card headroom alongside embed+rerank co-residency at 256K context. Backup: UD-IQ4_NL (39.2 GB) or more conservative UD-Q3_K_S (33.3 GB) if quality issues. **Caveat:** Coder-Next replaces Qwen3.6-35B-A3B in the chat slot; can't run both simultaneously. **Deploy decision (2026-05-25): staying with Qwen3.6** — empirical benchmark via [`scripts/tools/benchmark-coder-vs-rag.py`](./scripts/tools/benchmark-coder-vs-rag.py) (commit d534311) attempted but blocked on HF Inference Providers auth (token rejected with 401 even on whoami); rather than burn more time on the benchmark or commit local VRAM blind, keeping the current Qwen3.6-35B-A3B as the chat model. Router has `qwen3-coder` + `qwen3-coder-next` aliases pre-configured in [`scripts/files/router-app.py`](./scripts/files/router-app.py) `ALIAS_MAP` (commit 0c44407) so future deploy is one-command swap per day-2-ops § 4.4. The failed UD-Q4_K_XL (49.6 GB) cache directory has been removed from `/tank/models/.cache/`; ~50 GB reclaimed. To deploy later: re-fetch via the day-2-ops procedure, run `scripts/51-lxc-amd.sh` + `scripts/53-lxc-router.sh`.
- [x] `setup-runbook.md` Phase 5 rewrite + `local-gpu-cluster-v2.md` model-section rewrite — **done 2026-05-25** in commit 5d3ab61 (5-file refresh against ground-truth). Day-2-ops follow-on doc added in cf4f277.
- [x] Router-side tool execution — **done 2026-05-25** in commits e79add8 (v1: tool registry + 5 tools + multi-turn loop) and af935c4 (v2 fixes: Tavily results -> markdown, tool_call_id synthesis, MAX_TOOL_ITERATIONS 5->10, log enhancement). Verified end-to-end: model executed `tavily_search x2 + tavily_extract x1` in 3 iterations, produced cited answer about TrueNAS 26-BETA.1. Pre-existing `/v1/tavily/search` proxy retained for browser-side direct use; clients can opt in to server-side execution per-request via `"tool_execution": "server"`. See [day-2-ops.md § 6.8](./day-2-ops.md#-68-server-side-tool-execution).
- [ ] **Monitoring & alerting setup** — surfaced by the day-2-ops review (cf4f277). The router exposes `/metrics` (Prometheus, IP-allowlisted) but there's no scrape config, no alert rules, no dashboards. Scope: Prometheus scrape configuration for the router endpoint, suggested alert thresholds (chat p95 latency, 5xx rate, embed queue depth, /tank disk %, fan-bridge down), and integration patterns with an external receiver (Grafana / AlertManager / ntfy / Discord webhook). Day-2-ops.md §2 + §12 cover reading existing metrics manually; this item is about closing the loop into proactive alerting.
- [ ] **Security hardening checklist** — surfaced by the day-2-ops review (cf4f277). Current posture is Bearer auth on the router + IP-allowlists on `/metrics`, assuming LAN-only deployment behind a firewall. Scope for hardening: per-LXC SSH audit (key-only, non-default port, hardened sshd_config), fail2ban or equivalent against brute-force, audit logging strategy (who-did-what beyond access.log), secrets-at-rest review (file modes on /etc/router.env etc.), TLS termination if any service ever needs to be exposed beyond LAN. Becomes more important if the cluster's network exposure changes.

### Standing items (no urgent action)

- [x] Implement Phase 2 RSS handler — **done 2026-05-25** in commit 57adec5. Handler at [`scripts/rag/handlers/rss.py`](./scripts/rag/handlers/rss.py); feedparser + trafilatura pipeline; new top-level `removal_policy: additive_only` source field (in [`scripts/rag/lib/plan.py`](./scripts/rag/lib/plan.py) + [`scripts/rag/refresh.py`](./scripts/rag/refresh.py)) prevents the diff layer from deleting historical entries that fall out of an RSS feed's sliding window. **truenas-blog itself stays parked**: TrueNAS / iXsystems publish no public feed (probed `/feed`, `/blog/feed`, `/blog/index.xml`, `/sitemap.xml`, all variants — either 404 or 301 -> SPA-fallback HTML). 595 historical entries remain searchable through the sdg-documentation workspace. Handler is reusable for any future source that DOES have a feed (homenetworkguy, klarasystems, ServeTheHome, Phase Two community blogs) — just add a new sources.yaml entry with `handler: rss` and `removal_policy: additive_only`. For fresh TrueNAS blog content specifically, the server-side tool execution route ([day-2-ops § 6.8](./day-2-ops.md#-68-server-side-tool-execution)) covers live web search via `tavily_search`.
- [ ] Implement Phase 2 split: `sphinx_sitemap` collect()/fetch() so `--dry-run` isn't expensive
- [ ] migrate_backfill: extract_url shape fix for refresh.py-uploaded docs
- [ ] cleanup script: batch-size handling (1800s timeout on 409 removes)
- [ ] opnsense-docs: some workspace duplicates exist (non-blocking, acceptable)
- [ ] TrueNAS audit script: ground-truth on TrueNAS box (script lives in scratch / not committed yet)
- [ ] Sync LESSONS.md + RULES.md from repo to `~/.config/opencode/` on new PC so OpenCode picks up the updated rules

---

## Things you might forget

- **Cluster context window** is 256K (Qwen3.6 n_ctx_train), with `--parallel 1` and `CHAT_CONCURRENCY=1` so a single request gets the full window. Router enforces `MAX_CHAT_INPUT_TOKENS=200000` (leaves ~56K for output/thinking).
- **OpenCode config** is `~/.config/opencode/config.json` and includes Tavily MCP. That key is also what's in `/etc/router.env` (TAVILY_API_KEY) — they should match.
- **Router models endpoint** returns 5 aliases that all resolve to the same llama-server backend with different behaviors: `rag-qwen3.6` strips `<think>` blocks via heuristic, `qwen3.6-think` keeps them in `reasoning_content`, `qwen3.6` is non-thinking. Pick the right alias for the use case.
- **OpenCode reasoning support** is independent of the router. OpenCode handles `<think>` blocks based on its own config; the `rag-qwen3.6` alias on the router is best for RAG queries (no thinking) and `qwen3.6-think` is best for coding/agent tasks.
- **vzdump backups** of LXCs go to `/tank/backups/` (or wherever `vzdump` is configured). Run periodically — both the chat model setup and the router config are non-trivial to rebuild from scratch.

---

## Quick file map

| File | Purpose |
|---|---|
| `scripts/files/router-app.py` | FastAPI router. Auth, CORS, admission control, Tavily proxy, /v1/{chat,embed,rerank,models,tavily/search}. |
| `scripts/51-lxc-amd.sh` | Provisions LXC 151 (chat/embed/rerank llama-server units). |
| `scripts/53-lxc-router.sh` | Provisions LXC 153 (router). Wires API keys and env into `/etc/router.env`. |
| `scripts/54-lxc-anythingllm.sh` | Provisions LXC 154 (AnythingLLM). |
| `scripts/config.env.example` | Template for `scripts/config.env` (the real one is gitignored on host). |
| `scripts/rag/` | RAG corpus refresh system (Phase 1 shipped, Phase 2 paused). |
| `scripts/rag/sources.yaml` | Declarative source list. 5 sources declared. |
| `setup-runbook.md` | Full deployment guide. Has 12+ stale references to Qwen3.6 model details — needs rewrite eventually. |
| `local-gpu-cluster-v2.md` | Architecture doc. Same staleness. |
| `LESSONS.md`, `RULES.md`, `TASK-LOOP.md` | Operating-rules docs (uncommitted earlier in session; check if you need to push). |
| `SESSION_HANDOFF.md` | This file. |

---

*End of handoff. If something here is wrong or missing, that's a bug in the recorded notes — open the file on the original PC and diff against this doc.*
