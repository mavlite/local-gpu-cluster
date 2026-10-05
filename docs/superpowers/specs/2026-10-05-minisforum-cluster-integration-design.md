# Minisforum nodes ↔ GPU cluster integration

**Status:** rev 2 — Phase 1 rewritten from measurement · **Date:** 2026-10-05
**Path:** architectural (brainstorming → spec → plan)

## 1. Intent

**Outcome:** the three Minisforum hosts (hyp01-03, the VCF lab's management cluster) add
capacity to the GPU cluster so that more work runs in parallel, the V620s spend their VRAM on the
main model, and nothing user-facing breaks when the lab goes down.

**What the user asked for (2026-10-04/05):** use the nodes alongside the GPU cluster — as
subagent workers coordinated by the GPU model, possibly as standalone GPU nodes. All four
candidate uses were judged plausible: opencode subagents, Claude Code delegation, freeing the
V620s, and redteam per-role agents.

**User decisions:**

| # | Decision | Choice |
|---|---|---|
| D1 | Architecture | **Approach A** — router learns named backends; phased |
| D2 | Phase 1 | ~~Move embed/rerank off the V620s~~ → **fix them in place** (rev 2). The placement benchmark the user asked for ("test ... and compare") showed the V620 wins 6-13× and on parity; see §4 |
| D3 | Worker routing | Per-node router alias, per-backend semaphore, health check, GPU fallback **on by default** |
| D4 | opencode | Pinned `worker-1..3` subagents, Qwen3.8 coordinates |
| D5 | Redteam fast tier | Retarget to CPU workers only after a measured comparison |
| D6 | Delegate pool | Phase 3, **gated on** and **kept out of** the local-delegate Phase-1 A/B |
| D7 | GPUs in the nodes | Later one-node spike, not part of this build |

**Success criteria:**
- Embed/rerank stay on the V620s with their VRAM cut to what the repo specifies; the chat slot
  count is a profile setting.
- RAG query-embed latency does not regress (it improves 50× if flash attention is adopted, §4),
  and stored-vector compatibility is either exact or deliberately re-established by re-embedding.
- Each worker alias serves requests from its own node with a warm prompt cache across turns.
- Powering off any or all workers changes no client-visible behaviour beyond speed (fallback).
- N-1 holds on the VCF cluster with workers running: survivors' active memory ≤ 50% of DRAM.

## 2. Governing principle

**The lab only hosts work whose loss degrades the cluster, never breaks it.** The Minisforums are
a VCF lab that lost vcsa and cascaded HA restarts as recently as 2026-10-04 (tier-drive failure),
and the lab network (172.16.10.0/24) reaches the cluster LAN (192.168.6.0/24) only through narrow
CRS309 accepts. Anything on the critical path of RAG/chat either stays off the lab or gains a
fallback that does not touch the lab.

## 3. Evidence this design rests on (measured 2026-10-03..05)

**Worker model — agentic eval** (agent-eval-v3 with hidden-test grading, same protocol as the
Qwen3.8 baseline; 24 GB VM, 16 vCPU, Ryzen 9 7945HX, ik_llama.cpp unless noted):

| Model | Hidden set | Visible (all sets) | Mean turns | Min/task | Verdict |
|---|---|---|---|---|---|
| **Qwen3.6-35B-A3B UD-IQ4_XS + ik MTP** | **9/15 (60%)** | 28/28 | 7.6 | 0.6 | **worker model** |
| Gemma 4 26B-A4B q4_0 | 9/15 (60%) | 20/21 | 11.4 | 8.0 | ~12× slower (600-900 tokens/turn); RSS 23.2 GB at the VM limit |
| gpt-oss-20b MXFP4 (low effort) | 5/15 (33%), ik and mainline | 19/28 | 8.4-9.4 | 0.9 | malformed harmony output in ~1/3 of runs on both runtimes |
| LFM2-24B-A2B Q4_K_M | 0/15, ik and mainline | 6-8/28 | 8-14 | 0.7-2.1 | not an agent |
| *reference: Qwen3.8-27B on V620s* | *9/15 (60%)* | | *~5* | | *coordinator* |

Caveat: one 15-task hidden suite. Qwen3.6 matches Qwen3.8 on pass rate, not on turn efficiency.

**Settings A/B** (30 hidden runs per arm, ±9 pts sd): baseline 15/30, `--min-p 0` 16/30,
presence penalty 1.5 + `--repeat-last-n -1` 13/30, thinking ON (coding preset, `preserve_thinking`,
8K budget, reasoning echoed) 12/30, verify-before-finish system prompt 16/30 at 2× wall-clock.
**None beats baseline beyond noise.** Thinking scored 0/6 on `chunk_intent` vs 3-5/6 elsewhere.
Two of five hidden tasks fail 0/6 in every arm (one unstated contract each), so ~60% is this
suite's ceiling for the model. No loops: 1 turn-cap in 180 runs, 0 tool errors.

**Worker runtime facts:**
- Config: `-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 -rtr
  --spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs`, Qwen3.6 sampling (`temp 0.7, top-p 0.8,
  top-k 20, --min-p 0`), `enable_thinking: false`, no presence penalty.
- ik defaults verified on build 5f89bfc that the config overrides: `--min-p` **0.1** (Qwen
  specifies 0) and MTP `p_min` **0.75** (truncated drafts to 1.68 of 2 tokens). The MTP change
  decodes 26.6-28.6 t/s vs 22.5 (speculative decoding is verified, so quality is unaffected);
  `-muge` measured 0%.
- **`-cram 256 -ctx-ckpt 8` are mandatory.** ik's defaults (8 GiB RAM prompt cache, 32 context
  checkpoints × ~63 MiB) OOM-killed the server twice in a 24 GB guest. With both set, RSS held at
  20.8-21.4 GB with ~2.4 GB free.
- MTP n=2: +44% generation at ~90% acceptance. ik prefill is ~2× mainline.
- Turn cost (2K in / 300 out): ~23 s at 8K context depth, ~30 s at 32K.

**Prompt-cache affinity is mandatory.** CPU prefill runs at ~125-300 tokens/s, so a job whose
turns move between backends re-prefills its whole context every turn.

**V620 VRAM held by non-chat services:** the live embedder had drifted from
`scripts/51-lxc-amd.sh` (4 slots / 65536 live vs 2 / 32768 in the repo) and held ~9.8 GB on GPU0;
reconciled to the repo on 2026-10-05 (GPU0 31.8 → 24.9 GB used). Reranker bge-reranker-v2-m3
holds 1.5 GB on GPU1.

## 4. Phase 1 — fix the V620 embedder in place (rev 2)

### What the placement benchmark showed (2026-10-05)

Same model files (SHA-verified copies of LXC 151's), same llama.cpp b11026, same flags; harness
`bench_embed.py` with distinct texts per rep (repeated texts hit the prompt cache and read ~10 ms):

| Metric | P0 V620 | P1 Proxmox-host CPU LXC | P2 Minisforum VM CPU |
|---|---|---|---|
| 4K / 16K-token chunk | **2.1 / 14.9 s** | 12 / 108 s | 8.0 / 68 s |
| Bulk ingest (4 workers, 1K chunks) | **4.0-4.5 chunks/s** | 0.38-0.47 | 0.63-0.71 |
| Rerank 20 × 300 tokens | **0.59 s** | 7.2-7.6 s | 4.4 s |
| Vectors vs stored | **bit-exact** | cos 0.9993-0.9998 | cos 0.9993-0.9998 |

The CPU is compute-bound (flash attention and 12 threads did not help), and its vectors shift. The
CPU's one apparent win — fresh-query latency 39-59 ms vs ~605 ms — was a fault in the GPU unit:

| LXC 151 embed config (fresh texts, ≥10-token queries) | Query p50 | 16K chunk | Ingest | GPU0 VRAM | Parity |
|---|---|---|---|---|---|
| V0 live: 4 slots, ctx 65536, FA off | 603 ms | 14.9 s | 4.0/s | 31.8 GB | exact |
| V1 repo: 2 slots, ctx 32768, FA off (**live since 2026-10-05**) | 604 ms | ≈V0 | ≈V0 | **24.9 GB** | exact |
| V2: V1 + `--flash-attn on` | **12 ms** | **4.8 s** | **7.9/s** | 24.9 GB | cos 0.99954 |

The ~600 ms floor is the non-flash-attention path, not slot count (`kv_unified` was already false).

### 1a. Done — repo config applied (V1)

The live unit now matches the repo (per-slot 16384 kept, so the three-way context alignment
holds); backup at LXC 151 `/root/llamacpp-embed.service.bak-20261005`. Rollback: restore the
backup, `daemon-reload`, restart.

### 1b. Adopt flash attention (decision pending)

V2 is better on every axis except parity. Two ways to take it:

- **Re-embed** (recommended): flip `--flash-attn on` in the unit **and in `51-lxc-amd.sh`**, then
  re-embed every AnythingLLM workspace so stored and query vectors share one space (~21k chunks
  at ~7.9 chunks/s ≈ 45 min of embedder time; the re-ingest procedure through AnythingLLM still
  needs to be written and timed).
- **Accept drift:** flip the flag only. Same-text vectors move by cos ~0.0005; retrieval impact
  is unmeasured and would need a top-k overlap test on real queries before trusting it.

The reranker (`--flash-attn off`, 0.59 s per 20-doc rerank) likely has the same floor; test it
the same way (scores and ranking order before and after) before changing it.

### 1c. Chat slots as a profile setting

- Add `LLAMA_PARALLEL` / `LLAMA_CTX` to each chat profile in `swap-chat-model.sh`. The default
  stays 1 × 256K; a `-3slot` variant provides 3 × 128K if the freed ~7 GB plus existing headroom
  fits it — measure VRAM at 3 slots before shipping.
- Router `CHAT_CONCURRENCY` follows the active profile's slot count.

## 5. Phase 2 — CPU worker aliases

### 2a. Router: named backends

Replace the single `V620_URL` with a backend table loaded from env/YAML (`yaml.safe_load` only):

```yaml
backends:
  v620:         {url: "http://192.168.6.151:8080", concurrency_from_profile: true}
  cpu-1:        {url: "http://172.16.10.205:8090", concurrency: 1, fallback: v620}
  cpu-2:        {url: "http://172.16.10.206:8090", concurrency: 1, fallback: v620}
  cpu-3:        {url: "http://172.16.10.207:8090", concurrency: 1, fallback: v620}
aliases:
  qwen3.6-cpu-1: cpu-1
  qwen3.6-cpu-2: cpu-2
  qwen3.6-cpu-3: cpu-3
default_backend: v620
```

- **Resolution:** the request's `model` maps through `aliases` to a backend. An unknown model goes
  to `default_backend`, exactly as today. Existing profile aliases (qwen3.8, coder, ...) keep
  their swap-webhook behaviour on `v620`.
- **Admission:** one `asyncio.Semaphore` per backend, replacing the global `chat_sem` for
  non-default backends, so a worker never queues behind the GPU chat or vice versa.
- **Health:** a background task probes each backend's `/health` every 15 s. Two consecutive
  failures mark it down; one success marks it up.
- **Fallback:** a request for a down backend with `fallback` set is served by the fallback
  backend under the fallback's alias, and the response carries `x-router-fallback: <backend>`.
  Without `fallback` it returns 503 `{"error": "backend cpu-2 unavailable"}`. Fallback is not
  retried mid-stream: a worker that dies mid-response fails that request, and the next request
  falls back.
- **Auth:** the router sends a per-worker API key read from its existing secrets env file. Keys
  never appear in argv or logs.

### 2b. Worker VMs

- One per host, `llmbench01..03` re-provisioned as `llmworker01..03`.
- 24 GB fully reserved, `sched.mem.enableTiering = FALSE`, 16 vCPU at low CPU shares.
- HA restart priority disabled.
- DRS VM-host should-rules pin one worker to each host (DRS stays PartiallyAutomated, the
  user's setting).
- Runtime: ik_llama.cpp `llama-server` as a systemd unit with `Restart=on-failure`, the §3 config
  plus `-cram 256 -ctx-ckpt 8`, `-np 1`, bound to the VM's VLAN-10 address.
- The API key comes from a root-only file, never argv. Unverified: whether ik takes
  `--api-key-file` or a `LLAMA_API_KEY` env var. Check `--help` first; if neither works, put a
  key-checking reverse proxy on the worker.
- Apt timers disabled and needrestart set to list-only (it restarted units mid-benchmark on
  2026-10-03).
- Network: CRS309 accepts from the router LXC only, to the workers' `:8090` and nothing else.
- **N-1 check before declaring done:** with workers on, simulate the loss of each host on paper
  from measured active memory. Each survivor must stay ≤ 50% of DRAM active.

### 2c. Clients

- **opencode:** the user-level config gains provider models `qwen3.6-cpu-1..3` and agents
  `worker-1..3`, each pinned to one alias with a narrow, checkable-task prompt. Qwen3.8 (the
  primary agent) delegates to them.
- **Probe first:** whether opencode runs `task` subagents concurrently is undocumented. Measure
  it with three timed sleep-and-write tasks. If they serialise, parallelism comes from separate
  opencode sessions, and the spec records that limit.
- **Redteam:** recon/source/fuzzer stay on the Qwen3-4B fast tier (LXC 151:8085) until a
  comparison on a recorded engagement shows CPU Qwen3.6's quality gain is worth its per-turn
  latency. That comparison is a follow-up, not part of this build.

**Rollback:** power off the workers (aliases fall back to `v620`), then remove the backend
entries.

## 6. Phase 3 — delegate backend pool (gated)

Starts only if the local-delegate Phase-1 A/B returns **go**
(`docs/superpowers/specs/2026-10-02-local-delegate-mcp-design.md` §1). CPU backends stay out of
the delegate until then, so the A/B measures one variable.

- `GpuLease` (one file lock) becomes `BackendPool`: config entries
  `{name, alias, tier: strong|bulk}`. `acquire(tier)` leases one free backend for the job's
  lifetime, giving stickiness.
- `submit_task(..., tier="strong"|"bulk")`. Bulk jobs prefer CPU backends and use the GPU only if
  allowed by a flag.
- The ledger records the backend per job, so CPU- and GPU-delegated work can be compared.
- On a **no-go** result, phase 3 is dropped. Phases 1-2 do not depend on it.

## 7. Testing

- **Router:** pytest unit tests (repo convention) — alias resolution incl. unknown → default,
  per-backend semaphore isolation, health state transitions (2-fail down, 1-success up), fallback
  header and 503 path, and that the existing profile/swap behaviour is unchanged. Fakes assert on
  recorded calls, not elapsed time.
- **Deployment scripts:** `bash -n` plus shellcheck.
- **Live checks per step:** curl through the router to each alias. Kill one worker and confirm
  the fallback header. Run `embed-dump.py` parity after any embed-unit change (1b). Confirm the RAG answer with a
  citation in AnythingLLM.

## 8. Rollout order

| Step | Change | Rollback |
|---|---|---|
| 1a | ✅ Embed unit reconciled to repo (2 slots / 32768) — done 2026-10-05 | restore unit backup |
| 1b | Embed `--flash-attn on` + corpus re-embed (pending decision); rerank FA test | flip flag back (+ re-embed again if 1b re-embedded) |
| 1c | Chat slot count as a profile setting | profile default unchanged |
| 2a | Router backend table (no workers yet → behaviour unchanged) | revert router deploy |
| 2b | Worker VMs + firewall + keys + N-1 check | power off workers |
| 2c | opencode worker agents + concurrency probe | remove agent entries |
| 3 | Delegate `BackendPool` (only after A/B go) | config lists GPU only |

## 9. Out of scope / later

- **3060 per node (spike):** check whether the BD795i SE BIOS offers x8/x4/x4 bifurcation, on
  one host in maintenance mode. That would free the x4 idle under the vSAN drive for an OCuLink
  3060 (DirectPath; all devices report `passthruCapable=True`). If it exists, the worker becomes
  a hybrid (experts on CPU, attention on GPU) behind the same alias.
- Splitting one model across nodes over 10G (llama.cpp RPC): rejected — the network is far
  below what tensor/pipeline parallel needs.
- Moving the redteam fast tier (see 2c).

## 10. Risks

| Risk | Mitigation |
|---|---|
| Worker memory bandwidth slows the VCF management plane (CPU shares do not govern bandwidth) | Watch vSAN latency during 2b; low shares; workers are expendable |
| Lab outage mid-job | Fallback on next request; jobs are retryable by design |
| Flash attention adopted without re-embedding mixes two vector spaces (cos ~0.9995 apart) | 1b's default is re-embed; "accept drift" requires a top-k overlap test first |
| Live units drift from the repo again (the 4-slot embed drift went unnoticed) | Deploy unit changes from `51-lxc-amd.sh`, then diff live vs repo after each phase |
| Alias table drift between repo and live router | Aliases edited in repo, deployed by script (lesson from the nothink alias loss) |
| Quality claim rests on one 15-task suite | Workers get narrow, checkable tasks; the coordinator verifies |
