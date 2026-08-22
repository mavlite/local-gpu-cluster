# Bootstrap scripts for local-gpu-cluster

Idempotent automation of Phases 4–11 of [`setup-runbook.md`](../setup-runbook.md), plus
post-deployment phases 11.5–12.5. Each script is independently runnable and may be invoked through
the top-level orchestrator.

Once the cluster is deployed, see [`day-2-ops.md`](../day-2-ops.md) for operational
procedures (health checks, model swaps, key rotation, RAG refresh, embedder
retuning, updates, hardware changes).

| Phase | Script                  | Covers                                                     |
| ----- | ----------------------- | ---------------------------------------------------------- |
| 4     | `40-host-config.sh`     | IOMMU, AMD firmware, ZFS mirror, LXC template, THP disable (V620-only — no NVIDIA driver, no kernel pin) |
| 5     | `51-lxc-amd.sh`         | V620 LXC, ROCm, llama.cpp HIP, **three systemd units** (chat/embed/rerank), API key, warm-up, SSH harden |
| 6     | (removed)               | Old `52-lxc-nv.sh` (3060 LXC) was deleted in the V620-only pivot. |
| 6.5   | `52-swap-webhook.sh`    | Host systemd service (port 9100) letting the router trigger profile swaps; generates `SWAP_WEBHOOK_KEY` and wires `SWAP_WEBHOOK_URL` into LXC 153. Allowlist is parsed from `swap-chat-model.sh` `PROFILE_NAMES`, never executed |
| 7     | `53-lxc-router.sh`      | Router LXC, FastAPI app, API key gen, EnvironmentFile with admission control + rate limit + metrics IPs + Tavily proxy + CORS |
| 8     | `54-lxc-anythingllm.sh` | AnythingLLM LXC, Docker, compose stack                     |
| 9     | `55-lxc-mcp.sh`         | MCP stack LXC, Docker, optional rsync from previous host   |
| 5.13  | `56-fan-control.sh`     | Host PWM bridge (needs `FAN_PWM_PATH` discovered manually) |
| 10    | `57-configure-anythingllm.sh` | Create + tune RAG workspaces via REST API (needs `ALLM_API_KEY`) |
| 10.5  | `58-rag-refresh-timer.sh` | Systemd timer (daily 03:15 UTC by default) running `scripts/rag/refresh.py` on the PVE host via `/opt/vcf-scraper-venv`. Emits Prometheus textfile metrics to `/var/lib/rag-refresh/metrics.prom` after each run. |
| 10.7  | `58-mcp-sdg.sh`         | `mcp-sdg.service` in LXC 155 (port 3004, SSE) — exposes the `sdg-documentation` + `vcf-reference` AnythingLLM workspaces as `query_*`/`search_*` MCP tool pairs. Python venv + mcp SDK (`mcp>=1.2,<2` — see note below) |
| 10.8  | `59-llamacpp-restart-timer.sh` | Systemd timer in LXC 151 that periodically restarts `llamacpp-chat` (keeps weights hot / recovers a wedged unit) |
| 11    | `60-verify.sh`          | Appendix C smoke tests                                     |
| 11.5  | `61-lxc-memory-vault.sh` | Memory Vault LXC 156, Docker, ZFS dataset, docker compose stack with override config |
| 12    | `62-memory-vault-bridge.sh` | MCP **Streamable HTTP** bridge in LXC 156, mounted at `/mcp` on port 3005. Python venv + mcp SDK (`mcp>=1.2,<2` — see note below) + uvicorn + starlette, systemd unit. Uses the SDK's low-level `Server` + `StreamableHTTPSessionManager`, **not** FastMCP |
| 12.5  | `63-cluster-monitor.sh` | Read-only cluster health + metrics dashboard (host systemd service, port 8888), SQLite state, Python 3 stdlib only |
| 12.6  | `64-memory-vault-backup-timer.sh` | Host systemd timer (daily 02:30) running pg_dump backup inside LXC 156 via `pct exec`; dumps to tank-backed dir, retains 14 |

Phases 1–3 (hardware, BIOS, PVE ISO install) are not automatable; follow the
runbook for those.

> **MCP SDK pin (load-bearing).** Both MCP venvs install `'mcp>=1.2,<2'`. **mcp 2.0.0
> removed `mcp.server.fastmcp`**, which `mcp-sdg` imports — an unbounded `mcp>=1.2` took
> LXC 155 down from 2026-08-21 13:53 UTC to 2026-08-22 (8,677 crash-loop restarts, nothing
> listening on 3004). The unit reports `activating`, never `failed`, so `systemctl is-failed`
> misses it; probe the port. Do not relax the `<2` bound without porting `mcp-sdg-server.py`
> off FastMCP.

### Runtime tools (not phases — invoked on demand after deployment)

| Script | Purpose |
|---|---|
| [`swap-chat-model.sh`](./swap-chat-model.sh) | Atomically flip the chat unit between profiles (e.g., `qwen3.6` ↔ `coder`). Rewrites `LLAMA_HF_*` + `LLAMA_TENSOR_SPLIT` + `LLAMA_CTX` + `LLAMA_CACHE_REUSE` in `config.env`, re-runs `51-lxc-amd.sh`, restarts `llamacpp-chat`, waits for active. See [day-2-ops § 4.4](../day-2-ops.md#-44-vram-budget-template). |
| [`tools/stability-test-coder.sh`](./tools/stability-test-coder.sh) | Three escalating chat-completions requests (~2K → ~30K → ~100K input tokens) through the router; snapshots `rocm-smi` between each; scans `journalctl` for errors; reports peak VRAM, drift, latency, throughput. Re-run after any coder-profile tuning change. |
| [`tools/`](./tools/) | Document ingestion tools (`ingest-urls.sh`, `recover-long-urls.sh`, `ingest-github-repo.sh`, `clear-workspace.sh`, etc.). See [`tools/README.md`](./tools/README.md). |
| [`rag/refresh.py`](./rag/) | Declarative RAG corpus refresh (replaces ad-hoc ingest commands). See [`rag/README.md`](./rag/README.md). |
| [`tools/doc-lint.py`](./tools/doc-lint.py) | Catch documentation drift. Three checks: resolves every `file.sh:NN` citation in the docs against the real file, greps a blacklist of phrases that were true once and are not now, and diffs `scripts/NN-*.sh` against the phase table above. Exit 0 clean / 1 findings, so it can gate CI. Run it before any docs PR — three manual sweeps still missed a rotted `--pooling last` citation and four stale strings that this catches in a second. Mark a deliberate historical quote with `<!-- doc-lint: allow -->`. |

## Quick start

```bash
# On the Proxmox host, after Phase 3 (PVE installed, no-sub repo enabled)
cd /root && git clone https://github.com/<you>/local-gpu-cluster.git
cd local-gpu-cluster/scripts
cp config.env.example config.env
$EDITOR config.env        # set DATA_NVME_A and DATA_NVME_B at minimum (ZFS mirror devices)

./bootstrap.sh --list     # see what will run
./bootstrap.sh            # run everything

# Or run phase-by-phase
./bootstrap.sh --only 40
./bootstrap.sh --from 51
./bootstrap.sh --phases 51,53,54
```

## Required config

The values with no sane default are **`DATA_NVME_A`** and **`DATA_NVME_B`** — the two
block devices used for the `tank` ZFS mirror (use `/dev/disk/by-id/nvme-...` paths to
avoid re-enumeration issues). Run `ls -l /dev/disk/by-id/ | grep nvme | grep -v part` to
identify them.

Other commonly customized values:

- `PIN_KERNEL` — defaults to empty (no pin). V620-only build does not require pinning. Set
  only if you have a specific reason (e.g., AQC113 10 GbE regression).
- `FAN_PWM_PATH` — discovered via `sensors-detect`; needed only for `56-fan-control.sh`.
- `OLD_HOST` — SSH alias of a previous deployment to rsync MCP source from. Skipped if empty.

API keys (`LLAMACPP_API_KEY` and `ROUTER_API_KEY`) are auto-generated by the provisioning
scripts and stored in `/etc/llamacpp.env` on LXC 151 and `/etc/router.env` on LXC 153 (mode
600). You do not paste them into `config.env`.

## Reboot handling

One phase triggers a reboot:

1. **4.2 IOMMU enable** — requires reboot for `amd_iommu=on` to take effect.

(Pre-pivot builds also rebooted for kernel pin; V620-only removes that requirement.)

After the reboot, re-run `./bootstrap.sh` (or just `./40-host-config.sh`). Each step detects
completed state and resumes.

## Idempotency

Every script is safe to re-run. Examples of what is detected and skipped:

- `pct status <VMID>` → skip create
- `dpkg-query` → skip apt install
- `zpool list tank` → skip pool create
- Existing binaries (`/opt/llama.cpp/build/bin/llama-server`) → skip build
- `pveam list` → skip template download

## Model stack (defaults)

V620-only build — tuned for 2× V620 32 GB (64 GB total VRAM). All inference runs in LXC 151
as three separate `llama-server` systemd units with per-card pinning:

| Where | Service (unit) | Model | VRAM | Port |
|------|----------------|-------|------|------|
| V620 LXC 151 | `llamacpp-chat.service` (both V620s, tensor-split per profile) | **Profile-switched** via [`swap-chat-model.sh`](./swap-chat-model.sh): `qwen3.8` (Qwen3.8-27B **UD-Q6_K_XL**, 256K ctx (262144), `tensor-split 1,1`, `--split-mode tensor`, `--spec-type draft-mtp` n-3 (tensor/MTP gains measured on the Q4_K_XL sibling — see `51-lxc-amd.sh` bench comments), cache-reuse 1024 auto-disabled, ~23.6 GB — default), `qwen3.6` (UD-Q6_K, 256K, 1,1.5, ~29 GB), `qwen3.6-fast` (UD-Q4_K_M, 256K, 1,1, ~22 GB), `coder` (UD-IQ4_XS, 128K, 1,1.5, cache-reuse 1024, ~38 GB), `devstral` (Q8_0, 256K, 1,1.5, q4_0 KV, ~25 GB), `devstral-large` (UD-IQ2_M, 64K, 1,1.5, q4_0 KV, ~40.6 GB). Common flags: q8_0 KV, `--cache-ram 16384 --mlock --log-prefix --reasoning-format deepseek --jinja --no-mmproj`. †llama.cpp auto-disables cache_reuse for Q*_K_M + q8_0 KV (universal behavior, not profile-specific) | ~23.6 GB (qwen3.8) / ~29 GB (qwen3.6) / ~22 GB (qwen3.6-fast) / ~38 GB (coder) / ~25 GB (devstral) / ~40.6 GB (devstral-large) | 8080 |
| V620 LXC 151 | `llamacpp-embed.service` (V620 #1, `--main-gpu 0`, `HIP_VISIBLE_DEVICES=0`) | Qwen3-Embedding-0.6B Q8_0, **`--pooling last`** (NOT cls), 1024-dim, `--ctx-size 65536 --parallel 4` (16 K per slot) | ~1.2 GB | 8082 |
| V620 LXC 151 | `llamacpp-rerank.service` (V620 #2, `--main-gpu 1`, `HIP_VISIBLE_DEVICES=1`) | BGE-Reranker-v2-m3 Q4_K_M (gpustack GGUF; Qwen3-Reranker-0.6B is gated alt), `--embeddings --pooling rank --reranking` | ~1.5 GB | 8083 |
| Router LXC 153 | `llm-router.service` | FastAPI: routes by path (`/v1/chat/completions`, `/v1/completions` FIM passthrough, `/v1/embeddings`, `/v1/rerank`, `/v1/tavily/search` proxy, `/v1/models`, `/healthz`, `/metrics`) to LXC 151 ports 8080/8082/8083; Bearer auth on inbound (`ROUTER_API_KEY`) + Bearer on upstream (`LLAMACPP_API_KEY`); admission control (chat=1, embed=4) + slowapi rate-limit + Prometheus + CORS middleware; fourteen chat aliases across six profiles (`rag-qwen3.8`/`qwen3.8-think`/`qwen3.8`/`qwen3.8-xhigh` for `qwen3.8`; `rag-qwen3.6`/`qwen3.6-think`/`qwen3.6` for `qwen3.6`; `rag-qwen3.6-fast`/`qwen3.6-fast-think`/`qwen3.6-fast` for `qwen3.6-fast`; `qwen3-coder`/`qwen3-coder-next` for `coder`; `devstral`; `devstral-large`) all resolve to the active chat backend with different `enable_thinking` / `strip_thinking` / `reasoning_effort` defaults | — | 8000 |

Override anything via `config.env` — see `config.env.example` for the full knob list
(`LLAMA_*`, `EMBED_*`, `RERANK_*`, admission + rate-limit env vars on the router).

### Where models live

- Host: `/tank/models/` on the ZFS mirror — actual storage.
- LXC 151: `/opt/models/` — read-write bind mount of the same directory
  (`pct set 151 -mp0 /tank/models,mp=/opt/models`).
- Files use the HuggingFace cache layout:
  `models--<org>--<repo>/snapshots/<rev>/<file>.gguf` symlinking to `blobs/<sha256>`.
- On first `systemctl start llamacpp-chat`, llama-server downloads via `--hf-repo`
  and writes here (`LLAMA_CACHE=/opt/models/.cache` in the unit).
- To pre-fetch from the host:
  `huggingface-cli download <repo> --local-dir /tank/models/<dirname>/`
  **and** switch the unit from `--hf-repo` to `--model /opt/models/<dirname>/<file>.gguf`.

### Why these choices

- **Qwen3.8-27B at UD-Q6_K_XL** is the chat target (default profile). Dense 27B,
  bandwidth-bound — tensor split (`--split-mode tensor`) plus in-GGUF MTP spec-decode
  (`draft-mtp` n-3) are load-bearing for throughput; ~23.6 GB on disk.
  Qwen3.6-35B-A3B (MoE, 3B active params) remains the RAG alternative: UD-Q6_K for
  quality, UD-Q4_K_M (`qwen3.6-fast`) for throughput. Unsloth Dynamic (UD-) quant is
  calibrated against an imatrix dataset and tends to score slightly better than vanilla
  quant at the same size.
- **256K context with q8_0 KV cache and `--parallel 1`** gives a single in-flight request
  the full trained context window of the qwen3.8/qwen3.6 profiles (`n_ctx_train=262144`;
  coder runs 128K, devstral-large 64K). Sub-agent calls from OpenCode/Cline queue at
  the router instead of in llama.cpp (router can emit SSE keepalives while waiting).
  Total chat VRAM is ~23.6 GB weights (qwen3.8 default) + KV; embed + rerank pinned
  per-card add ~3 GB; per-profile VRAM distribution and peak headroom: see the table
  in `day-2-ops.md` § 4.4.
- **Speculative decoding is disabled by default on the MoE profiles** (qwen3.6,
  qwen3.6-fast, coder) — the A3B architecture incurs per-token expert-loading overhead
  during draft verification that exceeds any acceptance-rate speedup (benchmarks measure
  a 3-12% regression with a vocab-matched draft). It is **enabled on `qwen3.8`** via
  in-GGUF MTP heads (`--spec-type draft-mtp`, n-max 3; +63.8% measured 2026-08-19 on the
  UD-Q4_K_XL sibling, no draft model). On the MoE profiles the chat unit boot log shows
  `common_speculative_init: no implementations specified for speculative decoding`, which
  is the expected/normal state. History: the original blocker was a vocab mismatch
  (Qwen3.6-35B-A3B 248,320 vs Qwen3-0.6B 151,936); Qwen3.5-0.8B later matched the vocab,
  but the MoE regression kept spec-decode off. Re-enable via `LLAMA_DRAFT_REPO` in
  `config.env` if benchmarks ever flip.
- **Embedder pooling: `--pooling last` is CRITICAL.** Qwen3-Embedding uses the final
  `<|endoftext|>` token. Using `cls` produces semantically wrong embeddings and silently
  invalidates AnythingLLM's vector DB.
- **Reranker: `--embeddings --pooling rank` alongside `--reranking`** per llama.cpp
  upstream best practice. Qwen3-Reranker-0.6B is the default; BGE-Reranker-v2-m3 is a
  fallback if you can source a community GGUF (see runbook §5.7).
- **Router admission control** replaces the hardware-level isolation the 3060 used to
  provide. Semaphores (chat=1, embed=4) prevent bulk re-embed from stalling chat;
  `slowapi` adds per-IP rate limits; Prometheus middleware exposes `/metrics`.
- **`--reasoning-format deepseek`** moves `<think>` content into a separate
  `reasoning_content` field so RAG UIs that don't read it ignore the thinking entirely.

## AnythingLLM auto-configuration

Phase 54 writes a complete `/opt/anythingllm/.env` so the container boots
already pointing at the router for both LLM and embedder. Specifically:

| Env var | Default | Effect |
|--------|---------|--------|
| `LLM_PROVIDER` | `generic-openai` | Use OpenAI-compatible upstream |
| `GENERIC_OPEN_AI_BASE_PATH` | `http://<router>:8000/v1` | All chat requests via router |
| `GENERIC_OPEN_AI_MODEL_PREF` | `rag-qwen3.8` | Picks the V620 main model. MUST be an alias of the **currently loaded** profile — the router returns 409 for a cross-profile alias. The `rag-` prefix is required: it strips thinking, without which reasoning can consume the whole token budget and return empty content |
| `GENERIC_OPEN_AI_MODEL_TOKEN_LIMIT` | `200000` | AnythingLLM-side input cap, set to the router's `MAX_CHAT_INPUT_TOKENS=200000`. The chat unit's `LLAMA_CTX` is 262144, so 200K leaves ~56K for output + thinking. Raised from 131072 (which was sized for the 128K Coder-Next window) during the qwen3.8 pivot; verified live 2026-08-22 |
| `EMBEDDING_ENGINE` | `generic-openai` | Same provider style for embedder |
| `EMBEDDING_BASE_PATH` | `http://<router>:8000/v1` | Embeddings via router |
| `EMBEDDING_MODEL_PREF` | `qwen3-embed` | 1024-dim Qwen3-Embedding (matches `EMBED_ALIAS` in 51-lxc-amd.sh) |
| `EMBEDDING_MODEL_MAX_CHUNK_LENGTH` | `16384` | Chunk token cap fed to embedder (matches embed unit's 16 K per-slot ctx) |

Phase 57 then uses the AnythingLLM REST API to create the two reference
workspaces (`vcf-reference`, `sdg-documentation`) with the runbook's tuned
params: `chatMode=query`, `vectorSearchMode=rerank`, `topN=10/12`,
`similarityThreshold=0`, refusal sentinels, RAG-tailored system prompts.
Document upload and re-embedding are not in scope here — see runbook §10.4.

## Things the scripts intentionally do NOT do

- **Download model weights up-front** — they're large (~22 GB) and the exact
  filenames on HuggingFace change. The systemd units use `--hf-repo` so models
  download on first `systemctl start`. Pre-fetch manually if you prefer.
- **Upload documents to AnythingLLM** — Phase 57 only creates workspaces and
  sets their RAG params. Document upload (`/api/v1/document/upload`) and
  triggered re-embedding still follow runbook §10.4. AnythingLLM document
  metadata is sourced from per-deployment paths.
- **Reserve DHCP / set static IPs** — the LXCs get DHCP leases. If you want
  stable IPs, either reserve in your router or set `--net0 ...,ip=...,gw=...`
  on each `pct create`.
- **Run `60-verify.sh` automatically** — the verifier is best run after you've
  started the model services (which involve large downloads on first run).

## Differences from runbook Appendix D

The runbook's Appendix D stubs were the starting point. These scripts diverge in
a few ways verified against current upstream docs:

| Topic           | Appendix D                          | These scripts                                  |
| --------------- | ----------------------------------- | ---------------------------------------------- |
| GPU passthrough | Legacy `lxc.cgroup2.devices.allow`  | Modern `pct set --dev0` syntax (PVE 8.2+)      |
| llama.cpp repo  | `ggerganov/llama.cpp`               | `ggml-org/llama.cpp` (current canonical fork)  |
| CMake AMD flag  | `-DAMDGPU_TARGETS`                  | `-DGPU_TARGETS` (current llama.cpp idiom)      |
| Docker repo     | Traditional `sources.list` + `.gpg` | DEB822 `sources.list.d/docker.sources` + `.asc` |
| ROCm version    | Pinned 6.2                          | `latest` URL alias (currently 7.2.x)           |
| NVIDIA install  | Inline driver download              | **Removed** in V620-only pivot                  |
| Kernel pin      | Not handled                         | **Optional** (defensive only — no NVIDIA dep)   |
| AMD firmware    | Not handled                         | `firmware-amd-graphics` in Phase 4.4           |
| Fan control     | Not handled                         | LXC publisher + host bridge (5.13)             |
| Verification    | Not handled                         | `60-verify.sh` runs Appendix C tests           |
