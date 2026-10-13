# Qwen3.8 optimization, larger models, and image/video — research synthesis (2026-10-12)

Four parallel web/Hugging Face research threads, checked against our hardware (2x V620 32 GB gfx1030,
ROCm 7.2.3, llama.cpp b11026) and our own measurements. Detailed reports with sources sit next to this file:
`qwen38-optimize.md`, `qwen38-flashnext-larger.md`, `vision-recognition.md`, `image-video-generation.md`.
Facts checked locally on 2026-10-12 are marked **[checked]**; everything else is from the cited sources.

## 1. Is there a better Qwen3.8-27B?

No better checkpoint or quant exists. Our UD-Q6_K_XL is the current Unsloth file (blob sha256 `701d8fa9…aa917`
**[checked]**); official weights unchanged since 2026-08-13; no Coder or refresh release. Q8_0 measures KLD 0.00064 vs
0.00110 for Q6_K_XL and costs ~12% decode — not worth it. The only fine-tune with independent support,
ThinkingCap-Qwen3.8-27B, saves thinking tokens at **xhigh**; we run **medium**, which already roughly halves thinking
(this is also why the Swift claim did not reproduce for us).

**The real gains are in llama.cpp, not the model:**

| # | Change | Expected effect | How to measure |
|---|---|---|---|
| 1 | Upgrade b11026 → b11577 (≥ b11259 for PR #29638) | Fixes "every follow-up turn re-prefills the previous answer" under MTP on hybrid models (issue #28049: reused 0/8 → 12/13). Saves ~9 s per 5K-token previous answer, every agent turn | Turn-2+ prompt tokens and time-to-first-token in an OpenCode session; hidden-test agentic eval wall time; Aider unchanged |
| 2 | `--spec-draft-sampling probabilistic` (≥ b11368) | +2-4% decode in thinking mode | MTP acceptance + t/s on Aider Py-34; do not combine with `--backend-sampling` |
| 3 | Cherry-pick PR #27558 (RDNA2 Q5_K/Q6_K MMQ tiles, author tested on a V620) | +7.6% prefill, decode unchanged | `test-backend-ops -o MUL_MAT`, then pp512/pp2048 |
| 4 | MTP n-max 2/3/4 sweep on real traffic | ±5% | medians of 3 runs |
| 5 | reasoning_effort xhigh vs medium on a larger coding set | AA: +8 index points for ~2.1x tokens | Aider with n > 34 (34 cases cannot separate 73.5 from 70.6) |

Housekeeping: `--cache-reuse 1024` can never work on this hybrid model (the server disables it whenever memory
cannot shift) — drop the flag; reuse comes from context checkpoints + `--cache-ram`. b11521 changes the default port
to 9931 (our units pass `--port`, ad-hoc bench servers may not); b11531 refactors the chat API (re-run tool-call smoke
tests). Already correct **[checked]**: think/nothink sampling match the model card (presence 0.0 / 1.5); normal-mode
chat is 262,144 ctx on one slot = native limit, no YaRN needed.

Not worth it: vLLM (no upstream gfx1030; fork needs ROCm 7.14, 28.5 t/s), ik_llama.cpp (no ROCm), RDNA3+ forks,
FP8/NVFP4 builds, Q4/Q5, p-min > 0.

## 2. Flash-Next and larger Qwen3.8 models

**Family:** the only open-weight Qwen3.8 models are the 27B, Flash-Next (180B, 6B active) and a 2.4T-A95B (out of
reach). Max/Flash/Omni are API-only. There is no 32B/72B/122B/Coder size to step up to.

**Flash-Next is worth one more bounded test — the August result was partly an AMD bug.** Our 51.5 t/s prefill matches
issue #27856 (an attention step fell back to CPU on AMD past ~1K tokens); the fix #27466 "ROCm: add radix TOP_K for
long rows" is in b11026 **[checked]** but not in build 10687 we tested on. A quant now fits the 45 GB bar we set:
ISTA-DASLab IQ2_XS, 39.2 GB VRAM + 28.8 GB n-gram table in RAM, average 89.16 vs 93.12 BF16. At full precision it
leads the 27B (AA index 40 vs 34, Terminal-Bench 25% vs 6%) but trails on long context (AA-LCR 80 vs 82) and writes
~1.6x more tokens. Against it: every fitting quant is heavily degraded (Unsloth UD-Q2_K_XL KLD 0.225 vs ~0.001 for our
27B), MTP is not in mainline for it, and open AMD issue #29149 is a hang when part of the model sits in RAM.

**Go/no-go (fixed now):** IQ2_XS, layer split. Go only if prefill ≥ 300 t/s at 16K, generation ≥ 35 t/s, and it beats
the 27B on the hidden-test agentic eval by ≥ 2 tasks at ≤ 1.5x wall time per task. A tie is a no-go. If fast but tied,
one last try with IQ3_XXS (47 GB), then stop. It cannot coexist with embed + rerank (it takes ~39 GB of 64 plus the
chat must be unloaded), so at best it is a swap profile.

**Higher 27B quants:** skip. Q8_0 is within noise of Q6_K_XL and ~13% slower; BF16 (54.7 GB, ~22 t/s) does not fit
next to embed/rerank with context.

## 3. Image and video recognition — we already have it

Qwen3.8-27B is natively multimodal (images and video). The projector is in the repo we already use:
`mmproj-F16.gguf` 928 MB (also BF16; ggml-org Q8_0 0.63 GB). Production disables it with `--no-mmproj`; the Qwen3.8
cache on 151 holds no projector **[checked]**. b11026 supports image input as OpenAI `image_url` parts and native video
as `input_video` parts (needs ffmpeg in LXC 151), with flags `--image-min-tokens/--image-max-tokens`, `--video-fps`,
`-mmdev` **[flags checked in --help]**. Card scores include OSWorld-Verified 84.3, OmniDocBench 91.1, RealWorldQA 85.9;
the older Qwen3.6-27B floor is MMMU 82.9 / OCRBench 89.4 / VideoMME 87.7.

Our runbook note "MTP doesn't support --mmproj" (`day-2-ops.md:911`) is reportedly out of date (MTP+image crashes
#22867/#23233 fixed May 2026) — verify in step 1 before editing it.

Risks on our cards: open issue #28608 — on HIP, large images (2048 px) crash in the vision encoder's flash-attention
kernel (reported on gfx1100; assumed for gfx1030). Mitigate with `--image-max-tokens` ~1280-1600 or
`--no-mmproj-offload`. Pass the F16 projector explicitly (auto-selection would pick BF16, which RDNA2 lacks). Put it on
GPU1 with `-mmdev` (GPU0 has ~3.5 GB free at peak). Video cost ≈ frames/2 × ceil(w/32) × ceil(h/32) tokens: 60 s at
1 fps 640x360 ≈ 7.2K tokens (~13 s prefill); at the default 4 fps 1080p it is ~245K — cap fps and resolution.

Router changes needed (`router-app.py`): count image tokens in admission control; tolerate list-type system content
(currently a TypeError → 500 in `should_strip_thinking`); block or proxy `http(s)` image URLs (llama-server fetches
them itself → SSRF from LXC 151); rewrite `video_url` → `input_video`; 4xx or auto-swap when the active profile has no
vision; cap the request body size. Clients: AnythingLLM already sends `image_url` data URIs; OpenCode needs
`"attachment": true` and `"modalities": {"input": ["text","image"], "output": ["text"]}` on the model entry.

**Path:** (1) a `qwen3.8-vision` swap profile with `--mmproj <F16> -mmdev <GPU1> --image-max-tokens 1280 --video-fps 1`,
measure MTP decode stays within ~5%, peak VRAM per GPU, and images at 512-2048 px plus a short clip; score 40-60
hidden-answer items from our own screenshots/diagrams/scans (character error rate for OCR). (2) If clean, enable on
the default profile, apply the router fixes, update OpenCode config. Fallback: `--no-mmproj-offload`, or a small
separate VLM (Qwen3.5-4B ~5.2 GB with projector, PaddleOCR-VL) on GPU1 or a CPU worker.

## 4. Image and video generation

**Engine:** stable-diffusion.cpp built from source with HIP for gfx1030 on ROCm 7.2.3 (its prebuilt ROCm binaries
need ROCm 10.1). Same stack as our llama.cpp; its server already exposes OpenAI-style `/v1/images/generations` and
`/v1/images/edits`, and it can place the text encoder and the diffusion model on different cards. ComfyUI is a poor
fit here: its Qwen path allows only bf16/fp32, RDNA2 has no bf16, so it would run fp32 without fast attention.

**Models (open weights):** Qwen-Image-2512 and Qwen-Image-Edit-2511 (20B MMDiT, Apache-2.0, Qwen2.5-VL-7B text
encoder, 4-step Lightning LoRAs; GGUF Q6_K 16.8 GB, Q8_0 21.8 GB). Qwen-Image-2.1 + 8-step Turbo (7B, generation and
editing in one, Q8_0 7.7 GB) is **research/non-commercial only**. Qwen-Image 2.0/3.0 are API-only.

**Estimated speed (not measured anywhere on gfx1030):** 20B + Lightning-4 at 1024²: ~1-2 min per image; 2.1-Turbo:
~0.75-1.5 min; one edit ~1.5-3.5 min; full 50-step 20B ~20-40 min (impractical). **Video:** open weights stop at
Wan 2.2; Wan2.2-TI2V-5B or Wan2.1-1.3B short 480p clips ~3-15 min as async jobs with chat unloaded; 720p 5 s
~45-90 min (overnight only).

**VRAM plan:** the 20B image models and any video need the chat model unloaded (the existing swap webhook, like the
redteam auto-switch); 2.1-Turbo might fit beside chat if ~7 GB + 12 GB are free per card. The router must serialise
image jobs and chat reloads (an image server holding VRAM would break a chat load). Generation holds both cards at the
180 W cap for minutes; sd.cpp moves daily — pin the commit. Known gfx1030 issues have workarounds (NaN/black images:
scale flags; VAE OOM: tiling; Vulkan worse than HIP for Qwen). Clients: Open WebUI calls an OpenAI image endpoint
directly; AnythingLLM's `/img` custom base URL is unconfirmed; for OpenCode a small MCP tool calling the router.

**First test:** build sd.cpp (`-DSD_HIPBLAS=ON -DGPU_TARGETS=gfx1030`) in LXC 151 during a swap window; Qwen-Image-2512
Q6_K + Lightning-4 at 1024², text encoder on one card and DiT on the other, flash attention on/off; record s/step,
VRAM per card, junction temperature.

## 5. Recommended order

1. **llama.cpp b11577 upgrade** (side-by-side worktree, the b11026 runbook) — the one change that speeds up every
   agent turn; then probabilistic drafting and the #27558 cherry-pick in the same build cycle.
2. **Vision on qwen3.8** — swap-profile A/B, then default-on with the router fixes. No new model, < 1 GB.
3. **sd.cpp + Qwen-Image-2512 Lightning** — first image-generation measurement in a swap window.
4. **Flash-Next IQ2_XS** — one bounded test against the go/no-go above.
5. Later: reasoning_effort study on a larger coding set; Wan 2.2 short clips if image generation proves useful.
