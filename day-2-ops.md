# Day-2 Operations Guide

**Operating a deployed local-gpu-cluster.** What to do *after* [`setup-runbook.md`](./setup-runbook.md) finishes.

This guide is for the operator who inherits or maintains a running cluster — checking health, rotating keys, swapping models, refreshing RAG corpora, planning updates, replacing hardware. It is deliberately not a re-explanation of how the cluster was built. For that, see the runbook (the *how* of deployment) or [`local-gpu-cluster-v2.md`](./local-gpu-cluster-v2.md) (the *why* of the architecture).

**Pointer discipline:** every concrete value (env vars, model names, ports, paths) is a *link* to its source-of-truth file rather than a copy. If a value here looks stale, check the linked file — that's the truth.

**Tested vs speculative:** procedures marked ✅ have been run against the live cluster. Procedures marked ⚠ are reasonable but not yet exercised — verify on a snapshot first.

---

## § 1. Quick-jump index

| If you need to... | Go to | Also see |
|---|---|---|
| Check if the cluster is healthy right now | [§ 2](#-2-cluster-health--observability) | `60-verify.sh` |
| Diagnose something that's broken | [§ 3](#-3-common-troubleshooting) | [LESSONS.md](./LESSONS.md) |
| Swap or update the chat / embed / rerank model | [§ 4](#-4-model-management) | [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh), [`config.env.example`](./scripts/config.env.example) |
| Understand or adjust input-token caps | [§ 5](#-5-three-tier-token-limit-layering) | — |
| Tune rate limits, CORS, ALIAS_MAP, or read access logs | [§ 6](#-6-router-operations) | [`scripts/files/router-app.py`](./scripts/files/router-app.py), [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh) |
| Use or extend server-side tool execution (Tavily, web_fetch, etc.) | [§ 6.8](#-68-server-side-tool-execution) | [`scripts/files/router-app.py`](./scripts/files/router-app.py) `TOOLS` registry |
| Rotate an API key | [§ 7](#-7-secrets--key-rotation) | — |
| Add a new RAG source / refresh / wipe / orphans | [§ 8](#-8-rag-operations) | [`scripts/rag/README.md`](./scripts/rag/README.md) |
| Change embedder or reranker model | [§ 9](#-9-embedderreranker-retuning--re-ingest-playbook) | — |
| Snapshot, back up, restore | [§ 10](#-10-storage--backups) | [setup-runbook.md §4.8-4.9](./setup-runbook.md) |
| Update PVE / LXC / llama.cpp / AnythingLLM / ROCm | [§ 11](#-11-updates) | — |
| Establish a performance baseline or detect regressions | [§ 12](#-12-performance-baselines) | — |
| Add/replace GPU, RAM, NVMe | [§ 13](#-13-hardware-changes) | [local-gpu-cluster-v2.md §1.3](./local-gpu-cluster-v2.md) |
| Cross-reference index | [§ 14](#-14-when-to-consult-what) | — |

**Authority hierarchy** (when this doc and another disagree, the link wins):

| Topic | Authoritative source |
|---|---|
| LXC inventory, IPs, ports | [README.md](./README.md) |
| What scripts do | [scripts/README.md](./scripts/README.md) |
| Detailed deployment steps | [setup-runbook.md](./setup-runbook.md) |
| Architecture rationale | [local-gpu-cluster-v2.md](./local-gpu-cluster-v2.md) |
| Router code + endpoints | [scripts/files/router-app.py](./scripts/files/router-app.py) |
| Chat / embed / rerank unit defaults | [scripts/51-lxc-amd.sh](./scripts/51-lxc-amd.sh) |
| Router env defaults | [scripts/53-lxc-router.sh](./scripts/53-lxc-router.sh) |
| AnythingLLM env defaults | [scripts/54-lxc-anythingllm.sh](./scripts/54-lxc-anythingllm.sh) |
| RAG ingest system | [scripts/rag/README.md](./scripts/rag/README.md) |
| Past pain points + how they were fixed | [LESSONS.md](./LESSONS.md) |
| Current session state / decisions in flight | [SESSION_HANDOFF.md](./SESSION_HANDOFF.md) |

---

## § 2. Cluster health & observability

> **When to read this:** every time you sit down at the cluster. "Is anything broken right now?"

### Quick reference

One-liner full smoke test (run on the PVE host):

```bash
/root/local-gpu-cluster/scripts/60-verify.sh
```

If that's not deployed, the equivalent manual probe:

```bash
# LXC inventory — all 4 should be running
pct list | awk 'NR==1 || $1 ~ /^15[1345]$/'

# Chat / embed / rerank units on LXC 151
pct exec 151 -- systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank

# Router /healthz (no auth required)
curl -sf http://192.168.6.153:8000/healthz | jq

# ROCm sees both V620s
pct exec 151 -- rocminfo 2>/dev/null | grep -c "gfx1030"   # expect 2

# Fan-bridge (only if 56-fan-control.sh was run)
systemctl is-active v620-fan-bridge.service 2>/dev/null && cat /var/lib/v620-temps/current-temp

# /tank disk usage
zfs list tank tank/models tank/anythingllm tank/rag-state tank/backups
```

### Details

**Log locations:**

| Source | Where | How to tail |
|---|---|---|
| Chat / embed / rerank | LXC 151 journald | `pct exec 151 -- journalctl -u llamacpp-chat -f` (or `-embed` / `-rerank`) |
| Router access log (JSON lines, 50 MB rotation × 5) | LXC 153 `/var/log/llm-router/access.log` | `pct exec 153 -- tail -f /var/log/llm-router/access.log \| jq .` |
| Router systemd | LXC 153 journald | `pct exec 153 -- journalctl -u llm-router -f` |
| AnythingLLM container | LXC 154 | `pct exec 154 -- docker logs -f anythingllm` |
| MCP bridge | LXC 155 journald | `pct exec 155 -- journalctl -u mcp-sdg -f` |
| Fan bridge | PVE host journald | `journalctl -u v620-fan-bridge -f` |
| V620 temp publisher | LXC 151 journald | `pct exec 151 -- journalctl -u v620-temp-publish -f` |

**Router access-log JSON shape** (one line per request):

```json
{"ts": 1779721615.69, "route": "/v1/chat/completions", "model": "rag-qwen3.6",
 "input_tokens": 12000, "output_tokens": 540, "duration_ms": 8200,
 "status": 200, "client_ip": "192.168.6.154"}
```

Useful one-liners:

```bash
# Average chat duration over the last 100 requests
tail -100 access.log | jq -s 'map(select(.route=="/v1/chat/completions")) | (map(.duration_ms) | add / length)'

# 95th-percentile input tokens
tail -1000 access.log | jq -s 'map(.input_tokens) | sort | .[(length * 0.95 | floor)]'

# Anything that returned 5xx in the last hour
awk -v cutoff=$(($(date +%s) - 3600)) '$0 ~ /"ts": [0-9]+\.[0-9]+/ {print}' access.log \
  | jq 'select(.status >= 500)'
```

**Prometheus / metrics endpoint:**

- URL: `http://192.168.6.153:8000/metrics`
- IP-allowlisted via `METRICS_ALLOWED_IPS` (default `127.0.0.1,192.168.6.150` per [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh)). Calls from other IPs get 403.
- Sample query (from the PVE host or loopback):

```bash
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -s http://192.168.6.153:8000/metrics | grep -E "http_request_duration_seconds_(count|sum)" | head
```

**Per-LXC resource monitoring:**

```bash
# CPU / memory / I/O for all running LXCs at once
for vmid in 151 153 154 155; do
  echo "=== $vmid ==="
  pct exec $vmid -- bash -c "free -h | head -2; uptime"
done

# AnythingLLM container memory + heap
pct exec 154 -- docker stats anythingllm --no-stream --format \
  "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"

# ZFS ARC pressure (host)
arc_summary | head -40   # if zfs-auto-snapshot package installed; else /proc/spl/kstat/zfs/arcstats
```

**V620 GPU status (from LXC 151):**

```bash
pct exec 151 -- rocm-smi --showuse --showmemuse --showtemp -d 0 1
# Expect: both cards showing similar VRAM usage (chat tensor-split is even)
# and temps below 85°C.
```

### Verification

A healthy cluster shows:

- All 4 LXCs `running` in `pct list`
- 3 services `active` on LXC 151 (chat / embed / rerank)
- Router `/healthz` returns `{"ok": true, "upstream": {"chat": "ok", "embed": "ok", "rerank": "ok"}}`
- ROCm shows exactly 2 `gfx1030` agents
- V620 temps idle around 30-45°C, under load 60-80°C (fan bridge keeps cards below 85°C)
- `/tank` has at least 15% free
- Access log has recent entries with `status: 200` and `duration_ms` consistent with [§ 12](#-12-performance-baselines) expectations

---

## § 3. Common troubleshooting

> **When to read this:** something looks broken, or [§ 2](#-2-cluster-health--observability) flagged an anomaly.

### Quick symptom → action

| Symptom | First action | Section |
|---|---|---|
| Chat unit won't start | `pct exec 151 -- journalctl -u llamacpp-chat -n 50` | [§ 3.1](#-31-chat-unit-wont-start) |
| Slow first token after idle | Check 5-min keepalive timer | [§ 3.2](#-32-slow-first-token-after-idle) |
| Embedder silently dropping chunks | Check per-slot ctx vs chunk size | [§ 3.3](#-33-embedder-dropping-chunks) |
| Router 5xx / `service_degraded` | Walk upstream LXC 151 services | [§ 3.4](#-34-router-5xx--service_degraded) |
| ROCm only sees one V620 | Restart LXC 151 or check KFD | [§ 3.5](#-35-rocm-only-sees-one-v620) |
| AnythingLLM OOM | Bump `ANYTHINGLLM_MEMORY` in [54-lxc-anythingllm.sh](./scripts/54-lxc-anythingllm.sh) | [§ 3.6](#-36-anythingllm-oom) |
| Fan-bridge stopped, fans loud | `systemctl status v620-fan-bridge` on host | [§ 3.7](#-37-fan-bridge-stopped) |
| `413 request too large` | See token-limit layering | [§ 5](#-5-three-tier-token-limit-layering) |
| `403` from router | Bearer auth — wrong / missing key | [§ 3.8](#-38-client-bearer-auth-failures) |
| Log says `no implementations specified for speculative decoding` | **Expected, not a bug** | [§ 3.9](#-39-no-implementations-specified-for-speculative-decoding) |

### § 3.1 Chat unit won't start

```bash
pct exec 151 -- systemctl status llamacpp-chat
pct exec 151 -- journalctl -u llamacpp-chat -n 100 --no-pager
```

Common causes:

- **First-start model download stalled.** The chat unit's `--hf-repo` downloads ~22 GB on first start. `TimeoutStartSec=1800` (30 min) allows for this. If slower, the `warm-chat.sh` ExecStartPost will eventually time out but the model download continues in the background; restart the unit once download visible in `/opt/models/.cache/`.
- **VRAM OOM at weight load.** Most often when someone tries to swap to a larger model. `journalctl` shows `hipMalloc failed` or `out of memory`. Recovery: edit [`scripts/config.env`](./scripts/config.env.example) to revert `LLAMA_HF_REPO` / `LLAMA_HF_QUANT`, re-run `scripts/51-lxc-amd.sh`. Then do a VRAM budget exercise per [§ 4.4](#-44-vram-budget-template) before retrying.
- **KFD ioctl rejected** (`HSA_STATUS_ERROR_OUT_OF_RESOURCES`). Usually means the LXC's AppArmor profile got reset. Confirm `/etc/pve/lxc/151.conf` still contains `lxc.apparmor.profile: unconfined`. Restart the LXC: `pct restart 151`.

### § 3.2 Slow first token after idle

The chat model is `--mlock`'d into VRAM so weights don't page out, but the prompt-processing path benefits from a warm host RAM prefix cache.

- Verify the 5-min keepalive timer is running: `pct exec 153 -- systemctl status llm-router-keepalive.timer` (installed by [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh) phase 7.5).
- If it's running but first-token is still slow (>5s for short prompts), inspect access-log `duration_ms` distribution and compare to [§ 12](#-12-performance-baselines).

### § 3.3 Embedder dropping chunks

llama.cpp's embedder divides `--ctx-size` by `--parallel` to get per-slot context. Inputs exceeding the per-slot capacity silently truncate or 413.

Current defaults from [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh): `EMBED_CTX=65536`, `EMBED_PARALLEL=4` → **16 384 tokens per slot.**

If embedder logs show requests being truncated, or AnythingLLM reports `429`/`400` on embed:

```bash
# Inspect what's actually configured
pct exec 151 -- systemctl cat llamacpp-embed | grep -E "ctx-size|parallel"

# If you need larger per-slot context (e.g., for huge legal doc pages):
# Edit scripts/config.env: EMBED_CTX=65536 stays, EMBED_PARALLEL=2 → 32 K per slot
# Then re-run scripts/51-lxc-amd.sh
```

Also align downstream caps:

- Router: `MAX_EMBED_INPUT_TOKENS` in [`/etc/router.env`](./scripts/53-lxc-router.sh) must match per-slot ctx
- AnythingLLM: `EMBEDDING_MODEL_MAX_CHUNK_LENGTH` in [`/opt/anythingllm/.env`](./scripts/54-lxc-anythingllm.sh) must match

All three values currently sit at 16 384.

### § 3.4 Router 5xx / `service_degraded`

The router's [`sse_stream_with_keepalive`](./scripts/files/router-app.py) emits a `service_degraded` SSE frame when an upstream returns ≥500 or the connection drops mid-stream.

```bash
# Recent 5xx in access log
pct exec 153 -- tail -200 /var/log/llm-router/access.log | jq 'select(.status >= 500)'

# Check upstream health
curl -sf http://192.168.6.153:8000/healthz | jq .upstream
# If any upstream != "ok", find the failed unit on LXC 151
pct exec 151 -- systemctl status llamacpp-chat llamacpp-embed llamacpp-rerank
```

If an upstream is wedged, restart it: `pct exec 151 -- systemctl restart llamacpp-chat`. The router will recover automatically on the next request.

### § 3.5 ROCm only sees one V620

```bash
pct exec 151 -- rocminfo | grep -c "gfx1030"   # should be 2
```

If it returns 1 (or 0):

1. Restart LXC 151: `pct restart 151`. Most KFD wedges resolve after a clean LXC restart.
2. If still broken, check the host: `lspci -nn | grep 73a1` should show **two** entries. If only one, the card may have crashed at the PCIe level — power-cycle the host.
3. Confirm AppArmor: `grep apparmor.profile /etc/pve/lxc/151.conf` → must show `unconfined`.

### § 3.6 AnythingLLM OOM

AnythingLLM's Node.js heap + LanceDB index can saturate the LXC's RAM when handling thousands of docs.

Per [`scripts/54-lxc-anythingllm.sh:25`](./scripts/54-lxc-anythingllm.sh), default is 16 GB + 2 GB swap. To bump:

```bash
# Set ANYTHINGLLM_MEMORY in config.env (e.g., 24 GB) then
./scripts/54-lxc-anythingllm.sh
# OR live:
pct set 154 --memory 24576 --swap 2048
pct reboot 154
```

### § 3.7 Fan-bridge stopped

Fan-bridge is **optional** — only deployed if [`scripts/56-fan-control.sh`](./scripts/56-fan-control.sh) was run with `FAN_PWM_PATH` set in `config.env`. If it's not deployed, the BIOS fan curve drives the V620 shroud fans (safe but loud).

```bash
# Is it deployed?
systemctl status v620-fan-bridge.service 2>/dev/null || echo "fan-bridge not installed (BIOS curve in effect)"

# If deployed but inactive
systemctl restart v620-fan-bridge.service
journalctl -u v620-fan-bridge --since "5 min ago"
```

The LXC-side publisher also has to be running:

```bash
pct exec 151 -- systemctl status v620-temp-publish.service
cat /var/lib/v620-temps/current-temp   # should be a number, updated every 5s
```

### § 3.8 Client Bearer-auth failures

Router returns `403 unauthorized` if:

- Missing `Authorization: Bearer <key>` header on a non-`/healthz` non-`/metrics` non-`OPTIONS` request
- Wrong key (compared via `secrets.compare_digest`)
- `ROUTER_API_KEY` not set in [`/etc/router.env`](./scripts/53-lxc-router.sh) — router returns 503 `router_misconfigured` instead

Recovery: see [§ 7.2](#-72-rotating-router_api_key).

### § 3.9 "no implementations specified for speculative decoding"

This log line in `llamacpp-chat` is **expected and correct.** Spec-decode is disabled by design on Qwen3.6 (vocab 248,320) because no vocab-compatible small variant exists yet (Qwen3-0.6B is vocab 151,936). See [`setup-runbook.md` § 5.11.3](./setup-runbook.md) for the full rationale.

Do not try to "fix" this. To re-enable when a compatible draft ships, see [§ 4.5](#-45-enabling-spec-decode-when-a-compatible-draft-ships).

---

## § 4. Model management

> **When to read this:** you need to add, swap, or clean up a model on the running cluster.

### § 4.1 Where models live

Models are downloaded by `llama-server`'s `--hf-repo` flag into `LLAMA_CACHE=/opt/models/.cache` inside LXC 151. The bind mount maps that to `/tank/models/.cache` on the host — RW so the cache survives container recreation.

**HuggingFace cache layout** (this is what `--hf-repo unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M` produces):

```
/tank/models/.cache/
└── models--<org>--<repo>/
    ├── snapshots/<rev>/<file>.gguf  ← symlink
    └── blobs/<sha256>               ← actual file
```

Currently deployed (verified live):

| Repo | File | Size | Used by |
|---|---|---|---|
| `unsloth/Qwen3.6-35B-A3B-GGUF` | `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf` | ~22 GB | chat |
| `unsloth/Qwen3.6-35B-A3B-GGUF` | `mmproj-BF16.gguf` | several GB | **unused** (chat has `--no-mmproj`) |
| `Qwen/Qwen3-Embedding-0.6B-GGUF` | `Qwen3-Embedding-0.6B-Q8_0.gguf` | ~1 GB | embed |
| `gpustack/bge-reranker-v2-m3-GGUF` | `bge-reranker-v2-m3-Q4_K_M.gguf` | ~1.5 GB | rerank |
| `unsloth/Qwen3-0.6B-GGUF` | `Qwen3-0.6B-Q4_K_M.gguf` | ~400 MB | **unused** (spec-decode disabled — see [§ 3.9](#-39-no-implementations-specified-for-speculative-decoding)) |
| `unsloth/Qwen3-Coder-Next-GGUF` | `Qwen3-Coder-Next-UD-Q4_K_XL.gguf` | ~50 GB | **unused** (leftover from failed swap, see [SESSION_HANDOFF.md](./SESSION_HANDOFF.md)) |

### § 4.2 Pre-fetching a model

If you want a model on disk *before* the unit starts (e.g., to avoid a slow first-start download):

```bash
# Pre-download into the HF cache layout
huggingface-cli download unsloth/Qwen3.6-35B-A3B-GGUF Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
    --local-dir /tank/models/Qwen3.6-35B-A3B-GGUF/

# Or place it in the standard HF cache structure:
HF_HOME=/tank/models/.cache huggingface-cli download \
    unsloth/Qwen3.6-35B-A3B-GGUF Qwen3.6-35B-A3B-UD-Q4_K_M.gguf
```

Either works. The `--hf-repo` flag in the unit file will detect the cached file and skip download.

To pin to a static path instead of `--hf-repo` (e.g., for air-gapped deployments), edit the systemd unit to use `--model /opt/models/<path>/<file>.gguf` instead of `--hf-repo`. The provisioning script does not currently support this flag automatically — edit `/etc/systemd/system/llamacpp-chat.service` directly, `systemctl daemon-reload`, restart.

### § 4.3 Swapping the chat model ✅

The procedure is **edit config.env + re-run script.** Do not hand-edit the unit file or wget into `/tank/models`.

```bash
cd /root/local-gpu-cluster

# 1. Plan the swap. Run a VRAM budget exercise first (§ 4.4).

# 2. Edit config.env
$EDITOR scripts/config.env
# Change:
#   LLAMA_HF_REPO=unsloth/<new-model>-GGUF
#   LLAMA_HF_QUANT=<new-quant>
#   LLAMA_ALIAS=<new-alias>  (optional — affects what /v1/models returns)

# 3. Re-run the AMD-LXC provisioning script (idempotent)
./scripts/51-lxc-amd.sh

# 4. Watch the unit start (first download is slow)
pct exec 151 -- journalctl -u llamacpp-chat -f
```

The script regenerates `/etc/systemd/system/llamacpp-chat.service` from the new config.env values, runs `systemctl daemon-reload`, and restarts the unit.

If the new model uses a different chat-template / reasoning format, also update [`scripts/files/router-app.py`](./scripts/files/router-app.py) `ALIAS_MAP` — see [§ 6.2](#-62-adding-or-editing-router-aliases).

### § 4.4 VRAM budget template

Captured from the [Qwen3-Coder-Next OOM lesson](./SESSION_HANDOFF.md) (May 2026): a 49.6 GB GGUF was downloaded and tried before checking VRAM math; it failed at weight allocation. Always run this before downloading anything > 30 GB.

| Component | Formula | Current values |
|---|---|---|
| Chat weights (Q4_K_M or UD-Q4_K_M) | ~0.65 × file size | ~22 GB |
| Chat KV cache (Q8) | `--ctx-size × 2 × hidden_size × n_layers × bytes_per_token / 8` ≈ **~11 GB at 256K × Q8** | ~11 GB |
| Embed weights + KV (Q8) | ~1.2 GB | 1.2 GB |
| Rerank weights + KV | ~1.5 GB | 1.5 GB |
| **Pool total** | | **~36 GB of 64 GB pool, ~28 GB headroom** |

Rules of thumb:

- 2× V620 pool = **64 GB**, but allow ~5 GB safety margin → **~59 GB usable** worst-case
- Tensor-split is even (`--tensor-split 1,1`), so chat consumes ~half per card → cap chat weights+KV at **~25 GB per card** in the worst case
- A new chat model is feasible if: weights + KV + 4 GB headroom on each card ≤ 32 GB → `weights + KV ≤ 28 GB`

For a 50 GB GGUF model at Q4_K_M: weights alone are ~32 GB, ~16 GB per card → tight, KV at 128K already pushes per-card to ~22 GB. At 256K, no headroom. **Don't download.** Try a smaller quant or a smaller base.

**Worked example — Qwen3-Coder-Next sizing (May 2026):**

[`unsloth/Qwen3-Coder-Next-GGUF`](https://huggingface.co/unsloth/Qwen3-Coder-Next-GGUF) is an **80B-total / 3B-activated MoE** coder model with the same 256K trained context as Qwen3.6, but with a hybrid **Gated Attention + Gated DeltaNet** architecture. The first attempt (May 2026, [SESSION_HANDOFF.md](./SESSION_HANDOFF.md)) downloaded `UD-Q4_K_XL` (49.6 GB) blind and OOM'd on V620 #0 at a 24.2 GB single allocation. This worked example captures the post-mortem analysis.

Per-card target with embed + rerank co-resident:

| Per-card budget | Value |
|---|---|
| V620 capacity | 32 GB |
| Embed OR rerank pinned (~1.5 GB worst case) | −1.5 GB |
| Chat KV at 256K Q8, half per card (3-5 GB; DeltaNet hybrid usually lower than full transformer attention but verify on first run) | −3 to −5 GB |
| Safety margin (allocator fragmentation, transient activations) | −2 GB |
| **Usable for weights per card** | **~22-25 GB** |

So total chat weights across both cards ≤ **~44-50 GB**. From the 38 published quants, the candidates partition into four zones:

| Zone | Total weights | Quants |
|---|---|---|
| **OOM (>46 GB)** — don't try | 47-87 GB | Q4_K_M 48.5, UD-Q4_K_M 49.3, **UD-Q4_K_XL 49.6 ❌** (already OOM'd), Q4_1 50.1, MXFP4_MOE 48.0, all Q5/Q6/Q8 |
| **Borderline (42-46 GB)** — fits only if KV stays at the low estimate | 42-46 GB | IQ4_XS 42.7, IQ4_NL 45.1, Q4_0 45.3, Q4_K_S 45.5, UD-Q4_K_S 46.1 |
| **Comfortable (28-42 GB)** — fits with headroom | 28-42 GB | **UD-IQ4_XS 38.4**, UD-IQ4_NL 39.2, Q3_K_M 38.3, UD-Q3_K_XL 36.3, UD-Q3_K_M 35.9, Q3_K_S 34.6, UD-Q3_K_S 33.3, UD-IQ3_S 29.7, UD-IQ3_XXS 28.5 |
| **Aggressive (<28 GB)** — fits easily but coding-quality concerns at Q2 and below | 18-27 GB | UD-Q2_K_XL 26.8, smaller Q2/IQ2/IQ1 variants |

**Recommendation: `UD-IQ4_XS` (38.4 GB)** — best balance for coding workloads:

- **Quality:** Q4-class via Unsloth Dynamic 2.0 imatrix calibration. Q4 is the consensus sweet spot for code generation — Q3 starts showing edge-case syntax mistakes.
- **Footprint:** ~38.4 GB weights across 2 cards = ~19 GB per card → leaves ~10 GB per card for KV + embed/rerank + safety. Comfortable headroom even if KV is at the high end of the estimate.
- **Throughput:** 3 B active params per token means inference speed should be similar to or better than Qwen3.6-35B-A3B (which also has 3 B active). 80 B total parameters give broader knowledge / better recall at the same per-token compute cost.

**Backup if UD-IQ4_XS shows quality issues:** `UD-IQ4_NL` (39.2 GB) — different IQ4 quantization scheme, ~same size.

**More conservative (headroom over quality):** `UD-Q3_K_S` (33.3 GB) — drops to Q3 but ~12 GB per-card headroom, useful if running additional GPU services.

**Deployment procedure:**

```bash
# 1. Reclaim the failed 49.6 GB UD-Q4_K_XL leftover (~50 GB freed)
rm -rf /tank/models/.cache/models--unsloth--Qwen3-Coder-Next-GGUF

# 2. Edit config.env
$EDITOR /root/local-gpu-cluster/scripts/config.env
# Set:
#   LLAMA_HF_REPO=unsloth/Qwen3-Coder-Next-GGUF
#   LLAMA_HF_QUANT=UD-IQ4_XS
#   LLAMA_ALIAS=qwen3-coder       # whatever you want clients to address it as

# 3. Apply (idempotent; downloads ~38 GB on first start)
./scripts/51-lxc-amd.sh

# 4. Watch the unit boot
pct exec 151 -- journalctl -u llamacpp-chat -f
# Expect "server is listening on http://0.0.0.0:8080" after download + load
```

**Verification:**

```bash
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -sf -H "Authorization: Bearer $ROUTER_KEY" http://192.168.6.153:8000/v1/models | jq '.data[].id'

# VRAM check — both cards should be at ~20-25 GB used, ≥3 GB free
pct exec 151 -- rocm-smi --showmemuse -d 0 1

# Coding smoke test
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
  -d '{"model":"qwen3-coder","messages":[{"role":"user","content":"Write a Python function that reverses a linked list in place. Include a docstring."}]}' \
  http://192.168.6.153:8000/v1/chat/completions | jq -r '.choices[0].message.content'
```

If `rocm-smi` shows ≥3 GB free on each card and the unit is stable for ~10 min, the budget is right. If it OOMs at first start (single-allocation failure), drop one rank — try `UD-Q3_K_XL` (36.3 GB).

**Caveat — chat-model exclusivity:** Coder-Next at any of these quants replaces (doesn't coexist with) `rag-qwen3.6` in the chat slot. Three operational patterns:

- **Coding workstation** — Coder-Next is the chat model; all clients (OpenCode, AnythingLLM RAG, browser artifact) use it. RAG quality may degrade vs Qwen3.6 (different model strengths).
- **RAG workstation** — Keep Qwen3.6-35B-A3B; Coder-Next not deployed.
- **Switch on demand** — Keep both downloaded, change `LLAMA_HF_QUANT` in `config.env` and re-run `51-lxc-amd.sh` to swap (~30s downtime).

The router's [`ALIAS_MAP`](./scripts/files/router-app.py) doesn't currently expose a `coder-qwen3` alias; if you want client routing by alias name, add an entry per [§ 6.2](#-62-adding-or-editing-router-aliases).

### § 4.5 Enabling spec-decode (when a compatible draft ships)

Currently disabled (see [§ 3.9](#-39-no-implementations-specified-for-speculative-decoding)). When a vocab-compatible Qwen3.6 small variant ships:

```bash
# Edit scripts/config.env
$EDITOR scripts/config.env
# Set:
#   LLAMA_DRAFT_REPO=unsloth/<new-small-variant>-GGUF
#   LLAMA_DRAFT_QUANT=Q4_K_M
#   LLAMA_SPEC_NMAX=16
#   LLAMA_SPEC_NMIN=0

# Re-run the script
./scripts/51-lxc-amd.sh
```

[`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) detects the non-empty `LLAMA_DRAFT_REPO` and appends `--hf-repo-draft`, `--n-gpu-layers-draft all`, and `--spec-draft-n-max/min` to the ExecStart. After restart, the log should show acceptance rates instead of the "no implementations specified" line.

If the new draft has a mismatched vocab, llama.cpp will refuse to load it with `draft model vocab type must match target model`. Revert `LLAMA_DRAFT_REPO=` (empty) and re-run.

### § 4.6 Cleaning up unused cached models ✅

The HF cache accumulates downloaded files that may not be in use. As of last verification, ~50 GB is reclaimable:

```bash
# Inventory
du -sh /tank/models/.cache/models--*/

# Safely remove an unused repo's blobs:
rm -rf /tank/models/.cache/models--unsloth--Qwen3-Coder-Next-GGUF
rm -rf /tank/models/.cache/models--unsloth--Qwen3-0.6B-GGUF    # only if spec-decode stays disabled

# The mmproj file inside the chat repo cache is unused but the repo dir itself is needed.
# Delete only the specific blob:
find /tank/models/.cache/models--unsloth--Qwen3.6-35B-A3B-GGUF -name "mmproj-BF16.gguf" -delete
find /tank/models/.cache/models--unsloth--Qwen3.6-35B-A3B-GGUF/blobs -size +1G \
    -exec sh -c 'test ! -L "$(realpath {})" || file "{}"' \;   # find dangling blobs
```

⚠ Take a ZFS snapshot first ([§ 10](#-10-storage--backups)) — recovery is a `zfs rollback` away.

### Verification

After any model change:

```bash
# Unit is active
pct exec 151 -- systemctl is-active llamacpp-chat

# Smoke test through the router
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -sf -H "Authorization: Bearer $ROUTER_KEY" http://192.168.6.153:8000/v1/models | jq '.data[].id'

# A trivial completion
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"model":"rag-qwen3.6","messages":[{"role":"user","content":"in one word: hi"}],"max_tokens":5}' \
    http://192.168.6.153:8000/v1/chat/completions | jq '.choices[0].message.content'
```

If the model output looks degraded compared to before (worse coherence, weird tokens), the new model may have a different chat template — check [`scripts/files/router-app.py`](./scripts/files/router-app.py) `ALIAS_MAP` and verify `--reasoning-format deepseek --jinja` still applies.

---

## § 5. Three-tier token-limit layering

> **When to read this:** a client got `413 request too large`, or you want to bump the maximum input size, or you're confused about why three different "context" numbers exist.

### The three tiers

| Layer | Setting | Default | Source |
|---|---|---|---|
| llama.cpp chat unit | `--ctx-size` | **262 144** (256K, Qwen3.6 full trained window) | [`scripts/51-lxc-amd.sh:43`](./scripts/51-lxc-amd.sh) `LLAMA_CTX` |
| Router | `MAX_CHAT_INPUT_TOKENS` | **200 000** (200K input cap, ~56K reserved for output) | [`scripts/53-lxc-router.sh:50`](./scripts/53-lxc-router.sh) |
| AnythingLLM | `ALLM_LLM_TOKEN_LIMIT` | **131 072** (128K conservative client-side cap) | [`scripts/54-lxc-anythingllm.sh:42`](./scripts/54-lxc-anythingllm.sh) |

These are three *different* limits guarding different layers. They are intentionally not equal.

### Why three values?

- **llama.cpp 256K**: the actual model context window (`n_ctx_train` for Qwen3.6). The chat unit allocates KV cache for this size, so changing it changes VRAM consumption (~11 GB at Q8 KV for full 256K).
- **Router 200K input**: the router rejects inputs over this with `413 exceeds_max_chat_input_tokens` *before* forwarding upstream. Buffer of ~56K is left for the response (thinking blocks + completion). Without this, a request near the model's 256K limit would 200 OK but truncate or fail mid-generation.
- **AnythingLLM 128K**: conservative cap exposed to the GenericOpenAI provider. AnythingLLM uses this to decide when to truncate retrieved context. Lower than the router cap so AnythingLLM never sends a request the router would 413.

### When to bump each

| Scenario | What to change |
|---|---|
| AnythingLLM truncates retrieved chunks too aggressively | Bump `ALLM_LLM_TOKEN_LIMIT` in [`scripts/54-lxc-anythingllm.sh`](./scripts/54-lxc-anythingllm.sh), re-run, restart container. Safe up to 200 000 (router cap). |
| Direct API client (OpenCode, Cline) hits 413 from the router | Bump `MAX_CHAT_INPUT_TOKENS` in [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh), re-run, restart router. Safe up to ~256 000 minus headroom for output. |
| You want more KV-cache headroom (e.g., to run multi-slot) | Lower `LLAMA_CTX` in [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) — but you'll then also lower the router + AnythingLLM caps to match. |
| You want to *reduce* AnythingLLM's input to limit cost / hallucination on long prompts | Just lower `ALLM_LLM_TOKEN_LIMIT`. Doesn't affect anything else. |

After changing any of these, **the three must remain in order: ALLM ≤ router ≤ llama.cpp**. Out of order causes silent truncation or unexpected 413s.

### Verification

```bash
# llama.cpp ctx-size
pct exec 151 -- systemctl cat llamacpp-chat | grep ctx-size

# Router cap
pct exec 153 -- grep MAX_CHAT_INPUT_TOKENS /etc/router.env

# AnythingLLM cap
pct exec 154 -- grep MODEL_TOKEN_LIMIT /opt/anythingllm/.env
```

---

## § 6. Router operations

> **When to read this:** you need to tune rate limits, edit aliases, adjust CORS, or read access logs. For *router architecture* (why these features exist), see [README.md § Router features](./README.md) and [`scripts/files/router-app.py`](./scripts/files/router-app.py) docstring.

### § 6.1 Where router config lives

- **Code:** [`scripts/files/router-app.py`](./scripts/files/router-app.py) — the FastAPI app deployed to LXC 153
- **Env file:** `/etc/router.env` on LXC 153 (mode 600, generated by [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh))
- **Systemd unit:** `/etc/systemd/system/llm-router.service` on LXC 153

Edit pattern: change values in [`scripts/config.env`](./scripts/config.env.example) (host-side) → re-run [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh) → it `upsert`s `/etc/router.env` and restarts the unit.

### § 6.2 Adding or editing router aliases

`ALIAS_MAP` is **Python source** in [`scripts/files/router-app.py:336-340`](./scripts/files/router-app.py), not an env var. Procedure:

```bash
# 1. Edit the source
$EDITOR /root/local-gpu-cluster/scripts/files/router-app.py
# Add to ALIAS_MAP, e.g.:
#   "qwen3.6-rag":   {"backend": "rag-qwen3.6", "enable_thinking": False, "strip_thinking": True},

# 2. Re-run the router provisioning (deploys updated app.py + restarts unit)
./scripts/53-lxc-router.sh

# 3. Verify
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -sf -H "Authorization: Bearer $ROUTER_KEY" http://192.168.6.153:8000/v1/models | jq '.data[].id'
```

The script copies `router-app.py` to `/opt/llm-router/app.py` on LXC 153 and runs `systemctl restart llm-router`. The router has a fail-open SSE mode, so any in-flight chat requests during restart will receive a `service_degraded` frame and the client will retry.

### § 6.3 Changing rate limits

[`/etc/router.env`](./scripts/53-lxc-router.sh) holds:

```
RATE_LIMIT_CHAT=60/minute
RATE_LIMIT_EMBED=200/minute
RATE_LIMIT_TAVILY=30/minute
```

Edit via [`scripts/config.env`](./scripts/config.env.example), re-run `53-lxc-router.sh`. Format follows slowapi: `<N>/second|minute|hour|day`.

If a client is being rate-limited, the response is `429 rate_limit_exceeded`. Check access log for the offending `client_ip`:

```bash
pct exec 153 -- jq 'select(.status == 429) | .client_ip' /var/log/llm-router/access.log | sort | uniq -c
```

### § 6.4 Changing admission control

```
CHAT_CONCURRENCY=1
EMBED_CONCURRENCY=4
```

`CHAT_CONCURRENCY=1` is intentional and matches the chat unit's `--parallel 1` — see [`scripts/53-lxc-router.sh:39-43`](./scripts/53-lxc-router.sh) for the full rationale. Don't bump this above 1 unless you also bump the chat unit's `--parallel` (and reduce per-slot context proportionally).

`EMBED_CONCURRENCY=4` matches the embed unit's `--parallel 4`. If you bump embed concurrency at the router without also bumping the upstream, you'll get queueing inside llama.cpp instead of at the router (where keepalives can be emitted).

### § 6.5 CORS origin tightening

Default `CORS_ALLOW_ORIGINS=*` is fine for a LAN-only cluster but can be tightened once the legitimate caller set is known:

```bash
# config.env
CORS_ALLOW_ORIGINS=http://192.168.6.150,https://app.lan,file://
```

Comma-separated. The router falls back to `["*"]` if the env var is empty. CORS preflight (OPTIONS) bypasses Bearer auth so the browser can complete the preflight before sending the actual authed request.

### § 6.6 Parsing access logs

Each line is one JSON object. Useful queries:

```bash
LOG=/var/log/llm-router/access.log

# Last 20 chat requests with duration and token counts
pct exec 153 -- jq -c 'select(.route == "/v1/chat/completions") | {ts, model, input_tokens, output_tokens, duration_ms}' "$LOG" | tail -20

# Distribution of statuses in the last 1000 requests
pct exec 153 -- tail -1000 "$LOG" | jq -s 'group_by(.status) | map({status: .[0].status, count: length})'

# Slowest 10 requests in the file
pct exec 153 -- jq -s 'sort_by(-.duration_ms) | .[0:10] | .[] | {ts, route, duration_ms, status, input_tokens}' "$LOG"
```

### § 6.7 Tavily quota monitoring

The Tavily free tier is 1000 searches/month. Each Weekly Customer Adoption Review artifact refresh uses ~10 credits.

```bash
# Count Tavily proxy calls in the access log
pct exec 153 -- jq 'select(.route == "/v1/tavily/search") | .ts' /var/log/llm-router/access.log | wc -l
```

For real quota state, check the Tavily dashboard — the router has no awareness of remaining credits. If Tavily returns `429` or `402`, the proxy passes it through wrapped in the OpenAI-style error envelope.

### § 6.8 Server-side tool execution

The router can run the OpenAI tools/tool_calls multi-turn loop internally instead of returning `tool_calls` to the client. Browser-side clients (the Weekly Customer Adoption Review artifact, ad-hoc curl scripts) get tool-augmented chat without implementing a dispatcher.

**Opt-in per request:**

```bash
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)

curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-think",
    "messages": [{"role":"user","content":"What was announced about TrueNAS Scale in the last 7 days?"}],
    "tool_execution": "server",
    "stream": false
  }' \
  http://192.168.6.153:8000/v1/chat/completions | jq -r '.choices[0].message.content'
```

When `tool_execution` is `"server"`, the router:
1. Injects all registered tool schemas if the client didn't supply `tools` (lets the client restrict which tools are exposed by passing a subset).
2. Sends the augmented request upstream.
3. If the model emits `finish_reason: tool_calls`, the router executes each tool via the registry and appends `role: "tool"` messages with results.
4. Loops back to step 2 until either the model returns a non-tool-call response or `MAX_TOOL_ITERATIONS=10` is hit.
5. Returns only the final assistant message to the client (tool_call deltas are not streamed downstream).

Default is `"client"` — preserves legacy pass-through behavior for OpenCode/Cline/Continue, which run their own tool dispatchers.

**Registered tools (v1):**

| Name | Purpose | Backend |
|---|---|---|
| `tavily_search` | Web search with ranked results | Tavily Search API (`TAVILY_API_KEY`) |
| `tavily_extract` | Pull full content from URLs | Tavily Extract API |
| `tavily_crawl` | Follow links to gather site content | Tavily Crawl API |
| `tavily_map` | Map a site's URL structure (URLs only) | Tavily Map API |
| `web_fetch` | Direct HTTP(S) GET with 1 MB cap + SSRF guards | local httpx |

`web_fetch` blocks loopback, private IPv4 ranges, and known cloud-metadata endpoints (`169.254.169.254`, `metadata.google.internal`, etc.) to prevent SSRF. The list is in [`router-app.py`](./scripts/files/router-app.py) `WEB_FETCH_DENY_HOSTS`.

**Streaming:** server-side tool execution works in both streaming and non-streaming modes. In streaming mode, the router buffers tool_call deltas internally (the client never sees them) and only forwards `content` deltas. The result is a single streamed assistant response that may pause briefly between iterations while tools execute.

**Adding a new tool:**

1. Write the handler in [`scripts/files/router-app.py`](./scripts/files/router-app.py):

```python
async def _tool_my_new_tool(args: dict) -> dict:
    # validate args, call upstream, return a JSON-serializable dict
    return {"result": "..."}

MY_NEW_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "my_new_tool",
        "description": "What it does. Be clear — the model picks tools based on this text.",
        "parameters": {
            "type": "object",
            "properties": {"some_arg": {"type": "string", "description": "..."}},
            "required": ["some_arg"],
        },
    },
}
```

2. Add it to the `TOOLS` registry dict:

```python
TOOLS = {
    ...,
    "my_new_tool": {"handler": _tool_my_new_tool, "schema": MY_NEW_TOOL_SCHEMA},
}
```

3. Redeploy:

```bash
./scripts/53-lxc-router.sh
```

The script copies the updated `router-app.py` to LXC 153 and restarts the unit. The new tool is immediately available — clients calling with `tool_execution: "server"` will see it in the model's tool choices on the next request.

**Safety bounds:**

| Env var | Default | Purpose |
|---|---|---|
| `MAX_TOOL_ITERATIONS` | 10 | Cap on multi-turn loop iterations |
| `TOOL_EXECUTION_DEFAULT` | `client` | Default when request doesn't set `tool_execution` |
| `WEB_FETCH_MAX_SIZE_KB` | 1024 | Cap on `web_fetch` response body |
| `WEB_FETCH_TIMEOUT_SECONDS` | 15 | Cap on `web_fetch` request time |
| `MAX_STREAM_SECONDS` | 900 | Overall wall-clock cap across all iterations |

Tool calls log to `/var/log/llm-router/access.log` with `route: "/v1/chat/completions:tool"` and the tool name in the `model` field. Filter with:

```bash
pct exec 153 -- jq 'select(.route == "/v1/chat/completions:tool")' /var/log/llm-router/access.log
```

**Verification (smoke test):**

```bash
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)

# Non-streaming, simple Tavily search question
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6-think",
    "messages": [{"role":"user","content":"What is the current TrueNAS Scale stable release?"}],
    "tool_execution": "server",
    "stream": false
  }' \
  http://192.168.6.153:8000/v1/chat/completions | jq -r '.choices[0].message.content'

# Check the access log for the tool call
pct exec 153 -- tail -10 /var/log/llm-router/access.log | jq 'select(.route | contains("tool"))'
```

Expected: response references a recent TrueNAS release with citations, and the access log shows a `tavily_search` invocation.

### § 6.9 Safe restart

```bash
pct exec 153 -- systemctl restart llm-router
```

In-flight streaming requests will receive a `service_degraded` SSE frame. Non-streaming requests will get `502` or `503`. Clients should retry. Restart is typically <2s; warm-up via `llm-router-keepalive.timer` runs every 5 minutes.

### Verification

After any router change:

```bash
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -sf http://192.168.6.153:8000/healthz | jq
curl -sf -H "Authorization: Bearer $ROUTER_KEY" http://192.168.6.153:8000/v1/models | jq '.data | length'
# Trivial chat probe
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"model":"rag-qwen3.6","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' \
    http://192.168.6.153:8000/v1/chat/completions | jq -r '.choices[0].message.content'
```

---

## § 7. Secrets & key rotation

> **When to read this:** rotating a credential, replacing a leaked key, or onboarding a new client that needs a different key.

### § 7.1 Key inventory

Four secrets across the cluster. None are in `config.env.example`; all are generated on the live LXCs.

| Key | Where it lives | Generated by | Consumed by |
|---|---|---|---|
| `LLAMACPP_API_KEY` | `/etc/llamacpp.env` on **LXC 151** (mode 600) | [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) phase 5.11.1, mirrored to `/etc/router.env` by [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh) phase 7.1.5 | chat / embed / rerank `--api-key`; router upstream calls |
| `ROUTER_API_KEY` | `/etc/router.env` on **LXC 153** (mode 600) | [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh) phase 7.1.5 | Router inbound Bearer auth; clients (AnythingLLM, OpenCode, curl) |
| `TAVILY_API_KEY` | `/etc/router.env` on **LXC 153** + `scripts/config.env` on the **PVE host** | Manual paste from `https://tavily.com` | Router `/v1/tavily/search` proxy |
| `ALLM_API_KEY` | AnythingLLM Settings → API Keys (web UI) + `scripts/config.env` on the **PVE host** | AnythingLLM web UI after first signup | [`scripts/57-configure-anythingllm.sh`](./scripts/57-configure-anythingllm.sh) workspace REST setup; MCP bridge ([`scripts/58-mcp-sdg.sh`](./scripts/58-mcp-sdg.sh)) |

To recover any key on a fresh session:

```bash
# From the PVE host
pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env
pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}'   /etc/router.env
pct exec 153 -- awk -F= '/^TAVILY_API_KEY=/{print $2}'   /etc/router.env
# ALLM_API_KEY: open the AnythingLLM UI → Settings → API Keys
```

### § 7.2 Rotating `ROUTER_API_KEY` ✅

```bash
# 1. Generate new key
NEW_KEY=$(openssl rand -hex 32)

# 2. Update /etc/router.env on LXC 153
pct exec 153 -- bash -c "sed -i 's/^ROUTER_API_KEY=.*/ROUTER_API_KEY=$NEW_KEY/' /etc/router.env"

# 3. Restart router (in-flight streams get service_degraded)
pct exec 153 -- systemctl restart llm-router

# 4. Update every client that has the old key:
#    - AnythingLLM: Settings → AI Providers → both LLM and Embedder, paste new key in API Key
#      (UI persists to database; survives restart)
#    - OpenCode / Cline / Continue: ~/.config/<tool>/config.json
#    - Browser-side artifact: edit window.AI_SETTINGS.localApiKey in
#      weekly_customer_adoption_review.html or wherever the key is stored
#    - Any curl scripts: update inline
```

There's no graceful rotation — clients with the old key will get `403` until they update.

### § 7.3 Rotating `LLAMACPP_API_KEY` ⚠

Trickier because it's mirrored in two places (LXC 151 owns the source, LXC 153 holds a copy):

```bash
# 1. Generate new key
NEW_KEY=$(openssl rand -hex 32)

# 2. Update LXC 151's file
pct exec 151 -- bash -c "sed -i 's/^LLAMACPP_API_KEY=.*/LLAMACPP_API_KEY=$NEW_KEY/' /etc/llamacpp.env"

# 3. Mirror to LXC 153
pct exec 153 -- bash -c "sed -i 's/^LLAMACPP_API_KEY=.*/LLAMACPP_API_KEY=$NEW_KEY/' /etc/router.env"

# 4. Restart all three llama-server units (they read the key at start)
pct exec 151 -- systemctl restart llamacpp-chat llamacpp-embed llamacpp-rerank

# 5. Restart router so it reloads the upstream auth key
pct exec 153 -- systemctl restart llm-router
```

There's a brief window (steps 4-5) where one side has the new key and the other doesn't. To minimize impact, do steps 4 + 5 back-to-back.

### § 7.4 Rotating `TAVILY_API_KEY` ✅

```bash
# 1. Get a new key from https://tavily.com → API Keys
# 2. Update /etc/router.env
pct exec 153 -- bash -c "sed -i 's/^TAVILY_API_KEY=.*/TAVILY_API_KEY=NEW_KEY_HERE/' /etc/router.env"
# 3. Also update scripts/config.env so script re-runs don't revert
$EDITOR /root/local-gpu-cluster/scripts/config.env
# 4. Restart router
pct exec 153 -- systemctl restart llm-router
```

No client changes needed — clients call `/v1/tavily/search` with `ROUTER_API_KEY`; the router holds the Tavily key server-side.

### § 7.5 Rotating `ALLM_API_KEY` ⚠

Used only by [`scripts/57-configure-anythingllm.sh`](./scripts/57-configure-anythingllm.sh) and [`scripts/58-mcp-sdg.sh`](./scripts/58-mcp-sdg.sh) (the MCP bridge). Rotation:

```bash
# 1. In AnythingLLM web UI → Settings → API Keys → Delete old, Generate new
# 2. Update scripts/config.env
$EDITOR /root/local-gpu-cluster/scripts/config.env
# Update: ALLM_API_KEY=<new-key>
# 3. Re-run scripts that consume it
./scripts/57-configure-anythingllm.sh   # idempotent — applies new key but workspaces unchanged
./scripts/58-mcp-sdg.sh                  # rewrites /etc/mcp-sdg.env and restarts the bridge
```

### Verification

After any key rotation:

```bash
# Probe with the new ROUTER_API_KEY
ROUTER_KEY=<new-key>
curl -sf -H "Authorization: Bearer $ROUTER_KEY" http://192.168.6.153:8000/healthz

# Confirm upstream auth works (router → LXC 151)
curl -sf -H "Authorization: Bearer $ROUTER_KEY" http://192.168.6.153:8000/v1/models

# If Tavily was rotated
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"query":"test"}' http://192.168.6.153:8000/v1/tavily/search | jq -r '.results[0].title // .error.type'
```

---

## § 8. RAG operations

> **When to read this:** something's off with RAG ingest, refresh, or workspace state. For the full operational reference (handler config, manifest schema, CLI flags), see [`scripts/rag/README.md`](./scripts/rag/README.md) — this section covers only day-2-specific operations.

### § 8.1 Where RAG state lives

- **Sources manifest:** [`scripts/rag/sources.yaml`](./scripts/rag/sources.yaml) (git-tracked, on the PVE host)
- **Per-source state:** `/tank/rag-state/<source-id>/{manifest,documents}.json` + `errors.log` + `cache/`
- **Safety-halted plans:** `/tank/rag-state/_proposals/`
- **Vector DB:** LanceDB inside the AnythingLLM container at `/app/server/storage/lancedb` (mapped to `/tank/anythingllm/storage/lancedb` on the host via the bind mount)

Currently tracked sources (from `sources.yaml`): `truenas-api-v27`, `opnsense-docs`, `keycloak-docs`, `truenas-scale-docs`, `truenas-blog` (parked, handler stub not yet implemented), `openzfs-docs`.

### § 8.2 Adding a new source ✅

```bash
# 1. Edit sources.yaml — add an entry under `sources:` with handler-specific config
$EDITOR /root/local-gpu-cluster/scripts/rag/sources.yaml

# 2. Preview what would be ingested (no writes)
/opt/vcf-scraper-venv/bin/python /root/local-gpu-cluster/scripts/rag/refresh.py \
    --source <new-source-id> --dry-run

# 3. Apply
/opt/vcf-scraper-venv/bin/python /root/local-gpu-cluster/scripts/rag/refresh.py \
    --source <new-source-id>
```

See [`scripts/rag/README.md`](./scripts/rag/README.md) for handler-specific config keys.

### § 8.3 Routine refresh

```bash
# Refresh one source (respects refresh_interval)
/opt/vcf-scraper-venv/bin/python /root/local-gpu-cluster/scripts/rag/refresh.py --source opnsense-docs

# Force refresh ignoring interval
/opt/vcf-scraper-venv/bin/python /root/local-gpu-cluster/scripts/rag/refresh.py --source opnsense-docs --force

# All sources (respect intervals)
/opt/vcf-scraper-venv/bin/python /root/local-gpu-cluster/scripts/rag/refresh.py
```

### § 8.4 Safety threshold halts

If a refresh plan would delete > 10% of a source's existing docs **and** ≥ 5 absolute, the run halts and writes the plan to `/tank/rag-state/_proposals/<source-id>-<timestamp>.json`.

Diagnose:

```bash
PROPOSAL=$(ls -1t /tank/rag-state/_proposals/*.json | head -1)

# Are removed URLs renames (URL-shape change) or real deletions?
python3 -c "
import json
p = json.load(open('$PROPOSAL'))
def tail(u): return u.rstrip('/').rsplit('/', 1)[-1]
overlap = sum(1 for u in p['removes'] if tail(u) in {tail(a) for a in p['adds']})
print(f'removes={len(p[\"removes\"])}, adds={len(p[\"adds\"])}, tail-overlap={overlap}')
print('Sample removes:', p['removes'][:5])
print('Sample adds:', p['adds'][:5])
"
```

- **High overlap (>50%)** → URL-shape migration. Fix: wipe state + force-refresh + cleanup (see [§ 8.5](#-85-wipe--recover-a-source)).
- **Low overlap + removes are clearly scaffolding/test paths** → legitimate cleanup. Same wipe procedure works, since the new collection won't include them.
- **Low overlap + removes are legit content** → vendor actually deprecated docs. Approve by manually deleting the affected URLs from `documents.json` and re-running refresh.

### § 8.5 Wipe & recover a source ✅

For URL-shape migrations or after a handler-config change that invalidates existing state:

```bash
PY=/opt/vcf-scraper-venv/bin/python
SRC=<source-id>

# 1. Wipe state
rm -f /tank/rag-state/$SRC/{documents,manifest}.json

# 2. Re-run refresh (uploads fresh, all docs as ADDs)
$PY /root/local-gpu-cluster/scripts/rag/refresh.py --source $SRC --force

# 3. Clean up orphaned workspace docs (old uploads no longer in state)
$PY /root/local-gpu-cluster/scripts/rag/cleanup_interrupted_refresh.py --source $SRC --apply
```

For wiping every source at once, see the bash loop pattern in [SESSION_HANDOFF.md](./SESSION_HANDOFF.md) ("Full sweep" from 2026-05-25).

### § 8.6 The workspace ~2.0× dup-factor

The AnythingLLM `/workspace/{slug}` REST endpoint returns roughly two rows per actual document. Consistent across sources:

| Source | State docs | Workspace rows | Ratio |
|---|---|---|---|
| keycloak-docs | 6 | 12 | 2.00 |
| openzfs-docs | 76 | 150 | 1.97 |
| truenas-api-v27 | 1079 | 2159 | 2.00 |
| opnsense-docs | 418 | 798 | 1.91 |
| truenas-scale-docs | 452 | 904 | 2.00 |

Workspace doc-count APIs are not affected (they dedupe). This is a quirk of the workspace-list endpoint specifically. Doesn't affect retrieval quality. The [`cleanup_interrupted_refresh.py`](./scripts/rag/cleanup_interrupted_refresh.py) helper accounts for this by matching by `allm_doc_path`, not by URL or count.

### § 8.7 Manual one-off document upload

For a document that doesn't fit any existing source (an internal PDF, a one-off scraped page):

```bash
ALLM_KEY=$(grep ALLM_API_KEY /root/local-gpu-cluster/scripts/config.env | cut -d= -f2)

curl -sf -X POST "http://192.168.6.154:3001/api/v1/document/raw-text" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -H "Content-Type: application/json" \
    -d '{
        "textContent": "...",
        "metadata": {"title": "...", "docSource": "[MANUAL] <slug>", "sourceURL": "..."},
        "addToWorkspaces": "sdg-documentation"
    }'
```

These manual uploads are **not tracked by `refresh.py`**. They survive workspace re-tunes but won't be touched by source refreshes.

### § 8.8 LanceDB / vector DB backup

LanceDB lives at `/tank/anythingllm/storage/lancedb` on the host (via the bind mount into LXC 154).

```bash
# Stop AnythingLLM to ensure no writes mid-snapshot
pct exec 154 -- bash -c 'cd /opt/anythingllm && docker compose stop'

# ZFS snapshot of the anythingllm dataset
zfs snapshot tank/anythingllm@lancedb-$(date +%Y%m%d-%H%M)

# Restart
pct exec 154 -- bash -c 'cd /opt/anythingllm && docker compose start'
```

Or for a portable backup:

```bash
pct exec 154 -- bash -c 'cd /opt/anythingllm && docker compose stop'
tar -czf /tank/backups/lancedb-$(date +%Y%m%d).tar.gz -C /tank/anythingllm/storage lancedb
pct exec 154 -- bash -c 'cd /opt/anythingllm && docker compose start'
```

### Verification

```bash
# State + workspace alignment for a single source
$PY /root/local-gpu-cluster/scripts/rag/cleanup_interrupted_refresh.py --source <src>
# (no --apply = dry-run; expect "orphans: 0" if state and workspace agree)
```

---

## § 9. Embedder/reranker retuning + re-ingest playbook

> **When to read this:** you're about to change the embedder model, reranker model, embedder pooling mode, `EMBED_CTX`, or `EMBED_PARALLEL`. **Any of these changes invalidates existing embeddings.**

### § 9.1 "If you change X, you must do Y"

| Change | Re-ingest required? | Why |
|---|---|---|
| Embedder model (e.g., Qwen3-Embedding → bge-large) | ✅ **Yes — full re-embed** | New model produces different vectors; old vectors become noise |
| Embedder pooling (`last` → `cls`, or vice versa) | ✅ **Yes — full re-embed** | Different pooling = different embeddings for the same input |
| `EMBED_CTX` / `EMBED_PARALLEL` ratio changes per-slot ctx | ⚠ Probably — anything previously truncated needs re-embed | Existing chunks may have been silently truncated at old per-slot ctx |
| Reranker model | ❌ No | Rerank is applied at query time, not embedded into vectors |
| Chat model (`rag-qwen3.6` → different) | ❌ No | Chat synthesis is independent of vector DB |
| Workspace tuning (`topN`, `similarityThreshold`) | ❌ No | Applied at retrieval time |

### § 9.2 Pre-change checklist

```bash
# 1. Snapshot — recovery is a zfs rollback away
zfs snapshot tank/anythingllm@pre-embedder-change-$(date +%Y%m%d)
zfs snapshot tank/rag-state@pre-embedder-change-$(date +%Y%m%d)

# 2. Record the current embedder SHA so refresh.py knows the change happened
sha256sum /tank/models/.cache/models--*Embedding*/snapshots/*/Qwen3-Embedding-*.gguf \
    | awk '{print $1}' > /tank/models/.embedder-sha-before-change

# 3. Note current document counts per source so you can compare after re-embed
for src in /tank/rag-state/*/documents.json; do
  printf "%-30s %d\n" "$(basename $(dirname $src))" "$(jq 'length' $src)"
done
```

### § 9.3 Re-ingest workflow ✅

```bash
PY=/opt/vcf-scraper-venv/bin/python

# 1. Edit config.env — change EMBED_HF_REPO / EMBED_HF_QUANT / EMBED_POOLING
$EDITOR /root/local-gpu-cluster/scripts/config.env

# 2. Re-run the AMD-LXC script — rewrites the embed unit and restarts
./scripts/51-lxc-amd.sh

# 3. Verify the new embedder dimension matches AnythingLLM expectations
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"input":"dim check"}' http://192.168.6.153:8000/v1/embeddings \
    | jq '.data[0].embedding | length'
# For Qwen3-Embedding-0.6B this is 1024. If you switched to a model with different
# dimension, AnythingLLM will need EMBEDDING_DIMENSION updated and the vector DB
# rebuilt (lancedb is dimension-bound).

# 4. Wipe each source's state so refresh re-uploads everything as ADDs
for src in keycloak-docs openzfs-docs truenas-api-v27 opnsense-docs truenas-scale-docs; do
  rm -f /tank/rag-state/$src/{documents,manifest}.json
done

# 5. Run the wipe loop (preserves cache/, only clears state)
for src in keycloak-docs openzfs-docs truenas-api-v27 opnsense-docs truenas-scale-docs; do
  echo "=== $src ==="
  $PY /root/local-gpu-cluster/scripts/rag/refresh.py --source $src --force \
    && $PY /root/local-gpu-cluster/scripts/rag/cleanup_interrupted_refresh.py --source $src --apply \
    || { echo "FAILED on $src — stop and investigate"; break; }
done
```

This is the same wipe loop documented in [SESSION_HANDOFF.md § "Full sweep"](./SESSION_HANDOFF.md). Expect 1-2 hours wall-clock for the full corpus.

### § 9.4 Updating the `.embedder-sha` pin

After a successful embedder swap:

```bash
sha256sum /tank/models/.cache/models--<new-embed-repo>/snapshots/*/*.gguf \
    | awk '{print $1}' > /tank/models/.embedder-sha
```

This pin is used by [`setup-runbook.md`](./setup-runbook.md) Phase 6 to detect future embedder swaps. Keep it current.

### § 9.5 Critical: don't use `--pooling cls` with Qwen3-Embedding

Qwen3-Embedding-0.6B uses the final `<|endoftext|>` token for pooling. The router enforces this by appending `<|endoftext|>` to inputs missing it (see [`router-app.py`](./scripts/files/router-app.py) `_ensure_eot`). The embed unit MUST use `--pooling last` ([`scripts/51-lxc-amd.sh:92`](./scripts/51-lxc-amd.sh)).

If you accidentally set `--pooling cls`, embeddings are semantically wrong — retrieval becomes random — and there are no obvious error messages. The only symptom is "RAG suddenly returns irrelevant chunks." Full re-embed required to recover.

### Verification

```bash
# Vector dimension correct
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"input":"test"}' http://192.168.6.153:8000/v1/embeddings | jq '.data[0].embedding | length'

# Retrieval quality smoke test — query something you know is in the corpus
ALLM_KEY=$(grep ALLM_API_KEY /root/local-gpu-cluster/scripts/config.env | cut -d= -f2)
curl -sf -X POST "http://192.168.6.154:3001/api/v1/workspace/sdg-documentation/chat" \
    -H "Authorization: Bearer $ALLM_KEY" -H "Content-Type: application/json" \
    -d '{"message":"How does OPNsense configure VLAN interfaces?","mode":"query"}' \
    | jq -r '.textResponse' | head -20
# Expect a coherent answer with citations, not a refusal sentinel
```

---

## § 10. Storage & backups

> **When to read this:** before any risky change (model swap, update), or you need to recover from one. Also for routine snapshot hygiene.

### § 10.1 ZFS layout

`tank` is a 2-NVMe mirror created by [`scripts/40-host-config.sh`](./scripts/40-host-config.sh). Datasets:

| Dataset | Contents | Mounted into |
|---|---|---|
| `tank/models` | HF cache | LXC 151 at `/opt/models` (RW) |
| `tank/anythingllm` | AnythingLLM storage (LanceDB, documents) | LXC 154 at `/opt/anythingllm-data` |
| `tank/rag-state` | RAG refresh state | host-side (read by `refresh.py` running on host) |
| `tank/backups` | vzdump destination | host-side, registered as PVE storage `tank-backups` |
| `tank/mcp` | MCP container state | LXC 155 |

ZFS ARC capped at 16 GB on the host (see [`setup-runbook.md`](./setup-runbook.md) §4.8).

### § 10.2 vzdump cadence

**vzdump is NOT automated by the bootstrap scripts.** Operator must set up their own schedule.

Recommended cadence:

| LXC | Frequency | Why |
|---|---|---|
| 151 (llamacpp-amd) | Weekly | Stateless except for `/etc/llamacpp.env`; rebuild via 51-lxc-amd.sh is fast |
| 153 (llm-router) | Weekly | Stateless; rebuild via 53-lxc-router.sh is fast |
| 154 (anythingllm) | **Daily** | Stateful (LanceDB, workspace settings); irreplaceable without re-embed |
| 155 (mcp-stack) | Weekly | Mostly Docker images + Python venv |

Set up via PVE web UI → Datacenter → Backup → Add, or via cron:

```bash
# /etc/cron.d/cluster-vzdump
30 2 * * *     root  vzdump 154 --storage tank-backups --mode snapshot --compress zstd
30 2 * * 0     root  vzdump 151 153 155 --storage tank-backups --mode snapshot --compress zstd
```

### § 10.3 ZFS snapshots

Instant, atomic, cheap. Take before any risky operation:

```bash
# Single dataset
zfs snapshot tank/models@pre-model-swap-$(date +%Y%m%d-%H%M)

# Recursive — all tank/* datasets at once
zfs snapshot -r tank@pre-update-$(date +%Y%m%d-%H%M)

# List snapshots
zfs list -t snapshot

# Rollback (loses everything since the snapshot)
zfs rollback tank/models@pre-model-swap-20260525-1430

# Or send/receive to copy snapshot to a different pool / host
zfs send tank/anythingllm@pre-update-20260525 \
    | ssh backup-host zfs receive tankbackup/anythingllm
```

### § 10.4 Restore procedures

**vzdump backup → restore:**

```bash
# List available backups
ls /tank/backups/dump/

# Restore (destructive — wipes existing LXC)
pct restore 154 /tank/backups/dump/vzdump-lxc-154-2026_05_25-02_30_00.tar.zst --storage local-lvm

# Or restore to a new VMID for testing
pct restore 999 /tank/backups/dump/vzdump-lxc-154-2026_05_25-02_30_00.tar.zst --storage local-lvm
```

**ZFS rollback:**

```bash
# Roll back AnythingLLM data only
pct exec 154 -- bash -c 'cd /opt/anythingllm && docker compose stop'
zfs rollback tank/anythingllm@pre-update-20260525-1400
pct exec 154 -- bash -c 'cd /opt/anythingllm && docker compose start'
```

### § 10.5 Disk-space monitoring

```bash
# Pool capacity
zpool list tank
# CAP > 80% is yellow, > 90% is red

# Per-dataset
zfs list -o name,used,available,refer tank tank/models tank/anythingllm tank/rag-state tank/backups

# Largest single files
du -sh /tank/models/.cache/models--*/snapshots/*/* 2>/dev/null | sort -h | tail -10
```

If `/tank` fills past 90%:

1. Snapshot before deleting anything: `zfs snapshot tank@pre-cleanup-$(date +%s)`
2. Prune old `tank/backups/dump/` files via PVE UI → tank-backups → Backups
3. Delete unused model files per [§ 4.6](#-46-cleaning-up-unused-cached-models)
4. Prune old `/tank/rag-state/_proposals/` JSON files

### Verification

```bash
zpool status tank      # expect "state: ONLINE", no errors
zpool scrub tank       # monthly; reads every block + checksums (takes hours)
```

---

## § 11. Updates

> **When to read this:** before applying any OS, kernel, ROCm, or service update.

### § 11.1 Pre-update snapshot (always do this first)

```bash
zfs snapshot -r tank@pre-update-$(date +%Y%m%d-%H%M)
vzdump 154 --storage tank-backups --mode snapshot --compress zstd  # stateful LXC only
```

### § 11.2 PVE host update

```bash
apt update
apt full-upgrade -y
# If kernel updated:
[ -f /var/run/reboot-required ] && shutdown -r +5 "rebooting for kernel update"
```

After reboot, verify everything came up:

```bash
pct list                       # all 4 running
systemctl is-active v620-fan-bridge.service 2>/dev/null
zpool status tank
```

### § 11.3 LXC OS packages

Per-LXC apt upgrade:

```bash
for vmid in 151 153 154 155; do
  echo "=== $vmid ==="
  pct exec $vmid -- bash -c "apt update && apt upgrade -y"
done
```

LXC 151 is more sensitive — ROCm userspace updates can come through this path. After a ROCm-package update, restart the llama-server units:

```bash
pct exec 151 -- systemctl restart llamacpp-chat llamacpp-embed llamacpp-rerank
pct exec 151 -- rocminfo | grep -c gfx1030   # confirm both GPUs still visible
```

### § 11.4 Rebuilding llama.cpp

Periodically you'll want to rebuild llama.cpp against the latest main:

```bash
pct exec 151 -- bash -c "
  cd /opt/llama.cpp && git pull
  HIPCXX=\$(hipconfig -l)/clang HIP_PATH=\$(hipconfig -R) \
    cmake -S . -B build -DGGML_HIP=ON -DGPU_TARGETS=gfx1030 \
      -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=ON -DLLAMA_OPENSSL=ON \
      -DLLAMA_BUILD_SERVER=ON -DGGML_HIP_GRAPHS=ON -DGGML_OPENMP=ON
  cmake --build build --config Release -j\$(nproc)
"
pct exec 151 -- systemctl restart llamacpp-chat llamacpp-embed llamacpp-rerank
```

Or use the script's idempotent skip:

```bash
# First remove the built binary so the script rebuilds
pct exec 151 -- rm -f /opt/llama.cpp/build/bin/llama-server
# Now re-run — the script detects the missing binary and rebuilds
./scripts/51-lxc-amd.sh
```

Watch the build log: a `gfx1030` build that suddenly takes much longer or fails often indicates a llama.cpp main-branch regression. Roll back to a known-good commit if needed.

### § 11.5 Updating AnythingLLM

The container is pinned to `mintplexlabs/anythingllm:latest` (per [`scripts/54-lxc-anythingllm.sh`](./scripts/54-lxc-anythingllm.sh)). To pull a newer image:

```bash
# Take a backup first
vzdump 154 --storage tank-backups --mode snapshot --compress zstd

pct exec 154 -- bash -c '
  cd /opt/anythingllm
  docker compose pull
  docker compose down
  docker compose up -d
  docker logs anythingllm -f
'
# Watch for "Server listening on 0.0.0.0:3001" then Ctrl-C
```

After upgrade, verify workspace settings survived (rare, but AnythingLLM has changed schema in major releases):

```bash
./scripts/57-configure-anythingllm.sh   # idempotent — re-applies tuning if reset
```

### § 11.6 Updating the router

The router is pure Python:

```bash
# Update deps
pct exec 153 -- /opt/llm-router/venv/bin/pip install --upgrade \
    fastapi 'uvicorn[standard]' httpx slowapi prometheus-fastapi-instrumentator

# If router-app.py source changed, redeploy via the script
./scripts/53-lxc-router.sh
```

### § 11.7 Updating MCP services

```bash
# Python venv on LXC 155
pct exec 155 -- /opt/mcp-sdg/venv/bin/pip install --upgrade mcp httpx

# If server code changed
./scripts/58-mcp-sdg.sh

# Docker-based MCPs (if any deployed)
pct exec 155 -- bash -c '
  for d in /opt/anythingllm-mcp /opt/broadcom-techdocs-mcp /opt/sdg-mcp; do
    [ -d "$d" ] && cd "$d" && docker compose pull && docker compose up -d
  done
'
```

### § 11.8 Rollback

If an update breaks something:

```bash
# Stop the affected service
pct exec <vmid> -- systemctl stop <unit>

# Roll back the dataset (loses any data written after snapshot)
zfs rollback tank/<dataset>@pre-update-<timestamp>

# Or restore the entire LXC from vzdump
pct stop <vmid>
pct restore <vmid> /tank/backups/dump/vzdump-lxc-<vmid>-<timestamp>.tar.zst --force --storage local-lvm
pct start <vmid>
```

### Verification

After any update, run the full [§ 2](#-2-cluster-health--observability) probe, plus:

```bash
# Smoke-test a chat request and an embed
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"model":"rag-qwen3.6","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
    http://192.168.6.153:8000/v1/chat/completions | jq -r '.choices[0].message.content'
curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"input":"ping"}' http://192.168.6.153:8000/v1/embeddings | jq '.data[0].embedding | length'
```

---

## § 12. Performance baselines

> **When to read this:** establishing what "normal" looks like, or chasing a "feels slower" complaint.

### § 12.1 Expected performance

These are rough baselines measured at the deployed config (256K chat ctx, `--parallel 1`, q8_0 KV). Your mileage varies with prompt length, thinking mode, and concurrent load.

| Operation | Expected | Worst case before something's wrong |
|---|---|---|
| Chat: first-token latency (short prompt, warm) | < 1s | > 3s |
| Chat: tokens/sec (steady-state, thinking-on) | 15-30 t/s | < 10 t/s |
| Chat: tokens/sec (steady-state, thinking-off via `rag-qwen3.6`) | 25-40 t/s | < 15 t/s |
| Chat: full 128K-input first-token | 30-90s prompt-processing | > 180s |
| Embed: single 16K-token chunk | 200-500ms | > 2s |
| Embed: 100-chunk batch | 5-15s | > 60s |
| Rerank: 12 docs vs 1 query | 100-300ms | > 1s |
| Router /v1/models | < 50ms | > 500ms (upstream stall) |
| Router /healthz | < 100ms | > 1s |
| Full RAG query (AnythingLLM → router → chat with retrieval) | 5-30s | > 90s |

### § 12.2 Baseline measurement

Capture a baseline whenever you make a significant change (model swap, ROCm update, kernel update):

```bash
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
LOG=/tank/perf-baseline-$(date +%Y%m%d).log

# 1. Cold chat (first request after a 5-min idle)
sleep 300
time curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"model":"rag-qwen3.6","messages":[{"role":"user","content":"write a sentence"}],"max_tokens":50}' \
    http://192.168.6.153:8000/v1/chat/completions | jq -r '.choices[0].message.content' | tee -a $LOG

# 2. Sustained throughput
time curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"model":"rag-qwen3.6","messages":[{"role":"user","content":"explain TCP slow start in 500 words"}],"max_tokens":600}' \
    http://192.168.6.153:8000/v1/chat/completions | jq '{tokens:.usage.completion_tokens, ms:0}' | tee -a $LOG

# 3. Embed throughput
time for i in 1 2 3 4 5; do
  curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
      -d "{\"input\":\"baseline test $i — $(head -c 1000 /etc/services | base64 | head -c 800)\"}" \
      http://192.168.6.153:8000/v1/embeddings > /dev/null &
done; wait

# 4. Rerank throughput
time curl -sf -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
    -d '{"query":"what is RAG","documents":["RAG combines retrieval and generation","Cats are mammals","Vector databases store embeddings"]}' \
    http://192.168.6.153:8000/v1/rerank | jq | tee -a $LOG
```

Record the date, what changed, and timings. The next operator can grep `/tank/perf-baseline-*.log` to see drift.

### § 12.3 Regression detection from access.log

```bash
# 95th-percentile chat duration over the last 1000 requests
pct exec 153 -- tail -1000 /var/log/llm-router/access.log \
    | jq -s 'map(select(.route == "/v1/chat/completions" and .status == 200) | .duration_ms) | sort | .[(length * 0.95 | floor)]'

# Sliding window: same query against the previous 1000
pct exec 153 -- bash -c '
  tail -2000 /var/log/llm-router/access.log \
    | jq -s "map(select(.route == \"/v1/chat/completions\" and .status == 200)) | {
        recent_1000_p95:    (.[1000:] | map(.duration_ms) | sort | .[(length * 0.95 | floor)]),
        previous_1000_p95:  (.[:1000] | map(.duration_ms) | sort | .[(length * 0.95 | floor)])
    }"
'
```

If recent p95 is > 1.5× previous, something regressed. Walk back through recent updates / config changes.

### § 12.4 What "slow" usually means

| Symptom | Likely cause |
|---|---|
| All chat is slower by a constant factor | Model weight changed (different quant?), or ROCm tuning env var changed |
| Slow only when input is large | Prompt-processing bottleneck — `--cache-reuse` or `--cache-ram` got reset |
| First-request-after-idle slow, then fast | Keepalive timer stopped — see [§ 3.2](#-32-slow-first-token-after-idle) |
| Slow only when 2+ concurrent requests | Hitting `CHAT_CONCURRENCY=1` queue depth — see [§ 6.4](#-64-changing-admission-control) |
| Slow embed but fast chat | Embedder queue saturated — bump `EMBED_CONCURRENCY` or scale up `--parallel` on embed unit |

---

## § 13. Hardware changes

> **When to read this:** physical hardware is being added, replaced, or failing. Mark each procedure ✅ tested or ⚠ speculative before following.

### § 13.1 ✅ Replacing a failed NVMe in the ZFS mirror

ZFS mirror is self-healing — a single failed drive doesn't stop the cluster. Replace at next maintenance window:

```bash
# Identify the failed drive
zpool status tank
# Look for DEGRADED or UNAVAIL device

# Take a snapshot of every dataset for safety
zfs snapshot -r tank@pre-disk-replace-$(date +%Y%m%d)

# Power down (or hot-swap if hardware supports it — check chassis spec)
shutdown -h now

# Replace the physical drive
# Boot. ZFS will likely auto-detect the missing drive

# Tell ZFS about the replacement
zpool replace tank <old-device> /dev/disk/by-id/nvme-<new-drive-id>

# Watch the resilver
zpool status tank   # repeat; expect "scan: resilver in progress"
```

Resilver takes a few hours for a fully-populated 2TB drive. During resilver, performance is degraded but the cluster stays up.

### § 13.2 ⚠ RAM upgrade

```bash
# 1. Shut down LXCs that you can afford to lose for a few minutes
pct shutdown 151 153 154 155 --timeout 60

# 2. Shut down the host
shutdown -h now

# 3. Physical: install new RAM. With 4 DIMMs on AM5, AMD-validated speed drops to 3600 MT/s
#    (vs 5600 MT/s with 2 DIMMs).

# 4. Boot. Verify in BIOS that all RAM is recognized and DOCP/EXPO is applied.

# 5. Once Proxmox is up, LXCs auto-start (onboot=1 + startup order set by scripts).

# 6. Verify:
free -h               # host total RAM
pct list              # all LXCs running
```

If host memory is now significantly larger, consider bumping ZFS ARC cap:

```bash
echo "options zfs zfs_arc_max=34359738368" > /etc/modprobe.d/zfs.conf  # e.g., 32 GB
update-initramfs -u
```

### § 13.3 ⚠ Adding a third V620

PCIE_3 (bottom slot, PCIe 4.0 x4 from chipset) is empty by default. Adding a third V620:

**Power:** ~225W sustained, well under the 1200W PSU's headroom. Requires 2× 8-pin PCIe. The PSU has spare connectors after removing the 3060.

**Cooling:** needs another 80mm shroud kit + NF-A8. Probably also a third GPU support bracket.

**Lanes:** PCIE_3 is x4 from chipset — slower than PCIE_1/2's x8 CPU lanes. Acceptable for an inference card but tensor-split with the two CPU-attached V620s may be limited by PCIE_3's bandwidth.

**Software:** edit [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) to add the third render node and KFD mapping. The current logic finds exactly 2 V620 render nodes — that check needs to be relaxed for 3 cards.

Speculative — not yet exercised. See [`local-gpu-cluster-v2.md` §1.3](./local-gpu-cluster-v2.md) for the design analysis. Test on a snapshot first.

### § 13.4 ⚠ GPU for Bazzite VM in PCIE_3

Passing through a third GPU (different model, e.g., a 4070 or 7700 XT) to a Bazzite gaming VM:

- Slot: PCIE_3, x4 chipset
- VFIO passthrough to a Proxmox VM
- IOMMU groups should isolate cleanly per [`local-gpu-cluster-v2.md` §3](./local-gpu-cluster-v2.md)
- **X870E known quirk:** after a VFIO Function Level Reset, the slot can downgrade from x16 to x8 until next cold boot. Doesn't affect LXC passthrough (which is what the LLM cluster uses); does affect VFIO. Cold-boot recovery may be required after each VM stop.

Detailed procedure has not been written. The [`local-gpu-cluster-v2.md` §3 Known X870E PCIe quirk](./local-gpu-cluster-v2.md) section has the relevant analysis.

### § 13.5 ⚠ Adding a second host for cluster federation

Out of scope for this guide. If you genuinely need a second host, you're outgrowing the architecture; consult [`local-gpu-cluster-v2.md`](./local-gpu-cluster-v2.md) for thinking on Intel X710 10 GbE and federation.

### Verification

After any hardware change:

```bash
# Hardware level
lspci -nn | grep -iE "navi 21|\[1002:73a1\]"   # GPUs
lsblk -d                                         # storage
free -h                                          # RAM
dmidecode -t memory | grep -i "size:"            # per-DIMM

# Cluster level
/root/local-gpu-cluster/scripts/60-verify.sh
```

---

## § 14. When to consult what

Cross-reference index. When you want X, this is where to find the authoritative source.

### Tasks → docs

| Task | Primary | Secondary |
|---|---|---|
| Deploy from scratch | [setup-runbook.md](./setup-runbook.md) | [scripts/README.md](./scripts/README.md) |
| Understand why the architecture is this way | [local-gpu-cluster-v2.md](./local-gpu-cluster-v2.md) | — |
| Day-2 operate the cluster | **this doc** | — |
| Bootstrap automation | [scripts/README.md](./scripts/README.md) | — |
| RAG ingest system reference | [scripts/rag/README.md](./scripts/rag/README.md) | — |
| Document ingestion helper tools | [scripts/tools/README.md](./scripts/tools/README.md) | — |
| Past failures + fixes | [LESSONS.md](./LESSONS.md) | — |
| Current session decisions | [SESSION_HANDOFF.md](./SESSION_HANDOFF.md) | — |
| Operator/assistant rules | [RULES.md](./RULES.md) | [TASK-LOOP.md](./TASK-LOOP.md) |

### Components → files

| Component | Source of truth |
|---|---|
| Chat / embed / rerank model defaults | [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) |
| Router code | [`scripts/files/router-app.py`](./scripts/files/router-app.py) |
| Router env defaults | [`scripts/53-lxc-router.sh`](./scripts/53-lxc-router.sh) |
| AnythingLLM env defaults | [`scripts/54-lxc-anythingllm.sh`](./scripts/54-lxc-anythingllm.sh) |
| Workspace tuning | [`scripts/57-configure-anythingllm.sh`](./scripts/57-configure-anythingllm.sh) |
| MCP bridge | [`scripts/58-mcp-sdg.sh`](./scripts/58-mcp-sdg.sh) + [`scripts/files/mcp-sdg-server.py`](./scripts/files/mcp-sdg-server.py) |
| Fan control bridge | [`scripts/56-fan-control.sh`](./scripts/56-fan-control.sh) |
| RAG sources manifest | [`scripts/rag/sources.yaml`](./scripts/rag/sources.yaml) |
| RAG refresh orchestrator | [`scripts/rag/refresh.py`](./scripts/rag/refresh.py) |
| Operator-tunable env vars | [`scripts/config.env.example`](./scripts/config.env.example) |
| Verification suite | [`scripts/60-verify.sh`](./scripts/60-verify.sh) |

### Symptoms → sections

| Symptom | Section |
|---|---|
| Cluster won't start after host reboot | [§ 2](#-2-cluster-health--observability), [§ 3](#-3-common-troubleshooting) |
| Chat unit won't start | [§ 3.1](#-31-chat-unit-wont-start) |
| Chat is slow / first token slow | [§ 3.2](#-32-slow-first-token-after-idle), [§ 12](#-12-performance-baselines) |
| Embed dropping chunks | [§ 3.3](#-33-embedder-dropping-chunks) |
| `413 request too large` | [§ 5](#-5-three-tier-token-limit-layering) |
| `403 unauthorized` | [§ 3.8](#-38-client-bearer-auth-failures), [§ 7](#-7-secrets--key-rotation) |
| Workspace doc count looks doubled | [§ 8.6](#-86-the-workspace-20-dup-factor) |
| RAG retrieval suddenly worse | [§ 9.5](#-95-critical-dont-use---pooling-cls-with-qwen3-embedding) (pooling), [§ 9.1](#-91-if-you-change-x-you-must-do-y) (re-ingest needed?) |
| `/tank` nearing full | [§ 4.6](#-46-cleaning-up-unused-cached-models), [§ 10.5](#-105-disk-space-monitoring) |
| Tavily quota exhausted | [§ 6.7](#-67-tavily-quota-monitoring) |
| Fans loud | [§ 3.7](#-37-fan-bridge-stopped) |
| Slow specifically under concurrent load | [§ 6.4](#-64-changing-admission-control), [§ 12.4](#-124-what-slow-usually-means) |
| Spec-decode "not implemented" log | [§ 3.9](#-39-no-implementations-specified-for-speculative-decoding) (expected) |

---

## Maintenance notes

This doc points to source-of-truth files rather than duplicating values, so it should age more slowly than the runbook or architecture docs. Things that will still drift:

- Performance baselines (§ 12) — capture fresh baselines after any major model or ROCm change
- Currently-deployed model list (§ 4.1) — update after model swaps
- Cluster topology (§ 1 authority hierarchy) — update if LXCs are added/removed
- Tested-vs-speculative markers in § 13 — promote ⚠ to ✅ after exercising a procedure

Treat any specific value here that contradicts the linked source-of-truth file as a stale day-2-ops bug, not an authoritative override.
