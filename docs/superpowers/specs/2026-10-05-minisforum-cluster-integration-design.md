# Minisforum nodes ↔ GPU cluster integration

**Status:** draft for review · **Date:** 2026-10-05
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
| D2 | Phase 1 | Move embed/rerank off the V620s — placement decided by a **benchmark** of Proxmox-host LXC vs Minisforum VM (user: "test ... and compare") |
| D3 | Worker routing | Per-node router alias, per-backend semaphore, health check, GPU fallback **on by default** |
| D4 | opencode | Pinned `worker-1..3` subagents, Qwen3.8 coordinates |
| D5 | Redteam fast tier | Retarget to CPU workers only after a measured comparison |
| D6 | Delegate pool | Phase 3, **gated on** and **kept out of** the local-delegate Phase-1 A/B |
| D7 | GPUs in the nodes | Later one-node spike, not part of this build |

**Success criteria:**
- V620 VRAM holds only the chat model; the chat slot count is a profile setting.
- RAG query latency stays within 20% of today's V620-served baseline (threshold proposed here,
  not user-set), and embeddings are
  numerically equivalent (cosine ≥ 0.9998, the existing run-to-run noise floor).
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

**Worker runtime facts:**
- Config: `-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 -rtr
  --spec-type mtp:n_max=2`, Qwen3.6 sampling (`temp 0.7, top-p 0.8, top-k 20`),
  `enable_thinking: false`.
- **`-cram 256 -ctx-ckpt 8` are mandatory.** ik's defaults (8 GiB RAM prompt cache, 32 context
  checkpoints × ~63 MiB) OOM-killed the server twice in a 24 GB guest. With both set, RSS held at
  20.8-21.4 GB with ~2.4 GB free.
- MTP n=2: +44% generation at ~90% acceptance. ik prefill is ~2× mainline.
- Turn cost (2K in / 300 out): ~23 s at 8K context depth, ~30 s at 32K.

**Prompt-cache affinity is mandatory.** CPU prefill runs at ~125-300 tokens/s, so a job whose
turns move between backends re-prefills its whole context every turn.

**V620 VRAM held by non-chat services today** (`scripts/51-lxc-amd.sh`): embedder
Qwen3-Embedding-0.6B Q8_0 at 5.24 GB on GPU0 (2 slots × 16384), reranker bge-reranker-v2-m3 at
1.5 GB on GPU1.

## 4. Phase 1 — free the V620s

### 1a. Placement benchmark (measurement only)

Three placements serve the same models with the same slot contract (per-slot ctx 16384, embed
`--parallel 2 --ctx-size 32768`, `--pooling last`; rerank ctx 8192 × 4):

| Placement | Host |
|---|---|
| P0 (baseline) | today's units on LXC 151 (V620) |
| P1 | new CPU-only LXC on the Proxmox host (192.168.6.175), via the LXC deployment pattern |
| P2 | a VM on one Minisforum (re-using llmbench01), reached through a temporary CRS309 accept |

Metrics, each driven **through the router** (LXC 153) so network cost is included:

1. RAG query embed latency — p50/p95 over 200 queries of real chunk sizes.
2. Rerank latency — p50/p95 for a 20-candidate rerank.
3. Bulk ingest throughput — chunks/s on one 50-document tranche, measured from batch 2 onward
   (batch 1 carries a one-time LanceDB cost).
4. Embedding parity against P0 — `scripts/tools/embed-dump.py`, cosine on full 1024-dim vectors.
5. Router → backend RTT (curl, not `/dev/tcp`).

**Decision rule:** pick the placement that meets the latency criterion (§1) at the lowest
operational risk. If P2 wins on performance, it ships only with a P1 standby the router falls
back to (§2), so P2 wins only by a margin that justifies running both.

### 1b. Cutover

- Deploy the chosen placement as a permanent unit (`llamacpp-embed`, `llamacpp-rerank`) with
  unchanged aliases (`qwen3-embed`, `bge-rerank`).
- Repoint the router via `EMBED_URL` / `RERANK_URL` (env only — the router already treats these
  as independent upstreams).
- Disable the embed and rerank units on LXC 151.
- The three-way context alignment (LXC 151 per-slot ctx = router `MAX_EMBED_INPUT_TOKENS` =
  AnythingLLM `EMBEDDING_MODEL_MAX_CHUNK_LENGTH` = 16384) is preserved by construction, because
  the slot contract does not change.
- **Chat slot count becomes a profile setting.** Add `LLAMA_PARALLEL` / `LLAMA_CTX` to each chat
  profile in `swap-chat-model.sh`. The default stays 1 × 256K, and a `-3slot` profile variant
  provides 3 × 128K. Redteam mode's unload-embed/rerank step becomes a no-op.
- Router `CHAT_CONCURRENCY` follows the active profile's slot count.

**Rollback:** restore the `EMBED_URL` / `RERANK_URL` env and re-enable the LXC 151 units.

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
  the fallback header. Run `embed-dump.py` parity after cutover. Confirm the RAG answer with a
  citation in AnythingLLM.

## 8. Rollout order

| Step | Change | Rollback |
|---|---|---|
| 1a | Placement benchmark | none (measurement) |
| 1b | Embed/rerank cutover; slot count as profile setting | env revert + re-enable LXC 151 units |
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
| Embedding throughput on CPU too low for bulk re-ingest | 1a measures it; slot count can rise to 4 on a CPU host with RAM to spare |
| Alias table drift between repo and live router | Aliases edited in repo, deployed by script (lesson from the nothink alias loss) |
| Quality claim rests on one 15-task suite | Workers get narrow, checkable tasks; the coordinator verifies |
