# Qwen3.8-27B on 2x V620: better builds, variants, settings, llama.cpp changes

Research date: 2026-10-12. Every claim has a source URL. Tags used below:
- **[verified]**: I checked it myself in llama.cpp git history (blobless clone of ggml-org/llama.cpp at `cbe28bb3c`, 2026-10-13, which is b11577), in the HF API, or in the official chat template.
- **[vendor]**: the number comes from the model's or tool's own author.
- **[community]**: one third-party run.
- **[unverified]**: I could not confirm it.

## TL;DR

No newer official Qwen3.8-27B weights exist, and no newer Unsloth quant exists. The largest gap on our side is the
llama.cpp build. b11026 has a draft-mtp bug on hybrid models: every follow-up turn re-prefills the whole
previous answer. It was fixed in **b11259**. Builds after that also add probabilistic MTP draft sampling. The
settings side is mostly already right. One thing changes how to read the Swift result: the "fewer thinking
tokens" fine-tunes measure their savings against `reasoning_effort=xhigh`. We run `medium`, which adds no
instruction at all, so their savings largely do not apply to us. That is the likely reason Swift did not
reproduce.

---

## 1. GGUF builds and quant quality

**No new official or Unsloth weights. [verified via HF API]**
- `Qwen/Qwen3.8-27B` weights were uploaded on 2026-08-13. Commits after that date only touch the README and LICENSE.
  - There is no "2510"-style refresh and no official 27B Coder model. A search found nothing either.
  - An official `Qwen/Qwen3.8-27B-FP8` exists, but it is useless on RDNA2 under llama.cpp.
  - https://huggingface.co/Qwen/Qwen3.8-27B
- `unsloth/Qwen3.8-27B-GGUF` commit history:
  - 2026-08-14: "Add Unsloth style chat template".
  - 2026-08-19: Dynamic V3.0 re-upload. The guide says "Qwen3.8-27B GGUFs now use Unsloth Dynamic V3.0 for 10% more accuracy at the same size". https://unsloth.ai/docs/models/qwen3.8
  - 2026-08-20: "Upload imatrix".
  - Nothing has been uploaded since. https://huggingface.co/unsloth/Qwen3.8-27B-GGUF
- **Action: check that our file is the V3.0 upload.** We deployed on 2026-08-19, the same day as the re-upload.
  - Current `Qwen3.8-27B-UD-Q6_K_XL.gguf`: size **25,299,061,664 B**, sha256 `701d8fa9ed214ab21bfc130cd2a7df19ca89bbef7713e2dfb19f3c63696aa917`, last commit 2026-08-19T13:15Z.
  - If our file's sha256 differs, it is the pre-V3 quant.
- Unsloth also ships a separate `MTP/mtp-Qwen3.8-27B-Q4_0.gguf` (1.37 GB) standalone MTP head. We already use the in-GGUF heads, so this does not matter for us.

**KL divergence against BF16 (AtomicChat, llama.cpp b10454, 4.97M-token mixed corpus, 4096-token chunks) [community]**
Source: https://huggingface.co/AtomicChat/Qwen3.8-27B-GGUF (collection "Updated Aug 25")

| file | size | KLD | top-1 |
|---|---|---|---|
| Q8_0 | 28.9 GB | 0.00064 | 98.92% |
| AD-Q6_K | 25.0 GB | 0.00107 | 98.67% |
| **unsloth UD-Q6_K_XL** | 25.9 GB | **0.00110** | (not given) |
| unsloth UD-Q5_K_XL | n/a | 0.00437 | n/a |
| unsloth UD-Q4_K_XL | 17.9 GB | 0.00955 | n/a |
| ggml-org Q4_K_M | 19.0 GB | 0.01470 | n/a |

Other measurements:
- The Unsloth discussion has another KLD table, but it covers only sub-6-bit quants against a Q8_0 reference. It has no Q6 row. https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/discussions/49
- Quesma (2026-08-26) ran Terminal-Bench 2.1 (89 tasks, xhigh) and found Q4_K_M "Matches BF16". https://quesma.com/blog/qwen38-27b-quantizations-benchmarked/
- Our own sweep (memory, 2026-08-19) measured UD-Q4_K_XL 45.81, UD-Q6_K_XL 43.13 and Q8_0 40.35 t/s.

**Conclusion:** UD-Q6_K_XL is already within about 0.0005 KLD of Q8_0. Q8_0 would cost about 12% decode for a
quality gain we could not measure. A Q4/Q5 quant would gain about 6% speed for 4-9x the KLD. **Keep UD-Q6_K_XL.**

Other quant families:
- **GSQ-RCO** (`ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF`, Apache-2.0) is non-uniform 2.5-3.5 bpw. It targets small VRAM. Its IQ3_S scores LiveCodeBench v6 85.71, equal to BF16 [vendor].
  - There is an open llama.cpp issue #30302, "GSQ-RCO MTP Causes Black Screen of Death".
  - It is not useful at our VRAM size.

## 2. Fine-tunes and variants

| variant | licence | claim | independent evidence | verdict |
|---|---|---|---|---|
| **ThinkingCap-Qwen3.8-27B** (bottlecapai, 2026-09-23) https://huggingface.co/bottlecapai/ThinkingCap-Qwen3.8-27B | **PolyForm Small Business 1.0.0 + personal-use grant** (not Apache), gated access | [vendor] at **xhigh**: "Macro average 86.6 / 85.8" accuracy, thinking tokens "15,735 / 12,144" (-37.2%). LiveCodeBench v6 "91.14 / 91.21", Terminal-Bench 2.1 "75.84 / 75.28" (only -10.7% tokens) | [community] Reddit Aider run, mirrored 2026-09-24 (Q8_0, xhigh, 2 runs): ThinkingCap "27.1% / 77.6% / 7,436" median tokens vs Qwen3.8-27B "27.1% / 77.6% / 12,547". https://prismix.dev/news/6b86e882d4c8 | Conditional. Worth testing only if coding moves to xhigh. The GGUF keeps the MTP head. Its own Q6_K scored **-7.0 pp on AA-LCR** (long-context, p=0.065), so do not use it for RAG. |
| **Swift / Swift 1.5** (UkisAI) | custom; paid above $1M ARR | "up to 58.5% fewer thinking tokens" [vendor] | We rejected it on 2026-09-17: token counts within 0.3% at **medium**. MindStudio (2026-09-19) saw Swift use **more** output tokens, 60,572 vs 11,744, in one agentic task. https://www.mindstudio.ai/blog/qwen3-8-27b-swift-vs-original-thinking | Stays rejected. See the xhigh/medium explanation in section 3. |
| Qwen Pi | n/a | "medium setting matches the base model's x-high accuracy on Terminal Bench using about 41% fewer output tokens" [vendor via video summary] | none; "not been shown to generalize beyond the Pi harness". https://daily.dev/posts/which-is-the-best-qwen3-8-27b--2vptpl6qs (2026-10-04) | Not worth it. |
| DFlash2 drafter (z-lab) | Apache-2.0 | see section 5 | see section 5 | see section 5 |

**Key insight for reading the token-saving claims:**
- ThinkingCap, Swift and the prismix test all compare at the template default **xhigh**.
- We run **medium**, and the template injects nothing for medium (see section 3).
- Artificial Analysis, via Qubrid (updated 2026-08-31): xhigh scores "52" on 160M output tokens; medium scores "44" on 75M; non-reasoning scores "35" on 26M. https://qubrid.com/blog/qwen38-27b-api-the-complete-developer-guide
- So medium already removes about half of xhigh's tokens. A fine-tune that trims about 37-58% off xhigh should be expected to save little against medium. This fits our Swift result. It is my inference and has not been measured for ThinkingCap at medium.

## 3. Official settings (model card + chat template)

Sources:
- https://huggingface.co/Qwen/Qwen3.8-27B (Apache-2.0)
- https://unsloth.ai/docs/models/qwen3.8
- Template I read: `chat_template.jinja` from Qwen/Qwen3.8-27B (uploaded 2026-08-13) [verified].

**Sampling**
- Thinking: temp 1.0, top_p 0.95, top_k 20, min_p 0.0, **presence_penalty 0.0**, repetition_penalty 1.0.
- Non-thinking: temp 0.7, top_p 0.80, top_k 20, min_p 0.0, **presence_penalty 1.5**, repetition_penalty 1.0.
- The card says presence can be set "between 0 and 2", and higher values can cause "language mixing".
- Our router already injects 1.5 for nothink. Confirm that thinking aliases send 0.0, not llama.cpp's default.

**Max output**
- 262,144 tokens for reasoning and 131,072 for the final response.

**reasoning_effort** [verified in the template]
- Allowed values are `xhigh` (default), `medium` and `low`. Any other value raises an exception.
- xhigh injects into the system prompt: "Reasoning effort is set to xhigh. Please think carefully through the task, validate key assumptions, consider plausible alternatives, and prioritize correctness, consistency, and clarity in the final answer."
- low injects: "Reasoning effort is set to low. Keep your thinking brief and focused, moving directly to the conclusion without unnecessary elaboration."
- **medium injects nothing**, so it is the model's natural behaviour. Implicator (2026-08-17) called it "a silent no-op". https://www.implicator.ai/qwen-3-8-27b-xhigh-reasoning-default/
- The card warns that low effort in multi-turn agentic tasks can cause "insufficient analysis" and retries.

**Hard thinking cap**
- `thinking_budget_tokens` / `reasoning_budget_tokens` per request, plus a `--reasoning-budget N` server flag.
- Both are already present in b11026 [verified].
- This is a runaway guard for RAG and agent turns, which is our known failure mode with 29-34K-token RAG contexts.

**preserve_thinking**
- On by default in the template. It keeps reasoning from all past turns; `False` keeps only the turns after the last user message.
- llama.cpp `--reasoning-preserve` is also on by default.
- Keep both on for agentic use. They keep the earlier prompt prefix stable, which matters because prefix reuse on a hybrid model goes only through checkpoints (see section 4).
- Turning it off rewrites history after every new user message. [inference, unmeasured]

**Tool calls**
- XML format: `<tool_call><function=NAME><parameter=P>...</parameter></function></tool_call>`.
- Unsloth's template adds "Developer Role Support" and "Tool calling improvements: Makes parsing nested objects".

**Template issues**
- Without `--jinja` and the shipped template, stops break. DEV, 2026-08-14: https://dev.to/purpledoubled/run-qwen-38-27b-locally-real-gguf-sizes-the-kv-cache-trick-and-the-template-trap-114j
- Open llama.cpp issue #30056 (2026-10-06): tool-call grammar fails with about 60 tools ("MAX_REPETITION_THRESHOLD"). Relevant if opencode exposes a large MCP tool set.

**YaRN**
- The native context is 262,144. YaRN uses `factor` 4.0 for 1M, and the card suggests "2.0 for 524,288".
- The card warns: "static YaRN" "can affect performance on shorter texts".
- Our 393,216 context is fine only while it is split across 3 slots (131K each). **Never give a single slot more than 262,144 without YaRN.**

## 4. llama.cpp after b11026

b11026 = `b49650adb`, 2026-09-17. Today the latest is **b11577** (2026-10-12), 551 commits later. Each line below gives the first tag that contains the change [verified via `git tag --contains`].

| change | first build | relevance to us |
|---|---|---|
| **#29638 "common : stop accepting draft tokens at EOG"** (merged 2026-09-29) | **b11259** | **High.** It fixes issue #28049: "with draft-mtp on a hybrid model, every follow-up request re-prefills the previous answer" (Qwen3.8-27B: "Previous answer reused 0 of 8" before, "12 of 13" after). https://github.com/ggml-org/llama.cpp/issues/28049 , https://github.com/ggml-org/llama.cpp/pull/29638 . b11026 is affected. Each agent or chat turn currently re-prefills the previous answer at about 560 t/s (a 5K-token answer is about 9 s). |
| **#27694 `--spec-draft-sampling probabilistic`** (2026-10-02; default stays `greedy`) | **b11368** | Medium. It uses rejection sampling for draft-mtp. On RTX 5090 at temp 0.5-1.0, "Qwen3.6-27B, draft-mtp +3.40%" accepted length and "+3.23%" throughput. Gains grow with temperature, and thinking runs at 1.0. Rejection sampling runs on the CPU. One reported crash came from combining it with `--backend-sampling` and `--parallel 2`. https://github.com/ggml-org/llama.cpp/pull/27694 |
| #30087 GDN kernel: four state columns per warp (shared CUDA/HIP code) | b11494 | Low-medium. On RTX 4090 with Qwen3.8-27B: pp512 "3075.24 -> 3243.60 +5.5%", tg128 "+0.1%". No HIP numbers [unverified on RDNA2]. |
| #26004 server keeps context checkpoints across slot save/restore | b11506 | Low (we don't use slot save). |
| #29019 preserve batch order for spec-decode layer inputs (validated with tensor split + Qwen3.8 MTP) | b11307 | Correctness. |
| #29856 gather recurrent states once so the reserve covers every split; #29799 recurrent-memory assert fix; #29793 / #30217 meta (tensor-split) AllReduce and host-view fixes | b11326-b11375+ | Stability for `-sm tensor` on hybrid models. |
| #29294 fix tensor split for fused qkv with uneven K/V heads | b11176 | Correctness. |
| **#30159 default port changed 8080 -> 9931** | b11521 | **Breaking for ad-hoc bench servers.** Our units pass `--port` explicitly (`scripts/51-lxc-amd.sh:545`). |
| #30210 chat API refactor; several jinja fixes | b11531 | Re-run tool-call smoke tests after upgrading. |
| #30103 CUDA/HIP FA tile race fix | b11545 | Not RDNA2 (it fixes the MMA kernel, found on gfx1201). |

**Not in mainline:**
- **#27558 "HIP: Expand Q5_K and Q6_K tile widths for RDNA2"** (open, 2026-08-22; the author tests on a **Radeon Pro V620**).
  - It is a 22-line MMQ config change.
  - On one V620 (ROCm 7.14), qwen35 27B Q6_K: pp512 "384.91" -> "414.32" (+7.6%), pp2048 "379.06" -> "409.68".
  - This is prefill only. https://github.com/ggml-org/llama.cpp/pull/27558
- **#27210 `--spec-type draft-mtp-adaptive`** (open; last updated 2026-09-11).
  - It suggests `--spec-draft-n-max 12` and says not to use it below 7.
  - Community result on 2x R9700, ROCm tensor split: overall "67.5" vs "59.0" for fixed n-max 2 (+14%); code "78.7" vs "65.1". https://github.com/sudoingX/qwen38-mtp (sweeps/radeon.md, 2026-09)
- #29353 "cuda, hip: GDN chunked kernel" (open): about +7-14% prefill on CUDA and "+6.75%" on Strix Halo.
  - The branch is `gdn-mma-prefill`, so it probably needs MMA/WMMA, which gfx1030 lacks [unverified].
- #29463 `LLAMA_CKPT_OFFSETS` configurable checkpoints (open): this helps RPC, not us.
- #24785 recurrent shrink/expand for prompt cache (open since June).

**cache-reuse is not an MTP problem.**
- `tools/server/server-context.cpp` (HEAD, around line 1330) turns cache_reuse off whenever `llama_memory_can_shift()` is false. Hybrid/recurrent memory cannot shift [verified].
- So `--cache-reuse 1024` will never work for Qwen3.8, with or without MTP. Drop it so nobody mistakes it for a fix.
- Prefix reuse comes from context checkpoints and `--cache-ram`:
  - `--ctx-checkpoints` defaults to 32 per slot.
  - `--cache-ram` defaults to 8192 MiB; we use 16384.
- The EOG fix above is what actually makes multi-turn reuse work.

## 5. Other speed levers

- **DFlash2 drafter**: `z-lab/Qwen3.8-27B-DFlash2-GGUF`; Q8_0 is 2.0 GB; Apache-2.0. https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2-GGUF
  - llama.cpp support (#27342) was merged 2026-08-27, so it is **already in b11026** [verified].
  - [vendor] H200/SGLang, concurrency 1: HumanEval "214.6 (3.11x)" vs MTP "151.9 (2.20x)".
  - [community] RTX 3090 llama.cpp (2026-08-29): DFlash2 n-max 4 "+51.9%" vs MTP n-max 2 "+59.8%". https://hackmd.io/@thc1006/qwen3-8-27b-speculative-decoding-rtx-3090.md
  - [community] 7900 XT Vulkan: "31.0 -> 57.7" t/s, with no comparison against MTP.
  - It has not been tried with HIP plus `-sm tensor` [unverified]. Expected gain over MTP is about zero.
- **ik_llama.cpp**: its README says "The only fully functional and performant compute backends are CPU" and CUDA, and "Please do not enter issues related to ROCm". https://github.com/ikawrakow/ik_llama.cpp. **No.**
- **vLLM on gfx1030**: there is no upstream support (likely unsupported, and I did not find any).
  - The only option is a community fork image, `exaroot/gfx1030-vllm-rdna2-extras`. It needs ROCm 7.14, which we cannot run.
  - Its own numbers on 2x V620: "Qwen3.8-27B-AWQ-vd TP=2 28.5" t/s single-stream, which is slower than our 47-49. https://hub.docker.com/r/exaroot/gfx1030-vllm-rdna2-extras. **No.**
- **stew675/llama-cpp-rdna-boosts** (adaptive MTP, chunked GDN, hybrid all-reduce): targets "RDNA3 / RDNA3.5 / RDNA4" only. https://github.com/stew675/llama-cpp-rdna-boosts. **No** (only the adaptive-MTP idea carries over, via #27210).
- **HIP AllReduce** (#27825, 2026-09-15) is **already in b11026** [verified].
  - On Linux it tries NCCL/RCCL first, then the internal path (which needs P2P), then falls back to the butterfly.
  - The 2x R9700 row used `pci=disable_acs_redir` (P2P) plus `GGML_CUDA_ALLREDUCE=internal`.
  - Check which path our chat unit logs at startup. Whether P2P works for the V620s inside the LXC is unknown [unverified].

## 6. Ranked recommendations

### Worth testing on our hardware
1. **Upgrade llama.cpp to b11577 (at least b11259)** in a side-by-side worktree, as in the b11026 runbook.
   - Expected gain: removes the re-prefill of the previous answer on every turn under draft-mtp, saving seconds to tens of seconds per agent turn.
   - Measure: compare the "prompt processed" token counts and time-to-first-token on turn 2+ of an opencode session; then wall-clock on the hidden-test agentic eval; then check Aider is unchanged.
   - Diff `--help` first. Watch the port 9931 default and the chat refactor.
2. **`--spec-draft-sampling probabilistic`** (needs b11368 or later).
   - Expected gain: +2-4% decode in thinking mode (temp 1.0). Quality should be unchanged (rejection sampling is lossless in distribution).
   - Measure: MTP acceptance and t/s on Aider Python-34. Do not combine it with `--backend-sampling`.
3. **MTP depth sweep, n-max 2/3/4, on our actual traffic** (thinking coding vs nothink RAG).
   - Community optimum depends on card and workload: 3090 n-max 2 "+59.8%" vs n-max 3 "+52.3%"; on the XTX ROCm, code climbs to n-max 4.
   - Expected gain: about +/-5%.
   - Measure: medians of 3 runs, because the community noise band is about +/-2 t/s.
4. **Cherry-pick #27558 (RDNA2 Q6_K/Q5_K MMQ tiles)**.
   - Expected gain: about +5-8% prefill (V620 author: +7.6% pp512 on Qwen3.8 Q6_K); decode unchanged.
   - Measure: `test-backend-ops -o MUL_MAT` must pass, then pp512/pp2048 at 22K and 122K.
5. **reasoning_effort A/B**.
   - xhigh vs medium on Aider. Use a larger n than 34, because our past 73.5% vs 70.6% is noise. AA reports +8 index points for about 2.1x tokens.
   - low vs medium on the hidden-test agentic eval. Watch for retries.
   - Also add `thinking_budget_tokens` on the RAG and agent aliases as a runaway guard.
6. **ThinkingCap-Qwen3.8-27B**, only if step 5 makes xhigh the coding default.
   - Compare ThinkingCap@xhigh vs base@xhigh vs base@medium on Aider (pass@2, completion tokens, MTP acceptance).
   - Licence is fine for a personal lab. Keep stock for RAG.
7. **AllReduce path check** (`GGML_CUDA_ALLREDUCE=internal` vs default) plus a P2P check.
   - Expected gain: unknown, possibly a few % decode with `-sm tensor`. Cheap to check.
8. **Adaptive MTP (#27210, build from the PR branch)** and **DFlash2** (no rebuild; `-md` plus `--spec-type draft-dflash`).
   - Low priority. Community results show at best about +14% (adaptive, RDNA4) and zero (DFlash2 vs MTP on a 3090).

### Not worth it
- **Q8_0 / UD-Q8_K_XL**: KLD 0.00064 vs 0.00110 and already measured 12% slower. **Q4/Q5**: 4-9x the KLD for about 6% speed.
- **GSQ-RCO quants**: they target small VRAM, and there is an open MTP bug (#30302).
- **New official weights or a "2510" refresh**: none exist. The Unsloth quant has not changed since 2026-08-19 (just verify our sha256).
- **Swift 1.5, Qwen Pi**: vendor-only claims, measured at xhigh. Swift already failed on our hardware at medium.
- **vLLM (gfx1030)**: no upstream support. The community fork needs ROCm 7.14 and measured 28.5 t/s.
- **ik_llama.cpp**: no ROCm support.
- **rdna-boosts fork**: RDNA3+ only.
- **#29353 chunked GDN**: probably needs WMMA.
- **`--cache-reuse`**: structurally impossible on hybrid memory. Remove the flag.
- **`--spec-draft-p-min` > 0**: stays 0. This is our measured result and matches the community's "fast card" pattern.
- **NVFP4 / FP8 variants**: Blackwell / FP8 hardware only.
