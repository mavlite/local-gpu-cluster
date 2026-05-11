# Bootstrap scripts for local-gpu-cluster

Idempotent automation of Phases 4–11 of [`setup-runbook.md`](../setup-runbook.md).
Each script is independently runnable and may be invoked through the top-level
orchestrator.

| Phase | Script                  | Covers                                                     |
| ----- | ----------------------- | ---------------------------------------------------------- |
| 4     | `40-host-config.sh`     | kernel pin, IOMMU, AMD firmware, NVIDIA driver, ZFS, LXC template |
| 5     | `51-lxc-amd.sh`         | V620 LXC, ROCm, llama.cpp HIP, production systemd, temp publisher |
| 6     | `52-lxc-nv.sh`          | 3060 LXC, NVIDIA driver, CUDA, llama.cpp CUDA, embed/rerank systemd |
| 7     | `53-lxc-router.sh`      | Router LXC, FastAPI app, systemd                           |
| 8     | `54-lxc-anythingllm.sh` | AnythingLLM LXC, Docker, compose stack                     |
| 9     | `55-lxc-mcp.sh`         | MCP stack LXC, Docker, optional rsync from previous host   |
| 5.13  | `56-fan-control.sh`     | Host PWM bridge (needs `FAN_PWM_PATH` discovered manually) |
| 10    | `57-configure-anythingllm.sh` | Create + tune RAG workspaces via REST API (needs `ALLM_API_KEY`) |
| 11    | `60-verify.sh`          | Appendix C smoke tests                                     |

Phases 1–3 (hardware, BIOS, PVE ISO install) are not automatable; follow the
runbook for those.

## Quick start

```bash
# On the Proxmox host, after Phase 3 (PVE installed, no-sub repo enabled)
cd /root && git clone https://github.com/<you>/local-gpu-cluster.git
cd local-gpu-cluster/scripts
cp config.env.example config.env
$EDITOR config.env        # set SECONDARY_NVME at minimum

./bootstrap.sh --list     # see what will run
./bootstrap.sh            # run everything

# Or run phase-by-phase
./bootstrap.sh --only 40
./bootstrap.sh --from 51
./bootstrap.sh --phases 51,52,53
```

## Required config

The only value with no sane default is **`SECONDARY_NVME`** — the block
device used for the `tank` ZFS pool (e.g. `/dev/nvme1n1`). Run
`lsblk -d -o NAME,SIZE,MODEL` to identify it.

Other commonly customized values:

- `NVIDIA_DRIVER_VERSION` — runbook ships `580.105.06`; check
  https://www.nvidia.com/en-us/drivers/ for newer 580.x.
- `PIN_KERNEL` — defaults to `auto` (latest installed 6.14.x-pve). Set to
  empty to skip pinning.
- `FAN_PWM_PATH` — discovered via `sensors-detect`; needed only for `56-fan-control.sh`.
- `OLD_HOST` — SSH alias of a previous deployment to rsync MCP source from. Skipped if empty.

## Reboot handling

Two phases trigger reboots:

1. **4.1 kernel pin** — switches running kernel to 6.14.x-pve.
2. **4.2 IOMMU enable** — requires reboot for `amd_iommu=on` to take effect.

After each reboot, re-run `./bootstrap.sh` (or just `./40-host-config.sh`).
Each step detects completed state and resumes.

## Idempotency

Every script is safe to re-run. Examples of what is detected and skipped:

- `pct status <VMID>` → skip create
- `dpkg-query` → skip apt install
- `zpool list tank` → skip pool create
- Existing binaries (`/opt/llama.cpp/build/bin/llama-server`) → skip build
- `pveam list` → skip template download

## Model stack (defaults)

Tuned for 2x V620 32 GB (64 GB total VRAM) + 1x RTX 3060 12 GB:

| Where | Service | Model | VRAM | Port |
|------|--------|-------|------|------|
| V620 LXC 151 | `llama-server` (main) | Qwen3.6-35B-A3B UD-Q4_K_M, 256K ctx, q8_0 KV, `--reasoning-format deepseek`, `--mlock` | ~54 GB across both V620s | 8080 |
| V620 LXC 151 | speculative draft (same process) | Qwen3-0.6B Q4_K_M | ~600 MB | (same) |
| 3060 LXC 152 | `llama-embed` | Qwen3-Embedding-0.6B Q8_0, 1024-dim, `--pooling last` | ~1.2 GB | 8082 |
| 3060 LXC 152 | `llama-rerank` | bge-reranker-v2-m3 Q4_K_M, `--pooling rank --reranking` | ~700 MB | 8083 |
| 3060 LXC 152 | `llama-chat-fast` | Qwen3-4B-Instruct Q4_K_M, 32K ctx, `--reasoning-format deepseek` | ~3 GB | 8081 |
| Router LXC 153 | `llm-router` | FastAPI: routes `/v1/chat/completions` by model alias (V620 default, fast aliases -> 3060) | — | 8000 |

Override anything via `config.env` — see `config.env.example` for the full
knob list (`LLAMA_*`, `EMBED_*`, `RERANK_*`, `FAST_*`).

### Why these choices

- **Qwen3.6-35B-A3B at Q4_K_M** is the user-requested target for deep coding.
  MoE with 3 B active params keeps inference fast; ~22 GB on disk.
- **256K context with q8_0 KV cache** ≈ 32 GB of KV memory. Combined with the
  22 GB model that's ~54 GB / 64 GB total. Drop `LLAMA_CTX` to 131072 if you
  want headroom for `--parallel 4` or want to push `LLAMA_KV_TYPE=f16`.
- **In-process speculative draft (Qwen3-0.6B)** runs on the same HIP backend
  as the target — only viable cross-process path between V620 and 3060 would
  go through the network and erase the speedup, so we keep the draft local.
- **Qwen3-4B fast-chat on the 3060** uses the spare ~9 GB the embedder/reranker
  leave free. The router exposes it via `/v1/models` so any OpenAI-compatible
  client can pick it by name (`qwen3-4b-fast` by default) for sub-second replies
  on short queries / agent loops.
- **`--reasoning-format deepseek`** moves `<think>` content into a separate
  `reasoning_content` field on the response, so RAG UIs that don't read it
  ignore the thinking entirely. The router's per-request strip-thinking
  header still works as a fallback.
- **Embedder = Qwen3-Embedding-0.6B at 1024-dim** matches the runbook's
  AnythingLLM `Embedding dimension: 1024` setting. The embedder supports MRL
  truncation (32-1024) if you ever want to shrink vectors.

## AnythingLLM auto-configuration

Phase 54 writes a complete `/opt/anythingllm/.env` so the container boots
already pointing at the router for both LLM and embedder. Specifically:

| Env var | Default | Effect |
|--------|---------|--------|
| `LLM_PROVIDER` | `generic-openai` | Use OpenAI-compatible upstream |
| `GENERIC_OPEN_AI_BASE_PATH` | `http://<router>:8000/v1` | All chat requests via router |
| `GENERIC_OPEN_AI_MODEL_PREF` | `qwen3.6-coder` | Picks the V620 main model |
| `GENERIC_OPEN_AI_MODEL_TOKEN_LIMIT` | `262144` | Match `LLAMA_CTX` |
| `EMBEDDING_ENGINE` | `generic-openai` | Same provider style for embedder |
| `EMBEDDING_BASE_PATH` | `http://<router>:8000/v1` | Embeddings via router |
| `EMBEDDING_MODEL_PREF` | `qwen3-embedding` | 1024-dim Qwen3-Embedding |
| `EMBEDDING_MODEL_MAX_CHUNK_LENGTH` | `8192` | Chunk char cap fed to embedder |

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
| NVIDIA install  | Inline driver download              | Shared `/root/nvidia-installer/` cache on host |
| Kernel pin      | Not handled                         | Auto-detect newest 6.14.x-pve, pin, reboot     |
| AMD firmware    | Not handled                         | `firmware-amd-graphics` in Phase 4.4           |
| Fan control     | Not handled                         | LXC publisher + host bridge (5.13)             |
| Verification    | Not handled                         | `60-verify.sh` runs Appendix C tests           |
