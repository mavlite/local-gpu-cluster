# Migration: Ollama → llama.cpp

**Companion to [`local-gpu-cluster-reference.md`](./local-gpu-cluster-reference.md). Read that first — the host, model inventory, and downstream consumers (AnythingLLM, MCP servers, no-think proxy, OpenCode) referenced below are documented there.**

![Status](https://img.shields.io/badge/status-draft-orange)
![Risk](https://img.shields.io/badge/risk-medium-yellow)
![Rollback](https://img.shields.io/badge/rollback-supported-success)
![Revision](https://img.shields.io/badge/revision-3-lightgrey)
![Deployment](https://img.shields.io/badge/deployment-LXC-blue)

End-to-end procedure for replacing Ollama on `192.168.6.110` with llama.cpp + llama-swap, preserving every downstream consumer (AnythingLLM, OpenCode, the three MCP containers, the auto-updater, and the no-think proxy) at the same port (`:11434`) and the same OpenAI-compatible API surface. The migration is staged behind a parallel install, gated by a parity test the embedder must pass before cutover, and reversible in under a minute if anything regresses.

> **Scope note.** This describes a planned migration for a working production setup. Don't execute it as a single batch — work through each section, validate, and proceed only when the gate for that step is green. Numbers (token rates, memory footprints) are expectations from upstream benchmarks; verify against your own measurements before committing.

---

## Table of Contents

- [1. Decision and scope](#1-decision-and-scope)
- [2. Target architecture](#2-target-architecture)
- [3. Prerequisites and prep](#3-prerequisites-and-prep)
- [4. llama-server configurations](#4-llama-server-configurations)
- [5. llama-swap configuration](#5-llama-swap-configuration)
- [6. Parity gates](#6-parity-gates)
- [7. Cutover procedure](#7-cutover-procedure)
- [8. Rollback procedure](#8-rollback-procedure)
- [9. Post-cutover validation](#9-post-cutover-validation)
- [10. Modelfile → llama.cpp reference](#10-modelfile--llamacpp-reference)
- [11. TurboQuant: deferred, not adopted](#11-turboquant-deferred-not-adopted)
- [12. Known issues and mitigations](#12-known-issues-and-mitigations)
- [Appendix A — systemd units (hardened)](#appendix-a--systemd-units-hardened)
- [Appendix B — file layout](#appendix-b--file-layout)

---

## Revision history

| Rev | Date | Notes |
|---|---|---|
| 3 | May 2026 | LXC deployment model. Reflects the cluster's existing pattern: every service runs in its own LXC container. New `llama-cpp` container provisioned in §3.2.1 with privileged mode, GPU passthrough for all three RTX 3060s, and a host bind-mount of `/opt/gguf`. NVIDIA userspace install matches the host driver major version (kernel driver stays on host, userspace inside container). All build, install, GGUF prep, and systemd-unit work moves inside the container; cutover (§7) becomes a port-forward swap between the `ollama` and `llama-cpp` LXCs at `:11434`. Rollback is `lxc start ollama` after releasing the port; LXC snapshot taken pre-cutover for filesystem-level recovery. `Conflicts=ollama.service` removed from llama-swap unit (irrelevant across LXC boundaries; mutual exclusion is enforced by the host's port-forward device). Appendix A split into A.1 (in-container llama-swap unit) and A.2 (host-side LXC autostart). Appendix B split into host filesystem vs container filesystem. |
| 2 | May 2026 | Multi-reviewer pass. Removed `--system-prompt-file` (deleted from llama-server in #9811) — system prompts are per-request via AnythingLLM workspace `openAiPrompt` or OpenCode's agent prompt. Added `coder-256k` entry (256K context viable on 36 GB pool via KV q8_0; mainline only, no fork required). Group config uses `persistent: true` for the embedder. Optional `apiKeys:` block documented. Hardened systemd unit with CUDA carve-outs. Rollback procedure rewritten with explicit ordering around `Conflicts=ollama.service`. Validation unit on `:11440` separated from production unit on `:11434`. SHA256 verification required for binary and GGUF downloads. Backend processes bound to `127.0.0.1` (not `0.0.0.0`). Unsloth GGUF filename corrected (`UD-Q4_K_M`). VRAM math reconciled with q8_0 KV reduction. |
| 1 | May 2026 | Initial migration plan: llama.cpp + llama-swap replacing Ollama at port 11434. Embedding-parity gate as hard cutover blocker. TurboQuant deferred. |

---

## 1. Decision and scope

### 1.1 What this migration delivers

Three concrete, measured wins documented from upstream benchmarks on similar hardware, plus one capability that didn't exist before:

- **~25–40% generation throughput** on Qwen 3.6 35B-A3B at iso-quant (Q4_K_M). Community measurements show llama.cpp at 133–142 tok/s on a single high-end card vs Ollama's ~99 tok/s on the same model and quant — Ollama wraps llama.cpp but strips the knobs (KV-cache quantization, fine flash-attn control, MoE expert offload tuning) where most of the delta lives. The MoE model benefits more than the dense one because it's bandwidth-bound.
- **Roughly 2× usable context at the same VRAM** via `--cache-type-k q8_0 --cache-type-v q8_0`. KV cache footprint at f16 is ~70 MB per 1K tokens of context for Qwen 3.6 35B-A3B; q8_0 cuts that to ~35 MB per 1K. At 131K context this drops KV from ~9 GB to ~4.5 GB; **at 256K context it drops from ~18 GB (overflow on the 36 GB pool) to ~9 GB (fits with margin)**. The reference doc's claim that "above 131K is not viable on 3× 3060" was written against Ollama's f16 KV — q8_0 unlocks 256K on the same hardware. See `coder-256k` in §5.
- **Per-model parameter control** Ollama doesn't expose: split-mode tuning, MoE CPU-offload (`--n-cpu-moe`), per-slot save paths, custom chat templates with `--jinja`. Useful for both performance work and debugging.

### 1.2 What this migration does NOT deliver (and is not worth doing for)

- **TurboQuant.** Not in mainline llama.cpp, no published RTX 30-series benchmarks on Qwen MoE, and its primary benefit (KV memory compression) is no longer a constraint once KV q8_0 is available. See §11.
- **A free win.** This is a real migration with a real parity gate. If the embedder doesn't match Ollama's vectors, the LanceDB workspaces (`vcf-reference`, `sdg-documentation`) need a full re-embed of ~5,000 documents.
- **Ollama's UX.** No `ollama pull`, no Modelfile DSL, no `ollama ps`. You're trading polish for control.

### 1.3 What stays unchanged

This is the contract for the migration — anything outside this list is in scope for breakage and must be retested:

- **Port `11434`** — after cutover, llama-swap binds here. Every downstream consumer keeps its current base URL.
- **AnythingLLM chat configuration** — still talks to `:11435` (no-think proxy), which still talks to `:11434` upstream.
- **OpenCode configuration** — still talks to `:11436` (agentic no-think proxy).
- **MCP servers** (`anythingllm-search-mcp`, `broadcom-techdocs-mcp`, `sdg-docs-mcp`) — no change; they call AnythingLLM's REST API on `:3001`, not the LLM directly.
- **VCF auto-updater** — same; its only LLM dependency is the embedder via AnythingLLM. The `state.sqlite` schema is migration-safe.
- **No-think proxy itself** — the FastAPI app at `/opt/nothink-proxy/app.py` keeps its `UPSTREAM = "http://127.0.0.1:11434"` line. llama-swap speaks the same OpenAI-compatible API on the same port.
- **LanceDB collections** — *if* the embedding-parity gate (§6.1) passes. Re-embedding is the one rollback that isn't free.
- **Per-workspace `openAiPrompt`** — AnythingLLM workspaces already inject the assistant's behavioral prompt per-request. With `--system-prompt-file` removed from llama-server (see §3.6), the workspace prompt is now the *only* system prompt the model sees. This is fine — the workspace prompt was already the dominant one.

The single configuration change downstream of the LLM is at the **AnythingLLM embedder**, which currently provider-types as "Ollama" against `:11434`. llama-swap exposes embeddings on `/v1/embeddings` (OpenAI), not `/api/embeddings` (Ollama-native). After cutover, switch the AnythingLLM embedder provider to "Generic OpenAI" with the same base URL. Detail in §7.

---

## 2. Target architecture

### 2.1 Diagram

```text
Existing (current state)
─────────────────────────
AnythingLLM (3001) ─┐
OpenCode           ─┤
curl              ──┼──► no-think proxy (11435/11436) ──► Ollama (11434) ──► 3× RTX 3060
AnythingLLM embed ──┘                                              ▲
                                                                   │
                                                               Modelfiles
                                                          (rag-qwen25, coder-36,
                                                           coder-long, qwen3-embed-*)


Target (after cutover)
──────────────────────
AnythingLLM (3001) ─┐
OpenCode           ─┤
curl              ──┼──► no-think proxy (11435/11436) ──► llama-swap (11434) ──┐
AnythingLLM embed ──┘                                                          │
                                                                               ▼
                                                       ┌──────────────────────────────┐
                                                       │ llama-server: rag-qwen25     │ ┐
                                                       │   qwen2.5-32b Q4_K_M, 32K    │ │ chat group
                                                       ├──────────────────────────────┤ │ (exclusive:
                                                       │ llama-server: coder-36       │ │   only one
                                                       │   qwen3.6-35b-a3b, 64K       │ │   loaded
                                                       ├──────────────────────────────┤ │   at a time)
                                                       │ llama-server: coder-long     │ │
                                                       │   qwen3.6-35b-a3b, 131K      │ │
                                                       ├──────────────────────────────┤ │
                                                       │ llama-server: coder-256k     │ │
                                                       │   qwen3.6-35b-a3b, 256K      │ ┘
                                                       ├──────────────────────────────┤
                                                       │ llama-server: qwen3-embed    │   embed group
                                                       │   qwen3-embedding 0.6b       │   (persistent)
                                                       └──────────────────────────────┘
                                                                       ▲
                                                                  3× RTX 3060
```

### 2.2 Component responsibility map

| Layer | Ollama (current) | llama.cpp (target) |
|---|---|---|
| Model loader | `ollama serve` reads from `~/.ollama/models/blobs/` | `llama-server` reads a GGUF file path |
| Multi-model serving | One daemon, `OLLAMA_MAX_LOADED_MODELS=2`, LRU | llama-swap fronts N llama-server processes, hot-swaps |
| Sampler config | Per-Modelfile `PARAMETER` lines | Per-llama-server CLI flags + per-request OpenAI body fields |
| System prompt | Per-Modelfile `SYSTEM` block | Per-request system message (AnythingLLM workspace `openAiPrompt`, OpenCode agent prompt) |
| Chat template | Implicit per base model | Auto-loaded from GGUF; `--jinja` for full Qwen3 template |
| Multi-GPU | `CUDA_VISIBLE_DEVICES=0,1,2`, automatic layer split | `--split-mode layer --tensor-split 1,1,1` per process |
| Flash attention | `OLLAMA_FLASH_ATTENTION=1` (global) | `-fa on` per llama-server |
| KV-cache quant | Not exposed | `--cache-type-k q8_0 --cache-type-v q8_0` |
| Keep-alive | `OLLAMA_KEEP_ALIVE=30m` (global) | llama-swap per-model `ttl:` (within group); `persistent: true` for embedder |
| Embedding API | `POST /api/embeddings` | `POST /v1/embeddings` (OpenAI-compatible) — see §7 for AnythingLLM reconfig |
| Chat API | `POST /v1/chat/completions` | `POST /v1/chat/completions` — drop-in |
| Auth | None on Ollama | Optional `apiKeys:` in llama-swap config — required if binding to LAN |

---

## 3. Prerequisites and prep

> **Deployment model: LXC, matching the existing cluster.** Ollama, AnythingLLM, the three MCP containers, the auto-updater, and the no-think proxy all run inside LXC containers on this host — that's the homelab's standard pattern. llama.cpp + llama-swap must follow it. Section §3.2 below provisions a new LXC container; every subsequent step (build, install, GGUF prep, systemd validation unit) executes **inside that container** unless explicitly noted as a host operation. The cutover in §7 swaps which LXC container holds port `:11434` — Ollama's container stops; the new llama.cpp container takes over. Paths in this doc that look like `/opt/llama.cpp/...` are paths *inside the new LXC container's filesystem*; the **host** filesystem retains only what's needed for GPU passthrough, networking, and bind-mounted GGUF storage.

### 3.1 Verify the current setup is healthy first

Don't begin a migration on a degraded system. Run Appendix C of [`local-gpu-cluster-reference.md`](./local-gpu-cluster-reference.md) (the smoke tests) and confirm green across the board, including the VCF positive-retrieval test that proves the LanceDB embedding dimensions are intact. If anything's off now, fix it before adding a new variable.

Also confirm your LXC tooling is in shape:

```bash
# On the host
lxc --version                     # LXD / Incus client
lxc list                          # see existing containers (ollama, anythingllm, mcps, etc.)
lxc image list ubuntu: | head     # confirm ubuntu:24.04 image is available
lxc storage list                  # confirm a storage pool with >50 GB free
```

### 3.2 Build llama.cpp with CUDA

This step has two parts: provision the LXC container with GPU passthrough on the **host**, then build llama.cpp **inside** the container.

#### 3.2.1 Provision the LXC container (host operation)

The container name `llama-cpp` is used throughout this doc. Adjust if your naming convention differs.

```bash
# --- HOST shell ---
lxc launch ubuntu:24.04 llama-cpp

# Privileged + nesting (matches the existing Ollama/AnythingLLM containers per
# this cluster's deployment pattern; needed for GPU device passthrough and for
# running Docker/podman inside if ever desired)
lxc config set llama-cpp security.privileged true
lxc config set llama-cpp security.nesting true

# GPU passthrough — all three RTX 3060s. PCI BDFs from reference doc §2.2.
lxc config device add llama-cpp gpu0 gpu vendorid=10de pci=0000:03:00.0
lxc config device add llama-cpp gpu1 gpu vendorid=10de pci=0000:81:00.0
lxc config device add llama-cpp gpu2 gpu vendorid=10de pci=0000:83:00.0

# Bind-mount GGUF directory from host. /opt/gguf on host already holds (or
# will hold) the symlinked or downloaded GGUF files; sharing avoids duplicating
# 50 GB of model weights inside the container snapshot.
sudo mkdir -p /opt/gguf
sudo chown 1000000:1000000 /opt/gguf   # uid 1000000 = root inside privileged LXC
lxc config device add llama-cpp gguf disk source=/opt/gguf path=/opt/gguf

# Port-forward 11434 from host loopback to container loopback so the no-think
# proxy and AnythingLLM (both running in their own LXCs but reachable from the
# host's networking namespace) can hit llama-swap at the existing :11434.
# During validation use :11440 to coexist with Ollama's container on :11434.
lxc config device add llama-cpp llama-swap-validation proxy \
  listen=tcp:127.0.0.1:11440 connect=tcp:127.0.0.1:11440
# At cutover (§7) the listen-port flips to 11434 — see §7 step 3.

lxc restart llama-cpp

# Sanity: GPUs visible inside?
lxc exec llama-cpp -- nvidia-smi
# Expect three RTX 3060s. If "command not found", install NVIDIA userspace
# (next subsection); if "Failed to initialize NVML", the host driver version
# doesn't match what the container has installed yet (also next subsection).
```

#### 3.2.2 Install NVIDIA userspace and build dependencies (inside container)

Critical: the host already has the NVIDIA kernel driver from Phase 0 (existing setup). The container needs **userspace libraries that match the host's driver major version exactly**. Skew produces "Failed to initialize NVML" errors that are painful to debug.

```bash
# Discover the host driver version (run on HOST first)
nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1
# e.g. 550.107.02 — major version is 550

# --- enter the container ---
lxc exec llama-cpp -- bash

# Inside the container:
HOST_DRIVER_MAJOR=550   # paste from above

apt update
apt install -y \
  "libnvidia-compute-${HOST_DRIVER_MAJOR}" \
  "nvidia-utils-${HOST_DRIVER_MAJOR}" \
  build-essential cmake git ccache libcurl4-openssl-dev

# Verify NVML works inside the container
nvidia-smi
# Expect three RTX 3060s, same UUIDs as the host sees.
```

The container does **not** install the kernel driver (that lives on the host) — only the userspace libraries (`libnvidia-compute-*`, `nvidia-utils-*`) and CUDA toolkit components needed for compile/link.

#### 3.2.3 Build llama.cpp (inside container)

```bash
# --- still inside the container ---
cd /opt
git clone https://github.com/ggml-org/llama.cpp.git
cd /opt/llama.cpp

# Pin to a specific release tag — replace YOUR_TAG_HERE with the latest stable
# tag at the time of migration (see `git tag --sort=-v:refname | head -10`).
git fetch --tags
TAG="YOUR_TAG_HERE"   # e.g. b4500 or whatever's current
git checkout "$TAG"
git verify-tag "$TAG" || echo "WARNING: tag not GPG-signed (acceptable but noted)"

# CUDA toolkit for the build. nvidia-cuda-toolkit pulls nvcc; large but
# only needed at build time. Can be removed after to slim the container.
apt install -y nvidia-cuda-toolkit

cmake -S /opt/llama.cpp -B /opt/llama.cpp/build \
  -DGGML_CUDA=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build /opt/llama.cpp/build --config Release -j$(nproc)

# Verify these flags exist (each migration step depends on them):
/opt/llama.cpp/build/bin/llama-server --help \
  | grep -E -- '--n-gpu-layers|--split-mode|--tensor-split|--cache-type-k|--cache-type-v|--flash-attn|--jinja|--embedding|--pooling|--chat-template-kwargs'

# Service account inside the container — create it if not present, then chown
# the binary directory so the systemd unit (§3.5) can run unprivileged.
useradd --system --no-create-home --shell /usr/sbin/nologin ollama 2>/dev/null || true
chown -R ollama:ollama /opt/llama.cpp/build/bin
```

Compile time on the T7910 dual-Xeon (passed through to LXC, no virt overhead) is ~10–15 minutes.

### 3.3 Install llama-swap (inside container)

llama-swap is a single Go binary that fronts llama-server processes and dispatches requests by the `model` field in the body. Install inside the same `llama-cpp` LXC container — colocating with the llama-server binaries simplifies the YAML config (which references `cmd:` paths) and keeps the container self-contained.

```bash
# --- inside the llama-cpp container ---

# llama-swap releases are versioned with bare integers (e.g. "162"), not semver.
# Check https://github.com/mostlygeek/llama-swap/releases for the current value.
RELEASE="REPLACE_WITH_RELEASE_NUMBER"

cd /opt
curl -L -o llama-swap.tar.gz \
  "https://github.com/mostlygeek/llama-swap/releases/download/${RELEASE}/llama-swap_${RELEASE}_linux_amd64.tar.gz"
curl -L -o llama-swap-checksums.txt \
  "https://github.com/mostlygeek/llama-swap/releases/download/${RELEASE}/checksums.txt"

# Verify before installing
sha256sum -c <(grep "llama-swap_${RELEASE}_linux_amd64.tar.gz" llama-swap-checksums.txt)
# Expected: llama-swap_<RELEASE>_linux_amd64.tar.gz: OK

mkdir -p /opt/llama-swap
tar -xzf llama-swap.tar.gz -C /opt/llama-swap
chown -R ollama:ollama /opt/llama-swap
chmod +x /opt/llama-swap/llama-swap
/opt/llama-swap/llama-swap --version
```

### 3.4 Source the GGUFs

`/opt/gguf/` is bind-mounted from the **host** into the `llama-cpp` container (configured in §3.2.1), so the GGUF files live on the host filesystem and are visible inside both the new container and the existing Ollama container during validation. This is the right architecture: GGUFs are large (~30 GB total), shouldn't bloat container snapshots, and need to be readable by Ollama (Phase 1 baseline) and llama.cpp (post-cutover) alike.

You have two paths:

**Path A — reuse existing Ollama blobs (host operation).** The Ollama LXC container has its own model store, typically bind-mounted from a host directory like `/var/lib/ollama-data/.ollama/models/blobs/`. Each blob is a raw GGUF (llama.cpp identifies format by the `GGUF` magic bytes, not the extension — the `.gguf` symlink below is convention only, but keeps tooling and logs readable).

```bash
# --- HOST shell ---

# Find your Ollama LXC's model storage path on the host. Adjust to match
# your actual setup; common patterns:
OLLAMA_BLOBS=/var/lib/lxd/storage-pools/default/containers/ollama/rootfs/root/.ollama/models/blobs
# or if Ollama uses a host bind-mount:
# OLLAMA_BLOBS=/srv/ollama/.ollama/models/blobs

# Read the manifest (path inside the Ollama container; use `lxc exec ollama --`
# to read it):
lxc exec ollama -- cat /root/.ollama/models/manifests/registry.ollama.ai/library/qwen2.5/32b-instruct-q4_K_M
# Look for the "model" layer's "digest" field — that's the sha256 of the GGUF blob.

# Symlink into the host's /opt/gguf which is bind-mounted into the llama-cpp container.
sudo mkdir -p /opt/gguf
sudo ln -s "${OLLAMA_BLOBS}/sha256-abc123..." /opt/gguf/qwen2.5-32b-instruct-q4_K_M.gguf
# Ownership inside the privileged llama-cpp container maps host root → uid 0;
# no extra chown step needed for privileged containers.
```

**Path B — fresh download (recommended for Qwen 3.6 35B-A3B, host operation).** The unsloth team ships fixes to Qwen3 chat/tool-call templates faster than what's baked into your Ollama blob. For the agentic-coding model where tool calling matters, this delta matters.

```bash
# --- HOST shell ---
# Note the UD- prefix in the filename (Unsloth Dynamic). The plain Q4_K_M
# without the prefix does not exist in this repo.
cd /opt/gguf
sudo curl -L -o qwen3.6-35b-a3b-UD-Q4_K_M.gguf \
  'https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf?download=true'
sudo curl -L -o qwen3-embedding-0.6B-f16.gguf \
  'https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-f16.gguf?download=true'

# Verify SHA256 against HuggingFace's per-file etag (HF stores SHA256 in
# x-linked-etag for LFS files). Capture the expected hash from the model card
# UI or via the HF API and check:
sha256sum qwen3.6-35b-a3b-UD-Q4_K_M.gguf qwen3-embedding-0.6B-f16.gguf
```

Verify visibility from inside the container:

```bash
lxc exec llama-cpp -- ls -lh /opt/gguf/
# Expect to see the GGUF files (or symlinks resolving to readable blobs).
```

Qwen3-Embedding-0.6B is **Matryoshka-trained** — the output dimension is configurable from 32 to 1024 via the `output_dimension` parameter. Your existing LanceDB collections were embedded at 1024-dim under Ollama, so 1024 is the dimension to keep.

### 3.5 Validation systemd unit on a temporary port (`:11440`)

Two-layer systemd setup matching the rest of the cluster's pattern:
- **Inside the container:** systemd runs llama-swap as a service (the unit below).
- **On the host:** an LXC `Wants=` unit ensures the `llama-cpp` container starts at boot (Appendix A). During validation we don't enable boot-start; manual `lxc start` is fine.

Before cutover, llama-swap runs on `:11440` so it doesn't conflict with Ollama's container on `:11434`. The proxy device added in §3.2.1 already forwards host's `127.0.0.1:11440` to container's `127.0.0.1:11440` — so binding inside the container on 11440 makes it reachable from the host (and from the no-think proxy in its own LXC).

Create the unit **inside the container**:

```bash
lxc exec llama-cpp -- bash -c 'cat > /etc/systemd/system/llama-swap-validation.service << "EOF"
[Unit]
Description=llama-swap (validation) — temporary port 11440
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ollama
Group=ollama
WorkingDirectory=/opt/llama-swap
Environment="CUDA_VISIBLE_DEVICES=0,1,2"
ExecStart=/opt/llama-swap/llama-swap \
  --config /opt/llama-swap/config.yaml \
  --listen 127.0.0.1:11440
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
EOF'

lxc exec llama-cpp -- systemctl daemon-reload
lxc exec llama-cpp -- systemctl start llama-swap-validation
lxc exec llama-cpp -- systemctl status llama-swap-validation
```

Verify reachability from the host:

```bash
# --- HOST shell ---
curl -s http://127.0.0.1:11440/v1/models | jq
# Expect the four models defined in the §5 config.
```

This unit is removed at the end of cutover (§7) and replaced by the production unit.

### 3.6 Side-by-side install — don't touch the Ollama container yet

Two LXC containers (`ollama` and `llama-cpp`) share the same physical GPUs through the CUDA driver passed in via cgroup device entries. Don't load 32B + 35B concurrently across both containers (e.g., a chat request to Ollama while llama-swap has `coder-long` loaded) — that overflows the 36 GB pool. The CUDA driver itself doesn't enforce VRAM allocation across containers; it'll happily let both processes try to allocate, and one will OOM.

The Modelfile `SYSTEM` blocks (`rag-qwen25`'s retrieval-assistant prompt, `coder-36`'s agentic-coding prompt) **do not need to be extracted into files**. `--system-prompt-file` was removed from llama-server in [issue #9811](https://github.com/ggml-org/llama.cpp/issues/9811). System prompts are now per-request:

- AnythingLLM workspaces inject the assistant prompt via `openAiPrompt` (already configured per reference §5.5).
- OpenCode injects its agentic-coding prompt per session.
- For curl smoke tests, add `{"role":"system","content":"..."}` to the messages array.

The Modelfile system prompts were always shadowed by client-supplied system messages anyway; this is a no-op behavioral change in practice.

---

## 4. llama-server configurations

Each model gets one llama-server invocation, expressed as a llama-swap entry in §5. The flags below are the substantive choices. **All examples bind `--host 127.0.0.1`** — these are internal backends, not LAN-exposed; only llama-swap (§5, Appendix A) listens on a public-ish address.

### 4.1 `rag-qwen25` — Qwen 2.5 32B Instruct, RAG backbone

```bash
/opt/llama.cpp/build/bin/llama-server \
  --model /opt/gguf/qwen2.5-32b-instruct-q4_K_M.gguf \
  --host 127.0.0.1 --port 9001 \
  --n-gpu-layers 999 \
  --split-mode layer --tensor-split 1,1,1 \
  -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --ctx-size 32768 \
  --n-predict 4096 \
  --parallel 2 --cont-batching \
  --temp 0.3 --top-k 20 --top-p 0.95 --min-p 0 \
  --repeat-penalty 1 --presence-penalty 0 \
  --jinja
```

System prompt comes from AnythingLLM's per-workspace `openAiPrompt` (the retrieval-assistant prompt currently configured in the `vcf-reference` and `sdg-documentation` workspaces).

### 4.2 `coder-36`, `coder-long`, `coder-256k` — Qwen 3.6 35B-A3B (MoE)

Same model file, three llama-swap entries with different context windows. Qwen 3.6's thinking mode is on by default; `--jinja` is required for the template's thinking-toggle to render correctly. Per-request `enable_thinking: false` is what the no-think proxy uses; no server flag needed.

```bash
# coder-36: 64K context — typical agentic-coding sessions
/opt/llama.cpp/build/bin/llama-server \
  --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf \
  --host 127.0.0.1 --port 9002 \
  --n-gpu-layers 999 \
  --split-mode layer --tensor-split 1,1,1 \
  -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --ctx-size 65536 \
  --n-predict 16384 \
  --parallel 2 --cont-batching \
  --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0 \
  --repeat-penalty 1 --presence-penalty 0 \
  --jinja
```

`coder-long` and `coder-256k` differ from `coder-36` only in `--ctx-size` and `--parallel`:

| Variant | `--ctx-size` | `--parallel` | Approx VRAM (weights + KV) |
|---|---|---|---|
| `coder-36` | 65536 | 2 | ~23 GB + 2×~2.25 GB = **~27.5 GB** |
| `coder-long` | 131072 | 1 | ~23 GB + ~4.5 GB = **~27.5 GB** |
| `coder-256k` | 262144 | 1 | ~23 GB + ~9 GB = **~32 GB** |

`coder-256k` leaves only ~4 GB headroom on the 36 GB pool; the embedder (~1.4 GB) fits but `--parallel 2` does not. Generation rate at 256K will be lower than at 131K — long prefill plus full KV scan over PCIe 3.0 cross-shard. Expect ~7–10 tok/s vs ~15 tok/s at 131K. Use it when whole-repo context actually matters; default to `coder-36` or `coder-long` otherwise.

### 4.3 `qwen3-embed` — embedder

```bash
/opt/llama.cpp/build/bin/llama-server \
  --model /opt/gguf/qwen3-embedding-0.6B-f16.gguf \
  --host 127.0.0.1 --port 9004 \
  --n-gpu-layers 999 \
  --embedding \
  --pooling last \
  --ctx-size 2048 \
  --batch-size 512 --ubatch-size 512
```

Pooling for Qwen3-Embedding is **last-token**, not mean (confirmed against the model's HF discussion). `--ctx-size 2048` matches the `qwen3-embed-compact` variant from the reference doc — keeps KV cache tiny, frees VRAM for the chat models. The embedder is Matryoshka-trained with output dimension up to 1024; existing LanceDB collections use 1024, so no per-request `output_dimension` override is needed.

> The CPU-only embedding variant (current `qwen3-embed-cpu`) is replicable as a separate llama-swap entry with `--n-gpu-layers 0`. Reach for it during bulk re-ingestion runs to keep the GPUs free for chat — same use case as before, different implementation.

---

## 5. llama-swap configuration

Single source of truth for which models are available and how they're served. `/opt/llama-swap/config.yaml`:

```yaml
healthCheckTimeout: 180   # GGUF cold-load can run 30–90s; don't false-fail
logLevel: info

# Optional inbound auth — REQUIRED if llama-swap listens on a LAN interface.
# Generate with: openssl rand -hex 32
# Comment out only when binding to 127.0.0.1.
# apiKeys:
#   - "REPLACE_WITH_OPENSSL_RAND_HEX_32_VALUE"

models:
  rag-qwen25:
    cmd: |
      /opt/llama.cpp/build/bin/llama-server
      --model /opt/gguf/qwen2.5-32b-instruct-q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      --split-mode layer --tensor-split 1,1,1
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 32768
      --n-predict 4096
      --parallel 2 --cont-batching
      --temp 0.3 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800   # 30-min idle eviction (only fires if no other chat model is requested)

  coder-36:
    cmd: |
      /opt/llama.cpp/build/bin/llama-server
      --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      --split-mode layer --tensor-split 1,1,1
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 65536
      --n-predict 16384
      --parallel 2 --cont-batching
      --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

  coder-long:
    cmd: |
      /opt/llama.cpp/build/bin/llama-server
      --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      --split-mode layer --tensor-split 1,1,1
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 131072
      --n-predict 16384
      --parallel 1 --cont-batching
      --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

  coder-256k:
    cmd: |
      /opt/llama.cpp/build/bin/llama-server
      --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      --split-mode layer --tensor-split 1,1,1
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 262144
      --n-predict 16384
      --parallel 1 --cont-batching
      --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

  qwen3-embed:
    cmd: |
      /opt/llama.cpp/build/bin/llama-server
      --model /opt/gguf/qwen3-embedding-0.6B-f16.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      --embedding --pooling last
      --ctx-size 2048
      --batch-size 512 --ubatch-size 512
    proxy: http://127.0.0.1:${PORT}

groups:
  # Mutual-exclusion: one chat model loaded at a time (VRAM budget).
  chat:
    exclusive: true
    members: [rag-qwen25, coder-36, coder-long, coder-256k]
  # persistent: true keeps the embedder loaded across exclusive chat-group swaps.
  embed:
    persistent: true
    members: [qwen3-embed]
```

Key design choices:

- **`exclusive: true` on chat group** — only one of the four chat models loads at a time. `coder-256k` at 256K consumes ~32 GB of the 36 GB pool; loading anything else alongside it overflows. llama-swap evicts the previous chat model when a new one is requested.
- **`persistent: true` on embed group** — the embedder is always loaded and is exempt from chat-group eviction. AnythingLLM's interactive embedding traffic plus the auto-updater's nightly run make this the right choice.
- **`--parallel 2` on `rag-qwen25` / `coder-36`, `--parallel 1` on `coder-long` / `coder-256k`** — KV per slot scales with context. Two slots at 131K or 256K overflow the pool. Single slot at long contexts.
- **`${PORT}` is filled in by llama-swap** at backend launch — picks an unused port per backend; clients never see them.
- **`healthCheckTimeout: 180`** — a 23 GB GGUF off NVMe takes 15–30 seconds cold-load; a 32 GB load (`coder-256k` weights + KV pre-allocate) can run longer. The Ollama default of 60 seconds was too aggressive for this hardware.
- **No `OLLAMA_MAX_LOADED_MODELS=2` analog.** `exclusive: true` enforces strictly one chat model resident. Quick alternation between chat models (RAG + agentic coding from different users at the same time) will thrash with cold loads. This is a regression from Ollama's lazy LRU but is necessary at this VRAM budget — see §12.

### 5.1 Auth posture

If llama-swap listens on `127.0.0.1` (the production unit in Appendix A binds there by default), no auth is required — only local processes can reach it, and the no-think proxy is the only intended caller. **If you ever rebind to `0.0.0.0`** (e.g., to let OpenCode on a remote LAN machine talk to llama-swap directly without going through the no-think proxy), uncomment the `apiKeys:` block above and require all clients to send a matching `Authorization: Bearer <key>` or `x-api-key: <key>` header.

---

## 6. Parity gates

Three of the four gates are smoke tests; one (§6.1) is a **hard cutover blocker** because its failure mode is silent and not cleanly reversible.

All gates run against the validation unit on `:11440` (§3.5).

### 6.1 Embedding cosine parity (HARD GATE)

llama-server returns embeddings without applying L2 normalization for Qwen3 by default; Ollama applies it. A naive cutover replaces 1024-dim *normalized* vectors in LanceDB with 1024-dim *un-normalized* vectors at query time, and retrieval quality silently collapses (cosine similarity is no longer the inner product, and the rerank stage gets garbage to rerank).

**Test script** — `/opt/migration/test-embedding-parity.py`:

```python
"""
Compare Ollama and llama-server embeddings for the same inputs.
Fail loudly if cosine similarity isn't ~1.0 across the test set.
"""
import math
import httpx

OLLAMA = "http://127.0.0.1:11434"
LSWAP  = "http://127.0.0.1:11440"   # llama-swap during validation

SAMPLES = [
    "VCF-NET-014: edge uplink subnets must use prefix length /29 or /30.",
    "vSphere Distributed Switch supports a maximum of 1024 ports.",
    "Keycloak realm settings include token lifespans and SSO session timeouts.",
    "the quick brown fox jumps over the lazy dog",
    "import asyncio; await client.get(url)",
]

def l2_normalize(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]

def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))   # assumes both normalized

def ollama_embed(text):
    r = httpx.post(f"{OLLAMA}/api/embeddings",
                   json={"model": "qwen3-embed-compact:latest", "prompt": text},
                   timeout=60)
    r.raise_for_status()
    return r.json()["embedding"]

def lswap_embed(text):
    r = httpx.post(f"{LSWAP}/v1/embeddings",
                   json={"model": "qwen3-embed", "input": text},
                   timeout=60)
    r.raise_for_status()
    return r.json()["data"][0]["embedding"]

def main():
    failures = 0
    for s in SAMPLES:
        try:
            a = ollama_embed(s)
            b = lswap_embed(s)
        except httpx.HTTPStatusError as e:
            print(f"[ERROR] '{s[:60]}'  ({e})")
            failures += 1
            continue
        assert len(a) == len(b) == 1024, f"dim mismatch: {len(a)} vs {len(b)}"
        sim = cosine(l2_normalize(a), l2_normalize(b))
        status = "OK" if sim > 0.9999 else "FAIL"
        if sim <= 0.9999:
            failures += 1
        print(f"[{status}] cos={sim:.6f}  '{s[:60]}'")
    print(f"\n{failures} failures out of {len(SAMPLES)} samples")
    raise SystemExit(0 if failures == 0 else 1)

if __name__ == "__main__":
    main()
```

**Pass criterion:** every sample shows cosine ≥ 0.9999 *after both sides are L2-normalized*. If they don't match:

1. Confirm `--pooling last` on the llama-server side (Qwen3 embedder uses last-token pooling).
2. Confirm the same GGUF (or one quantized from the same source weights) — different quantizations of the embedder produce different vectors.
3. If still mismatched, treat as full re-embed required: in AnythingLLM, delete each workspace's documents, switch the embedder provider to "Generic OpenAI" (`http://localhost:11434/v1` after cutover), re-upload, re-embed. Use the existing CPU-embedding pattern from §3.4 of the reference doc to keep GPUs free for chat.

### 6.2 No-think proxy regex check

The proxy strips `<think>...</think>` blocks via `re.compile(r"<think>.*?</think>", re.DOTALL)`. Confirm Qwen 3.6 served by llama-server emits the same delimiter format:

```bash
curl -s http://127.0.0.1:11440/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "coder-36",
    "stream": false,
    "messages": [{"role":"user","content":"Add 2 and 2. Show your work first."}]
  }' | jq -r '.choices[0].message.content' | head -30
```

Expect literal `<think>` and `</think>` tags surrounding the reasoning. If the format has shifted (e.g., `<thinking>`, structured `reasoning_content` field), the proxy regex needs updating. As of this writing the format is `<think>\n...\n</think>\n\n`, unchanged from Ollama.

### 6.3 OpenCode tool-call parity

Real risk. Qwen3.5/3.6 tool-call templates have had multiple fix passes; the template baked into your existing Ollama blob and the latest unsloth GGUF can disagree on tool-call delimiters. Test the actual OpenCode tool-call path against both backends with the *same* prompt and compare. The test isn't scriptable from curl — drive it from OpenCode pointed at `http://192.168.6.110:11440/v1` (validation port) and watch the tool-call trace. Look for:

- Tool name extracted correctly (not nested in a `<tool_call>` wrapper that wasn't parsed)
- Arguments parsed as JSON, not as a string
- No `<think>` content leaking into the tool-call payload

If any of those fail, pin to a known-good llama.cpp commit (search the issue tracker for "Qwen3 tool call" near your build date) or fall back to the Ollama blob via Path A in §3.4 until the template ships clean.

### 6.4 Long-context corruption smoke test

llama.cpp issue #20052 reports layer-split garbage output above 2K context on certain dual-3090 boards without P2P. The T7910 chipset is older server-grade and may behave differently, but verify:

```bash
# Generate at 32K, 64K, 128K, 256K. Output should be coherent at every length.
for ctx in 32768 65536 131072 262144; do
  echo "=== context fill: $ctx ==="
  python /opt/migration/longctx-smoke.py --port 11440 \
    --model "$([ "$ctx" -le 65536 ] && echo coder-36 || \
              [ "$ctx" -le 131072 ] && echo coder-long || echo coder-256k)" \
    --fill "$ctx"
done
```

`longctx-smoke.py` should fill the context with a long input (e.g., a chunked book or repeated technical document), then ask a question that requires reading from the early part of the context. Failure mode: nonsense output, repeated tokens, or refusal to engage with content the model demonstrably had access to.

### 6.5 Workspace-prompt regression check

With `--system-prompt-file` removed (§3.6), the only system prompt the model sees on a RAG request is the workspace's `openAiPrompt`. The prompts were tuned against Ollama's rendering of the chat template; the unsloth GGUF's template may render system+user message slightly differently. Run the reference doc's positive-retrieval smoke (Appendix C, item 7) and confirm:

- The response still cites VCF-NET-014 / VCF-IP-015 for the edge uplink subnet test.
- The response style (refusal phrasing, citation format) matches what AnythingLLM users currently see.

Recoverable if it drifts (re-tune `openAiPrompt`), but expensive — flag it before flipping the embedder.

---

## 7. Cutover procedure

All five gates green. Execute in order — several steps have dependencies on the prior step. Cutover is a **port-binding swap between two LXC containers**: the `ollama` container releases `:11434`, the `llama-cpp` container claims it.

1. **Snapshot LanceDB and Ollama-container state.** AnythingLLM runs in its own LXC; quiesce it during the snapshot.
   ```bash
   # --- HOST shell ---
   lxc exec anythingllm -- systemctl stop anythingllm 2>/dev/null \
     || lxc stop anythingllm   # whichever pattern your AnythingLLM LXC uses

   df -h /opt/backups
   umask 077
   sudo tar -czf /opt/backups/lancedb-pre-cutover-$(date +%F).tgz \
     /opt/anythingllm/storage/vector-cache /opt/anythingllm/storage/lancedb
   sudo chmod 600 /opt/backups/lancedb-pre-cutover-$(date +%F).tgz
   sudo tar -tzf /opt/backups/lancedb-pre-cutover-$(date +%F).tgz >/dev/null \
     && echo "backup verified" || echo "BACKUP CORRUPT — STOP"

   # Snapshot the Ollama container itself for instant rollback
   lxc snapshot ollama pre-llamacpp-cutover

   # Restart AnythingLLM
   lxc start anythingllm 2>/dev/null \
     || lxc exec anythingllm -- systemctl start anythingllm
   ```

2. **Tear down validation, install production llama-swap unit (inside container).**
   ```bash
   lxc exec llama-cpp -- bash -c '
     systemctl stop llama-swap-validation
     systemctl disable llama-swap-validation
     rm /etc/systemd/system/llama-swap-validation.service
     # Install the production unit from Appendix A at
     # /etc/systemd/system/llama-swap.service (do this manually or via
     # `lxc file push llama-swap.service llama-cpp/etc/systemd/system/`)
     systemctl daemon-reload
   '
   ```

3. **Flip the host proxy device from `:11440` → `:11434`, and stop the Ollama container.** Order matters — Ollama must release `:11434` before llama-cpp's proxy device can bind it.
   ```bash
   # --- HOST shell ---

   # Stop the Ollama LXC. Don't `lxc delete` — keep it for rollback.
   lxc stop ollama
   lxc config set ollama boot.autostart false

   # Remove the validation proxy device on llama-cpp
   lxc config device remove llama-cpp llama-swap-validation

   # Add the production proxy device on :11434
   lxc config device add llama-cpp llama-swap-prod proxy \
     listen=tcp:127.0.0.1:11434 connect=tcp:127.0.0.1:11434
   # If the no-think proxy or AnythingLLM is on a different host on the LAN,
   # use listen=tcp:0.0.0.0:11434 with apiKeys: enabled in §5 config.

   # Start llama-swap inside the container with the production unit
   lxc exec llama-cpp -- systemctl enable llama-swap
   lxc exec llama-cpp -- systemctl start llama-swap
   lxc exec llama-cpp -- systemctl status llama-swap

   # Verify from the host
   curl -s http://127.0.0.1:11434/v1/models | jq
   ```

4. **Reconfigure AnythingLLM's embedder provider.**
   - UI: Settings → Embedding Preference
   - Provider: change from **Ollama** to **Generic OpenAI**
   - Base URL: `http://127.0.0.1:11434/v1` (or `http://192.168.6.110:11434/v1` if AnythingLLM is on a different host — but the reference setup has it co-resident)
   - API key: leave blank if llama-swap is on `127.0.0.1` with no `apiKeys:` configured. If `apiKeys:` is set, use one of the configured keys.
   - Model: `qwen3-embed`
   - Dimension: `1024` (must match what's in LanceDB)
   - Save and verify in the UI that the test embed succeeds.

5. **No-think proxy** keeps its config (`UPSTREAM = http://127.0.0.1:11434`) — llama-cpp's proxy device now claims that port. The no-think proxy runs in its own LXC; restart it from the host:
   ```bash
   # --- HOST shell ---
   lxc exec nothink-proxy -- systemctl restart nothink-proxy
   ```

6. **Run the reference doc's smoke tests (Appendix C).** Specifically:
   - VCF refusal path returns "Not in the provided VCF documents." for the France test.
   - VCF positive retrieval still cites VCF-NET-014 / VCF-IP-015 for the edge uplink subnet test.
   - All three MCP containers respond to `/sse` GET.

7. **Watch logs for one hour** under real workload (an OpenCode session, a few AnythingLLM RAG queries, the auto-updater's next scheduled run if it falls in window).
   ```bash
   # llama-swap inside the container
   lxc exec llama-cpp -- journalctl -u llama-swap -f

   # The host's LXC events (any container starts/stops/crashes)
   lxc monitor --type=lifecycle
   ```

> **Do not modify the Ollama LXC's internal config during cutover.** Stopping the container with `lxc stop ollama` and disabling autostart is sufficient — its filesystem and config are preserved untouched, ready for rollback. The Phase-0 snapshot taken in step 1 is belt-and-braces.

---

## 8. Rollback procedure

Order matters — the `:11434` port can only be claimed by one LXC proxy device at a time. Release llama-cpp's claim before re-binding Ollama's container.

```bash
# --- HOST shell ---

# 1. Stop the llama-cpp LXC so its proxy device on :11434 releases.
lxc exec llama-cpp -- systemctl stop llama-swap
lxc config device remove llama-cpp llama-swap-prod
lxc stop llama-cpp
lxc config set llama-cpp boot.autostart false

# 2. Restart the Ollama container. Its config (LAN binding, env vars from
#    reference doc §3.2) is preserved untouched in the LXC filesystem.
lxc start ollama
lxc config set ollama boot.autostart true
sleep 5
curl -s http://127.0.0.1:11434/api/tags | jq '.models[].name'

# 3. Reconfigure AnythingLLM's embedder back to Ollama (UI):
#    Settings → Embedding Preference
#    Provider: Ollama
#    Base URL: http://127.0.0.1:11434
#    Model: qwen3-embed-compact:latest
#    Dimension: 1024

# 4. Restart the no-think proxy so it drops cached connections.
lxc exec nothink-proxy -- systemctl restart nothink-proxy

# 5. Verify with the reference doc's smoke tests (Appendix C).
```

The procedure is idempotent against partial states except step 3 (UI reconfig) — that step depends on AnythingLLM's current saved settings. If you can't be sure the embedder UI was saved before rolling back, restart AnythingLLM and re-enter the values explicitly.

If something is irrecoverably wrong with the Ollama container's filesystem, restore from the snapshot taken at §7 step 1:

```bash
lxc stop ollama --force
lxc restore ollama pre-llamacpp-cutover
lxc start ollama
```

### 8.1 Mid-cutover rollback (if §7 failed partway)

If §7 failed at step 4 (AnythingLLM embedder reconfig) without reaching step 6, the system is in a half-state: llama-swap is up on `:11434` but AnythingLLM may still be configured for Ollama-native `/api/embeddings` and is failing. Run §8 steps 1–4 in order. Skip the snapshot restore (§7 step 1's tarball) unless step 6 actually wrote bad embeddings into LanceDB — which only happens if §6.1 was bypassed.

If §6.1 was bypassed and retrieval quality has measurably collapsed:

```bash
sudo systemctl stop anythingllm
sudo tar -xzf /opt/backups/lancedb-pre-cutover-$(date +%F).tgz -C /
sudo systemctl start anythingllm
```

---

## 9. Post-cutover validation

- **Throughput** — re-run the same prompt against `coder-36` pre- and post-cutover, capture tok/s. Expect ~25–40% improvement; if you don't see it, check that `-fa on` and `--cache-type-k/v q8_0` are actually in effect (`curl http://127.0.0.1:11434/v1/models | jq` and the llama-swap logs).
- **VRAM split** — `nvidia-smi --query-gpu=memory.used --format=csv -l 1` while a model is loaded. Should be roughly even across the three cards. If not, adjust `--tensor-split`.
- **Auto-updater** — the next scheduled run should complete with `errors=0, aborted=0` in its SQLite state. The updater's only LLM dependency is the embedder, so this is also the second-order check that §6.1 stayed green.
- **No-think proxy** — confirm `<think>` blocks are stripped on the RAG path (`:11435`) and preserved on the agentic path (`:11436`). Send a Qwen 3.6 request to each and inspect the response.
- **256K smoke** (if `coder-256k` is in active use) — fill `coder-256k` at 256K context and confirm output is coherent. Capture tok/s; expect 7–10 tok/s.

---

## 10. Modelfile → llama.cpp reference

Direct flag mapping for everything currently in the Modelfiles in [§3.4 of the reference doc](./local-gpu-cluster-reference.md#34-custom-modelfiles):

| Modelfile | llama-server flag | Per-request OpenAI body field |
|---|---|---|
| `FROM qwen2.5:32b-instruct-q4_K_M` | `--model /opt/gguf/qwen2.5-32b-instruct-q4_K_M.gguf` | (server-level) |
| `PARAMETER temperature 0.3` | `--temp 0.3` | `temperature` |
| `PARAMETER top_k 20` | `--top-k 20` | `top_k` |
| `PARAMETER top_p 0.95` | `--top-p 0.95` | `top_p` |
| `PARAMETER min_p 0` | `--min-p 0` | `min_p` |
| `PARAMETER num_ctx 32768` | `--ctx-size 32768` | (server-level) |
| `PARAMETER num_predict 4096` | `--n-predict 4096` | `max_tokens` |
| `PARAMETER repeat_penalty 1` | `--repeat-penalty 1` | `repeat_penalty` |
| `PARAMETER presence_penalty 0` | `--presence-penalty 0` | `presence_penalty` |
| `SYSTEM "..."` | (no server-side equivalent — flag was removed) | per-request system message |
| `TEMPLATE` (embedded) | auto-loaded from GGUF; add `--jinja` for Qwen3 thinking | — |
| (implicit Ollama defaults) | `-fa on`, `--cache-type-k q8_0`, `--cache-type-v q8_0` | — |

No clean Ollama-side equivalent exists for `--cache-type-k/v` or `--n-cpu-moe` — those are wins llama.cpp gives you, not translations.

---

## 11. TurboQuant: deferred, not adopted

Status as of May 2026:

- TurboQuant is a **KV-cache** quantization scheme (random-rotation + scalar quant, ~3.25 bits/value at `turbo3`, ~4.25 at `turbo4`), not a weight format. It does not replace Q4_K_M and you don't quantize models to TurboQuant — you enable it at runtime via `--cache-type-k turbo3`-style flags.
- **Not in mainline llama.cpp.** PR #21089 still open; tracked in [discussion #20969](https://github.com/ggml-org/llama.cpp/discussions/20969). Lives in forks (atomicmilkshake, TheTom). TheTom's fork README has minimal documentation — no benchmarks, no documented flag names — and the published benchmark file names suggest Apple M-series testing rather than Ampere.
- **No published RTX 30-series benchmarks on Qwen MoE.** Headline 8× numbers are H100. Apple Metal benchmarks show TurboQuant 8× *slower* than q8_0 on Qwen3.5-35B-A3B — the cross-architecture story is not consistent.
- **256K context is achievable on mainline alone.** With KV q8_0, 256K context fits in ~32 GB of the 36 GB pool (see `coder-256k` in §5). TurboQuant would shave that to ~27 GB, freeing room for `--parallel 2` at 256K or co-residency with the embedder + a second small process. Worthwhile — but only if (a) those use cases materialize, and (b) the kernel quality on Ampere is established by published benchmarks.

**Revisit when** TurboQuant lands in mainline llama.cpp *and* someone publishes a benchmark on Ampere consumer hardware (3060/3070/3090) running Qwen MoE that shows a clear win on both throughput and accuracy. Until both conditions are met, the migration in this document stops at mainline llama.cpp + KV-cache q8_0.

---

## 12. Known issues and mitigations

| Issue | Severity | Mitigation |
|---|---|---|
| Embedding-vector parity drift breaks LanceDB retrieval silently | **HARD GATE** | §6.1 cosine-parity test before cutover; full re-embed if it fails |
| Workspace `openAiPrompt` style drift after losing Modelfile SYSTEM | MEDIUM | §6.5 regression check; re-tune workspace prompt if response style shifts |
| Qwen3.6 tool-call template regression in OpenCode | MEDIUM | §6.3 OpenCode tool-call test; pin to known-good GGUF and llama.cpp commit |
| Layer-split corruption at long context (issue #20052 family) | MEDIUM | §6.4 long-context smoke at 32K/64K/128K/256K; if reproducible, fall back to single-GPU `--split-mode none` for affected model |
| `<think>` format change breaks no-think proxy regex | LOW | §6.2 visual check; format is unchanged from Ollama as of this writing |
| Chat-model thrashing — quick alternation between RAG and coding evicts each cold-load | MEDIUM | This is a behavior change from Ollama's lazy LRU. Document expected 30–90 s cold-load on swap. If thrashing is operational pain, segregate users (RAG via AnythingLLM, coding via OpenCode) by time-of-day or accept the latency. |
| Model-swap window returns 503 to in-flight requests | LOW | `healthCheckTimeout: 180` in §5; AnythingLLM has no built-in retry — users see an error during the 30–90 s window |
| llama-swap dies mid-request, cascading to no-think proxy hang | LOW | systemd `Restart=on-failure` on llama-swap; consider adding a per-request timeout in the no-think proxy to avoid indefinite client wait |
| llama-server child OOM-killed under VRAM pressure | LOW | Don't run two stacks against the same GPUs (already noted in §3.6); monitor `dmesg` for OOM events |
| NUMA cross-socket effects on dual-Xeon | LOW | GPUs 1 & 2 sit on the second CPU's root complex (reference §2.5). With one llama-server process per model, all three cards are pinned to a single process. Likely <5% impact; wrap `cmd:` in `numactl --interleave=all` if measured to matter |
| `--mmap` / `--mlock` tuning premature optimization | informational | Default mmap-on, mlock-off; only revisit if running CPU-offload via `--n-cpu-moe` |
| Ollama and llama.cpp coexisting during validation OOM the GPU | LOW | Don't load 32B + 35B concurrently across both stacks during §6 |
| AnythingLLM embedder provider switch (Ollama → Generic OpenAI) is a UI change not in version control | LOW | Document the embedder config separately; consider periodic AnythingLLM settings export |
| `--verbose` on llama-server logs full prompts to journald | informational | Don't add `--verbose` or `-v` to any `cmd:` in production config.yaml |

---

## Appendix A — systemd units (hardened)

Two units to install: one **inside** the `llama-cpp` LXC container that runs llama-swap, and one **on the host** that ensures the container itself starts at boot (matching the cluster's existing pattern for Ollama / AnythingLLM / MCP containers).

### A.1 Inside the LXC container — `llama-swap.service`

`/etc/systemd/system/llama-swap.service` *inside the `llama-cpp` container* — production unit, installed at §7 step 2:

```ini
[Unit]
Description=llama-swap — model router for llama-server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ollama
Group=ollama
WorkingDirectory=/opt/llama-swap
Environment="CUDA_VISIBLE_DEVICES=0,1,2"
ExecStart=/opt/llama-swap/llama-swap \
  --config /opt/llama-swap/config.yaml \
  --listen 127.0.0.1:11434
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

# --- hardening ---
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=no            # required for /dev/nvidia*
ReadWritePaths=/opt/llama-swap /opt/gguf /run/llama-swap
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=no    # required for CUDA JIT (W+X pages)
RestrictSUIDSGID=yes
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
AmbientCapabilities=
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

Push it from host:

```bash
# --- HOST shell ---
lxc file push /tmp/llama-swap.service llama-cpp/etc/systemd/system/llama-swap.service
lxc exec llama-cpp -- systemctl daemon-reload
```

Notes:
- `Conflicts=ollama.service` from prior drafts is removed — the Ollama service is in a separate LXC, so systemd-level conflicts don't apply. Mutual exclusion is enforced at the host level via the `:11434` port-forward device (only one LXC can claim that listen port at a time).
- `MemoryDenyWriteExecute=no` and `PrivateDevices=no` are necessary carve-outs for CUDA JIT and GPU device access. All other directives apply cleanly.
- After installing, run `lxc exec llama-cpp -- systemd-analyze security llama-swap.service` and confirm exposure score ≤ 5.
- The `ollama` service account inside the container was created in §3.2.3 (it does not need to match the host's user database — privileged LXC has its own `/etc/passwd`).

### A.2 On the host — autostart the LXC container at boot

LXD/Incus has built-in container autostart; you don't strictly need a systemd unit. Either:

```bash
# --- HOST shell ---
lxc config set llama-cpp boot.autostart true
lxc config set llama-cpp boot.autostart.priority 50    # after lower-priority services
```

Or wrap it explicitly in a systemd unit if your homelab convention uses host-level systemd for LXC orchestration. `/etc/systemd/system/lxc-llama-cpp.service`:

```ini
[Unit]
Description=Auto-start LXC container llama-cpp
After=lxd.service
Requires=lxd.service

[Service]
Type=oneshot
ExecStart=/usr/bin/lxc start llama-cpp
ExecStop=/usr/bin/lxc stop llama-cpp
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

The no-think proxy LXC (`nothink-proxy` per reference doc §4.4) needs no changes — it already points at `:11434` upstream, and that's now llama-cpp's port-forward.

---

## Appendix B — file layout

Two filesystems matter — the **host** filesystem (where bind-mounted data and host-side scripts live) and the **`llama-cpp` LXC container's** filesystem (where the build artifacts and llama-swap config live).

### B.1 Host filesystem

```text
/opt/                              # host paths
├── gguf/                          # bind-mounted into the llama-cpp LXC at /opt/gguf
│   ├── qwen2.5-32b-instruct-q4_K_M.gguf       (symlink to ollama LXC's blob, or fresh download)
│   ├── qwen3.6-35b-a3b-UD-Q4_K_M.gguf         (unsloth recommended)
│   └── qwen3-embedding-0.6B-f16.gguf
├── migration/                     # host-side migration tooling
│   ├── test-embedding-parity.py   # §6.1, runs on host (talks to two LXC containers)
│   └── longctx-smoke.py           # §6.4
└── backups/                       # mode 700, owned root
    └── lancedb-pre-cutover-YYYY-MM-DD.tgz     # mode 600

/var/lib/lxd/containers/           # (or /var/lib/incus/, depending on stack)
├── ollama/                        # existing — kept for rollback
├── anythingllm/                   # existing
├── nothink-proxy/                 # existing
├── anythingllm-mcp/               # existing
├── broadcom-techdocs-mcp/         # existing
├── sdg-mcp/                       # existing
├── vcf-doc-updater/               # existing
├── searxng/                       # existing
└── llama-cpp/                     # NEW — provisioned in §3.2.1
```

### B.2 Inside the `llama-cpp` LXC container

```text
/opt/                              # paths inside the container
├── llama.cpp/                     # built from source (§3.2.3)
│   └── build/bin/llama-server     # owned ollama:ollama (container's local user)
├── llama-swap/
│   ├── llama-swap                 # binary (owned ollama:ollama)
│   └── config.yaml                # model definitions (§5)
└── gguf/                          # bind-mounted from host /opt/gguf — same files
```

Existing LXC containers (`anythingllm`, `nothink-proxy`, `anythingllm-mcp`, `broadcom-techdocs-mcp`, `sdg-mcp`, `vcf-doc-updater`, `searxng`) are unchanged by this migration. The `ollama` container is stopped and disabled at cutover but its filesystem is preserved for rollback.

Backup retention — prune older than 30 days:

```bash
sudo find /opt/backups -name 'lancedb-pre-cutover-*.tgz' -mtime +30 -delete
```

---

## License

This document inherits the licensing of the parent reference: text under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), embedded code snippets under [MIT](https://opensource.org/licenses/MIT).
