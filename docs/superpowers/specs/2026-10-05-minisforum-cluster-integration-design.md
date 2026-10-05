# Minisforum nodes ↔ GPU cluster integration

**Status:** rev 3 — measure-first gate; Phase 0 shipped; Phase 2 is requirements, not a build
**Date:** 2026-10-05 · **Path:** architectural (brainstorming → spec → plan)

> **Rev 3 changes.** Rev 2 drew four adversarial reviews: ops/VCF safety, router architecture,
> security, and evidence/cost. Together they raised 59 findings (5 critical, 29 high). This
> revision makes four changes:
> 1. It records what has shipped as **Phase 0**.
> 2. It puts a **pre-registered measurement gate** ahead of all worker infrastructure (Phase 1).
> 3. It moves the worker build (Phase 2) behind that gate, as **requirements** drawn from the reviews.
> 4. It corrects several overstated claims.
>
> §11 gives the disposition of every critical and high finding. Since rev 2 the user also decided
> that the VCF lab runs in **Minimal mode** day to day (D8). That changes the coexistence problem:
> the management VMs are off, so the workers no longer compete with them for DRAM.

## 1. Intent

**Outcome.** The three Minisforum hosts (hyp01-03) add parallel agent capacity to the GPU cluster.
Qwen3.8 on the V620s coordinates. Nothing user-facing breaks when the lab is down.

**User decisions:**

| # | Decision | Choice |
|---|---|---|
| D1 | Architecture | Router learns named backends; phased (Approach A) |
| D2 | Embed/rerank | Keep them on the V620s and fix them in place. A placement benchmark showed the GPU wins 6-13× and keeps parity (see §3) |
| D3 | Worker routing | One router alias per node, per-backend admission, health check, GPU fallback (rev 3 tightens the semantics: §6) |
| D4 | opencode | `worker-1..3` subagents pinned to aliases; Qwen3.8 coordinates |
| D5 | Redteam fast tier | Move only after a measured comparison |
| D6 | Delegate pool | Phase 3, gated (§7) |
| D7 | GPUs in the nodes | A later one-node spike |
| D8 | **Lab operating mode** | **Minimal day to day** (`Start-VCFLab.ps1 -Mode Minimal`): hosts plus LLM workers, VCF stack off. Full only when VCF work needs it. Memory tiering stays on, DRS stays PartiallyAutomated, nsxa's reservation stays removed |
| D9 | **Build order** | **Measure first** (rev 3): Phase 2 is built only if the §5 gate passes |

**Success criteria (tied to the goal: more parallel work, finished sooner):**
- **Gate metric:** for K realistic agent tasks run in parallel, wall-clock to finish all K is lower
  than the best GPU-only configuration, by the margin set in §5. Quality is not worse by more than
  one task. Coordinator turn latency does not degrade by more than the §5 bound.
- **Resilience:** if a worker or the whole lab disappears mid-session, no GPU profile swap happens,
  the coordinator keeps working, and an in-flight worker request fails within 15 s with the router's
  error envelope.
- **Safety:** no lab host reaches tier-pressure territory because of a worker (§6.1). The
  management plane is never loaded while it is running.

## 2. Governing principles

1. **The lab only hosts work whose loss degrades the cluster, never breaks it.**
2. **Never load the cluster hosting its own control plane** (lesson recorded 2026-10-04). In
   Minimal mode the control plane is off, so the workers are the only tenants. Full mode and
   workers are mutually exclusive (§6.1).
3. **Measure before building.** Every phase gate is decided by numbers collected before the phase.
4. **Repo is the source of truth for live units.** Every live change is merged and deployable from
   `main`. Drift between the host checkout and `main` reverts fixes on the next deploy.

## 3. Evidence (measured 2026-10-03..05; claims narrowed per the evidence review)

**Worker model.** The model is Qwen3.6-35B-A3B UD-IQ4_XS on ik_llama.cpp with MTP. Other models
tested and rejected:
- Gemma 4: same pass rate but ~12× slower.
- gpt-oss-20b: malformed harmony output in ~1/3 of runs, on both runtimes.
- LFM2-24B: not usable as an agent.

**What the agentic suite can and cannot say.** The hidden set has 5 tasks.
- Two fail in every arm, because each depends on an unstated contract. Only three discriminate.
- On those three, Qwen3.6, Gemma and the earlier Qwen3.8 figure are indistinguishable
  (Fisher p ≈ 1; 95% CI ≈ 36-80%).
- **"Qwen3.6 matches Qwen3.8" is therefore not established.** The Qwen3.8 reference was measured
  on a different build and config.
- The shipped config (`--min-p 0`) scored **16/30**.
- **"Thinking hurts" is not supported:** p ≈ 0.6, and that arm changed sampling and the harness
  together.
- All tasks are single-file tasks under 5K tokens. Real delegated work is **unmeasured**, and the
  §5 gate exists to measure it.

**Worker runtime (shipped config).**
- Flags:
  `-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 -rtr --spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs --min-p 0`,
  plus Qwen3.6 non-thinking sampling.
- `-cram 256 -ctx-ckpt 8` are **mandatory**: ik's defaults OOM a 24 GB guest.
- ik defaults the config overrides (verified on build 5f89bfc): min-p 0.1, MTP p_min 0.75.
- **In Minimal mode: 30.1-30.4 t/s generation, 226-232 t/s prefill** (6 greedy prompts, all 3 nodes
  within 1%). With the VCF stack running it was 26.0 / 227 for the old setting: contention costs
  ~15-20%.
- Turn cost (2K in / 300 out) is ~19 s in Minimal mode.

**Throughput comparison (evidence review, back-of-envelope; the gate replaces it with measurement).**
Assume a delegated job: 12K-token start, ~15 turns, ending at ~45K.
- **CPU worker:** ~9-10 min with perfect prompt caching. Each forced re-prefill at 40K adds 4-5 min.
- **One GPU stream** (Qwen3.8: 562 t/s prefill, ~47 t/s gen): ~2.5 min.

Three CPU workers ≈ 0.75-1.2 GPU streams of a weaker model. Their real advantage is that they are
**additive**: they do not slow the coordinator. That advantage is unquantified.

**Embed/rerank (D2).**
- **The V620 wins clearly.** Bulk ingest 4-4.5 chunks/s vs 0.4-0.7 on CPU; rerank 0.59 s vs
  4.4-7.6 s.
- **CPU breaks parity.** Its vectors shift (cos 0.9993-0.9998).
- **The ~600 ms GPU query floor was flash-attention-off**, not slot count. With FA on: 12 ms
  per query, a 16K chunk 14.9 s → 4.8 s, ingest 4.0 → 7.9/s.
- **The reranker stays FA-off.** FA gains 3% but reorders 29/30 rankings, and it has no floor to
  remove.

## 4. Phase 0 — shipped 2026-10-05 (all merged into `main`; push pending)

| Item | Commit / where | Verified |
|---|---|---|
| Embed unit reconciled to repo: 2 slots / 32768 (−7 GB VRAM) | live unit, backup on LXC 151 | parity exact before FA |
| Embedder `--flash-attn on` (`EMBED_FLASH_ATTN`) | `a7a307f` | 12 ms query p50 live |
| **Corpus re-embed**: offline LanceDB vector rewrite, 19,891 chunks | `anythingllm_reembed_procedure` (memory) | 1.5 min outage; top-12 overlap 95-97%; self-retrieval 96→99/100; LanceDB 25 GB → 0.1 GB |
| Router: streams hold their admission slot (`hold_slot`, `ClosingStreamingResponse`); capacity follows chat `total_slots`; stream connect timeout | `5d942d7` | e2e: 1 slot → 1 upstream stream, 3 slots → 3; live chat OK |
| Router: embedding budget is per input (fixes the silent 413 detach of large documents since ~10-03) | `5d942d7` | 8×3K batch 413 → 200 live |
| VCF lab Minimal/Full modes, LLM workers owned by the scripts, 180-day license guard (fails closed) | `6661191` (branch `feat/9-1-1-right-sized-capacity`) | 82 offline checks; live Stop 14 min / Minimal start 1 min |

**Push pending.** Pushing to GitHub needs the user's own command. Until the host checkout pulls the
merged `main`, re-running `51-lxc-amd.sh` or `53-lxc-router.sh` from the host would revert the
embedder FA and the router fixes (principle 4).

## 5. Phase 1 — the measurement gate (no router change, no new VMs)

**1a. opencode concurrency probe (first, because everything downstream assumes it).** Do three
`task` subagents run in parallel? Measure with three timed sleep-and-write tasks. If they serialise,
parallel capacity must come from separate opencode sessions or the delegate. Record that limit
before anything else.

**1b. GPU arm A: three Qwen3.8 slots.** Use the existing proven mechanism, the redteam-mode drop-in
(3 × 128K) entered by hand for the benchmark. No new profile is needed (see §11 H5). Pin opencode's
context limit for this arm to ≤128K: the default 200K pin overflows a 128K slot.

Measure:
- per-stream and aggregate gen t/s;
- coordinator p50 turn latency with three concurrent agent streams;
- VRAM headroom.

**1c. CPU arm B: 1-slot GPU coordinator plus three CPU workers.** Start llama-server by hand on
`llmbench01-03` with the shipped config. Point opencode's provider entries **directly** at their
URLs, for the benchmark only. No router change and no persistent service.

The workstation reaches VLAN 10. Benchmark keys are throwaway and live in a root-only file on each
VM.

**1d. Fan-out benchmark.**
- K ≈ 9 realistic tasks: the Aider Polyglot Python subset on LXC 158, plus 3 recorded real
  delegations.
- Run each arm twice with opencode in the loop.

Metrics:
- wall-clock to finish all K;
- Polyglot/hidden pass rate;
- coordinator p50 turn latency during the fan-out;
- re-prefill count, from `timings.cache_n`;
- worker tokens per job, to validate the §3 estimate.

**Pre-registered decision rule:**
- **Build Phase 2** only if B finishes all K at least **30%** faster than A, loses no more than
  **1** task, and keeps coordinator p50 turn latency within **+50%** of the idle coordinator.
- **Also build it** if A's coordinator latency degrades by more than +100% while B's does not.
  The additive-capacity case.
- **Otherwise** close Phase 2. Ship 3-slot GPU as a selectable profile (§5e) and power the
  workers off.

**1e. If A wins: chat slots as a selectable mode.**
- Today's profiles are identified by `repo:quant` (swap idempotency). A "-3slot" profile cannot
  coexist with that.
- So model it as a **mode**, like redteam mode: a drop-in plus a router-side capacity that follows
  `total_slots` automatically (already shipped).
- When the mode is on, set opencode's context pin to 128K.

## 6. Phase 2 — CPU workers (only if the gate passes): requirements

These are requirements, not a design. The design is written after the gate, against them.

### 6.1 Placement and lab safety
- **Workers run only in Minimal mode.**
  - `Start-VCFLab.ps1 -Mode Full` must **refuse** while workers are running. Today it only warns.
  - `Stop-VCFLab -IncludeHosts` stops the workers first. It already does.
- **vCenter-independent guard** on the Proxmox host. It uses the `watchdog_multi.ps1` pattern,
  talking to the hosts directly. It powers all workers off within one poll when:
  - a host disconnects, or an HA failover occurs;
  - any tier device is not healthy;
  - a host's **free** DRAM drops below the margin.

  Test it by injecting a fake host-down signal before 2b ships.
- **Per-host gate before power-on:** consumed DRAM + 24 GB + overhead ≤ ~75 GB, read live.
- **Watch tier writes:** `BlocksWritten`/day is recorded for 48 h before and after. A rise is a
  rollback trigger, because consumer tier drives are the known failure class.

### 6.2 Network and security (security review, minimum before any worker is reachable)
- **A dedicated worker VLAN**, not VLAN 10 (the VCF management VLAN).
- **Firewall:** router → workers `:8090` only. Worker → LAN is **denied**, verified with curl.
- **Encrypted and authenticated router↔worker channel:** WireGuard, or TLS with the router
  pinning each worker's certificate.
- **Health:** a worker counts as up only after an authenticated identity check. `/health` is
  public on llama-server, so it is not enough.
- **Shutdown order:** remove a worker from the router *before* powering it off. This closes the
  window in which another host could take over its IP.
- **Worker processes:** llama-server runs non-root under systemd hardening. Keys are distributed as
  files and rotation is documented. Whether ik supports `--api-key-file` is verified by running
  it. Prompts are not retained on workers.
- **Agent permissions:** opencode `worker-N` agents get explicit, **deny-by-default** permissions
  (no `bash`/`edit` unless the task type needs them), verified by execution.

### 6.3 Router (architecture + security reviews)
- **Backend objects** own url, key and admission gate. A request can never send the V620 key to a
  worker.
- **Config** is JSON or env, not YAML: PyYAML is absent from the router venv. It is validated
  **fail-closed** at startup:
  - alias collisions are rejected;
  - unknown keys are rejected;
  - a missing key refuses to start.
- **Worker aliases live outside `ALIAS_MAP`** and bypass the profile guard and swap webhook
  entirely.
- **Fallback:**
  - The target is an explicit ALIAS_MAP alias.
  - Fallback happens **only if the active profile already serves it**. Never trigger a swap.
  - The alias's sampling defaults replace the client's.
  - The guard is not re-run.
  - Otherwise the router returns 503 in the router's existing error envelope.
- **Health and failure handling:**
  - No health-gating of `v620`, which would regress swaps and the scheduled restarts.
  - Worker connect timeout ~5 s.
  - A request that fails to connect falls back immediately, rather than waiting for the probe.
  - Recovery needs ≥3 consecutive good probes (hysteresis).
- **Limits:**
  - responses from workers are size-capped;
  - `tool_execution=server` is refused on worker aliases;
  - each client token has its own allowed aliases, so the redteam box gets no workers unless
    granted.
- **No leaks into GPU telemetry.** Worker traffic must not feed `_last_chat_ts`, which drives the
  fan feed-forward and the idle-restart gate.
- **Testability:** logic lives in small importable modules with pytest, following the pattern of
  `stream_admission.py`. `router-app.py` stays untestable by import.

### 6.4 Clients
- opencode `worker-N` entries set explicit sampling and `limit.context` ≤ 65536 − output.
- Each worker alias has exactly one consumer at a time. This is what makes it sticky in practice.
- **Redteam:** stays on the Qwen3-4B fast tier until a recorded-engagement comparison says
  otherwise.

## 7. Phase 3 — delegate pool (deferred)

Gated on all of the following:
1. The local-delegate A/B returns **go**.
2. JobStore gains real concurrency. Today it has one worker thread, so a pool adds no parallelism.
3. The overlay is generated per job, with the leased alias and context limit. Today it is
   hard-coded to `qwen3.8-think` at 200K.
4. **Delegate stage-2 sandboxing.** Lab-originated tool calls must not run as the user on the
   workstation.

Until then, `ask_local` stays GPU-only.

## 8. Testing

- **Pytest:** new router logic goes in importable modules, pytest per §6.3 requirement. The
  repo's suite (`scripts/rag/tests scripts/files/tests`) must stay green.
- **End-to-end:** the router app runs in-process via httpx `ASGITransport` against a fake upstream,
  inside the router venv. Shown 2026-10-05 for admission and embeddings.
- **Gate:** the benchmark harness and raw results are committed with the gate decision.
- **Shell:** `bash -n` and shellcheck on every deploy script.
- **PowerShell:** the vcf-lab stub harness stays green, and new guards are mutation-tested.

## 9. Rollout order

| Step | Change | Rollback |
|---|---|---|
| 0 | ✅ Phase 0 shipped (§4); **push + host `git pull --ff-only`** | revert the merge commits |
| 1a | opencode concurrency probe | none (measurement) |
| 1b-d | Fan-out benchmark, arms A and B; gate decision recorded | none (manual drop-in; servers started by hand) |
| 1e | *If A wins:* 3-slot chat mode + opencode 128K pin | mode off |
| 2 | *If B wins:* write the §6 design, review it, then plan | — |
| 3 | Delegate pool (§7 gates) | — |

## 10. Risks

| Risk | Mitigation |
|---|---|
| Host checkout reverts shipped fixes on the next deploy | Push, then `git pull --ff-only` on the host. The host has only untracked backup files, so the pull is clean |
| Benchmark arms differ in more than one variable | Same K tasks, same opencode version, two runs each, raw timings committed |
| The 3-slot drop-in collides with redteam mode | Enter and exit it with the redteam-mode scripts during the benchmark, so there is one mechanism |
| Workers left running when Full mode starts | §6.1 refusal (Phase 2). Until then the Full start warns |
| Quality on real tasks is unmeasured | The gate measures it on Polyglot and recorded delegations |
| The 30% / +50% thresholds are judgment calls | Pre-registered here. Changing them after the data is in is not allowed |

## 11. Disposition of review findings (critical + high)

| Finding | Disposition |
|---|---|
| Arch C1 — streams bypass `chat_sem` | **Fixed** in Phase 0 (`5d942d7`). Capacity follows slots, so redteam keeps 3 |
| Evidence C1 — Phase 2 built before the measurement that justifies it | **Adopted:** the §5 gate with a pre-registered rule |
| Ops C1 — host failure with workers pinned repeats the HA cascade | **Largely resolved by D8** (Minimal: no management VMs to restart). The §6.1 guard is still required |
| Ops C2 — pinned workers push management memory onto tier drives | **Resolved by D8** plus §6.1 Full-mode refusal and the per-host gate |
| Security F1 — plaintext, unauthenticated worker channel | **Required:** §6.2 tunnel/pinned TLS, identity-checked health, remove before power-off |
| Ops H1 / Evidence H4 — N-1 math wrong | **Superseded:** in Minimal mode N-1 concerns workers only. §6.1 gate uses live free DRAM |
| Ops H2/H3, Arch H1/H2, Sec F10 — fallback swaps profiles, storms, runaway thinking | **Required:** §6.3 fallback rules (never swap, active-profile only, alias defaults, 503 envelope) |
| Ops H4, Arch H4 — black-holed worker visible ≥30 s | **Required:** §6.3 5 s connect, immediate fallback on connect failure |
| Ops H5, Arch M5 — flapping breaks affinity | **Required:** §6.3 hysteresis |
| Ops H6/H7, Evidence H6/H7/H8 — FA default, re-embed procedure/estimate/quality | **Done:** re-embed shipped with backup and retrieval verification (§4) |
| Ops H8, Evidence M3 — parity claims | **Superseded** by the re-embed; parity now measured against stored vectors |
| Ops H9, Arch H5 — 3-slot profile vs opencode 200K pin and profile identity | **Adopted:** §5e mode instead of a profile; 128K pin when on |
| Ops H10 — CPU contention unlisted | **Measured:** 15-20% (§3); removed by Minimal mode |
| Arch H3 — health-gating `v620` regresses swaps | **Required:** §6.3 no gating of `v620` |
| Arch H6 — delegate pool gives no parallelism | **Adopted:** §7 gate 2 |
| Sec F2 — worker agents inherit `bash`/`edit` | **Required:** §6.2 deny-by-default, verified by execution |
| Sec F3/F4 — lab→LAN not narrow; workers on management VLAN | **Required:** §6.2 dedicated VLAN and worker→LAN deny, verified with curl |
| Sec F5 — global key would cross into the lab | **Required:** §6.3 backend objects own keys |
| Sec F6 — delegate pool sends code to the lab, runs lab tool calls | **Adopted:** §7 gate 4 |
| Evidence H1 — "matches Qwen3.8" unsupported | **Corrected** in §3 |
| Evidence H2/H3 — suite ≠ real work; cache affinity untested | **Adopted:** gate uses Polyglot + recorded delegations and counts re-prefills |
| Evidence H5 — success criteria not tied to the goal | **Rewritten** (§1) |

Medium and low findings are carried into the Phase 2 design review as a checklist. The four
review reports are kept with the session record.
