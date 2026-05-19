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
| 155 `mcp-stack` | Docker host for MCP servers | (empty, drop in compose files) | 192.168.6.155 |
| host | Fan-control bridge driving V620 shroud fans by GPU temp | `v620-fan-bridge.service` | — |

### Models

| Service | Model | Port | Notes |
|---|---|---|---|
| chat | Qwen3.6-35B-A3B (UD-Q4_K_M, ~22 GB) | 8080 | both V620s tensor-split; `--reasoning-format deepseek`, `--jinja`, `--api-key`, thinking-mode-off auto-injected by router for `rag-*` aliases |
| embed | Qwen3-Embedding-0.6B (Q8_0, 1024-dim) | 8082 | V620 #1 pinned; `--pooling last` (CRITICAL — `cls` produces wrong embeddings) |
| rerank | BGE Reranker v2-m3 (Q4_K_M) | 8083 | V620 #2 pinned; `--reranking --pooling rank` |

### Router (LXC 153) features

- Bearer auth on inbound + outbound
- `asyncio.Semaphore` admission control (chat=2, embed=4)
- Per-route token-budget pre-flight via upstream `/tokenize` (rejects oversized inputs with 413)
- `slowapi` per-IP rate limit
- Prometheus `/metrics` (IP-allowlist gated)
- SSE keepalive frames every 12 s
- Fail-open degraded mode on upstream 5xx
- Auto-injects `chat_template_kwargs.enable_thinking=false` for `rag-*` model aliases (Qwen3.6 leaks chain-of-thought as plain content otherwise)
- Strips `[CONTEXT N]` / `(Context 0, 1)` chunk-reference markers from chat output
- `/v1/completions` passthrough for FIM-style code completion
- Structured per-request access log at `/var/log/llm-router/access.log` (rotated)
- Periodic 5-min keepalive timer to keep chat model weights hot

### AnythingLLM workspaces

| Workspace | Purpose | Docs |
|---|---|---|
| `vcf-reference` | VMware Cloud Foundation 9.0+ technical reference | 564 docs (release-notes, deployment, lifecycle, security, licensing) |
| `sdg-documentation` | Self-hosted infrastructure tools | 413 docs (OPNsense; future: Keycloak) |

Both tuned for strict factual lookup: `chatMode=query`, `similarityThreshold=0.4`, `topN=12`, `vectorSearchMode=rerank`, refusal sentinel on no-match.

---

## Documents

| File | Purpose |
|---|---|
| [`setup-runbook.md`](./setup-runbook.md) | Operational deployment runbook. Phases 1-11 from BIOS to final acceptance suite. The exact *how*. |
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
