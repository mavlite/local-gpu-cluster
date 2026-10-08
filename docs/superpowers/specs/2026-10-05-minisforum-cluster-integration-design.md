# Minisforum nodes ↔ GPU cluster integration

**Status:** rev 3.1 — measure-first gate (runnable); Phase 0 shipped; Phase 2 is requirements
**Date:** 2026-10-05 · **Path:** architectural (brainstorming → spec → plan)

> **Revision history**
>
> - **Rev 3** answered four adversarial reviews of rev 2: 59 findings, 5 critical.
> - **Rev 3.1** answers two re-reviews of rev 3:
>   - *disposition check:* 18 resolved, 6 claimed but not resolved, 10 reasonable deferrals,
>     16 new issues;
>   - *gate methodology:* 3 blockers, the rev 3 §5 was not runnable.
> - **Rev 3.1 makes three changes:**
>   - It **ships the Full-mode refusal now**, not in Phase 2, so "workers plus full stack" cannot
>     happen (§4).
>   - It replaces §5 with a **runnable** gate. There are two sub-gates on their own harnesses, a
>     variance check first, a pinned environment, and minimum security for the arm-B benchmark.
>   - It corrects the merge claim, the resilience criterion and the contention figures.
> - **§11** gives the disposition of every critical/high finding. **§12** carries every medium/low
>   finding forward as a checklist.

## 1. Intent

**Outcome.** The three Minisforum hosts (hyp01-03) add parallel agent capacity to the GPU cluster.
Qwen3.8 on the V620s coordinates. Nothing user-facing breaks when the lab is down.

**User decisions:**

| # | Decision | Choice |
|---|---|---|
| D1 | Architecture | Router learns named backends; phased |
| D2 | Embed/rerank | Stay on the V620s, fixed in place (§3) |
| D3 | Worker routing | One router alias per node, per-backend admission, health, GPU fallback, under §6.3 rules |
| D4 | opencode | `worker-1..3` subagents pinned to aliases; Qwen3.8 coordinates |
| D5 | Redteam fast tier | Move only after a measured comparison |
| D6 | Delegate pool | Phase 3, gated (§7) |
| D7 | GPUs in the nodes | A later one-node spike |
| D8 | Lab operating mode | **Minimal day to day** (hosts plus, if the gate passes, LLM workers; VCF stack off). Full only for VCF work, and **Full refuses while workers run** (shipped, §4). Tiering stays on, DRS stays PartiallyAutomated, nsxa's reservation stays removed |
| D9 | Build order | **Measure first:** Phase 2 only if the §5 gate passes. **If it fails, the workers stay powered off and Minimal means hosts only** |

**Success criteria (measurable):**
- **Gate:** the §5.7 rule, applied to data collected under the §5.0 pinned environment.
- **Resilience:**
  - If a worker or the whole lab disappears, no GPU profile swap happens and the coordinator
    keeps working.
  - New requests to a dead worker fall back, or fail with the router's error envelope, within one
    probe interval (≤15 s).
  - An in-flight stream to a backend marked down is aborted within one further probe interval
    (§6.3). A dead worker cannot be told apart from a slow CPU prefill by byte-silence alone, so
    the abort is probe-driven, not a read timeout.
- **Lab safety:**
  - On every host with a worker: consumed DRAM + 24 GB + overhead ≤ 75 GB.
  - Tier `BlocksWritten`/day does not rise in the 48 h after workers start.
  - Full mode never runs with a worker on. This is enforced by the scripts.

## 2. Governing principles

1. **The lab only hosts work whose loss degrades the cluster, never breaks it.**
2. **Never load the cluster hosting its own control plane.**
   - In Minimal mode the control plane is off.
   - Full refuses while workers run, so the two never coexist.
   - The one exception is `-WithVCenter`, which runs vcsa (21 GB) beside one worker (24 GB). It is
     accepted because it fits a host's ~94 GB DRAM and nothing else of the control plane runs.
3. **Measure before building.** Every phase gate is decided by numbers collected before the phase,
   under a pinned environment, with the rule committed first.
4. **Repo is the source of truth for live units.** Live changes are merged and deployable from
   `main`. The host checkout must be pulled after every merge.

## 3. Evidence (measured 2026-10-03..05)

**Worker model:** Qwen3.6-35B-A3B UD-IQ4_XS on ik_llama.cpp with MTP. Rejected:
- Gemma 4 (same pass rate, ~12× slower);
- gpt-oss-20b (malformed harmony output on both runtimes);
- LFM2-24B (not usable as an agent).

**Limits of the agentic suite.** The hidden set has 5 tasks.
- Two fail in every arm because each depends on an unstated contract. Three discriminate.
- On those three, Qwen3.6, Gemma and the earlier Qwen3.8 figure are statistically
  indistinguishable (Fisher p ≈ 1).
- **"Qwen3.6 matches Qwen3.8" is not established. "Thinking hurts" is not supported.**
- The shipped config scored 16/30.
- Tasks are single-file and under 5K tokens. Real work is unmeasured, which is why §5 includes a
  Polyglot quality sub-gate.

**Worker runtime (shipped config).**
- Flags:
  `-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 -rtr --spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs --min-p 0`,
  plus non-thinking sampling.
- `-cram 256 -ctx-ckpt 8` are mandatory: ik's defaults OOM a 24 GB guest.
- Old MTP setting (`n_max=2`, default p_min): **22.5 gen / 188 prefill t/s with the VCF stack
  running, 26.0 / 227 in Minimal**. So the stack costs 16% of generation and 21% of prefill.
- Shipped setting in Minimal: **30.1-30.4 / 226-232** (all 3 nodes within 1%).

**Throughput (back-of-envelope; §5 replaces it with measurement).** Assume a delegated job: 12K
start, ~15 turns, ending at ~45K.
- A CPU worker takes ~9-10 min with perfect caching.
- One GPU stream takes ~2.5 min.

So per job the CPU is ~4× slower. Its only case is **additive capacity**: work done without slowing
the coordinator. §5.7 therefore leads with that.

**Embed/rerank:**
- The V620 beats CPU 6-13× and keeps parity.
- The ~600 ms query floor was flash-attention-off.
- The reranker stays FA-off: FA reorders rankings for a 3% gain.

## 4. Phase 0 — shipped 2026-10-05

| Item | Where | Verified |
|---|---|---|
| Embed unit reconciled to repo (2 slots / 32768, −7 GB VRAM) | live; backup on LXC 151 | parity exact against the FA-off store at the time |
| Embedder `--flash-attn on` | `a7a307f`, **merged into local `main`** | 12 ms query p50 live |
| Corpus re-embed (offline LanceDB vector rewrite, 19,891 chunks) | scripts in `scripts/tools/allm-reembed/` | 1.5 min outage; top-12 overlap 95-97%; self-retrieval 96→99/100; 25 GB → 0.1 GB |
| Router: streams hold their admission slot; capacity follows chat `total_slots`; stream connect timeout | `5d942d7`, **merged into local `main`** | e2e: 1 slot → 1 stream, 3 slots → 3; live chat OK; 26 admission tests |
| Router: embedding budget per input (fixes the silent 413 detach since ~10-03) | `5d942d7`, merged | 8×3K batch 413 → 200 live |
| VCF lab Minimal/Full modes; workers owned; 180-day license guard (fails closed) | `6661191`, **branch `feat/9-1-1-right-sized-capacity`, NOT merged** | 82 offline checks; live Stop 14 min / Minimal start 1 min |
| **Full refuses while LLM workers run; `-StopLlmWorkers`** | `61d3d2c`, same branch, NOT merged | 89 checks; refusal mutation-tested |

**Not yet pushed.** Pushing needs the user's command: `main` plus 4 branches. After that, the host
checkout must run `git pull --ff-only`. Until it does, re-running `51-lxc-amd.sh` or
`53-lxc-router.sh` would revert the embedder FA and the router fixes.

The vcf-lab scripts run from a workstation and are not deployed by the host. Their branch also
carries 72 commits of separate VCF-CA work, so merging it is the user's decision.

**Re-embed rollback** expires **2026-10-12**. Until then these exist:
- `lancedb.fa-off`;
- `vector-cache.fa-off`;
- snapshot `tank/anythingllm@pre-reembed-20261005-1155`;
- `/root/anythingllm-pre-reembed-20261005-1155`.

## 5. Phase 1 — the measurement gate (no router change, no persistent service)

The gate has two sub-gates, each **measured on its own harness**:
- a **quality ruler**, Aider Polyglot via aider: per-model, single endpoint;
- a **capacity probe**, opencode coordinator fan-out.

They are never fused into one wall-clock number. aider drives one endpoint and has no subagents;
opencode has no Polyglot grader.

### 5.0 Preconditions (all must hold before any timed run)

1. **Freeze the task sets** and commit them, with hashes, in the **gate commit**.
   - Quality set: the Aider Polyglot Python subset on LXC 158 (`run-polyglot.sh <alias> <run>
     python`).
   - Capacity set: **N ≥ 6 self-contained worker tasks** with machine-checkable `checks`, authored
     now and committed under `docs/superpowers/gate/worker-tasks/`. There are no recorded real
     delegations to replay: the delegate has never completed a real job.
2. **Benchmark-only opencode profile** (`~/.config/opencode/config.bench.json`, never the
   default). It contains:
   - `worker-1..3` provider entries by **IP** (`http://172.16.10.{205,206,207}:8090/v1`, with
     `limit.context = 65536 − output` and explicit non-thinking sampling);
   - a `coordinator` agent that dispatches via `task`.
3. **Security minimums for arm B**, which exposes plaintext workers to the workstation:
   - the worker agents get **deny-by-default permissions**: no `bash` and no `webfetch`, `edit`
     only inside a scratch worktree;
   - each worker VM's firewall accepts `:8090` only from the benchmark client's IP;
   - keys are throwaway, in a root-only file per VM;
   - llama-server is stopped and the keys removed at teardown;
   - the coordinator runs in a scratch clone, not a real repo.
4. **Reachability, verified by running.**
   - From the workstation and from LXC 158, `curl` each worker by IP with an **authenticated
     completion**, not just `/health`.
   - If any worker is unreachable, stop.
5. **Pin the environment and record it in the gate commit:**
   - router SHA and effective `/healthz` `chat_admission`;
   - `RATE_LIMIT_CHAT`, set to 1000/minute for the window (it is 60/minute per IP by default, so
     fan-out would otherwise 429 and read as latency);
   - llama.cpp `b11026`, worker flags verbatim, opencode version, config SHAs;
   - **thinking state for every alias.** Workers are non-thinking only. The coordinator's thinking
     state is fixed and identical in both arms.
6. **Stop the perturbing timers for the window, and restore them in 5.8:**
   - `llamacpp-chat-restart.timer` (04:00/16:00);
   - `redteam-mode-watch`, `redteam-mode-idle.timer` and `redteam-mode-precreate`;
   - the worker VMs' apt timers, with needrestart set to list-only.

   Schedule all runs outside the 03:15 rag-refresh window. **Embed/rerank state must be the same
   in both arms**, since arm A's mechanism stops them (5.4). Record `/healthz` before and after
   each run.
7. **Commit the gate config** — the frozen task sets, the thresholds in 5.7 and the environment
   record — **before** the first timed run. Its commit SHA is quoted with the results. Changing
   anything afterwards voids the gate.

### 5.1 Variance check first (it sizes the experiment)

Run the quality set against arm A's coordinator alias **5× back-to-back**. Compute:
- pass@2 mean and SD;
- wall-clock mean and CV.

If pass@2 SD > ~1 task, or wall-clock CV > 15%, the budget is too small. Then:
- raise runs to ≥5 per arm, and/or
- use the full Polyglot set,

until the 95% CI half-width on each decision metric is smaller than the threshold it is tested
against. Record the resulting N and runs in the gate commit (a pre-registration amendment, made
*before* any cross-arm data exists).

### 5.2 opencode concurrency probe (stop condition)

Three `task` subagents run three timed sleep-and-write tasks; compare wall-clock against the sum of
the durations.

**If they serialise**, there is no other fan-out mechanism today. The delegate has one worker thread
and its overlay is hard-coded to the GPU model (§7). In that case **Phase 1 halts**, and that is
recorded as the gate outcome: close Phase 2, adopt the 5.9 GPU mode if it is useful, and keep the
workers off.

### 5.3 Idle baseline

With nothing else running, measure the coordinator's p50 turn latency on a fixed probe prompt,
**separately in each arm's layout** (1 slot; 3 slots). This baseline is what "+50%" in 5.7 refers
to.

### 5.4 Arm A — 3-slot GPU

1. Enter the 3 × 128K mode with `redteam-mode-enter.sh`.
2. **Stop `redteam-mode-idle.timer` and `redteam-mode-watch`.** A thinking-alias benchmark produces
   none of their keep-warm signals, so the mode would revert to 1 slot after 15 minutes.
3. **Stop `llamacpp-fast`** (Qwen3-4B), which the enter script starts.
4. Embed/rerank are stopped by this mode. Arm B must run with them stopped too (5.0.6).
5. Before each run, confirm `/healthz` shows `chat_admission.capacity: 3` and the right
   `active_chat_profile`.
6. Pin the opencode benchmark profile's context to 128K.
7. Asymmetry to state with the results: in arm A, 3 slots serve 4 consumers (the coordinator plus 3
   workers), and the router queues the fourth.

### 5.5 Arm B — 1-slot GPU coordinator plus 3 CPU workers

1. The lab is in Minimal mode.
2. Start llama-server by hand on `llmbench01-03` with the frozen flags (§3), including
   `-cram 256 -ctx-ckpt 8`.
3. Verify each one with an **authenticated completion**.
4. The coordinator is router `qwen3.8` at 1 slot, not redteam mode, with the restart timer stopped
   (5.0.6).

### 5.6 Measurements

- **Quality sub-gate (aider):** run the Polyglot Python subset against (a) the coordinator alias and
  (b) one CPU worker directly, `--threads 1`, for the number of runs 5.1 sets. Metric: pass@2 mean
  with a 95% CI.
- **Capacity sub-gate (opencode):** run the frozen worker-task set through the coordinator, for the
  number of runs 5.1 sets, in each arm. Per run:
  - wall-clock to finish all N;
  - tasks completed per hour;
  - coordinator p50 turn latency **during** fan-out against the 5.3 baseline;
  - task pass/fail by `checks`;
  - re-prefill count and `cache_n`, scraped from the **llama-server logs** per turn;
  - worker tokens per job.

### 5.7 Pre-registered decision rule

**Quality veto (applies first).** If the CPU worker's pass@2 CI lower bound is more than one task
below the coordinator alias on the same ruler, with thinking state controlled, then close Phase 2.

**Build Phase 2 only if every one of these holds in every run:**
1. Additive capacity: in arm B, aggregate tasks/hour exceeds arm A's, with a 95% CI that excludes
   zero.
2. Coordinator protection: arm B's coordinator p50 turn latency stays within **+50%** of its own
   5.3 baseline.
3. Task quality: arm B passes no fewer `checks` than arm A minus one task.

**Wall-clock.** A ≥30% wall-clock win for B is noted as supporting evidence. It is neither required
nor sufficient on its own. There is no clause that builds Phase 2 on arm A's latency alone, which
removes rev 3's loophole.

**Otherwise:**
- close Phase 2;
- power the workers off, so Minimal means hosts only (D9);
- if arm A's numbers justify it, adopt 5.9.

### 5.8 Teardown

1. Restore every timer stopped in 5.0.6 and 5.4.
2. Exit redteam mode with `redteam-mode-exit.sh`.
3. Restore `RATE_LIMIT_CHAT`.
4. Stop llama-server and remove the throwaway keys on the workers.
5. Commit the raw per-run results, the environment record and the decision with its CIs, all
   referencing the gate-config SHA.

### 5.9 If arm A wins: a selectable 3-slot chat mode

- Profiles are identified by `repo:quant`, so a "-3slot profile" cannot coexist with them. Model it
  as a **mode**, like redteam mode.
- The router capacity already follows `total_slots`.
- While the mode is on, opencode's context pin must be 128K. It is enforced by a separate opencode
  profile, not by convention.
- RAG must be available in the mode. Measure VRAM for 3 × 128K with embed/rerank loaded; if they
  do not fit, the mode is RAG-off and documented as such.
- Redteam mode takes precedence. Entering redteam mode from the 3-slot mode, and exiting it again,
  is defined explicitly, and the exit restores the previous mode.

## 6. Phase 2 — CPU workers (only if the gate passes): requirements

These are requirements, not a design. The design is written after the gate, reviewed, then planned.

### 6.1 Placement and lab safety
- Workers run only in Minimal mode, and Full refuses while they run. Both are shipped (§4).
- **vCenter-independent guard.** It runs as a **systemd timer on the Proxmox host** and talks to
  each ESXi host directly (`govc` with `GOVC_PERSIST_SESSION=false`, or pyVmomi). It is not the
  scratch PowerShell watchdog. It powers all workers off within one poll when:
  - a host disconnects, or an HA failover occurs;
  - any tier device is not healthy;
  - a host's **free** DRAM falls below the margin.

  It is tested by injecting a fake host-down signal before Phase 2 ships.
- **Per-host gate before power-on:** consumed + 24 GB + overhead ≤ 75 GB, read live.
- **Tier writes:** `BlocksWritten`/day is recorded for 48 h before and after. A rise is a rollback
  trigger.

### 6.2 Network and security
- **Dedicated worker VLAN**, not VLAN 10. Its port group must be **ephemeral-binding** (or a
  standard vSwitch), because Minimal starts workers host-direct with vCenter off. Static binding
  breaks that. Verify the host-direct attach on the new port group.
- **Firewall:** router → workers `:8090` only. Worker → LAN **denied**, verified with curl.
- **Encrypted, authenticated channel** (WireGuard, or TLS with the router pinning each worker's
  certificate).
- **Health:** a worker counts as up only after an **authenticated identity check**. llama-server's
  `/health` is public, so it is not enough.
- **Shutdown order:** a worker is removed from the router **before** it is powered off.
- **Worker processes:**
  - llama-server runs non-root under systemd hardening;
  - keys are distributed as files, and rotation is documented;
  - whether ik supports `--api-key-file` is verified by running it;
  - no prompt retention.
- **Agent permissions:** opencode `worker-N` agents are deny-by-default, verified by execution.

### 6.3 Router
- **Backend objects** own url, key and admission gate. The V620 key can never be sent to a worker.
- **Config** is JSON or env, validated **fail-closed** at startup: alias collisions, unknown keys
  and missing keys refuse to start. It is not YAML, because PyYAML is absent from the router venv.
- **Worker aliases live outside `ALIAS_MAP`**, the profile guard, the swap webhook **and the
  redteam watcher's alias match**.
- **Fallback:**
  - the target is an explicit ALIAS_MAP alias;
  - it applies **only if the active profile already serves it**, and never triggers a swap;
  - the alias's sampling defaults replace the client's;
  - the guard is not re-run;
  - **the number of fallbacks in flight is capped**, so a lab outage cannot storm the GPU;
  - otherwise 503, in the existing error envelope.
- **Failure handling:**
  - No health-gating of `v620`.
  - Worker connect timeout ~5 s, with immediate fallback on connect failure.
  - A probe that returns **5xx, or times out**, counts as a failure.
  - **Streams in flight to a backend marked down are aborted** (probe-driven), and TCP keepalive
    is on.
  - Recovery needs ≥3 consecutive good probes (hysteresis).
- **Limits:**
  - responses from workers are size-capped;
  - `tool_execution=server` is refused on worker aliases;
  - each client token has its own allowed aliases.
- **Telemetry:** worker traffic does not feed `_last_chat_ts`, which drives the fan feed-forward
  and the idle-restart gate.
- **Testability:** logic lives in importable modules with pytest, following the
  `stream_admission.py` pattern.

### 6.4 Clients
- Worker entries set explicit sampling and `limit.context` ≤ 65536 − output.
- Each worker alias has exactly one consumer at a time.
- Redteam stays on the Qwen3-4B fast tier until a recorded-engagement comparison.

## 7. Phase 3 — delegate pool (deferred)

Gated on all of the following:
1. The local-delegate A/B returns **go**.
2. JobStore gains real concurrency. Today it has one worker thread.
3. The overlay is generated per job, with the leased alias and context limit. Today it is
   hard-coded to `qwen3.8-think` at 200K.
4. Delegate stage-2 sandboxing is in place.
5. Pool entries are **router aliases only**.
6. A **repo allowlist and a secret scan** run before any job content leaves the workstation.
7. **No fallback for leased jobs**, so a job that fails on a worker fails visibly.

## 8. Testing

- **Python:** pytest via importable modules. The repo suite stays green.
- **Router end-to-end:** in-process, via ASGITransport, against a fake upstream, in the router venv.
- **PowerShell:** the vcf-lab stub harness stays green, and new guards are mutation-tested.
- **Shell:** `bash -n` and shellcheck.
- **Gate:** the harness, the frozen task sets, the raw results and the environment record are all
  committed, referencing the gate-config SHA.

## 9. Rollout order

| Step | Change | Rollback |
|---|---|---|
| 0 | ✅ Phase 0 (§4). **Push `main` plus branches; host `git pull --ff-only`** | revert the merge commits |
| 0r | Re-embed rollback copies deleted after **2026-10-12** | (until then) directory swap + FA revert |
| 1 | §5 gate: commit the gate config → variance check → concurrency probe → baselines → arms A and B → decision | none (manual mode, servers started by hand); 5.8 teardown |
| 2 | *If the gate passes:* the §6 design → review → plan | — |
| 2' | *If arm A justifies it:* the 5.9 mode | mode off |
| 3 | Delegate pool (§7 gates) | — |

## 10. Risks

| Risk | Mitigation |
|---|---|
| The host checkout reverts the fixes on the next deploy | Push, then `git pull --ff-only` (verified fast-forwardable) |
| The 1-slot coordinator is killed mid-run by the restart timer | 5.0.6 stops it for the window |
| Arm A silently reverts to 1 slot | 5.4 stops the idle timer and watcher; `/healthz` is checked before each run |
| Rate-limit 429s read as latency | `RATE_LIMIT_CHAT` raised for the window and recorded |
| Underpowered comparison | The 5.1 variance check sizes N and runs before any cross-arm data |
| Thresholds tuned after the fact | The gate config is committed first; its SHA is quoted with the results |
| Benchmark exposes the workstation to plaintext workers | 5.0.3 minimums; teardown removes servers and keys |

## 11. Disposition of critical and high findings

| Finding | Disposition |
|---|---|
| Arch C1 — streams bypass `chat_sem` | **Fixed** (`5d942d7`): capacity follows slots |
| Evidence C1 — Phase 2 before its justification | **Adopted:** §5 gate, now runnable (rev 3.1) |
| Ops C1/C2 — workers beside the management stack (HA cascade, tier squeeze) | **Resolved:** Full refuses while workers run (`61d3d2c`, shipped). Phase 2 adds the §6.1 guard and gates for host failure in Minimal |
| Sec F1 — plaintext, unauthenticated channel | §6.2 for Phase 2; §5.0.3 minimums for the benchmark |
| Ops H1 / Ev H4 — N-1 math | Superseded: §6.1 live free-DRAM gate |
| Ops H2/H3 / Arch H1/H2 / Sec F10 — fallback hazards | §6.3, now including the redteam-watcher exclusion and the in-flight fallback cap (re-review: these had been dropped) |
| Ops H4 / Arch H4 — dead worker visible | §6.3: connect timeout, 5xx/timeout probe failures, probe-driven abort of in-flight streams. The §1 criterion is restated to match (re-review N6) |
| Ops H5 / Arch M5 — flapping | §6.3 hysteresis |
| Ops H6/H7/H8 / Ev H6/H7/H8/M3 — FA and re-embed | **Done** (§4); scripts committed |
| Ops H9 / Arch H5 — 3-slot profile | §5.9 mode, with RAG coexistence and redteam precedence defined |
| Ops H10 — CPU contention | Measured (16% gen / 21% prefill); removed by Minimal |
| Arch H3 — gating `v620` | §6.3: no gating |
| Arch H6 / Sec F6 — delegate pool | §7 gates 2-7 (all of F6's five items) |
| Sec F2 / F3 / F4 / F5 | §6.2 / §6.3 (Phase 2); §5.0.3 (benchmark) |
| Ev H1 / H2 / H3 / H5 | §3 corrected; quality sub-gate; re-prefill scraped from logs; §1 criteria rewritten |
| **Rev-3 re-review N1-N16** | N1: refusal shipped. N2: §2 exception. N3: §4 corrected. N4: 5.0.6 + 5.4. N5: 5.7 rewritten. N6: §1 + §6.3. N7: §6.2. N8: 5.0.3. N9: 5.9. N10: §5 harness split. N11: §3. N12: 5.0.7. N13: §1. N14: §6.1. N15: §4 + `scripts/tools/allm-reembed/`. N16: §12 |
| **Gate re-review C1-C3, H1-H6** | C1: frozen task set, no delegations. C2: two harnesses. C3: 5.4. H1: 5.0.2. H2: 5.2 stop condition. H3: 5.0.5. H4: thinking pinned. H5: 5.1. H6: 5.7 leads with additive capacity |

## 12. Checklist — medium and low findings (carried into the Phase 2 design review)

- **Ops:**
  - M1: router pre-checks couple worker requests to the V620;
  - M2: static pinning gives no exclusivity;
  - M3: `/healthz` changes vs the fan feed-forward and idle gate;
  - M4: "no client-visible change" overstated;
  - M5: rollback order vs the delegate A/B;
  - M6: maintenance mode and the tier-failure runbook;
  - M7: coded free-DRAM power-on gate;
  - L1-L4: path verification, patching plan, fallback visibility, argv keys.
- **Arch:**
  - M1: seven code paths still pinned to `V620_URL`;
  - M2: GPU-global token budget and tokenizer;
  - M3: worker traffic as GPU activity;
  - M4: fallback vs redteam and the A/B;
  - M6: ik `/health` under load;
  - M7: affinity with shared aliases;
  - M8: opencode entries need context and sampling;
  - M9: router tests not writable by import;
  - M10: PyYAML absent;
  - L1: 503 envelope; L2: alias typos go to the GPU; L3: workers invisible in `/v1/models`;
  - L4: per-IP rate limit; L5: argv keys; L6: parallel > 1 costs; note: single uvicorn worker.
- **Security:**
  - F7: worker key handling;
  - F8: unbounded responses;
  - F9: `tool_execution=server` exfiltration;
  - F11: per-client alias ACL;
  - F12: config validation and alias collisions;
  - F13: patching and provenance;
  - F14: prompt and secret retention;
  - F15: hygiene.
- **Evidence:**
  - M1: thinking claim;
  - M2: embed benchmark traps;
  - M4: 50× is ~1% of end-to-end;
  - M5: "nothing beyond speed";
  - M6: probe order;
  - L1-L6: reporting and reproducibility.
- **Re-review lows:** N12-N16 (handled above). Re-check each item against the Phase 2 design.
