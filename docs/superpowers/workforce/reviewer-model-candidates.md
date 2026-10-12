# Reviewer-model candidates for a later trial

Researched 2026-10-10 (web research by a subagent; every ROCm claim is **unverified** on our gfx1030
until a smoke test on llama.cpp b11026 with our exact `-fa` and KV-cache flags). Vendor benchmark
figures are self-reported. Context: the GPU lead reviews diffs; what matters is defect-spotting on a
diff with call sites, strict output format, prefill speed and reliable tool calling. The current
reviewer is Qwen3.8-27B (dense), which also implements.

## Shortlist

| Model | Size / active | Arch | Fit (quant) | Why | Risk |
|---|---|---|---|---|---|
| **Qwen3.6-35B-A3B** `Qwen/Qwen3.6-35B-A3B` | 35B / 3B | MoE (DeltaNet + gated attn) | Q6/Q8 (~22 GB at Q4_K_M) | Already runs on the V620s (78 t/s gen, ~1.6× the 27B); tied Qwen3.8 at 60% on our hidden tests; SWE-V 73.4 self-reported; non-thinking via `enable_thinking:false` | Same family as the implementer: shared blind spots |
| **Gemma 4 26B-A4B** `google/gemma-4-26B-A4B-it` | 25B / 3.8B | MoE, SWA 1024 | Q6 fits | Different family reviewing Qwen code; strongest tool-use / IFBench in class; Apache-2.0 | gfx1030 flash-attn unverified; non-thinking still emits an empty thought block; no SWE-bench published |
| **Laguna XS 2.1** `poolside/Laguna-XS-2.1` | 33B / 3B | MoE, 10 global + 30 SWA-512 layers | Q4_K_M 20.3 GB | Coding specialist, SWE-V 70.9; sibling ranked 3rd of 13 in Kilo's review data | **HIP reports:** FA crash on gfx1151 at the first SWA layer, and looping with MCP tools in the prompt on HIP (PR 25165); needs a Vulkan fallback |
| Muse Glimmer 30B `meta-models/Muse-Glimmer-30B` | 28B dense | SWA 2048, 2 KV heads | UD-Q6_K_XL ~21 GB | SWE-V 76.0, support in llama.cpp b10353 | Reasoning cannot be turned off (`--reasoning-budget 0` ignored) → not terse |
| Qwen3.5-9B `Qwen/Qwen3.5-9B` | 9B | hybrid | small | ~3× faster; IFEval 91.5 | Too weak alone (SWE-V ~50); a triage stage only |
| Mellum 2.1 `JetBrains/Mellum2.1-12B-A2.5B-Thinking` | 12B / 2.5B | MoE | — | SWE-V 47 | GGUF "coming soon"; thinking variant only |
| Gemma 4 31B | 31B dense | 60 layers | tight | LCB 80.0 | ~50K context fit in 32 GB reported: cannot hold 3×128K |

## Models built for review
No released open-weight PR-review model was found. **SWE-RM** (arXiv 2512.21919; a Qwen3-30B-A3B
reward model answering YES/NO on trajectories) could be a cheap ACCEPT/REVISE second opinion but
cannot write feedback; the `ksshumab/SWE-RM-2` repo has an empty README (unconfirmed). Sphinx
(2601.04252) and Critique-Coder-8B (2509.22824) released no weights. Evidence that shapes the design:
reviewer F1 fell from 0.66 on diffs under 10 lines to 0.04 on diffs over 150 lines, and a smaller
model beat a larger one (arXiv 2606.15689) — **keep diffs small**.

## Avoid
- GLM-4.7-Flash: `GGML_ASSERT(max_blocks_per_sm > 0)` on gfx1030 with FA + quantized KV (llama.cpp
  issue 19307, closed stale).
- Nemotron 3.5 Lightning 30B-A3B: SWE-V 51.6 vs 70.1 for Qwen3.6-35B-A3B in NVIDIA's own table.
- gpt-oss-20b at low reasoning: SWE-V 37.4.
- Qwen3-Coder-Next: our MTP crashes (qwen3next arch).
- Licences: everything listed is Apache-2.0 except Laguna/Nemotron (OpenMDW-1.1, commercial use
  allowed; a defensive-termination clause is under OSI dispute).

## Recommended trial order
1. **Qwen3.6-35B-A3B non-thinking (Q6/Q8)**: lowest risk, already proven on this hardware, prefill
   several times faster than the 27B (unmeasured).
2. **Gemma 4 26B-A4B non-thinking**: a second family; check FA on gfx1030, the empty thought block
   and the KV footprint first.
3. **Laguna XS 2.1**: behind a HIP smoke test with tools in the prompt, Vulkan as fallback.

**How to score them:** a seeded-defect set (wrong flags, signature mismatches, dead guards) that the
visible tests do not catch, i.e. the three real bugs the W3 reviewer found plus synthetic siblings,
replayed through `cli.py replay-reviews` with the reviewer alias switched. Measure verdict accuracy,
steps per review and prompt tokens per review against Qwen3.8.

## Trial 1 result — Qwen3.6-35B-A3B nothink (2026-10-11)

Run as `replay-2`: the 27 harvested W3 review rounds with the reviewer on Qwen3.6-35B-A3B (C2, served by
worker llmbench01 through the sandbox's worker provider — no router or chat change). Full record:
`replay-2-reviewer-trial.json`.

| | qwen3.8-nothink (replay-1, V620 pair) | Qwen3.6-35B-A3B nothink (CPU worker) |
|---|---|---|
| Verdicts | 21 ACCEPT / 6 REVISE / 0 NONE | 19 ACCEPT / 8 REVISE / 0 NONE |
| Agreement with W3 (20 judged rounds) | 16/20 | 14/20 |
| Agreement with each other | — | 21/27 |
| Median reviewer steps | 10 | 3 (12 of 27 verdicts after one step) |
| Capped / verdict turns | 1 / 1 | 1 / 1 |
| Median seconds per review | 201 | 119 |

Disagreements, graded against the hidden tests: 2 gains for Qwen3.6 (a state the hidden tests fail that
both other reviewers accepted; the `should_cache` arity bug qwen3.8 missed), 2 misses (the redirect
`urljoin` bug; the zfs-import unit defects), 2 spurious REVISEs from hallucinated bugs in one-step verdicts.

**Verdict:** quality on par with qwen3.8, 3× fewer steps, 40% faster per review even on a CPU box (it would
be several times faster on the V620s); the new risk is the one-step hallucination (2 of 27, one rework
round each). A credible throughput reviewer, not a quality upgrade. Next if the lead stays the bottleneck
after the round-2 window: measure it on the GPU as an extra arm in a later window, after trying a
"read the changed function before you decide" rule to cut the one-step verdicts. Gemma 4 26B-A4B remains
the next candidate to replay.

## Trial 2 result — same model + the read-first rule (2026-10-11)

`replay-3`: trial 1's setup plus `--read-first` ("read at least one changed function with the read tool
before you decide; never from the packet alone"). Full record: `replay-3-reviewer-trial-read-first.json`.

| | qwen3.8 (V620) | Qwen3.6 trial 1 | Qwen3.6 + read-first (trial 2) |
|---|---|---|---|
| Agreement with W3 (20 judged) | 16/20 | 14/20 | **17/20** |
| Agreement with qwen3.8 | — | 21/27 | 24/27 |
| One-step verdicts | 0 | 12 | 1 |
| Hallucinated REVISEs | 0 | 2 | **0** |
| Valid catches qwen3.8 missed | — | 2 | 2 (different ones) |
| Median steps | 10 | 3 | 11 |
| Capped / verdict turns | 1 / 1 | 1 / 1 | 4 / 5 |
| Median seconds per review | 201 (GPU) | 119 (CPU) | 277 (CPU) |

**Verdict:** the rule does what it was for — the two hallucinated findings are gone and the model now reads
before it judges, finding two real defects nobody else did (`RemainAfterExit` under `[Unit]`; an unclosed
`httpx.AsyncClient`). The price is qwen3.8-level step counts and a high cap rate (4 of 27, all rescued by the
verdict turn), so the "3× cheaper" result of trial 1 does not survive the rule; what survives is the per-step
cheapness of a 3B-active model, i.e. still a likely 2× on the GPU. Keep the rule for any small reviewer.
Next variant worth a GPU measurement: rule + `REVIEW_STEPS 30`, as an extra T arm in a later window. qwen3.8
remains the pre-registered round-2 reviewer.

## Trial 3 result — Gemma 4 26B-A4B (2026-10-11/12)

`replay-6` after a 19-config serving sweep (`gemma4-worker-sweep.json`) and two aborted starts: Google's QAT q4_0
build + Unsloth's MTP draft head on ik llama-server (FA on, q8 KV, MTP n3, reasoning off, no prompt-cache RAM),
read-first rule on, worker llmbench01. Full record: `replay-6-reviewer-trial-gemma4.json`.

| | qwen3.8 (V620) | Qwen3.6 + read-first | **Gemma 4 26B-A4B + read-first** |
|---|---|---|---|
| Verdicts | 21 / 6 / 0 | 20 / 7 / 0 | **23 ACCEPT / 3 REVISE / 1 NONE** |
| Agreement with W3 (20 judged) | 16/20 | 17/20 | 14/20 |
| Agreement with qwen3.8 | — | 24/27 | 22/27 |
| Valid catches qwen3.8 missed | — | 2 | 1 (minor: duplicated unit key) |
| Misses of qwen3.8's catches | — | 1 | 2 (+ one role-confusion round with no verdict at all) |
| Median steps / verdict turns | 10 / 1 | 11 / 5 | 7 / 6 |
| Seconds per review (CPU worker) | 201 (GPU) | 277 | 239 |

**Verdict:** the weakest reviewer of the three. Lenient, lowest agreement, shares both of qwen3.8's misses (so the
"second family, different blind spots" idea did not pay off), and it brings integration problems of its own:
it thinks by default (must be switched off), on ik it answers a tool result with an empty message on ~15% of
rounds, its full-attention KV OOMs the 32 GB worker with the prompt-cache RAM save on, and mainline llama.cpp
prefills it 10× slower on these CPUs. **Do not pursue Gemma 4 as the reviewer.**

Rank after three trials: qwen3.8-nothink (pre-registered) ≈ Qwen3.6-35B-A3B + read-first (equal quality,
cheaper per step) > Qwen3.6 alone (fast, hallucinates) > Gemma 4 26B-A4B. Laguna XS 2.1 stays untried; not
worth a trial before the round-2 window.
