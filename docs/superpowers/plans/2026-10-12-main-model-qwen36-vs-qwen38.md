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
4. **Qwen3.6 with its in-GGUF MTP head on the GPU** (`draft-mtp`). The 2026-09 spec-decode regression on
   this MoE was a *separate draft model*; the MTP-variant GGUF (already used by the CPU workers) is
   untested on the V620s.
5. **Not covered yet — RAG answer quality.** There is no discriminating RAG eval; the vcf-lookup path
   needs a small known-answer set before it can be compared. Flagged, not run.

## Decision rule (fixed before the runs)

- Qwen3.6 replaces qwen3.8 as the main model if its Polyglot pass@2 (best mode) is within 5 points of
  qwen3.8-think **and** its S-arm acceptance is within one task of qwen3.8's, at ≥1.5× the speed.
- Within 5–10 points: keep qwen3.8 as the default, add Qwen3.6 as the fast profile for agents/workforce.
- More than 10 points behind: keep qwen3.8; Qwen3.6 only as the workforce lead where speed dominates.

Operational dependencies to weigh either way: the red-team profile is an abliterated qwen3.8; OpenCode's
default is qwen3.8-think for coding and nothink for RAG; router aliases and the swap webhook are keyed to
profile names.

## Status

Queued behind the S/G repeat chain (2026-10-12, GPU busy until ~16:30Z). Results go to
`docs/superpowers/workforce/main-model-qwen36-vs-qwen38.json`.
