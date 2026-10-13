# Local coding engine: GPU server + three Minisforum nodes — recommendation and design

**Status:** proposal, 2026-10-12. Written for the cluster owner. Builds on the measured results of
2026-10-03..12 (sources in §9); supersedes nothing yet — §8 lists the decisions it needs.

## 1. Goal and honest ceiling

**Goal:** move most day-to-day coding work from Claude to local agents on hardware already in place: the
Proxmox GPU server (2x Radeon PRO V620, 64 GB VRAM) and the three Minisforum hosts (Ryzen 9 7945HX, 16 cores
each, CPU only).

**Ceiling:** the best local model, Qwen3.8-27B, is a strong mid-tier coder (Aider Polyglot Python 70.6% pass@2
thinking; SWE-bench Pro 61.7 published) but clearly below frontier models on long, multi-step agentic work. The
design therefore does not try to replace Claude everywhere. It makes local agents do the **implementation volume**
(where most tokens go) and keeps Claude for **planning hard changes, reviewing risky diffs and rescuing failures**.
Target: 70-80% of coding sessions finish without Claude; Claude spend falls by more than that, because
implementation turns are the token-heavy part.

## 2. What the measurements say (drives every choice below)

| Finding | Evidence | Design consequence |
|---|---|---|
| The GPU is the engine | Qwen3.6 alone on the V620s did the 16-task set in 27-37 min; three CPU workers with a GPU lead took 93 min | Everything interactive and every implementation runs on the GPU |
| CPU nodes are 4-5x slower per task, and prefill is their bottleneck | median implement 7.2 min CPU vs 1.5 min GPU; CPU prefill ~125-170 t/s, so a 16K-token turn costs ~2 min | CPU models never sit on an interactive path; they do asynchronous work |
| GPU review does not pay on the GPU | G (implement + review on GPU) 12.7 accepted in 72 min vs S (GPU alone) 13.3 in 33 min; one cheap review (budget 1) found 3 of 4 real failures, netted +1 task for ~8 min | Move review **off** the GPU onto the CPU nodes, where it costs the GPU nothing |
| Qwen3.6 on CPU reviews about as well as qwen3.8 | replay trials: W3 agreement 14-17/20 vs qwen3.8 16-17/20; 119-277 s per review on CPU | The CPU nodes become the review and verification tier |
| Agents need to execute | CPU workers rewrote the same file 16-37x when they could not run tests | Every agent loop has a sandbox that runs the tests; the CPU nodes supply it |
| qwen3.8 is the right resident model | coding is a tie with tuned Qwen3.6 (Polyglot 70.6 vs 76.5, S arm 13 vs 13.3); qwen3.8 wins blind RAG grounding 17 vs 12 of 22 and the published long-horizon benchmarks | qwen3.8-27B stays resident on the GPU; Qwen3.6 lives on the CPU nodes |
| Single runs are noisy | identical S runs scored 12, 13, 15 of 16 | Tune with a larger task set (§7) |

## 3. Architecture

```
                         ┌──────────────────────── workstation ────────────────────────┐
                         │ OpenCode (interactive)        Claude Code (plan / review /   │
                         │  primary: qwen3.8-think        escalation only)             │
                         └──────────────┬──────────────────────────────┬────────────────┘
                                        │ OpenAI API (router, scoped keys)
                 ┌──────────────────────▼─────────────────────────────────────────────┐
                 │ Router LXC 153: aliases, admission, reserved user lane, swap hook  │
                 └───────┬───────────────────────────────┬────────────────────────────┘
                         │                               │
   ┌─────────────────────▼──────────────┐   ┌────────────▼──────────────────────────────────┐
   │ GPU server LXC 151 (2x V620)        │   │ Minisforum hyp01-03 (VCF lab "Minimal" mode)   │
   │  qwen3.8-27B Q6 + MTP, 3 x 128K     │   │  per node, one LLM VM + one runner VM:         │
   │   slot 1 = user lane (reserved)     │   │   - Qwen3.6-35B-A3B (ik, CPU): reviewer,       │
   │   slots 2-3 = agents / subagents    │   │     verifier, overnight overflow implementer   │
   │  + vision projector (images/video)  │   │   - runner: Docker, toolchains, test grading   │
   │  embed + rerank (RAG)               │   │  pulls work from the queue, never blocks the   │
   └─────────────────────────────────────┘   │  user                                          │
                                             └───────────────────────────────────────────────┘
   Background queue (workforce harness, sandbox VM 176 → runners): ticket → GPU implements →
   CPU node reviews + runs tests → GPU reworks once → branch + report for human/Claude review
```

### 3.1 GPU server — the brain (interactive and implementation)

- **Model:** qwen3.8-27B UD-Q6_K_XL with in-GGUF MTP, tensor split, resident all day. Thinking (`medium`) for
  coding, non-thinking for RAG and quick tools (aliases already exist and match the model card).
- **Upgrade llama.cpp to b11577** (≥ b11259). It fixes the MTP bug that re-processes the previous answer on every
  follow-up turn — the single biggest speed-up for agent loops. Same build cycle: `--spec-draft-sampling
  probabilistic`, the V620 prefill patch (PR #27558), drop the dead `--cache-reuse` flag.
- **Layout: 3 slots x 128K during working hours** (the "window" layout, measured to fit with embed + rerank on
  2026-10-08), with the router's **reserved user lane** (built in Plan A): slot 1 always free for the person at the
  keyboard; slots 2-3 for OpenCode subagents and the background queue. 128K per slot is ample for coding sessions
  with OpenCode compaction; the 1 x 262K layout remains a swap profile for rare very-long-context jobs.
- **Vision on:** load the 928 MB F16 projector (`-mmdev` GPU1, `--image-max-tokens` ~1280) so screenshots,
  diagrams and error dialogs can go straight into a coding session.
- **Not on the GPU:** Qwen3.6 (cannot co-reside with qwen3.8: 27 + 40 GB > 64 GB), image generation (swap window
  only), Flash-Next (bounded test only, see the research synthesis).

### 3.2 Minisforum nodes — the verification and background tier

Each node runs, in VCF lab **Minimal mode** (the default, already shipped; Full mode refuses while workers run):

- **One LLM VM** (24 GB, as built): Qwen3.6-35B-A3B on ik_llama.cpp, the measured best CPU worker. Jobs:
  1. **Reviewer** for every background task (read-first + defects-only rules, one review, verdict feeds one GPU
     rework). Runs while the GPU implements the next task — review becomes pipelined instead of serial.
  2. **Verifier**: summarises test failures and diffs, checks the change against the ticket, writes the report a
     human or Claude reads.
  3. **Overflow implementer**, only under the overflow rules: overnight, or when the user lane holds the GPU and the
     backlog is deep (`len(backlog) × GPU-minutes-per-task > CPU-minutes-per-task`); any rework goes back to the GPU.
- **One runner VM**: Docker + the repo toolchains (Python/pytest, bash + shellcheck, PowerShell + Pester, Go if
  needed). It runs visible and hidden tests, linters and mutation checks for the background queue, so the agents
  always execute what they write. Sixteen cores per node are exactly what test execution needs.

Three nodes give three parallel review/verify lanes, which matches the GPU's implement rate (one task every
1.5-3 min against 2-5 min per CPU review).

### 3.3 Two ways of working

**A. Interactive (most sessions):** OpenCode on the workstation, primary agent `qwen3.8-think`, explore/search
subagents on `qwen3.8-nothink` in the parallel slots, the existing TASK-LOOP (baseline → plan → implement → verify)
with the workstation's own shell executing tests. MCP: SearXNG, Context7, docs search (`docs_search_`, not
`docs_query_`), memory vault, vcfspec. Skills: cluster-ops, vcf-lookup and the installed pack.

**B. Background queue (batch work):** write tickets (a markdown file or GitHub issues with acceptance tests), hand
them to the queue, read the results later. The workforce harness already does this end to end (sandbox VM 176,
hidden-test grading, scoped keys, locked egress). Changes: the reviewer moves from the GPU lead to the CPU nodes;
review budget 1 (one review, one rework, no lead fix); results land as branches plus a one-page report per ticket.
Good fits: test backfill, refactors with existing tests, small bug fixes, doc updates, script hardening, dependency
bumps. Poor fits: anything without an executable acceptance check.

### 3.4 Where Claude stays

| Use Claude for | Why |
|---|---|
| Plans for multi-file or architectural changes (then hand the tasks to local agents) | long-horizon planning is where the local gap is largest |
| Review of risky diffs before merge (security, auth, infra that touches production) | cheap in tokens, high value |
| Rescue: a task that failed the local loop twice | avoids burning hours on a stuck agent |
| Novel research and adversarial reviews of designs | quality ceiling matters more than cost |

Rule of thumb for the user: start every task locally unless it is in the left column; escalate on the second failed
attempt, not the fifth.

## 4. Expected effect

- Interactive turns get faster after the llama.cpp upgrade (no re-prefill of the previous answer: ~9 s saved per
  5K-token answer per turn) and parallel subagents stop queuing behind each other (3 slots).
- Background queue: about 13 of 16 small tickets accepted per ~40 min of GPU time (measured with Qwen3.6 alone;
  qwen3.8 measured 13 in 49 min), with review and test execution moved off the GPU, so the GPU's time per ticket
  should drop towards the S-arm figure while keeping a review gate.
- The GPU stays free for the user: the reserved lane guarantees a slot; background agents yield.

## 5. Implementation phases

| Phase | Work | Done when |
|---|---|---|
| 1. GPU (1-2 days) | llama.cpp b11577 side-by-side build + swap (runbook from b11026), probabilistic drafting, PR #27558 cherry-pick, drop `--cache-reuse`; 3 x 128K + reserved user lane as the working-hours default; vision projector as a swap profile first | turn-2 prompt tokens fall in an OpenCode session; Aider Py-34 and the 16-task S arm unchanged or better; vision A/B passes (MTP within 5%, no crash ≤ 1280 px) |
| 2. Runners (1 day) | runner VM per node from the sandbox VM 176 recipe (toolchains, ripgrep, Docker grader); harness grading pointed at the runners | the 16-task set grades on the runners with the same results as on VM 176 |
| 3. Review offload (2-3 days) | harness: reviewer backend = CPU nodes (round-robin, health-checked), review budget 1, GPU rework; per-ticket report | a 16-task run reaches S-arm wall time ± 20% with the review gate on, and acceptance ≥ the S mean |
| 4. Queue front-end (1-2 days) | tickets from a markdown file or GitHub issues; results as branches + report; nightly schedule that yields to the user lane | one week of real tickets through the queue |
| 5. Overflow + escalation | CPU overflow implementer per the proposal; an "escalate to Claude" step that packages the failed attempt (ticket, diffs, test output) | escalation packets are self-contained enough that Claude fixes them in one pass |

## 6. What not to do (already measured)

- CPU workers as the main implementers or as interactive subagents (3x slower; prefill dominates).
- Full GPU review of every change on the GPU (doubles wall time for no acceptance gain).
- Swapping qwen3.8 out for Qwen3.6 as the resident model (RAG grounding and long-horizon losses).
- Q8 or BF16 of the 27B (no measurable quality gain, 13-50% slower), vLLM or ik_llama.cpp on the V620s (unsupported).

## 7. Measuring progress

- **Local share:** log every coding session as local-only, local + Claude review, or escalated. Target 70-80%
  local-only after a month.
- **Regression suite on every model/build change:** Aider Polyglot Python-34 (thinking), the 16-task hidden-test
  set (arm S), the 22-question blind RAG set.
- **Grow the task set to ~50** from this repo's history (the W2 replay method) — at 16 tasks, run-to-run noise is
  3 tasks, which hides most real improvements.

## 8. Decisions needed from the owner

| # | Decision | Recommendation |
|---|---|---|
| D1 | Working-hours GPU layout | 3 x 128K with the reserved user lane (1 x 262K as a swap profile) |
| D2 | Minisforum default mode | Minimal + LLM workers + runners every day; Full (VCF stack) only on demand, which already stops the workers |
| D3 | Claude's role | plan / review risky diffs / rescue after the second failed local attempt |
| D4 | Background queue scope | tickets with an executable acceptance check only |
| D5 | Phase order | 1 → 2 → 3 → 4 → 5; phase 1 is valuable on its own even if nothing else is built |

## 9. Sources (this repo)

- `docs/superpowers/workforce/qwen36-lead-orchestration-runs.json` — T/S/G runs, repeats, cheap review (lt-1..8)
- `docs/superpowers/workforce/main-model-qwen36-vs-qwen38.json` — Polyglot, RAG, S-arm comparison
- `docs/superpowers/workforce/replay-*-reviewer-trial*.json` — reviewer quality and speed on CPU
- `docs/superpowers/workforce/cpu-overflow-proposal.md` — overflow rules
- `docs/superpowers/research/2026-10-12-qwen38-optimize-and-multimodal.md` — llama.cpp upgrade, vision, image gen
- `docs/superpowers/specs/2026-10-05-minisforum-cluster-integration-design.md` — Minimal/Full modes, worker VMs
- `docs/superpowers/specs/2026-10-07-distributed-workforce-design.md` — sandbox, keys, egress, harness
(the first five are on branch `feat/wf-experiment`, PR #20)
