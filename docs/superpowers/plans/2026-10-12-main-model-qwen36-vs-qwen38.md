# Main GPU model: optimized Qwen3.6-35B-A3B vs Qwen3.8-27B — research plan

**Question.** The cluster moved its main chat/coding model from Qwen3.6-35B-A3B to Qwen3.8-27B in
August–September 2026. The 2026-10-12 workforce trials showed a *tuned* Qwen3.6 on the V620s reviewing
at qwen3.8's quality 2.5× faster, and implementing the 16 workforce tasks alone in 27 minutes. Does the
original decision still hold once Qwen3.6 runs with the right settings?

## What the original decision rested on

| Evidence | Result | Weakness found 2026-10-12 |
|---|---|---|
| Aider Polyglot Python-34, think (2026-09-06 sweep) | qwen3.8 73.5% pass@2; Qwen3.6 **63.2%** | Qwen3.6 run cut at **19/34** cases by the sweep deadline; ran with `--split-mode tensor`, which costs this MoE ~45% of its generation speed (33 vs 59 t/s) — hence its 218 s/case |
| Agentic loop eval (2026-08-19) | qwen3.8 5 turns, parallel tool calls | Qwen3.6 was not in that eval (it was qwen3.8 vs Qwen3-Coder-Next) |
| Speed (qwen3.8 profile) | qwen3.8 49 t/s with tensor split + in-GGUF MTP | Qwen3.6 never re-tuned after the pivot; tonight: 59.7 t/s generation, 2559 t/s prefill at layer 1:1, ub 1024, f16 KV |
| CPU worker eval (2026-10-05) | Qwen3.6 60% hidden-test pass = Qwen3.8 | on CPU, not GPU |

So the quality evidence for qwen3.8 is one partial run, and the speed evidence compared against a
mis-configured Qwen3.6.

## Published evidence (researched 2026-10-12)

- **Qwen's Qwen3.8-27B card** compares against the *dense* Qwen3.6-27B, not the 35B-A3B, and wins
  clearly: SWE-bench Pro 61.7 vs 53.5, Terminal-Bench 2.1 73.0 vs 63.4, LiveCodeBench v6 90.3 vs 83.9,
  NL2Repo 42.3 vs 36.2, IFBench 79.5 vs 69.1. Source: huggingface.co/Qwen/Qwen3.8-27B.
- **Qwen3.6-35B-A3B's own card**: SWE-bench Verified 73.4, SWE-bench Pro 49.5, Terminal-Bench 2.0 51.5,
  LiveCodeBench v6 80.4 — below the dense 27B on every shared benchmark. Source: huggingface.co/Qwen/Qwen3.6-35B-A3B.
- **Artificial Analysis** (artificialanalysis.ai/models/comparisons/qwen3-8-27b-vs-qwen3-6-35b-a3b):
  Intelligence Index 34 vs 18; AutomationBench-AA 48% vs 5%; SciCode 47 vs 37; AA-LCR (long context)
  82 vs 72; output speed 46.5 vs 129.4 tok/s. Effort-confounded: qwen3.8 ran at "Xhigh" (67K output
  tokens/task) vs Qwen3.6 default reasoning (35K).
- **Aggregators** (llm-stats, BenchLM): qwen3.8 wins 8/8 shared benchmarks; SWE-bench Pro 61.7 vs 49.5.
- **Optimizations for Qwen3.6-35B-A3B in llama.cpp** (Unsloth docs, Jarvislabs, community):
  in-GGUF MTP via the `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` with `--spec-type draft-mtp --spec-draft-n-max 2`
  — only ~1.15–1.25× on this MoE (1.17× measured on an RTX PRO 6000; 1.73× for the dense 27B), acceptance
  falls 83% → 50% at 4 drafts, ~1 GB extra memory; community reports Q4 noticeably weaker than Q6 on
  this MoE for tool calling and long context; bf16 KV as the fix if output degrades; `--n-cpu-moe` is
  irrelevant here (the model fits in VRAM).

**Implication.** The external gap on hard agentic coding is large (~12 points SWE-bench Pro, 48% vs 5%
AutomationBench). Our own tasks showed near-parity — which is exactly the pattern of a saturated
ruler (2026-09-06: "thinking is worse" looked true until Aider Polyglot showed +23.5 points). The
decision therefore rests on the discriminating measurements below, not on the workforce replays.

## Measurements (each model at its own model-card sampling)

1. **Coding quality — Aider Polyglot Python-34, full set** (LXC 158, the established discriminating
   ruler), against a direct llama-server on 151 like the September sweep:
   - Qwen3.6 tuned, thinking (temp 0.6, top-p 0.95, top-k 20, presence 0 — the card's coding preset)
   - Qwen3.6 tuned, non-thinking (temp 0.7, top-p 0.8, top-k 20, presence 1.5)
   - qwen3.8 references: think 73.5 / nothink 50.0 (2026-09). Re-run qwen3.8 think the same day only if
     Qwen3.6 lands within 10 points.
2. **Agentic, our own work — workforce arm S with qwen3.8** on the same 16 frozen tasks (hidden tests),
   to set against tonight's Qwen3.6 S runs (lt-2-s, lt-4-s, lt-6-s). Same harness (ripgrep fixed).
3. **Speed and capacity at production shapes** — generation/prefill at 262K context; `--parallel 3`
   (Qwen3.6's hybrid attention keeps KV small, so three large slots may fit where qwen3.8 needs the
   window layout); VRAM headroom for embed + rerank alongside.
4. **Qwen3.6 serving optimizations on the V620s**: the MTP GGUF with `draft-mtp` at n-max 1/2/3 (the
   2026-09 spec-decode regression on this MoE was a *separate draft model*); **Q6 instead of Q4** (UD-Q6_K
   crashed with a ROCm illegal memory access under layer 1:1.5 on 2026-10-12 — retry at layer 1:1 and
   with UD-Q6_K_XL); bf16 KV. The Polyglot and S runs use whichever quant wins, so Qwen3.6 is not judged
   at a handicapped quant.
5. **RAG answer quality, frozen retrieval.** 24 VCF questions with known answers (from facts the lab
   verified by running, e.g. three-host vSAN, .local scope, memory tiering needing maintenance mode);
   AnythingLLM `vcf-reference` vector-search top-6 frozen once, so both models answer from identical
   context with the workspace's own system prompt, non-thinking (the RAG path's mode). 22 of 24 questions
   have their key facts in the retrieved context and are used. Graded blind (model labels hidden) against
   the frozen context: correct and supported / partly / wrong or unsupported, plus latency.
   Scripts and questions: `docs/superpowers/workforce/rag-eval/` (`rag_eval.py` retrieve / answer / grade).

## Decision rule (fixed before the runs)

- Qwen3.6 replaces qwen3.8 as the main model if its Polyglot pass@2 (best mode) is within 5 points of
  qwen3.8-think **and** its S-arm acceptance is within one task of qwen3.8's, at ≥1.5× the speed,
  **and** its blind-graded RAG correctness is no more than 2 questions (of 22) below qwen3.8's.
  (The RAG clause was added 2026-10-12 before any RAG answers were generated.)
- Within 5–10 points: keep qwen3.8 as the default, add Qwen3.6 as the fast profile for agents/workforce.
- More than 10 points behind: keep qwen3.8; Qwen3.6 only as the workforce lead where speed dominates.

Operational dependencies to weigh either way: the red-team profile is an abliterated qwen3.8; OpenCode's
default is qwen3.8-think for coding and nothink for RAG; router aliases and the swap webhook are keyed to
profile names.

## Status

Queued behind the S/G repeat chain (2026-10-12, GPU busy until ~16:30Z). Results go to
`docs/superpowers/workforce/main-model-qwen36-vs-qwen38.json`.
