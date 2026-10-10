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
