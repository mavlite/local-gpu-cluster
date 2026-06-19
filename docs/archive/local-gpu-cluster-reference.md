# Local GPU Cluster — Technical Reference

**Dell Precision T7910 · 3× RTX 3060 12 GB · Ollama · AnythingLLM · MCP · VMware Cloud Foundation knowledge base**

![Platform](https://img.shields.io/badge/platform-Linux-informational)
![GPU](https://img.shields.io/badge/GPU-3×%20RTX%203060%2012GB-76b900)
![Runtime](https://img.shields.io/badge/runtime-Ollama-000000)
![Models](https://img.shields.io/badge/models-Qwen%202.5%20%7C%20Qwen%203.6-blue)
![RAG](https://img.shields.io/badge/RAG-AnythingLLM%20%2B%20LanceDB-0a6cf5)
![Revision](https://img.shields.io/badge/revision-4-lightgrey)
![License](https://img.shields.io/badge/license-CC%20BY--SA%204.0-yellow)

End-to-end setup of a single-workstation LLM serving stack on a used Dell Precision T7910 with three RTX 3060 12 GB cards pooled for 36 GB of VRAM. Covers hardware baseline, PCIe topology verification, Ollama multi-GPU configuration, a custom SSE-keepalive proxy for reasoning-class models, an AnythingLLM RAG pipeline over VMware Cloud Foundation and Keycloak documentation, three custom MCP containers exposing those workspaces (and a live Broadcom TechDocs scraper) to remote agentic clients, an automated documentation-refresh service that keeps the VCF corpus current against the upstream site, and the diagnostic path that produced the chunk-size and embedder-dimension fixes documented later.

> **Scope note.** This document describes one working configuration verified on the author's hardware as of the revision date. Paths, hostnames, and service port numbers are illustrative — adapt to your environment. Upstream software changes quickly; always check current project documentation before acting on version-specific details here.

---

## Table of Contents

- [1. Hardware](#1-hardware)
  - [1.1 Host: Dell Precision T7910](#11-host-dell-precision-t7910)
  - [1.2 GPUs: 3× EVGA RTX 3060 12 GB](#12-gpus-3-evga-rtx-3060-12-gb)
  - [1.3 Why this GPU selection](#13-why-this-gpu-selection)
- [2. PCIe Topology & Link Verification](#2-pcie-topology--link-verification)
- [3. Ollama Configuration](#3-ollama-configuration)
  - [3.1 Install](#31-install)
  - [3.2 Multi-GPU and network binding](#32-multi-gpu-and-network-binding)
  - [3.3 Models pulled](#33-models-pulled)
  - [3.4 Custom Modelfiles](#34-custom-modelfiles)
  - [3.5 Sanity check: confirm all three GPUs are being used](#35-sanity-check-confirm-all-three-gpus-are-being-used)
  - [3.6 VRAM budget for 131K context](#36-vram-budget-for-131k-context)
- [4. No-Think Proxy (SSE Keepalive Shim)](#4-no-think-proxy-sse-keepalive-shim)
- [5. AnythingLLM + `rag-qwen25` RAG Pipeline](#5-anythingllm--rag-qwen25-rag-pipeline)
- [6. MCP Server Architecture](#6-mcp-server-architecture)
  - [6.1 Three custom MCP containers](#61-three-custom-mcp-containers)
  - [6.2 Tool routing and selection](#62-tool-routing-and-selection)
  - [6.3 Container build pattern](#63-container-build-pattern)
  - [6.4 Citation formatting helper](#64-citation-formatting-helper)
  - [6.5 OpenCode client configuration](#65-opencode-client-configuration)
  - [6.6 External MCP servers (SearXNG, Tavily, Context7)](#66-external-mcp-servers-searxng-tavily-context7)
- [7. VCF Documentation Scraping & Ingestion](#7-vcf-documentation-scraping--ingestion)
- [8. The Chunk-Size Fix](#8-the-chunk-size-fix)
- [9. VCF Documentation Auto-Updater](#9-vcf-documentation-auto-updater)
  - [9.1 Goal and design constraints](#91-goal-and-design-constraints)
  - [9.2 Architecture](#92-architecture)
  - [9.3 Discovery strategy: workspace seeds + one-hop crawl](#93-discovery-strategy-workspace-seeds--one-hop-crawl)
  - [9.4 Change detection and ingestion](#94-change-detection-and-ingestion)
  - [9.5 Safety controls](#95-safety-controls)
  - [9.6 Operational pattern](#96-operational-pattern)
- [10. SDG (Keycloak) Workspace](#10-sdg-keycloak-workspace)
- [Appendix A — Directory Layout on the Host](#appendix-a--directory-layout-on-the-host)
- [Appendix B — Service Port Map](#appendix-b--service-port-map)
- [Appendix C — Smoke Tests](#appendix-c--smoke-tests)

---

## Revision history

| Rev | Date | Notes |
|---|---|---|
| 4 | Apr 2026 | Three custom MCP containers replacing npx-launched servers (`anythingllm-search-mcp`, `broadcom-techdocs-mcp`, `sdg-docs-mcp`). Tool descriptions tightened for routing reliability. `format_source()` helper for human-readable citations. New §9 documenting the Docker-deployed VCF documentation auto-updater (discover-fetch-ingest pipeline with SQLite state and dry-run safety). New §10 documenting the SDG (Keycloak) workspace. |
| 3 | Apr 2026 | Qwen 3.6 model family adopted (`coder-36`, `coder-long`). Parameter recipes corrected to Qwen's published thinking-mode sampling guidance. §3.4.1 rationale added. Public-posting polish. |
| 2 | Apr 2026 | Embedder switched from `nomic-embed-text` (768-dim) to `qwen3-embed-compact` (1024-dim). Custom Modelfiles documented. §5.7 re-embedding procedure added. VRAM math at 131K context. |
| 1 | Apr 2026 | Initial reference: hardware, PCIe verification, Ollama, no-think proxy, AnythingLLM + Qwen 2.5, MCP, VCF scraping, chunk-size fix. |

---

## 1. Hardware

### 1.1 Host: Dell Precision T7910

| Component | Spec |
|---|---|
| Form factor | Full tower (conventional airflow, front-to-back) |
| CPUs | Dual Intel Xeon E5-26xx v3/v4 (LGA 2011-3) |
| PSU | 1300 W; **675 W dedicated to graphics** via three 8-pin PCIe auxiliary connectors on the Power Distribution Board |
| PCIe slots (dual-CPU config) | 5× PCIe 3.0 + 1× PCIe 2.0 + 1× legacy PCI |
| Chassis airflow | Tower — designed for finned active coolers; **not** adequate for passively cooled server GPUs without a supplemental fan shroud |

The dual-CPU riser is required to expose all five PCIe 3.0 slots; single-CPU T7910 configurations only light up a subset. The three GPU-capable 8-pin connectors on the Power Distribution Board are the hard ceiling for how many discrete cards the chassis can legitimately power.

### 1.2 GPUs: 3× EVGA RTX 3060 12 GB

| Spec | Per card | Pooled (3×) |
|---|---|---|
| VRAM | 12 GB GDDR6 | **36 GB** |
| Memory bandwidth | 360 GB/s | — |
| Architecture | Ampere (GA106) | — |
| CUDA cores | 3,584 | — |
| TDP | 170 W | ~510 W |
| Form factor | Dual-slot, full-height, active blower/axial | — |
| PCIe | 4.0 x16 (operates at PCIe 3.0 x16 on T7910) | — |

Each card is dual-slot, so the three cards occupy six physical slot widths — which is exactly what the T7910's PCIe layout supports when the cards are assigned to slots 1, 3, and 5 (leaving the single-slot gaps between them for airflow). Total power draw of ~510 W sits comfortably inside the 675 W graphics budget.

### 1.3 Why this GPU selection

36 GB pooled VRAM is enough to run a 30B-class model at Q4 quantization end-to-end on the GPUs. Used 3060 12 GB cards sit in the $200–$300 range, giving the full cluster a street cost of roughly $750 — far below what a single 24 GB card (3090, 4090, A6000) would run.

---

## 2. PCIe Topology & Link Verification

### 2.1 Expected topology

On a dual-CPU T7910, GPU slots split across both CPUs' root complexes. A correct install lights up all three cards at PCIe 3.0 x16.

### 2.2 Verify bus enumeration

All three cards must enumerate on the PCI bus before anything else matters. Run:

```bash
lspci -nn | grep -i nvidia
```

Expected output (bus addresses will vary):

```text
03:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA106 [GeForce RTX 3060] [10de:2504]
04:00.0 Audio device [0403]: NVIDIA Corporation GA106 High Definition Audio Controller [10de:228e]
81:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA106 [GeForce RTX 3060] [10de:2504]
82:00.0 Audio device [0403]: NVIDIA Corporation GA106 High Definition Audio Controller [10de:228e]
83:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA106 [GeForce RTX 3060] [10de:2504]
84:00.0 Audio device [0403]: NVIDIA Corporation GA106 High Definition Audio Controller [10de:228e]
```

Three VGA entries, three matching audio functions. If only two show up, the card is either seated incorrectly or the slot is unpopulated on the riser (common when the second CPU isn't installed).

### 2.3 Verify link width and speed

`lspci -nn` confirms presence but not link negotiation. To confirm each card is actually running at PCIe 3.0 x16 — not degraded to x8 or Gen2 — query each bus address:

```bash
for bus in 03:00.0 81:00.0 83:00.0; do
  echo "=== $bus ==="
  sudo lspci -vvv -s $bus | grep -E "LnkCap:|LnkSta:"
done
```

Look for:

```text
LnkCap: Port #0, Speed 16GT/s, Width x16, ...
LnkSta: Speed 8GT/s (downgraded), Width x16 (ok), ...
```

Two failure modes to watch for:

- **Width x8 when x16 was expected** — the card is in a slot wired for x8 only, or a neighbouring NVMe adapter is stealing lanes. On T7910 that means re-seating into slots 1, 3, or 5, which are the full x16 slots on the dual-CPU riser.
- **Speed 8GT/s (Gen3) shown as "downgraded"** — this is expected and fine. The 3060 is a PCIe 4.0 card; the T7910 is Gen3 silicon, so Gen3 x16 is the ceiling.

### 2.4 NVIDIA-side verification

Once the driver is loaded:

```bash
nvidia-smi --query-gpu=index,name,pci.bus_id,pcie.link.gen.current,pcie.link.width.current --format=csv
```

Expected:

```text
index, name, pci.bus_id, pcie.link.gen.current, pcie.link.width.current
0, NVIDIA GeForce RTX 3060, 00000000:03:00.0, 3, 16
1, NVIDIA GeForce RTX 3060, 00000000:81:00.0, 3, 16
2, NVIDIA GeForce RTX 3060, 00000000:83:00.0, 3, 16
```

All three at Gen3 x16. Anything lower and inter-GPU tensor transfers during model sharding will bottleneck.

### 2.5 NUMA / CPU affinity note

Because GPUs 1 and 2 (buses `81:00.0` and `83:00.0`) hang off the second CPU's root complex, inference processes benefit from being pinned to the same NUMA node as the card they use. For the current single-server, single-model setup this is mostly moot — Ollama runs one model across all three GPUs and the cross-socket hop is dominated by PCIe latency, not NUMA. Flag it as something to measure if performance tuning becomes a priority.

---

## 3. Ollama Configuration

### 3.1 Install

Ubuntu Server 24.04 LTS host:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

The installer drops a `systemd` unit at `/etc/systemd/system/ollama.service`.

### 3.2 Multi-GPU and network binding

Edit the unit to expose Ollama on the LAN, allow CORS from AnythingLLM/OpenCode hosts, and explicitly enable all three GPUs. `sudo systemctl edit ollama` produces an override file:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_ORIGINS=*"
Environment="CUDA_VISIBLE_DEVICES=0,1,2"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_FLASH_ATTENTION=1"
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Notes on the envs:

- `CUDA_VISIBLE_DEVICES=0,1,2` forces Ollama to see all three GPUs. With this set, a model that exceeds 12 GB is automatically sharded across cards by layer.
- `OLLAMA_FLASH_ATTENTION=1` enables Flash Attention kernels. These work on Ampere, so they're compatible with the 3060s.
- `OLLAMA_KEEP_ALIVE=30m` holds model weights in VRAM for half an hour after the last request, eliminating cold-start reload on bursty RAG traffic.
- `OLLAMA_NUM_PARALLEL=2` allows two concurrent requests against the same loaded model. Push this higher only after measuring VRAM headroom — KV cache grows per active request.

### 3.3 Models pulled

Two model families are in active use — Qwen 2.5 32B dense (the original RAG backbone) and Qwen 3.6 35B-A3B (the newer MoE model used for agentic coding). Both are Q4_K_M.

```bash
# Base models from the registry
ollama pull qwen2.5:32b-instruct-q4_K_M   # Dense 32.8B, 32K ctx — legacy RAG base
ollama pull qwen3.6:35b-a3b               # MoE 36B total / 3B active, 256K ctx, vision+tools+thinking

# Embedding family — Qwen3 0.6B embedder (1024-dim vectors)
ollama pull qwen3-embedding:0.6b          # Base embedder, inherits 32K ctx
```

### 3.4 Custom Modelfiles

The actual runtime inventory is a set of purpose-built Modelfiles layered on top of the base models. Build each with `ollama create <name> -f <Modelfile>`.

**`rag-qwen25`** — the retrieval-augmented answering variant. Built from qwen2.5:32b. Low temperature for deterministic citation behavior:

```dockerfile
FROM qwen2.5:32b-instruct-q4_K_M
PARAMETER temperature 0.3
PARAMETER top_k 20
PARAMETER top_p 0.95
PARAMETER min_p 0
PARAMETER num_ctx 32768
PARAMETER num_predict 4096
SYSTEM """
You are a focused retrieval-augmented answering assistant. Given a user question and retrieved
document chunks, produce a direct, concise answer. Cite which document each claim comes from.
If the retrieved context does not contain the answer, say so explicitly.
"""
```

**`coder-36`** — standard agentic-coding variant. Built from `qwen3.6:35b-a3b`. 64K context for typical coding sessions:

```dockerfile
FROM qwen3.6:35b-a3b

# Qwen 3.6 thinking-mode recommended sampling
PARAMETER temperature 0.6
PARAMETER top_k 20
PARAMETER top_p 0.95
PARAMETER min_p 0

# Coding-appropriate repetition control
PARAMETER presence_penalty 0
PARAMETER repeat_penalty 1

# Context sizing
PARAMETER num_ctx 65536
PARAMETER num_predict 16384

SYSTEM """
You are an autonomous coding agent running inside <your-coding-client> on <your-workstation-OS>.
<Environment-specific rules here: shell conventions, tool-call requirements, workflow hints.
 Keep this section tailored to your actual toolchain — a generic coding-agent prompt will work
 but won't guide the model away from platform-specific pitfalls like shell syntax differences.>
"""
```

**`coder-long`** — extended-context variant for large refactors and whole-repo reasoning. Identical parameters to `coder-36` except for the context window:

```dockerfile
FROM qwen3.6:35b-a3b
# All PARAMETER lines and SYSTEM prompt identical to coder-36,
# except:
PARAMETER num_ctx 131072      # up from 65536
```

Keep `num_predict` at 16384 even on `coder-long`. A 32K output ceiling at the cluster's ~15 tok/s generation rate allows a single response to run past 30 minutes, during which long silent stretches inside a generation can trip downstream idle timeouts even with the no-think proxy's SSE keepalive in place. Chain shorter responses instead.

**Tip — extract existing Modelfiles with `ollama show --modelfile`.** When adapting this setup, don't retype Modelfiles from scratch; `ollama show <model> --modelfile` produces a round-trippable Modelfile including the complete `SYSTEM`, `TEMPLATE`, `RENDERER`, and `PARSER` directives. Edit only the `PARAMETER` lines and re-create with `ollama create <n> -f <file>`.

### 3.4.1 Parameter rationale

The `coder-36` / `coder-long` parameter block differs from Ollama's Qwen 3.6 pull defaults (`temperature 1`, `presence_penalty 1.5`). Both defaults are inherited from upstream tuning aimed at chat and creative-writing use, not agentic coding. The adjustments:

- **`temperature 0.6`, `top_p 0.95`, `top_k 20`, `min_p 0`** — Qwen's published thinking-mode sampling recipe for the 3.x family. The alternative non-thinking recipe (`temperature 0.7`, `top_p 0.8`) applies when `/no_think` is used to suppress reasoning; since the proxy in §4 handles think-block stripping externally for RAG traffic and leaves reasoning intact for agentic traffic, thinking-mode sampling is the appropriate baseline.
- **`presence_penalty 0`** — The default 1.5 penalises *any* token repetition, which fights code where variable names, import patterns, and structural tokens legitimately repeat across a response. At 1.5 the model will start substituting synonyms for a name that's already appeared (e.g., `user_id` → `userID` → `uid` within the same function), breaking syntactic consistency. Qwen's guidance for coder-tuned variants recommends 0–0.5; 0 keeps naming consistent.
- **`repeat_penalty 1`** — 1.0 means no penalty. Stacking `repeat_penalty > 1` with a non-zero `presence_penalty` is a known path to degraded output in the Qwen family.

**Embedding variants.** Three derivatives of `qwen3-embedding:0.6b`, used in different deployment modes:

| Tag | num_ctx | num_gpu | When to use |
|---|---|---|---|
| `qwen3-embedding:0.6b` | 32768 (default) | auto | Default GPU embedder |
| `qwen3-embed-compact:latest` | 2048 (override) | auto | Short-chunk embedding — keeps KV cache tiny, frees VRAM for concurrent chat |
| `qwen3-embed-cpu:latest` | 32768 | 0 (forced CPU) | Offloads embedding entirely to CPU to preserve GPU for chat during bulk ingestion |

The CPU variant is what you reach for when doing a full re-embed of the VCF corpus — the embedder runs on CPU while `coder-long` or `rag-qwen25` continues serving chat traffic on the GPUs uninterrupted.

**⚠ Critical: re-embed required after switching embedders.** The previous version of this document specified `nomic-embed-text` (768-dim). Qwen3 embedding produces **1024-dim vectors**. LanceDB collections are dimension-locked — a query embedded at 1024 cannot search chunks stored at 768, and the mismatch fails silently with nonsense retrieval quality rather than a clear error. If any workspace was populated under the old embedder, every document in it needs to be re-embedded after the switch. See §5.7.

### 3.5 Sanity check: confirm all three GPUs are being used

With a model loaded:

```bash
ollama ps                                              # what's loaded, VRAM total, keep-alive
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv -l 1
```

Expected snapshot for `coder-long` at 131K context:

```text
NAME                 SIZE     PROCESSOR    CONTEXT
coder-long:latest    32 GB    100% GPU     131072
```

Per-GPU VRAM should split roughly evenly across the three cards (~10.7 / 10.5 / 9.9 GB). 100% GPU placement means nothing overflowed to CPU. A split like 11 / 11 / 4 would indicate the scheduler clumped layers on cards 0 and 1 and left card 2 underutilized — set `OLLAMA_SCHED_SPREAD=1` in the unit file and restart to force even distribution.

### 3.6 VRAM budget for 131K context

The 32 GB `ollama ps` size for `coder-long` is **23 GB weights + ~9 GB KV cache**. That leaves ~4 GB free across the pooled 36 GB — enough to serve one request, not enough for concurrent sessions. For single-user workflows this is fine; if `OLLAMA_NUM_PARALLEL > 1` is set, either drop `coder-long` context to 64K (use `coder-36` instead) or accept that the second request will queue.

Rule of thumb for Qwen 3.6 35B-A3B at Q4_K_M: KV cache grows at roughly 70 MB per 1K tokens of context. A 256K-context session would need ~18 GB of KV cache on top of the 23 GB weights, which overflows the 36 GB pool and triggers CPU offload. **Context windows above 131K are not viable on 3× 3060 36 GB without dropping to Q3 quant or a smaller base model.**

---

## 4. No-Think Proxy (SSE Keepalive Shim)

### 4.1 Problem being solved

Qwen 3.6 (`qwen35moe` architecture, `thinking` capability confirmed by `ollama show`) emits a `<think>` reasoning block before the visible answer. For RAG retrieval, the reasoning is unused — and on the 3060 cluster it takes 30–60 seconds to generate at the cluster's ~18–25 tok/s on a 131K-context load. Downstream clients have opinionated idle-timers; OpenCode in particular has known timeout bugs where a 30-second silence on the SSE stream terminates the request even though the model is still producing reasoning tokens off-stream.

The proxy sits between clients and Ollama and does two jobs:

1. **Normalize messages** to strip or suppress the `<think>` preamble so the client sees the final answer with minimal latency.
2. **Emit SSE keepalive pings** (`: ping\n\n` comment frames) every 10–15 seconds on the outbound stream so the client's idle-timer never expires while the model is thinking.

**Important caveat for Qwen 3.6 specifically:** Qwen 3.6 introduced "thinking preservation" — the ability to retain reasoning context across multi-turn conversations. For agentic-coding workflows (OpenCode iterating on a repo), this is valuable and the strip-all-thinking behavior actively fights it. Route agentic traffic through a second proxy instance with `<think>` stripping disabled (keepalive only), and keep the full-strip behavior only for single-shot RAG traffic. See §4.5.

### 4.2 Architecture

```text
Client (OpenCode / AnythingLLM / curl)
          │
          │  OpenAI-compatible /v1/chat/completions
          ▼
   No-Think Proxy  (FastAPI, port 11435)
          │  - inject "no_think" directive into system prompt
          │  - normalize request format
          │  - emit SSE keepalives during upstream silence
          │  - strip <think>...</think> blocks from response
          ▼
        Ollama  (port 11434)
          │
          ▼
     3× RTX 3060
```

### 4.3 Minimal implementation

`/opt/nothink-proxy/app.py`:

```python
import asyncio, json, re, time
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()
UPSTREAM = "http://127.0.0.1:11434"
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
KEEPALIVE_INTERVAL = 12  # seconds

def normalize_request(body: dict) -> dict:
    # Inject a no-think directive as a system-level hint
    msgs = body.get("messages", [])
    if msgs and msgs[0].get("role") != "system":
        msgs.insert(0, {"role": "system", "content": "/no_think"})
    else:
        sys = msgs[0].get("content", "")
        if "/no_think" not in sys:
            msgs[0]["content"] = "/no_think\n" + sys
    body["messages"] = msgs
    return body

async def sse_stream_with_keepalive(upstream_url: str, payload: dict):
    """
    Proxy an upstream SSE stream, emitting ': ping' comment frames
    whenever the upstream is silent for KEEPALIVE_INTERVAL seconds.
    """
    last_event = time.monotonic()
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", upstream_url, json=payload) as r:
            queue: asyncio.Queue = asyncio.Queue()

            async def reader():
                async for chunk in r.aiter_text():
                    await queue.put(chunk)
                await queue.put(None)  # EOF

            task = asyncio.create_task(reader())
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            queue.get(), timeout=KEEPALIVE_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        # Upstream silent — emit SSE comment keepalive
                        yield b": ping\n\n"
                        continue
                    if chunk is None:
                        break
                    # Strip think blocks from any complete payloads
                    cleaned = THINK_RE.sub("", chunk)
                    yield cleaned.encode()
                    last_event = time.monotonic()
            finally:
                task.cancel()

@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    body = normalize_request(body)
    stream = body.get("stream", False)
    url = f"{UPSTREAM}/v1/chat/completions"

    if stream:
        return StreamingResponse(
            sse_stream_with_keepalive(url, body),
            media_type="text/event-stream",
        )
    # Non-streaming path
    async with httpx.AsyncClient(timeout=None) as c:
        r = await c.post(url, json=body)
        data = r.json()
        # Strip think blocks from the final message
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            if "content" in msg:
                msg["content"] = THINK_RE.sub("", msg["content"])
        return data
```

### 4.4 systemd unit

`/etc/systemd/system/nothink-proxy.service`:

```ini
[Unit]
Description=No-Think Proxy (Ollama SSE keepalive + think-block stripper)
After=ollama.service
Requires=ollama.service

[Service]
Type=simple
User=ollama
WorkingDirectory=/opt/nothink-proxy
ExecStart=/opt/nothink-proxy/venv/bin/uvicorn app:app \
    --host 0.0.0.0 --port 11435 \
    --timeout-keep-alive 300
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### 4.5 Client configuration

All downstream clients point at the proxy, not Ollama directly:

- **OpenCode** (coding assistance — wants thinking preserved for multi-turn refactors):
  - `base_url: http://<host>:11435/v1`
  - `model: coder-36` (or `coder-long` for big contexts)
  - For the agentic-coding use case, consider running a **second proxy instance on port 11436 with the `<think>` strip disabled** (comment out the `THINK_RE.sub(...)` call) — Qwen 3.6's "thinking preservation" feature relies on reasoning being present across turns, and the RAG proxy's aggressive strip actively fights it. Route OpenCode to 11436, AnythingLLM to 11435.
- **AnythingLLM** (RAG — wants thinking stripped for latency):
  - OpenAI-compatible endpoint: `http://<host>:11435/v1`
  - Model: `rag-qwen25:latest`
- `curl` for manual test:

```bash
curl -N http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "rag-qwen25:latest",
    "stream": true,
    "messages": [{"role":"user","content":"ping"}]
  }'
```

`-N` disables curl's buffering so the `: ping` keepalive frames are visible.

---

## 5. AnythingLLM + `rag-qwen25` RAG Pipeline

### 5.1 Role in the stack

AnythingLLM is the RAG front end: document ingestion, chunking, embedding, vector storage, retrieval, and the chat UI all live inside it. It talks to Ollama (via the no-think proxy) for generation and for embeddings.

### 5.2 Deployment

Docker Compose on the same host as Ollama. `/opt/anythingllm/docker-compose.yml`:

```yaml
services:
  anythingllm:
    image: mintplexlabs/anythingllm:latest
    container_name: anythingllm
    restart: unless-stopped
    cap_add:
      - SYS_ADMIN
    ports:
      - "3001:3001"
    volumes:
      - /opt/anythingllm/storage:/app/server/storage
      - /opt/anythingllm/.env:/app/server/.env
    environment:
      STORAGE_DIR: /app/server/storage
```

### 5.3 LLM and embedding provider

**Chat LLM** (Settings → LLM Preference):

- Provider: **Ollama** (or Generic OpenAI if pointing at the no-think proxy)
- Base URL: `http://<host>:11435/v1` (no-think proxy) — this is the path that strips `<think>` blocks and keepalives the SSE stream
- Model: `rag-qwen25:latest` — the custom Modelfile from §3.4 (lower temperature, RAG-focused system prompt)
- Token context window: `32768` (matches the Modelfile's `num_ctx`)

The global default is `rag-qwen25:latest` for now. The workspace-level override path (§5.5) lets the VCF workspace stay on Qwen 2.5 while other workspaces migrate to a Qwen 3.6-based RAG variant when one is built.

**Embedder** (Settings → Embedding Preference):

- Provider: **Ollama**
- Base URL: `http://<host>:11434` (direct — embeddings bypass the no-think proxy)
- Model: **`qwen3-embed-compact:latest`** (2K `num_ctx` override — minimizes KV cache during embedding, matches VCF chunk sizes at §8's 2500/500 config)
- Embedding dimension: **1024** (Qwen3 embedder's native — not 768)
- Max embed chunk length: matches AnythingLLM's character-based chunk size; no additional tuning needed at the embedder level

The three `qwen3-embed-*` variants produce **identical vectors** — they're all the same underlying model with only inference-time parameter differences (`num_ctx`, `num_gpu`). Switching between them at query time does not invalidate an existing LanceDB collection. Use `qwen3-embed-compact` as the standard; swap to `qwen3-embed-cpu` only when doing bulk ingestion and you want the GPU free for concurrent chat traffic.

The embedder screen is the one labeled "LLM Provider" at the top in the current AnythingLLM UI — this is cosmetic and has caused confusion before. If `qwen3-embed-compact:latest` or similar appears in the chat-LLM slot, that's wrong; embedding-only models cannot serve `/v1/chat/completions` and the workspace will fail silently.

### 5.4 Vector database

LanceDB (AnythingLLM's default) is sufficient. LanceDB is also the only backend that supports the in-app reranker, which the workspace configuration below relies on. LanceDB collections are **dimension-locked at creation time** — a collection created with a 768-dim embedder cannot be queried with 1024-dim vectors, which matters for §5.7.

### 5.5 Workspace tuning (per-workspace API call)

Two workspaces are configured: `vcf-reference` (the VCF documentation corpus, ~5,000 chunked documents) and `sdg-documentation` (Keycloak 26.3.3 reference content; see §10). The defaults are too conservative for both. The whole set of tuning knobs can be flipped in a single API call per workspace:

```bash
export ALLM_URL="http://localhost:3001"
export ALLM_KEY="<workspace API key>"
export WS="vcf-reference"

curl -X POST "$ALLM_URL/api/v1/workspace/$WS/update" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "similarityThreshold": 0.0,
    "topN": 10,
    "chatMode": "query",
    "vectorSearchMode": "rerank",
    "openAiTemp": 0.3,
    "chatModel": "rag-qwen25:latest",
    "queryRefusalResponse": "Not in the provided VCF documents.",
    "openAiPrompt": "You are a technical reference assistant for VMware Cloud Foundation (VCF). Answer questions using ONLY the content retrieved from the attached VCF documentation. If the answer is not in the retrieved context, say so — do not fall back on general VMware knowledge. Cite which document each claim comes from when possible."
  }'
```

Key choices:

- `similarityThreshold: 0.0` — no minimum similarity filter. With strict chunking over structured VCF rule documents, the correct chunk sometimes scores below the default threshold (0.20) and gets dropped silently.
- `topN: 10` — retrieve ten snippets per query instead of the default four. `rag-qwen25`'s 32K context absorbs ten chunks without issue, and more snippets reduce the chance that a relevant rule block is missed.
- `chatMode: "query"` — force refusal when retrieval comes up empty rather than letting the model fall back on general knowledge.
- `vectorSearchMode: "rerank"` — second-stage reranker. Required for the "accuracy optimized" mode AnythingLLM exposes in the UI.
- `chatModel: "rag-qwen25:latest"` — pin the workspace to the RAG-tuned Modelfile even if the global default is later migrated to a Qwen 3.6 variant.
- `queryRefusalResponse` — sentinel string used as the smoke-test. Asking "what's the capital of France?" should return this, not "Paris." If Paris comes back, query mode isn't actually in effect (see §8.5).

Verify with `GET /api/v1/workspace/$WS` and confirm the fields match.

The `sdg-documentation` workspace uses the same call with two changes: `WS=sdg-documentation`, `topN: 12` (slightly more snippets to compensate for Keycloak's denser narrative-style chunks), and an `openAiPrompt` describing SDG/Keycloak content rather than VCF content. The `queryRefusalResponse` is correspondingly adjusted to "Not in the provided SDG documents."

### 5.6 Pinning as an alternative to RAG

For small authoritative documents (< 100 KB) like the VCF pattern files themselves, the most reliable path is to **pin** them in the workspace document picker instead of relying on retrieval. Pinning injects the full text of the document into every query. With `rag-qwen25` at 32K context, two or three pinned pattern docs fit comfortably, and the model never misses a rule because a chunk got dropped. Reserve chunked RAG for the corpus you can't afford to fit in-context — the full TechDocs scrape below.

### 5.7 Re-embedding after an embedder change

If the workspace was ever populated under a different embedder (`nomic-embed-text` at 768 dimensions, or any other model) the current Qwen3 1024-dimension embeddings will not match what's stored in LanceDB. This fails **silently** — retrieval returns low-quality results rather than an explicit dimension-mismatch error. Symptoms: chunks that obviously should match the query don't come back; the reranker has nothing useful to rerank; VCF queries return `queryRefusalResponse` for questions that are clearly answered in the corpus.

The fix is to purge and re-embed:

```bash
# 1. In the UI: Workspace → Documents → select all → Remove from workspace → Delete from system.
#    This deletes the LanceDB collection for that workspace, which is the only way
#    to change its vector dimension.

# 2. Re-upload the corpus
WS=vcf-reference
for f in /opt/vcf-ingest/out/*.md; do
  curl -s -X POST "$ALLM_URL/api/v1/document/upload" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -F "file=@$f" \
    -F "addToWorkspaces=$WS"
done

# 3. Trigger embedding — this creates a new LanceDB collection at 1024 dim
curl -s -X POST "$ALLM_URL/api/v1/workspace/$WS/update-embeddings" \
  -H "Authorization: Bearer $ALLM_KEY"
```

For bulk re-embedding of a large corpus, switch the embedder to `qwen3-embed-cpu:latest` first so the GPU stays free for chat traffic. Switch back to `qwen3-embedding:0.6b` for normal query-time embedding after ingestion completes.

---

## 6. MCP Server Architecture

### 6.1 Three custom MCP containers

The MCP layer exposes the local AnythingLLM workspaces (and a live Broadcom TechDocs scraper) to remote agentic clients like OpenCode. Each MCP runs as its own Docker container, listening on its own port, with its own scope and tool naming. This is a deliberate departure from the original `npx mcp-anythingllm` setup: stdio-launched MCPs rebuild on every client reconnect, share a process tree with the editor, and cannot be tuned independently. Containerized SSE servers live independently of the client and are easy to instrument.

| Container | Port | Backing data | Purpose |
|---|---|---|---|
| `anythingllm-search-mcp` | 3002 | AnythingLLM `vcf-reference` workspace | RAG over the VCF documentation corpus |
| `broadcom-techdocs-mcp`  | 3003 | Live `techdocs.broadcom.com` | On-demand fetch of pages not yet in the local corpus |
| `sdg-docs-mcp`           | 3004 | AnythingLLM `sdg-documentation` workspace | RAG over Keycloak 26.3.3 (and future SDG infra docs) |

All three run on the same host as AnythingLLM (`192.168.6.110` in this deployment) and connect to it over the Docker bridge or via `host.docker.internal`. They listen on `0.0.0.0` so OpenCode running on a different machine can reach them over SSE.

### 6.2 Tool routing and selection

MCP tool selection is **description-driven, not name-driven**. When OpenCode connects to an MCP server, it receives a manifest of tool names and docstrings; the agent (Qwen 3.6 in this case) picks tools based on how well each docstring matches the user's question. There is no router middleware — the docstrings *are* the routing logic.

Two practical implications:

**Distinct tool function names per server.** Originally both VCF and SDG MCPs used the same function names (cloned from a single template), causing the agent to call whichever server happened to come first alphabetically — sometimes serving VMware questions out of the Keycloak workspace. The fix was to rename functions explicitly: `search_vcf_docs` / `list_vcf_documents` for VCF, `search_sdg_docs` / `list_sdg_documents` for SDG.

**Asymmetric, opinionated docstrings.** Each tool's docstring should encode three things: what content it covers, when to prefer it over alternatives, and when *not* to use it (with a pointer to the right tool). For VCF specifically:

```python
@mcp.tool()
def search_vcf_docs(query: str) -> str:
    """
    Query the comprehensive VMware Cloud Foundation (VCF) documentation
    corpus. This is a full scrape of techdocs.broadcom.com's VCF
    section plus custom-authored design rule documents.

    PREFER THIS TOOL for ANY question about VCF, NSX, vSphere, vSAN,
    or VMware Cloud Foundation operations. The corpus is comprehensive
    and indexed — it should be your first and default choice for VMware
    questions, not a fallback.

    USE THIS for:
      - VCF deployment, configuration, or operations questions
      - NSX networking: T0/T1 gateways, edge nodes, transport zones,
        TEP configuration, BGP, segments, distributed firewall
      - vSphere features, vSAN, VCF lifecycle, SDDC Manager
      - VCF design rules and validation IDs (e.g. VCF-NET-014, VCF-IP-015)

    DO NOT USE for:
      - Content published AFTER the last corpus scrape — for release notes
        or KB articles, use broadcom-live instead.
      - Questions about Keycloak or other SDG infrastructure — use sdg-docs.

    The answer is strictly grounded in the indexed corpus. If the topic
    isn't covered, the response will say so explicitly rather than
    hallucinate.
    """
```

The "DO NOT USE for X — use Y instead" sentence is the one that actually fixes overlapping-scope routing. The reciprocal exclusion in `broadcom-live`'s and `sdg-docs`'s docstrings completes the routing contract.

### 6.3 Container build pattern

All three MCPs share a Dockerfile pattern. Source lives at `/opt/anythingllm-mcp/`, `/opt/broadcom-techdocs-mcp/`, and `/opt/sdg-mcp/` respectively. Each directory contains:

```
docker-compose.yml
Dockerfile
requirements.txt
server.py            # the MCP tool definitions
.env                 # ANYTHINGLLM_API_KEY (mode 600)
```

`/opt/anythingllm-mcp/server.py` (excerpt — full source omitted; the SDG MCP is structurally identical with renamed functions and the `sdg-documentation` workspace target):

```python
"""
Read-only MCP server wrapping AnythingLLM's workspace chat API.
Runs in HTTP/SSE mode on port 3002 for remote consumption by OpenCode.
"""
import os, re
import httpx
from mcp.server.fastmcp import FastMCP

API_KEY   = os.environ["ANYTHINGLLM_API_KEY"]
BASE_URL  = os.environ.get("ANYTHINGLLM_BASE_URL", "http://host.docker.internal:3001")
WORKSPACE = os.environ.get("ANYTHINGLLM_WORKSPACE", "vcf-reference")

mcp = FastMCP("vcf-reference", host="0.0.0.0", port=3002)

@mcp.tool()
def search_vcf_docs(query: str) -> str:
    """<routing-aware docstring as shown in §6.2>"""
    with httpx.Client(timeout=180.0) as client:
        r = client.post(
            f"{BASE_URL}/api/v1/workspace/{WORKSPACE}/chat",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"message": query, "mode": "query"},
        )
        r.raise_for_status()
        data = r.json()
        answer = strip_thinking(data.get("textResponse", "(no response)"))
        sources = data.get("sources", [])
        if sources:
            items = sorted({format_source(s) for s in sources})
            return f"{answer}\n\nSources:\n" + "\n".join(f"- {t}" for t in items)
        return answer

if __name__ == "__main__":
    mcp.run(transport="sse")
```

Build, deploy, and verify:

```bash
cd /opt/anythingllm-mcp
sudo docker compose up -d --build
sudo docker logs anythingllm-search-mcp --tail 20      # expect: Uvicorn running on 0.0.0.0:3002
timeout 5 curl -sN -H "Accept: text/event-stream" http://192.168.6.110:3002/sse | head -5
# expect: event: endpoint  +  data: /messages/?session_id=...
```

A common failure mode after editing: stacked `@mcp.tool()` decorators (paste artifact) produce a "Tool already exists" warning at startup. The tool still works but the duplicate registration is a code-cleanliness issue worth fixing — `grep -c '@mcp.tool()' server.py` should match the number of tool functions defined.

### 6.4 Citation formatting helper

The original tool returned source identifiers as raw cache filenames like `raw-keycloak-26-3-87dc9f0d-a02b-4077-804d-0ac828f8e91e.json`, which is unusable. The improved citations build a human-readable string from the document's metadata fields:

```python
def format_source(meta: dict) -> str:
    """Build a human-readable source string from a document's metadata."""
    chunk_src = meta.get("chunkSource", "")
    doc_src   = meta.get("docSource", "")
    if doc_src and chunk_src:
        try:
            parts = doc_src.rstrip("/").split("/")
            guide = parts[-2] if parts[-1] in ("index.html", "") else parts[-1].replace(".html", "")
            return f"{chunk_src} \u2192 {guide}  ({doc_src})"
        except Exception:
            return f"{chunk_src} ({doc_src})"
    if chunk_src:
        return chunk_src
    return meta.get("title") or meta.get("filename") or "untitled"
```

After this change, citations look like:

```
Sources:
- keycloak-26.3.3 → server_admin  (https://www.keycloak.org/docs/26.3.3/server_admin/index.html)
- vcf-9.0 → nsx-edge-design  (https://techdocs.broadcom.com/.../nsx-edge-design.html)
```

For VCF documents whose `docSource` field is the literal string `"a text file uploaded by the user."` (a quirk of file-upload ingestion versus URL-fetch ingestion), `chunkSource` alone is used as the fallback. The actual source URL for those documents lives inside the document body's YAML front-matter — see §9.3 for how the auto-updater discovers it.

### 6.5 OpenCode client configuration

OpenCode's `config.json` references the three MCPs as remote SSE endpoints:

```json
{
  "mcp": {
    "vcf-reference": {
      "type": "remote",
      "url": "http://192.168.6.110:3002/sse",
      "timeout": 120000
    },
    "broadcom-live": {
      "type": "remote",
      "url": "http://192.168.6.110:3003/sse",
      "timeout": 120000
    },
    "sdg-docs": {
      "type": "remote",
      "url": "http://192.168.6.110:3004/sse",
      "timeout": 120000
    }
  }
}
```

A connection problem ("SSE error: Unable to connect") almost always means one of three things, in order of frequency: (1) the container isn't running because of a Python parse error introduced during a docstring edit, (2) `docker compose up -d --build` rebuilt the image but reused the old container without picking up new volume mounts (`docker compose down && up -d --build` forces a clean recreate), (3) a host firewall rule blocking the new port.

### 6.6 External MCP servers (SearXNG, Tavily, Context7)

Three additional MCPs are configured outside the Docker-container pattern:

- **`searxng`** — local container on port 8888 (see config below); meta-search across DuckDuckGo, Brave, Wikipedia, etc. Free, self-hosted, privacy-preserving.
- **`tavily`** — hosted commercial search API with LLM-friendly result cleaning. API key required.
- **`context7`** — hosted, surfaces current library documentation (npm/pip/crates/etc.) inline. API key required.

These are launched via stdio rather than as containers because they're upstream-maintained and don't benefit from the local-container pattern. The OpenCode config has them alongside the remote servers above:

```json
"searxng": { "type": "local", "command": ["npx", "-y", "mcp-searxng"], "environment": { "SEARXNG_URL": "http://192.168.6.109:8888" } },
"tavily":  { "type": "local", "command": ["npx", "-y", "tavily-mcp"],  "environment": { "TAVILY_API_KEY": "<key>" } },
"context7":{ "type": "local", "command": ["npx", "-y", "@upstash/context7-mcp"] }
```

SearXNG itself runs as `/opt/searxng/docker-compose.yml`:

```yaml
services:
  searxng:
    image: searxng/searxng:latest
    container_name: searxng
    restart: unless-stopped
    ports:
      - "8888:8080"
    volumes:
      - /opt/searxng/settings.yml:/etc/searxng/settings.yml:ro
    environment:
      - BASE_URL=http://localhost:8888/
      - INSTANCE_NAME=homelab-searxng
```

with a minimal `settings.yml` enabling JSON output:

```yaml
use_default_settings: true
search:
  formats: [html, json]
server:
  secret_key: "<random 64-char hex>"
  limiter: false
```

---

## 7. VCF Documentation Scraping & Ingestion

> The pipeline below describes the **initial bulk scrape** that populates the workspace. For ongoing refresh against the upstream site, see §9 (the auto-updater service that runs periodically and only re-embeds changed pages).

### 7.1 Source reality

Broadcom's TechDocs site (`techdocs.broadcom.com`) publishes VCF documentation as HTML plus per-section/per-document PDF exports. **There is no official markdown export.** Knowledge base articles live on a separate domain (`knowledge.broadcom.com`) with a different structure. Both need separate ingestion paths.

Of the two source formats, HTML → markdown produces cleaner RAG chunks than PDF extraction. The auto-generated PDFs bring along headers/footers and frequently split tables awkwardly across page boundaries.

### 7.2 Scraping pipeline

```text
┌─────────────────────┐
│ sitemap.xml crawl   │  scrape each product's sitemap,
│ (filter vcf/*)      │  filter to VCF-relevant paths
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ HTML fetch          │  httpx async, rate-limited to 2 req/sec
│ + retry / backoff   │  User-Agent identifies the crawler
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ trafilatura         │  main-content extraction; strips nav,
│ + html-to-markdown  │  cookie banners, related-article footers
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Markdown normalize  │  stable headings, front-matter with
│ + front-matter      │  source URL, fetch date, title
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ AnythingLLM upload  │  POST /api/v1/document/upload-link
│ + embed             │  then /workspace/$WS/update-embeddings
└─────────────────────┘
```

### 7.3 Crawler sketch

`/opt/vcf-ingest/scrape.py` — the critical decisions, not the boilerplate:

```python
import httpx, trafilatura, re, time, pathlib, yaml
from urllib.parse import urlparse

ROOT = "https://techdocs.broadcom.com"
SITEMAP = f"{ROOT}/sitemap.xml"
VCF_PATH_RE = re.compile(r"/us/en/vmware-cis/vcf/")
OUT_DIR = pathlib.Path("/opt/vcf-ingest/out")
OUT_DIR.mkdir(parents=True, exist_ok=True)

UA = "homelab-vcf-ingest/1.0 (personal research; contact: admin@example.local)"

def urls_from_sitemap() -> list[str]:
    r = httpx.get(SITEMAP, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    locs = re.findall(r"<loc>([^<]+)</loc>", r.text)
    return [u for u in locs if VCF_PATH_RE.search(u)]

def fetch_and_convert(url: str) -> tuple[str, str]:
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=30,
                  follow_redirects=True)
    r.raise_for_status()
    # trafilatura keeps headings, strips chrome
    md = trafilatura.extract(
        r.text,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        favor_precision=True,
    ) or ""
    # Pull a title for front-matter
    m = re.search(r"<title>([^<]+)</title>", r.text)
    title = m.group(1).strip() if m else urlparse(url).path
    return title, md

def write_doc(url: str, title: str, body: str) -> pathlib.Path:
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-",
                  urlparse(url).path.strip("/")).strip("-")
    front = yaml.safe_dump({
        "source_url": url,
        "title": title,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, sort_keys=False).strip()
    doc = f"---\n{front}\n---\n\n# {title}\n\n{body}\n"
    p = OUT_DIR / f"{slug}.md"
    p.write_text(doc, encoding="utf-8")
    return p

def main():
    urls = urls_from_sitemap()
    print(f"Found {len(urls)} VCF URLs")
    for i, url in enumerate(urls, 1):
        try:
            title, md = fetch_and_convert(url)
            if len(md) < 200:
                print(f"[{i}] SKIP thin: {url}")
                continue
            write_doc(url, title, md)
            print(f"[{i}] OK: {url}")
        except Exception as e:
            print(f"[{i}] FAIL: {url}  ({e})")
        time.sleep(0.5)  # 2 req/sec

if __name__ == "__main__":
    main()
```

### 7.4 Respecting source terms

Before the first run:

1. Fetch and read `https://techdocs.broadcom.com/robots.txt`.
2. Confirm the use case (internal/personal RAG) is covered by Broadcom's terms of use. Redistribution is a separate question and not something this pipeline supports.
3. Identify the crawler in the User-Agent header with contact info so any rate-limit complaint has somewhere to land.

### 7.5 Ingestion into AnythingLLM

Once `/opt/vcf-ingest/out/` is populated, the markdown files are uploaded to the `vcf-reference` workspace:

```bash
WS=vcf-reference
ALLM_URL=http://localhost:3001
ALLM_KEY=<key>

for f in /opt/vcf-ingest/out/*.md; do
  curl -s -X POST "$ALLM_URL/api/v1/document/upload" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -F "file=@$f" \
    -F "addToWorkspaces=$WS"
done

# Trigger embeddings
curl -s -X POST "$ALLM_URL/api/v1/workspace/$WS/update-embeddings" \
  -H "Authorization: Bearer $ALLM_KEY"
```

After this completes, the chunk-size configuration from §8 applies to the embeddings.

---

## 8. The Chunk-Size Fix

### 8.1 Symptoms that indicated a chunking problem

During RAG evaluation against the `VCF-NETWORKING-PATTERNS.md` pattern document, the model produced summaries that were "plausible and consistent with VCF reference designs" but **overgeneralized across distinct rule blocks**. Specifically:

- It claimed non-overlapping subnet requirements applied to all five traffic types, when the source rule (`VCF-IP-005`) scoped that constraint more narrowly.
- It smoothed over separate validation clauses that the source kept distinct, producing a single merged statement where two existed.

Both failure modes traced back to chunk boundaries cutting across rule blocks. The pattern documents encode each rule as a `- id: VCF-*` YAML-like block with a `rule:` and a paired `validation:` clause. Each block runs 400–900 characters. With AnythingLLM's default text splitter at **1000 characters, 200 overlap**, the splitter would regularly slice a rule in half — stranding the `validation:` block in a neighbouring chunk with no link back to the `rule:` it belonged to.

When retrieval grabbed the fragment containing the `rule:` header but not its matching `validation:`, the model filled in the gap from general VCF knowledge. That's the drift.

### 8.2 Why this isn't a per-workspace knob

AnythingLLM exposes chunk size and overlap only as a **global** system setting, not per workspace. The knob lives under **Settings → Text Splitter & Chunking** in the UI, and — unlike the workspace-level settings in §5.5 — there is no stable documented API endpoint for it. Two paths:

1. Change the values in the UI (applies to all future embeddings system-wide).
2. Edit the SQLite DB directly. The settings table is `system_settings`, labels `TextSplitterChunkSize` and `TextSplitterChunkOverlap`. On Docker with a persistent volume: `/app/server/storage/anythingllm.db` inside the container.

Either way, **existing embeddings are not regenerated automatically**. Already-ingested documents must be deleted and re-embedded after the chunk size changes.

### 8.3 The fix

New chunking parameters for the VCF corpus:

| Parameter | Default | New value |
|---|---|---|
| `TextSplitterChunkSize` | 1000 | **2500** |
| `TextSplitterChunkOverlap` | 200 | **500** |

2500 characters comfortably holds a complete `VCF-*` rule block (rule + validation + any nested list formatting) in a single chunk. The 500-character overlap ensures that when a block does straddle a boundary, the entire block is repeated in the next chunk rather than being sliced.

### 8.4 Procedure

1. In AnythingLLM UI: Settings → Text Splitter & Chunking → set chunk size `2500`, overlap `500` → Save.
2. In the workspace document picker, select all VCF documents → Remove from workspace → Delete from system.
3. Re-upload the documents (via the `for f in ...` loop in §7.5).
4. Trigger re-embedding:
   ```bash
   curl -X POST "$ALLM_URL/api/v1/workspace/vcf-reference/update-embeddings" \
     -H "Authorization: Bearer $ALLM_KEY"
   ```
5. Regression test: ask the same question that produced the overgeneralization before. If the response now correctly scopes the non-overlap requirement to the specific traffic types named in the source rule, the fix held.

### 8.5 Known AnythingLLM gotcha: chatMode silent failure

Separately from chunk size, issue #3503 on the AnythingLLM GitHub tracks a bug where a `chatMode` change made via the API is recorded in the database but the chat handler keeps using the old mode. The smoke test is: ask something unambiguously not in the docs — "what's the capital of France?" — and confirm the workspace returns the `queryRefusalResponse` ("Not in the provided VCF documents.") rather than "Paris." If "Paris" comes back, toggle `chatMode` once in the UI and the subsequent API updates start sticking.

### 8.6 Remaining retrieval limitation

AnythingLLM does not currently support **semantic** or **structure-aware** splitting (no SentenceSplitter, no markdown-heading-aware splitter). The open feature request has been on the tracker since 2024. For the VCF pattern files, character-based chunking at 2500/500 is good enough — the rule blocks are regular in size. For less-structured corpora (KB articles, blog-style posts), expect the chunk-size fix to help less, and consider pinning small authoritative documents (§5.6) as the fallback.

---

## 9. VCF Documentation Auto-Updater

### 9.1 Goal and design constraints

The VCF documentation corpus drifts. Broadcom publishes new VCF 9.0 pages, edits older ones, deprecates and removes others. A one-time scrape goes stale fast. The auto-updater is a Docker-deployed service that runs on a cron schedule (default: 03:00 Tuesday and Friday) and keeps the `vcf-reference` workspace in sync against the upstream site, surgically — only re-embedding pages that actually changed.

Three constraints shaped the design:

1. **Be polite to Broadcom.** Rate-limited HTTP, descriptive User-Agent, conditional requests with `If-Modified-Since` / `If-None-Match` headers so a no-change page is a single round-trip.
2. **Detect change, don't blindly re-embed.** Embedding is the GPU-expensive step; re-embedding 5,000 documents nightly when 5 changed wastes hours of compute. Hash the fetched body and only ingest when the hash actually differs from what's stored.
3. **Make destructive operations safe by default.** A bug in URL discovery could delete the entire corpus on a single bad run. Ship with `DRY_RUN=true` as default, hard caps on per-run deletion and update counts, and a 3-run grace period before any URL is considered truly removed.

### 9.2 Architecture

The service lives at `/opt/vcf-doc-updater/` and is a single Docker container running cron internally:

```text
                 ┌──────────────────────────────────────┐
                 │  vcf-doc-updater (cron-driven)       │
                 └──────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌──────────┐    ┌──────────┐    ┌──────────┐
        │ discover │    │  fetch   │    │  ingest  │
        └────┬─────┘    └────┬─────┘    └────┬─────┘
             │               │               │
             ▼               ▼               ▼
   workspace seed list  HEAD / cond.GET   AnythingLLM API:
   + one-hop crawl      compare hash      delete old + upload
                                          new + reindex
             │               │               │
             └───────────────┼───────────────┘
                             ▼
                  ┌────────────────────┐
                  │   state.sqlite     │
                  │ ─────────────────  │
                  │ url, location,     │
                  │ lastmod, etag,     │
                  │ hash, last_seen    │
                  └────────────────────┘
```

Project layout:

```
/opt/vcf-doc-updater/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── entrypoint.sh           # writes crontab from $SCHEDULE, runs cron -f
├── discover.py             # workspace + crawl URL discovery
├── fetcher.py              # HEAD-then-conditional-GET with hash compare
├── html_to_md.py           # main-content extraction + YAML front-matter
├── ingester.py             # AnythingLLM upload/remove/reindex client
├── updater.py              # orchestrator: discover → fetch → ingest
├── .env                    # ANYTHINGLLM_API_KEY (mode 600)
└── state/
    └── state.sqlite        # persistent state, mounted from host
```

### 9.3 Discovery strategy: workspace seeds + one-hop crawl

The obvious approach — walk the `techdocs.broadcom.com` sitemap — does not work. Broadcom's public sitemap (`/sitemap.xml` with 25 sub-sitemaps) covers the legacy CA Technologies catalog but excludes VMware acquisition content entirely. Sitemap-driven discovery returns 0 VCF URLs. The auto-updater supports sitemap mode for completeness, but the **default and recommended** mode is `workspace+crawl`:

1. **Seed list** comes from the existing AnythingLLM corpus. Each document's source URL is extracted from the YAML front-matter at the top of its `pageContent` field — specifically the `source_url:` line, which the original scrape pipeline embedded. This is read directly from the on-disk JSON cache files at `/opt/anythingllm/storage/documents/custom-documents/` (mounted read-only into the updater container at `/allm-storage`). Reading from disk avoids API endpoint shape dependencies and is dramatically faster than 5,000 API calls.

   The top-level `docSource` field is unreliable for these documents — for content uploaded as files (rather than URL-fetched), AnythingLLM stores `"a text file uploaded by the user."` there. The real URL is inside the document's body front-matter:
   ```yaml
   ---
   title: "VMware Cloud Foundation 9.0"
   source_url: https://techdocs.broadcom.com/us/en/vmware-cis/vcf/...
   scraped_at: 2026-04-23T01:54:11+00:00
   ---
   ```

2. **One-hop link expansion** crawls each seed URL, extracts in-scope `<a href>` links, and adds any new ones to the discovery set. This catches newly-published VCF pages that aren't yet in the corpus. The crawl is the most expensive part of a run (~80 minutes for 5,000 seeds at 1 second per request), so for routine refresh you can run pure `workspace` mode (no crawl) twice a week and full `workspace+crawl` weekly.

A `CRAWL_MAX_SEEDS` env var caps how many seeds get crawled per run, useful for testing or for spreading the cost across multiple runs.

### 9.4 Change detection and ingestion

For each discovered URL, `fetcher.py` performs:

1. Look up stored state (`last_modified`, `etag`, `content_hash`) in SQLite.
2. If never seen, do a full GET.
3. Otherwise, send a conditional GET with `If-Modified-Since` and `If-None-Match`. A `304 Not Modified` response means skip.
4. On 200 OK, hash the body and compare against the stored hash. Headers may have changed cosmetically (cache-busting, load balancer headers) without the content actually changing — the hash is the source of truth.

Only documents that pass step 4 with a different hash proceed to ingestion. Ingestion is delete-then-add: remove the previous version of the document from the AnythingLLM workspace, upload the new converted markdown, and trigger embedding. The HTML-to-markdown conversion (`html_to_md.py`) preserves the YAML front-matter format so subsequent updater runs can re-extract `source_url` from re-ingested content.

### 9.5 Safety controls

The `docker-compose.yml` ships with conservative defaults:

```yaml
environment:
  - DRY_RUN=true                  # log what would change; don't change anything
  - MAX_DELETES_PER_RUN=50        # abort the run if more than 50 documents would be deleted
  - MAX_UPDATES_PER_RUN=500       # cap updates per run; deferred excess goes to the next run
  - DELETE_GRACE_RUNS=3           # require a URL to be absent 3 runs in a row before deletion
  - SCHEDULE=0 3 * * 2,5          # 03:00 Tuesday and Friday
  - REQUEST_DELAY_S=1.0           # 1 second between HTTP requests to broadcom.com
```

Each run is recorded in a `runs` table in `state.sqlite` with counters and an `aborted` flag. A weekly check is one query:

```bash
sudo sqlite3 /opt/vcf-doc-updater/state/state.sqlite \
  "SELECT id, datetime(started_at,'unixepoch','localtime') AS started,
          discovered, unchanged, updated, new, deleted, errors, aborted, abort_reason
   FROM runs ORDER BY id DESC LIMIT 10;"
```

A healthy steady-state pattern looks like `discovered≈4900, unchanged≈4895, updated≈3, new≈1, deleted≈0, errors=0, aborted=0`. Deviations worth investigating: `discovered` drops by more than 10% between runs (sitemap or crawl problem), `aborted=1` (a safety cap fired — `abort_reason` says which), or sustained `errors > 5%` (rate limiting from upstream).

### 9.6 Operational pattern

Recommended rollout:

1. **Initial dry-runs** (`DRY_RUN=true`, `DISCOVERY_MODE=workspace+crawl`). Run manually two or three times a few hours apart. The first run discovers all 4,900-odd seed URLs and "would upload" up to `MAX_UPDATES_PER_RUN=500` of them; subsequent runs gradually fingerprint the rest.
2. **Live mode** (`DRY_RUN=false`). Once dry-runs look healthy in the SQLite summary, flip the flag and `docker compose up -d`. Cron drives it from there.
3. **Mixed cadence over time.** A reasonable steady state is `workspace` mode twice weekly (catches updates to known pages, skips the 80-minute crawl) and `workspace+crawl` once weekly (catches new pages too). Both are achievable with two cron lines.

To trigger an immediate run instead of waiting for the schedule:

```bash
sudo docker exec vcf-doc-updater python /app/updater.py
```

Or with a small crawl cap for testing:

```bash
sudo docker exec -e CRAWL_MAX_SEEDS=20 vcf-doc-updater python /app/updater.py
```

The same architectural pattern applies to the SDG (Keycloak) workspace, though Keycloak's update cadence is slow enough that manual quarterly refreshes are usually sufficient.

---

## 10. SDG (Keycloak) Workspace

### 10.1 Purpose

The `sdg-documentation` workspace holds reference content for the self-hosted infrastructure stack supporting a Space Engineers community: currently Keycloak 26.3.3, with additional tools (reverse proxy, identity sync, monitoring) to be added as the stack documents itself. This is operationally separate from VCF — the two workspaces have different content, different update cadences, and different MCP-server descriptions to keep agentic routing clean.

### 10.2 Ingestion source

Markdown files for Keycloak 26.3.3 live at `/opt/keycloak-ingest/output/keycloak-26.3.3/`, organized by guide (`server_admin`, `authorization_services`, `securing_apps`, `release_notes`, etc.). Each file has YAML front-matter identifying its source:

```yaml
---
source: keycloak-26.3.3
product: keycloak
version: 26.3.3
guide: authorization_services
source_url: https://www.keycloak.org/docs/26.3.3/authorization_services/index.html
---
```

The `source` and `guide` fields drive the `format_source()` citation rendering documented in §6.4 — a search result cites `keycloak-26.3.3 → authorization_services (https://...)` rather than a UUID filename.

### 10.3 Workspace configuration

Same API call as §5.5 for the VCF workspace, with three substitutions:

```bash
WS=sdg-documentation
# topN: 12 (slightly higher than VCF's 10 to compensate for Keycloak's denser narrative chunks)
# queryRefusalResponse: "Not in the provided SDG documents."
# openAiPrompt: describes Keycloak / SDG infra scope, explicitly excludes VMware/VCF content.
```

The `openAiPrompt` should mention that each document has a `source: <tool-name>` front-matter field so the answering model can name the originating tool when citing.

### 10.4 MCP exposure

The `sdg-docs-mcp` container at port 3004 (see §6.1) wraps this workspace. Its tool docstrings explicitly exclude VMware/VCF questions and direct the agent to `vcf-reference` for those. This asymmetric exclusion — both `vcf-reference` and `sdg-docs` describe the *other* as where to go for off-topic questions — is what makes routing reliable when both tools are simultaneously available to OpenCode.

---

## Appendix A — Directory Layout on the Host

```text
/opt/
├── anythingllm/
│   ├── docker-compose.yml
│   ├── .env
│   └── storage/                    # LanceDB collections, document JSON cache, workspaces
│       └── documents/
│           └── custom-documents/   # ~5,000 ingested .json files (JSON cache)
├── searxng/
│   ├── docker-compose.yml
│   └── settings.yml
├── nothink-proxy/
│   ├── app.py                      # FastAPI proxy (ports 11435 RAG / 11436 agentic)
│   └── venv/
├── anythingllm-mcp/                # VCF MCP container — exposes vcf-reference workspace
│   ├── docker-compose.yml          #   (port 3002, search_vcf_docs / list_vcf_documents)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py
│   └── .env
├── broadcom-techdocs-mcp/          # Live techdocs.broadcom.com fetcher (port 3003)
│   ├── docker-compose.yml
│   ├── server.py
│   └── cache/
├── sdg-mcp/                        # SDG/Keycloak MCP container (port 3004,
│   ├── docker-compose.yml          #   search_sdg_docs / list_sdg_documents)
│   ├── server.py
│   └── .env
├── vcf-doc-updater/                # Auto-refresh service (§9)
│   ├── docker-compose.yml
│   ├── Dockerfile
│   ├── entrypoint.sh
│   ├── discover.py
│   ├── fetcher.py
│   ├── html_to_md.py
│   ├── ingester.py
│   ├── updater.py
│   ├── .env
│   ├── state/state.sqlite
│   └── logs/updater.log
├── vcf-scrape/                     # Original VCF ingestion source files
│   └── vcf-docs-md/                #   (organized by section: design, deployment, etc.)
└── keycloak-ingest/                # Keycloak ingestion source files
    └── output/keycloak-26.3.3/
```

## Appendix B — Service Port Map

| Port | Service | Bind | Purpose |
|---|---|---|---|
| 11434 | Ollama | 0.0.0.0 (LAN) | Direct generation + embedding API |
| 11435 | No-Think Proxy (RAG) | 0.0.0.0 (LAN) | Strips `<think>`, keepalives — for AnythingLLM |
| 11436 | No-Think Proxy (agentic) | 0.0.0.0 (LAN) | Keepalive only — for OpenCode (preserves thinking) |
| 3001  | AnythingLLM | 0.0.0.0 (LAN) | RAG UI + REST API |
| 3002  | `anythingllm-search-mcp` | 0.0.0.0 (LAN) | VCF MCP (SSE) — wraps `vcf-reference` workspace |
| 3003  | `broadcom-techdocs-mcp`  | 0.0.0.0 (LAN) | Live Broadcom TechDocs fetch (SSE) |
| 3004  | `sdg-docs-mcp`           | 0.0.0.0 (LAN) | SDG MCP (SSE) — wraps `sdg-documentation` workspace |
| 8888  | SearXNG | 0.0.0.0 (LAN) | Self-hosted meta-search |

## Appendix C — Smoke Tests

Run in sequence after any rebuild:

```bash
# 1. All three GPUs present and linked at Gen3 x16
nvidia-smi --query-gpu=index,name,pcie.link.gen.current,pcie.link.width.current --format=csv

# 2. Ollama responds; expected chat + embed models are present
curl -s http://localhost:11434/api/tags | jq '.models[] | .name' \
  | grep -E 'rag-qwen25|coder-36|coder-long|qwen3-embedding|qwen3.6:35b-a3b'

# 3. No-think proxy strips think blocks and keeps SSE alive
curl -N -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"rag-qwen25:latest","stream":true,"messages":[{"role":"user","content":"one word reply: ready"}]}' \
  | head -n 20

# 4. Embedding dimension check — probes the embedder actually in use for VCF
curl -s http://localhost:11434/api/embeddings \
  -d '{"model":"qwen3-embed-compact:latest","prompt":"dimension probe"}' \
  | jq '.embedding | length'
# Expect: 1024

# 5. AnythingLLM API reachable
curl -s -H "Authorization: Bearer $ALLM_KEY" \
  http://localhost:3001/api/v1/workspaces | jq '.workspaces[] | .slug'

# 6. VCF workspace refusal path
curl -s -X POST http://localhost:3001/api/v1/workspace/vcf-reference/chat \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the capital of France?","mode":"query"}' \
  | jq '.textResponse'
# Expect: "Not in the provided VCF documents."

# 7. VCF workspace positive retrieval — confirms dimension match and chunking
curl -s -X POST http://localhost:3001/api/v1/workspace/vcf-reference/chat \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"What prefix lengths are valid for edge uplink subnets?","mode":"query"}' \
  | jq '.textResponse'
# Expect: reference to /29 or /30 and citation of VCF-NET-014 / VCF-IP-015.
# If this returns the refusal string instead, re-embedding is needed (§5.7).

# 8. MCP containers are listening
for port in 3002 3003 3004; do
  printf "Port $port: "
  timeout 3 curl -sN -H "Accept: text/event-stream" \
    "http://localhost:$port/sse" 2>&1 | head -1
done
# Expect each: a line starting with "event: endpoint"
# A "Connection refused" indicates the container isn't running.

# 9. MCP tool routing — VCF query should fire vcf-reference, not sdg-docs
# (Run from OpenCode and watch the trace; not scriptable from curl alone.)
# Sample query: "What does VCF-IP-015 require for edge uplink subnet prefixes?"
# Expected tool call: vcf-reference_search_vcf_docs (NOT sdg-docs_*)

# 10. Auto-updater health
sudo sqlite3 /opt/vcf-doc-updater/state/state.sqlite \
  "SELECT id, datetime(started_at,'unixepoch','localtime') AS started,
          discovered, unchanged, updated, new, deleted, errors, aborted
   FROM runs ORDER BY id DESC LIMIT 3;"
# Expect: discovered ~4900, errors=0, aborted=0 in healthy steady state.
```

---

## Contributing and feedback

Issues and pull requests welcome. This document describes a single author's verified configuration and the lessons learned while building it — corrections and additions from other setups (different hardware, different software versions, different workloads) are especially useful. Please include:

- Your hardware and software versions in the issue title or PR description
- A specific reproduction path for any behavior that contradicts what's documented here
- Whether you confirmed the behavior on a clean install or against an existing modified environment

## License

This document is published under [Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/). You are free to share and adapt the material, including for commercial purposes, provided you give appropriate credit and distribute derivative work under the same license.

Code snippets embedded in this document are provided under the [MIT License](https://opensource.org/licenses/MIT) to reduce friction for copy-paste into proprietary or differently-licensed projects.

## Acknowledgements

- **Ollama** — the runtime that makes local multi-GPU serving painless
- **AnythingLLM (Mintplex Labs)** — the RAG frontend, configurable down to the settings that matter
- **Qwen team (Alibaba Cloud)** — open model weights and published sampling guidance that this configuration follows
- **Broadcom / VMware** — source of the VCF documentation corpus ingested in §7
- **LanceDB** — vector store doing the actual retrieval work underneath AnythingLLM
