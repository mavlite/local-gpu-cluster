# Local GPU Cluster (v2)

**Proxmox VE 9 · ASUS ProArt X870E + Ryzen 7600 · 2× AMD V620 (64 GB pooled VRAM) · llama.cpp + ROCm · AnythingLLM RAG**

![Platform](https://img.shields.io/badge/platform-Proxmox%20VE%209-informational)
![GPU](https://img.shields.io/badge/GPU-2%C3%97%20V620%2032GB-red)
![Runtime](https://img.shields.io/badge/runtime-llama.cpp%20ROCm-000000)
![Model](https://img.shields.io/badge/chat-Qwen3.6--35B--A3B-blue)
![RAG](https://img.shields.io/badge/RAG-AnythingLLM%20%2B%20LanceDB-0a6cf5)

Single-workstation LLM serving stack with all inference (chat + embedding + reranking) on two AMD Radeon Pro V620 cards running ROCm under Proxmox VE 9. Four LXCs separate the inference engine, an auth/admission router, the AnythingLLM RAG UI, and an MCP Docker host.

This is the V620-only revision. An earlier hybrid topology (V620 + RTX 3060) and an older v1 build (Dell T7910 + 3× RTX 3060 with Ollama) are kept as historical reference.

---

## What's deployed

| LXC | Role | Service | IP |
|---|---|---|---|
| 151 `llamacpp-amd` | Inference (privileged for ROCm KFD) | `llamacpp-chat` (8080) + `llamacpp-embed` (8082) + `llamacpp-rerank` (8083) | 192.168.6.151 |
| 153 `llm-router` | FastAPI router (auth, admission, FIM, metrics) | `llm-router` (8000) | 192.168.6.153 |
| 154 `anythingllm` | Docker host running AnythingLLM | `anythingllm` (3001) | 192.168.6.154 |
| 155 `mcp-stack` | Docker host for MCP servers + Python MCP bridge | `mcp-sdg.service` (port 3004) exposes `sdg-documentation` + `vcf-reference` workspaces via SSE; Docker compose stack optional | 192.168.6.155 |
| host | Fan-control bridge driving V620 shroud fans by GPU temp | `v620-fan-bridge.service` | — |

### Models

| Service | Model | Port | Notes |
|---|---|---|---|
| chat | Qwen3.6-35B-A3B (UD-Q4_K_M, ~22 GB) | 8080 | both V620s tensor-split; `--reasoning-format deepseek`, `--jinja`, `--api-key`, thinking-mode-off auto-injected by router for `rag-*` aliases |
| embed | Qwen3-Embedding-0.6B (Q8_0, 1024-dim) | 8082 | V620 #1 pinned; `--pooling last` (CRITICAL — `cls` produces wrong embeddings) |
| rerank | BGE Reranker v2-m3 (Q4_K_M) | 8083 | V620 #2 pinned; `--reranking --pooling rank` |

### Router (LXC 153) features

- Bearer auth on inbound (`ROUTER_API_KEY`) + outbound (`LLAMACPP_API_KEY`)
- `asyncio.Semaphore` admission control (chat=1, embed=4) — chat=1 matches the chat unit's `--parallel 1` single-user policy so multi-agent sub-calls queue at the router (which can emit SSE keepalives) instead of inside llama.cpp
- Per-route token-budget pre-flight via upstream `/tokenize` (rejects oversized inputs with 413; chat cap 200K, embed cap 16K)
- `slowapi` per-IP rate limit (chat 60/min, embed 200/min, Tavily 30/min)
- Prometheus `/metrics` (IP-allowlist gated; default allows host + loopback)
- CORS middleware (`CORS_ALLOW_ORIGINS=*` by default) — required for browser-side clients loaded from `file://` (e.g. the local HTML artifact) to call the router via `fetch()`; OPTIONS preflight bypasses Bearer auth so the preflight succeeds
- SSE keepalive frames every 12 s, plus an immediate `: ping` flushed before upstream connect so clients' read timers don't fire during prompt-processing latency; `MAX_STREAM_SECONDS=900` wall-clock cap prevents a wedged upstream from holding a chat slot forever
- Fail-open degraded mode on upstream 5xx (emits a `service_degraded` SSE frame so AnythingLLM can fall back without breaking the stream)
- Three client-facing chat aliases all resolving to the same backend: `rag-qwen3.6` (thinking off, for AnythingLLM RAG synthesis), `qwen3.6-think` (thinking on, for OpenCode/Cline coding agents), `qwen3.6` (thinking-on convenience alias) — alias controls `chat_template_kwargs.enable_thinking` and whether `<think>...</think>` is regex-stripped from the response
- Strips `[CONTEXT N]` / `(Context 0, 1)` chunk-reference markers from chat output (Qwen3.6 training-prior leak)
- `/v1/completions` passthrough for FIM-style code completion (Continue.dev, Cody)
- `POST /v1/tavily/search` proxy — holds the Tavily API key server-side so browser clients (e.g. the Weekly Customer Adoption Review artifact) can do live web search without ever seeing the key; whitelisted body fields, separate rate limit
- **Server-side tool execution** — when a chat completion request includes `"tool_execution": "server"`, the router runs the OpenAI tools/tool_calls multi-turn loop internally instead of returning `tool_calls` to the client. The model can call any of the registered tools (`tavily_search`, `tavily_extract`, `tavily_crawl`, `tavily_map`, `web_fetch`), the router executes them, feeds results back into the conversation, and returns only the final answer. Browser-side clients get tool-augmented chat without implementing the dispatcher themselves. Default mode is `"client"` (legacy pass-through) so OpenCode/Cline/Continue work unchanged. `MAX_TOOL_ITERATIONS=5` caps the loop; see [`day-2-ops.md` § 6.8](./day-2-ops.md#-68-server-side-tool-execution) for usage and extension
- Qwen3 Embedding compliance: appends `<|endoftext|>` to embedding inputs if missing
- Structured per-request access log at `/var/log/llm-router/access.log` (50 MB rotation, 5 backups) — JSON per line with route/model/tokens/duration/status/client_ip
- Periodic 5-min keepalive timer (separate systemd unit) to keep chat model weights hot in VRAM

### AnythingLLM workspaces

| Workspace | Purpose |
|---|---|
| `vcf-reference` | VMware Cloud Foundation 9.0+ technical reference (Broadcom techdocs — release-notes, deployment, lifecycle, security, licensing) |
| `sdg-documentation` | Self-hosted infrastructure tools (OPNsense, Keycloak, OpenZFS, TrueNAS Scale + TrueNAS API v27) |

Document counts vary per refresh cycle and are managed by the declarative RAG system at [`scripts/rag/`](./scripts/rag/) — see [`scripts/rag/sources.yaml`](./scripts/rag/sources.yaml) for the source list and [`SESSION_HANDOFF.md`](./SESSION_HANDOFF.md) for a recent post-wipe baseline.

Both workspaces tuned for strict factual lookup: `chatMode=query`, `similarityThreshold=0` (rely on rerank for quality filtering), `topN=10` (vcf-reference) / `12` (sdg-documentation), `vectorSearchMode=rerank`, refusal sentinel on no-match. Workspace tuning is applied by [`scripts/57-configure-anythingllm.sh`](./scripts/57-configure-anythingllm.sh) via the AnythingLLM REST API.

The AnythingLLM `/workspace/{slug}` REST endpoint returns ~2 rows per underlying document — a known list-endpoint quirk that's harmless for retrieval but means raw `documents[]` counts are roughly double the actual unique-document count.

---

## Documents

| File | Purpose |
|---|---|
| [`setup-runbook.md`](./setup-runbook.md) | Operational deployment runbook. Phases 1-11 from BIOS to final acceptance suite. The exact *how* of greenfield deployment. |
| [`day-2-ops.md`](./day-2-ops.md) | Day-2 operations guide. What to do *after* the cluster is running: health checks, troubleshooting, model swaps, key rotation, RAG operations, embedder retuning, updates, hardware changes. |
| [`local-gpu-cluster-v2.md`](./local-gpu-cluster-v2.md) | Architecture reference. Hardware rationale, GPU passthrough strategy, LXC vs VM trade-offs. The *why*. |
| [`local-gpu-cluster-reference.md`](./local-gpu-cluster-reference.md) | Historical v1 reference (Dell T7910 + 3× RTX 3060 + Ollama). Kept for context. |
| [`scripts/README.md`](./scripts/README.md) | Bootstrap automation under `scripts/`. Idempotent shell+python scripts for Phases 4-11. |

---

## Quick start

After Proxmox VE 9.x is installed on the host (runbook Phases 1-3):

```bash
cd /root
git clone https://github.com/mavlite/local-gpu-cluster.git
cd local-gpu-cluster/scripts
cp config.env.example config.env
$EDITOR config.env   # at minimum set DATA_NVME_A and DATA_NVME_B (ZFS mirror devices)

./bootstrap.sh --list   # see what will run
./bootstrap.sh          # run all phases end-to-end
```

Or phase-by-phase:

```bash
./bootstrap.sh --only 40   # host config (IOMMU, ZFS, AMDGPU)
./bootstrap.sh --only 51   # V620 LXC + ROCm + llama.cpp + three llama-server units
./bootstrap.sh --only 53   # router LXC
./bootstrap.sh --only 54   # AnythingLLM LXC
./bootstrap.sh --only 55   # MCP stack LXC
./bootstrap.sh --only 56   # fan-control bridge (needs FAN_PWM_PATH discovered via lm-sensors)
./bootstrap.sh --only 57   # AnythingLLM workspace creation via REST API (needs ALLM_API_KEY)
./bootstrap.sh --only 60   # acceptance verification suite
```

---

## Client connection details

For any OpenAI-compatible client (AnythingLLM, OpenCode, Cline, Continue.dev, raw curl):

```
Base URL:     http://192.168.6.153:8000/v1
API Key:      pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env
Model:        rag-qwen3.6
Provider:     OpenAI-compatible
```

For embeddings or reranking, use the same endpoint with model `qwen3-embed` or `bge-rerank`.

---

## Document ingestion tooling

URL-scraping pipelines live in `scripts/files/`:

- `ingest-vcf-urls.sh` — POSTs a URL list to AnythingLLM's `/document/upload-link` endpoint with crawl-delay support
- `ingest-vcf-urls-parallel.sh` — same as above but with per-worker state directories for parallel ingestion
- `recover-long-urls.sh` — sidecar for URLs whose AnythingLLM-derived filename would exceed `NAME_MAX` (255 bytes); uses `trafilatura` to extract clean text then POSTs to `/document/raw-text` with a hash-based short filename

These handle real-world doc ingestion at scale — used to bring in the VCF and OPNsense corpora.

---

## License

CC BY-SA 4.0 (matching the prior reference doc). Hardware/software choices are illustrative — adapt to your environment. Upstream software (Proxmox, ROCm, llama.cpp, AnythingLLM, models) changes quickly; check current project docs before acting on version-specific details.
