# Coding engine — Phase 1: GPU server (llama.cpp upgrade, 3 x 128K default, vision)

**Design:** `docs/superpowers/specs/2026-10-12-local-coding-engine-design.md` §3.1, §5 phase 1 (approved 2026-10-12).
**Research:** `docs/superpowers/research/2026-10-12-qwen38-optimize-and-multimodal.md` §1, §3.

## Done-when (from the design)

Turn-2 prompt tokens fall in an OpenCode session; Aider Polyglot Python-34 (thinking) and the hidden-test S arm are
unchanged or better; vision passes its A/B (MTP decode within 5%, no crash at ≤ 1280 px).

## What changes, and where it must change together

| Change | Live (LXC 151 / 153 / 154) | Repo | Also |
|---|---|---|---|
| llama.cpp b11026 → b11577 + PR #27558 (commit 1 + RDNA2 MoE port) | `/opt/llama.cpp-b11577` built side by side, swapped into `/opt/llama.cpp/build`; b11026 kept as `build-b11026` | runbook note (build script) | rollback = swap dirs back |
| Drop `--cache-reuse` (cannot work on hybrid memory) | chat unit | `51-lxc-amd.sh`: omit the flag when `LLAMA_CACHE_REUSE=0`, default 0 | host `redteam-mode-enter.sh` drop-in hardcodes the flag list — update in step |
| `--spec-draft-sampling probabilistic` (only if the bench shows ≥ +2% and no quality change) | chat unit | `51-lxc-amd.sh`: new `LLAMA_SPEC_SAMPLING` (default unset) | same drop-in |
| Default layout 3 x 128K (`LLAMA_CTX=393216`, `LLAMA_PARALLEL=3`) | `/etc/llamacpp.env` | `config.env.example` | router `MAX_CHAT_INPUT_TOKENS` 200000 → 120000; AnythingLLM token limit 200000 → 120000 (a request over a slot's 131K fails at the server) |
| Vision profile (`--mmproj mmproj-F16.gguf`, `-mmdev` GPU1, `--image-max-tokens 1280`) | first as a bench, then the default | `51-lxc-amd.sh`: `LLAMA_MMPROJ*` replacing `--no-mmproj` when set | router vision fixes (admission counts images, list-type system content, block remote image URLs, `video_url` → `input_video`, body-size cap) — separate TDD task; OpenCode `attachment`/`modalities` |

## Steps

1. **Build** b11577 + PR #27558 in `/opt/llama.cpp-b11577` (no production impact). Verify `--version`, then diff
   `--help` against b11026 for every flag the chat/embed/rerank units and the redteam drop-in pass (b11521 changed the
   default port; b11531 refactored the chat API).
2. **Bench window** (chat stopped ~45 min; the user has agreed chat usability during tests does not matter). Same
   production flags on a transient server, b11026 vs b11577, then b11577 variants:
   - turn-2 re-prefill: a two-turn conversation; `timings.prompt_n` of turn 2 (the bug re-processes the whole
     previous answer);
   - speed: generation t/s on a 700-token code answer (thinking sampling), prefill t/s at ~8K;
   - tool-call smoke test (OpenAI `tools` → `tool_calls` parsed);
   - `--spec-draft-sampling probabilistic` on vs off;
   - vision: projector F16 on GPU1, images at 512/1024/1280 px, MTP decode with and without the projector, peak VRAM per GPU.
3. **Repo changes** (`51-lxc-amd.sh`, `config.env.example`, router env doc) with `bash -n`/shellcheck and the
   existing tests; commit on a branch.
4. **Production swap** in one short window: build dir swap, unit flags, `/etc/llamacpp.env` 3 x 128K, router
   `MAX_CHAT_INPUT_TOKENS`, AnythingLLM token limit, host redteam drop-in. Verify `/healthz` capacity 3, chat,
   embed, rerank, a RAG query, an OpenCode turn. Rollback recorded before each step.
5. **Post-swap verification**: Aider Polyglot Python-34 thinking (baseline 70.6% today); turn-2 prompt tokens in a
   real OpenCode session; the 16-task S arm with qwen3.8 (baseline 13/16 in 49 min).
6. **Vision default** if step 2 passed, with the router vision fixes test-first.
