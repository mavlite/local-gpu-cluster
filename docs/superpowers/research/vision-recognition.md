# Image and video recognition on the local cluster (research, 2026-10-12)

Question: can we add image and video understanding (describe, OCR, screenshots, diagrams, short clips), ideally with the Qwen3.8-27B we already run?

**Short answer: yes, probably with the model we already run.** Qwen3.8-27B is natively multimodal, and the unsloth repo we pull from already ships the vision projector. Our pinned llama.cpp b11026 has image input, video input (through ffmpeg) and the Qwen frame merge. I checked these against the b11026 source. Turning it on mostly means replacing `--no-mmproj` with an explicit `--mmproj`. The real risks are ROCm/HIP bugs in the vision encoder (large-image crashes, MUL_MAT errors) and VRAM on GPU0. A one-session A/B test can settle them.

Legend: **[verified]** = I read it in the primary source (model card, HF file API, or the llama.cpp source at tag `b11026`). **[secondary]** = blog, forum or search-summary claim that I did not confirm. **[inferred]** = my own reasoning or arithmetic, not measured.

---

## 1. Is Qwen3.8-27B natively multimodal? Projector files and benchmarks

**Yes: native image and video input.** [verified]
- Qwen model card (https://huggingface.co/Qwen/Qwen3.8-27B, citation dated August 2026, collection "Updated Aug 14") says it is "a native vision-language model that understands images and videos", with "Native support for image and video understanding, from STEM diagrams and documents to hour-scale videos." It lists the type as "Causal Language Model with Vision Encoder".
- Only two Qwen3.8 open-weight sizes exist: 27B (Image-Text-to-Text) and 2.4T-A95B (https://huggingface.co/collections/Qwen/qwen38). There is no small Qwen3.8 VL sibling. [verified]
- Video defaults on the card: "By default, `fps=2` and `do_sample_frames=True`". For hour-scale video, set `longest_edge` to 469,762,048 (about 224k video tokens). That fps setting is configurable in vLLM only. [verified]

**Projector (mmproj) files on Hugging Face** [verified via the HF tree API, 2026-10-12]

| Repo | File | Bytes |
|---|---|---|
| unsloth/Qwen3.8-27B-GGUF (**our repo**) | `mmproj-BF16.gguf` | 931,146,432 |
| unsloth/Qwen3.8-27B-GGUF | `mmproj-F16.gguf` | 927,607,488 |
| ggml-org/Qwen3.8-27B-GGUF | `mmproj-Qwen3.8-27B-BF16.gguf` / `mmproj-Qwen3.8-27B-Q8_0.gguf` | 0.93 GB / 0.63 GB |
| bartowski/Qwen3.8-27B-GGUF | `mmproj-Qwen3.8-27B-bf16.gguf` / `-f16.gguf` | 0.93 GB each |

- Our weights (`Qwen3.8-27B-UD-Q6_K_XL.gguf`, 25,299,061,664 bytes) sit next to the projector in the same repo. Because `51-lxc-amd.sh` launches with `--hf-repo`, llama.cpp would **auto-download a projector** if `--no-mmproj` were removed.
- At b11026, `find_best_sibling` in `common/download.cpp` picks the projector closest in quant bits. BF16 and F16 tie against Q6, and the first match wins, so I expect **BF16** to be chosen. [inferred from source] RDNA2 has no native BF16, so pass `--mmproj <path>/mmproj-F16.gguf` explicitly (see risks).
- Encoder type: `qwen3vl_merger` (`PROJECTOR_TYPE_QWEN3VL`), a 27-layer ViT of about 888 MiB in BF16. [secondary: the omphalos issue #160 summary] The projector type is in b11026 `tools/mtmd/clip-impl.h`. [verified]

**Vision benchmarks on the Qwen3.8-27B card** [verified, raw README]. The card has **no MMMU, DocVQA, OCRBench or Video-MME rows.** It reports:

| Benchmark | Qwen3.8-27B | Qwen3.6-27B (same table) |
|---|---|---|
| OSWorld-Verified (computer use) | 84.3 | 63.9 |
| WebArena-Verified | 64.8 | 48.8 |
| AndroidWorld | 81.9 | 70.3 |
| SWE-MM | 38.6 | 25.7 |
| Vision2Web | 62.9 | 45.0 |
| MathVision (no CI / with CI) | 90.0 / 94.6 | 85.1 |
| BabyVision (no CI / with CI) | 65.7 / 85.6 | 28.9 |
| CharXiv RQ (no CI / with CI) | 83.7 / 90.2 | 78.4 |
| OmniDocBench 1.5 | 91.1 | 89.4 |
| RealWorldQA | 85.9 | 84.1 |
| ERQA | 65.5 | 62.5 |

The missing rows can be bounded with **Qwen3.6-27B**, which Qwen3.8 beats on every shared row. The Qwen3.6-27B card (https://huggingface.co/Qwen/Qwen3.6-27B) [verified] reports: "MMMU ... 82.9", "MMMU-Pro ... 75.8", "OCRBench ... 89.4", "VideoMME(w sub.) ... 87.7", "VideoMMMU ... 84.4", "MLVU ... 86.6". Treat these as a **lower-bound proxy**, not Qwen3.8 figures. BenchLM lists MMMU 82.9 for Qwen3.6-27B, not 3.8. [secondary]

The agentic rows matter for us. OSWorld 84.3 and AndroidWorld 81.9 mean the 27B is strong at GUI/screenshot grounding, which is the core of a "read this screenshot" use case.

---

## 2. llama.cpp multimodal (libmtmd) at b11026

All of the following is **[verified]** by reading the source at tag `b11026` (`tools/server/server-context.cpp`, `server-common.cpp`, `common/arg.cpp`, `tools/mtmd/*`).

### Image input through the OpenAI API
- `messages[].content[]` parts with `{"type":"image_url","image_url":{"url":...}}` are accepted. The URL can be `data:image/...;base64,`, raw base64, `http(s)://`, or `file://`. `file://` works only with `--media-path`. Formats are whatever stb_image reads (jpeg, png, bmp, gif...), and webp is decoded through ffmpeg.
- **Remote http(s) URLs are fetched by llama-server itself**, with a 10 MB cap and a 10 s timeout (`params.max_size = 1024 * 1024 * 10`). This is an SSRF surface from inside LXC 151; see section 5.
- Without a loaded projector, an image part returns `"image input is not supported - hint: if this is unexpected, you may need to provide the mmproj"`. That is what we get today because of `--no-mmproj`.
- Any `content[].type` other than `text`, `image_url`, `input_audio` or `input_video` returns `unsupported content[].type`.

### Flags present in b11026 (`common/arg.cpp`)
- `--mmproj FILE`, `--mmproj-url`, `--mmproj-auto/--no-mmproj`, `--mmproj-offload/--no-mmproj-offload` (default on), `-mmdev/--mmproj-device DEVICE` ("default: follows --device")
- `--image-min-tokens N` / `--image-max-tokens N`: "only used by vision models with dynamic resolution (default: read from model)"
- `--mtmd-batch-max-tokens N` (default 1024)
- `--video-fps N` (default **4.0**), `--video-timestamp-interval N` (default 5000 ms), `--video-ffmpeg-dir DIR`, `--media-path PATH`

### Qwen-VL token budget (`clip.cpp`, `PROJECTOR_TYPE_QWEN3VL`)
- `hparams.set_limit_image_tokens(8, 4096)`, which is 8 to 4096 tokens per image by default.
- Warmup is sized at `46*46` (2,116) tokens "to avoid OOM on warmup".
- On load the server warns: *"Qwen-VL models require at minimum 1024 image tokens to function correctly on grounding tasks ... try adding --image-min-tokens 1024"*.
- Spatial merge 2 at an assumed ViT patch size of 16 (Qwen3-VL lineage) means one LLM token covers about 32x32 px. So about 1,000 tokens is roughly 1000x1000 px, and the 4,096 cap is roughly 2048x2048. [inferred]

### Video input (**supported**, not just frames)
- PR #24269 "mtmd : add video input support" was merged Jun 8, 2026 (https://github.com/ggml-org/llama.cpp/pull/24269). It calls `ffmpeg` as a subprocess that is "NOT pre-bundled, user need to install it manually". [verified]
- PR #21858 "mtmd: support 'frame merge' for qwen-vl-based models" was merged Jun 6, 2026 (https://github.com/ggml-org/llama.cpp/pull/21858). It uses Qwen's conv3d to fold **2 consecutive frames into one embedding**, which halves video tokens. [verified]
- At b11026, `clip_model_n_temporal_merge()` returns 2 for QWEN3VL. Frames are mergeable "only on frames of the same video", so separate `image_url` frames sent by a client are **not** merged. [verified]
- The API part is `{"type":"input_video","input_video":{"data"|"url": ...}}`. This is llama.cpp's own name. vLLM uses `video_url`, and OpenAI has no video part.
- The build option `MTMD_VIDEO` defaults ON when `LLAMA_SUBPROCESS` is ON, which is the Linux default. `mtmd_helper_support_video()` is simply "has vision", so Qwen3.8 qualifies.
- ffmpeg resamples with `-vf fps=<target>` and emits rgb24. Each frame is then resized like an image within the min/max token limits. There is **no frame-count cap** in the code: frames = duration x fps. The model also gets `"Video:"` plus a timestamp text every 5 s.

### Interaction with our existing flags
- **MTP:** b11026 does **not** disable speculative decoding when a projector is loaded. With a projector loaded, the server only disables `ctx_shift` and `cache_reuse` ("cache_reuse is not supported by multimodal, it will be disabled"). Our README already says cache-reuse is auto-disabled for this arch, so we lose nothing. [verified]
- The note in `day-2-ops.md:911` ("MTP doesn't support `--mmproj`") is **stale**. Earlier MTP+vision bugs were fixed in May 2026 [verified issue pages]:
  - #22867, "MTP + Vision ... slot position corruption and OOM", closed May 10, 2026.
  - #23233, "crash when evaluating images with MTP with Qwen3.6" (GGML_ASSERT in get_rows: the MTP graph read token IDs that image batches lack), closed May 29, 2026 via PR #23643.
  - unsloth issue #5534 (May 18, 2026): "There is no gain in token generation with the vision mmproj loaded". Closed the same day after commit 4699c7e; the reporter said "I'm now getting the speed up". No numbers were given.
  - **No open Sept/Oct 2026 MTP+image issue found for Qwen3.8**, but the search index for those months is thin. Regression-test this ourselves.
- **Tensor split:** the projector is not split. It lands on one device, which "follows --device", in practice the first GPU (GPU0). [verified for the flag; placement is inferred] An Unsloth Studio report (#6415) describes vision models crashing at warmup under `--split-mode tensor` with `GGML_ASSERT(src_ss[0].axis != GGML_BACKEND_SPLIT_AXIS_0)` in ggml-backend-meta.cpp, after which Studio falls back to layer split. [secondary, search summary only] Must be tested on our box.
- **Memory-fit:** b11026 adds the projector's worst-case memory to the fit targets (`mtmd_get_memory_usage`), but **we run `--split-mode tensor`, which skips auto-fit**. Our own comment in `51-lxc-amd.sh` says so. So nothing protects us from an OOM on GPU0.

### VRAM cost [inferred, needs measuring]
- Projector weights are about 0.93 GB (F16) or 0.63 GB (Q8_0), plus a ViT compute buffer that scales with image size and is sized at warmup for 2,116 tokens.
- **Image tokens do not add KV VRAM.** Our KV is pre-allocated for 262,144 tokens, so images only consume context.
- Our measured headroom (day-2-ops.md, 2026-08-19, b10509): GPU0 peaks at 89% (about 3.5 GB free) and GPU1 at about 63% (about 11 GB free). The embed unit was shrunk 2026-10-05 (about -7 GB, per memory), so re-measure first. **Put the projector on GPU1 with `-mmdev ROCm1`** (check the name with `--list-devices`).
- Prefill on our box is about 534-553 t/s (day-2-ops.md). LLM time per image is therefore roughly 2 s per 1,000 image tokens, plus the ViT encode, which is unmeasured on gfx1030.

---

## 3. Alternatives if the 27B route is weak or unstable

GGUF and projector availability checked via the HF tree API on 2026-10-12. [verified sizes]

| Model | GGUF (size) | mmproj | Fits where | Notes |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (already our CPU-worker pick and a swap profile) | UD-Q4_K_M 22.13 GB | BF16/F16 0.90 GB, F32 1.79 GB | CPU workers (24 GB VM: tight with Q4) or GPU swap profile | Native VL. OCRBench 90.0, MMMU 81.7, VideoMME(w sub) 86.6 (Qwen3.6-27B card table) [verified]. Our workers use ik_llama.cpp, where image + MTP broke at ik build 4530 ("prepare_mtp_graph_inputs: Source hidden state size mismatch", fixed by reverting to 4520) [secondary] |
| **Qwen3.5-9B** | Q4_K_M 5.68 GB / Q8_0 9.53 GB | F16 0.92 GB | CPU worker easily; GPU1 spare | MMMU 78.4, OCRBench 89.2, OmniDocBench1.5 87.7, VideoMME(w sub) 84.5, ScreenSpot Pro 65.2 [verified, Qwen3.5-9B card] |
| **Qwen3.5-4B** | Q4_K_M 2.74 GB / Q8_0 4.48 GB | F16 0.67 GB | GPU1 alongside main, or CPU | MMMU 77.6, OCRBench 85.0, ScreenSpot Pro 60.3 [verified] |
| Qwen3-VL-8B / 4B / 32B (Qwen official GGUF) | 8B Q8_0 8.71 GB; 4B Q8_0 4.28 GB; 32B Q4_K_M 19.76 GB | 0.45-1.2 GB | CPU or GPU1 | Superseded by Qwen3.5 small models (natively VL) |
| Gemma 4 E4B (ggml-org) | Q8_0 8.03 GB | Q8_0 0.56 GB, BF16 0.99 GB | CPU / GPU1 | The PR #24269 web UI test model for video; also hit by HIP FA crash #28608 (Gemma4-31B) |
| **PaddleOCR-VL-1.6** (PaddlePaddle official GGUF) | about 0.9B | mmproj about 0.88 GB (v1.5 file) | anywhere | "state-of-the-art score of 96.33% on OmniDocBench v1.6" [verified card claim]. Task prompts `OCR:`, `Table Recognition:`, `Chart Recognition:`; run with `--temp 0` |
| GLM-OCR (ggml-org) | Q8_0 0.95 GB | Q8_0 0.48 GB | anywhere | dedicated OCR |
| HunyuanOCR (ggml-org) | Q8_0 0.58 GB | Q8_0 0.73 GB | anywhere | dedicated OCR |
| dots.ocr (ggml-org) | Q8_0 1.89 GB | Q8_0 1.34 GB | anywhere | layout + OCR |
| DeepSeek-OCR (ggml-org) | Q8_0 3.13 GB | Q8_0 0.45 GB | anywhere | b11026 also has `deepseekocr2.cpp` |

b11026 `tools/mtmd/models/` contains qwen2vl, qwen3vl, gemma4v, paddleocr, deepseekocr, deepseekocr2, dotsocr, hunyuanvl, glm4v, internvl, minicpmv, pixtral and more. [verified directory listing]

**CPU workers (Zen 4 16C, 24 GB):** Qwen3.5-9B Q8_0 with its F16 projector (about 10.5 GB), or Qwen3.6-35B-A3B Q4 (22 GB, very tight). Our measured CPU prefill (memory, 2026-10-04) is 125-170 t/s for Qwen3.6-35B-A3B. One 1,000-token image therefore costs at least 6-8 s of LLM prefill plus a CPU ViT encode. The encode is **unmeasured**, and I expect it to dominate. [inferred] CPU suits batch OCR, not interactive chat.

---

## 4. Video paths that work on our stack

**A. Native `input_video` (preferred).** llama-server runs ffmpeg in LXC 151, samples at `--video-fps`, applies Qwen 2-frame temporal merge, and interleaves timestamps. Requirements: `apt install ffmpeg` in LXC 151 (ffmpeg and ffprobe on PATH, or `--video-ffmpeg-dir`). [verified code path] Untested by anyone on ROCm that I could find. There is an open issue "mtmd video input hangs on Windows" (linked from PR #24269), which doesn't apply to Linux.

**B. Client-side frame sampling.** ffmpeg extracts N frames, which are sent as N `image_url` parts. Frames are not temporally merged across separate images, so this costs **about 2x the tokens** of path A and carries no timestamps unless the client adds text. [verified merge rule; cost inferred] Use it only for clients that can't send `input_video`.

**Token arithmetic** [inferred]. Tokens are about (frames / 2) x ceil(w/32) x ceil(h/32):

| Clip | Settings | Video tokens | Prefill at ~550 t/s |
|---|---|---|---|
| 60 s, 1080p | **defaults** (4 fps, 1920x1080) | 120 pairs x ~2,040 = **about 245k**, which overflows practical context | n/a |
| 60 s, 640x360 | 1 fps | 30 pairs x 240 = **about 7.2k** | about 13 s |
| 5 min, 448x252 | 0.5 fps | 75 pairs x 112 = **about 8.4k** | about 15 s |

Practical limits:
- Set `--video-fps` to 0.5-1 server-wide.
- Have the router or client downscale video to a 448-640 px long edge before sending. There is no per-request fps or size knob in b11026; `--image-max-tokens` also caps frames, but it applies to stills too.
- Clips of 1-5 minutes are realistic. Qwen's "hour-scale" claim (224k video tokens) is not practical at our prefill speed.
- Base64 video bodies are large; see the router limits in section 5.

---

## 5. Client integration

**Router (`scripts/files/router-app.py`, read 2026-10-12).** The router forwards `body["messages"]` unchanged, so `image_url` parts already pass through. It sets no request-size limit (FastAPI/uvicorn have none by default). Gaps to fix:
1. **Admission undercounts.** `_approx_text_from_messages` counts only `type=="text"` parts, so each image adds up to 4,096 uncounted tokens (or `--image-max-tokens`). Add an estimate per image part (decode the header for w x h, or reserve a fixed `--image-max-tokens` per part).
2. **Possible crash.** `should_strip_thinking` calls `NOTHINK_HINT.search(msgs[0].get("content",""))`. If a client sends a **list** system content, that raises TypeError and returns a 500. Our current clients send string system prompts, so this is low priority, but it costs one line.
3. **SSRF.** llama-server will fetch any `http(s)` `image_url` from inside LXC 151 (10 MB / 10 s). Have the router reject non-`data:` URLs, or fetch them itself with an allowlist.
4. **Video naming.** Accept `video_url` (vLLM-style) and rewrite it to llama.cpp's `input_video`. Optionally downscale or trim with ffmpeg in the router before forwarding.
5. **Profile awareness.** If the active profile is coder/devstral (no projector), llama-server errors on images. Return a clean 4xx or trigger the existing swap webhook to `qwen3.8`, the way STRICT_PROFILE_MATCH already does for aliases.
6. **Body cap.** Set an explicit cap, e.g. 25-50 MB, to bound memory.

**AnythingLLM.** We use `LLM_PROVIDER=generic-openai` (scripts/54-lxc-anythingllm.sh:218). Upstream `genericOpenAi/index.js` builds content arrays through `attachmentToContentBlock(attachment, { imageDetail: "high" })`, which emits `{"type":"image_url","image_url":{"url": <data URI>, "detail":"high"}}` (llama.cpp ignores `detail`). [verified, master branch source] It also re-sends past images in chat history (`formatChatHistory(chatHistory, this.#generateContent)`), so long image chats re-prefill each image unless llama.cpp's prompt cache hits. **No AnythingLLM change is needed** beyond the backend accepting images.

**OpenCode.** The config schema (https://opencode.ai/config.json) has per-model `"attachment": boolean` and `"modalities": {"input": ["text","image","video","pdf","audio"], "output": [...]}`. [verified]
- Our `opencode.json` sets neither, so image parts are stripped client-side.
- Issue anomalyco/opencode#20802 ("Custom OpenAI-compatible providers: image file attachments do not reach vision-capable models correctly", closed as stale Aug 9, 2026): the workaround is to add `modalities.input` including `"image"`, then restart OpenCode. [verified issue page; workaround comes from users, not a maintainer]
- A maintainer notes "the completions api doesnt support tools returning images". **MCP tools that return image content will not reach the model** over our chat-completions provider; tools must return text.

**MCP for screenshots/vision.** These use a describe-then-reason pattern: the tool sends the image to a VLM and returns text.
- **jshsakura/mcp-local-vision** (MIT, v0.1.0 Aug 5, 2026, flagged "Stale" on Glama): tools `read_image`, `read_pdf`, `vision_doctor`. Its "External mode (VISION_MANAGED=0)" points at an existing OpenAI-compatible `VISION_SERVER_URL` with `VISION_API_KEY`; its `VISION_MAX_EDGE` defaults to 1568 px. [secondary: Glama listing]
- Others: SecKatie/vision-mcp (`VISION_API_BASE_URL`), opencode-openai-vision-mcp (`VISION_BASE_URL`), Image Parse MCP. [secondary]
- Playwright MCP's `browser_take_screenshot` returns JPEG or PNG; `--caps=vision` adds coordinate tools. [secondary]
- Given our MCP outage history (unpinned deps), writing a ~100-line tool in our own MCP stack that posts to the router may be simpler than adopting a stale third-party server. [opinion]

---

## 6. Recommended path

**Step 0: prerequisites (no downtime)**
- In LXC 151: `llama-server --help | grep -E 'mmproj|image-|video'` confirms the flags in our binary. `llama-server --list-devices` gives the GPU1 device name. `apt install ffmpeg` is needed for video.
- Pre-download `mmproj-F16.gguf` (927,607,488 B) from unsloth/Qwen3.8-27B-GGUF and record its sha256.
- Measure current per-GPU VRAM (`pct exec 151 -- rocm-smi --showmeminfo vram`) at idle and under a long prefill. The embed unit shrank on 2026-10-05, so the 3.5 GB GPU0 figure is stale.

**Step 1: A/B a `qwen3.8-vision` swap profile (maintenance window, since it replaces the chat unit)**
- Flags: identical to `qwen3.8`, except `--no-mmproj` becomes `--mmproj /tank/models/.../mmproj-F16.gguf -mmdev <GPU1> --image-max-tokens 1280 --image-min-tokens 256 --video-fps 1`. Remember the LESSONS.md heredoc trap: verify the live argv with `ps -ef`.
- **Text regression:** run our existing decode/prefill bench with mmproj on vs off. Pass if MTP decode stays within about 5% and acceptance is unchanged. unsloth #5534 says MTP gain once vanished with mmproj loaded.
- **Stability ladder:** square and screenshot-shaped images at 512, 1024, 1280, 1600 and 2048 px; then 2 images in one turn; then a follow-up turn with the image in history (the #23233 pattern); then one 30-60 s `input_video` clip. All with MTP on. Watch for `HSA_STATUS_ERROR_MEMORY_APERTURE_VIOLATION`, `MUL_MAT failed`, or a `ggml-backend-meta` assert. If the tensor-split assert appears, retest with `--split-mode layer` to isolate it.
- **VRAM:** record peak per GPU during the largest image and the video. Pass if GPU0 has at least 1.5 GB free at peak.
- **Latency:** read `timings.prompt_ms`/`prompt_n` from the response, plus the server log lines for image encode time. Target under 5 s for a 1280-token screenshot. [inferred target]
- **Quality:** build a 40-60 item hidden-answer set from our own material: VCF/vSphere UI screenshots, Proxmox/rocm-smi terminal screenshots, architecture diagrams, scanned PDFs/tables, and 5 short clips.
  - Score OCR by character error rate (CER) against ground-truth text.
  - Score screenshots/diagrams by exact-answer questions, graded blind (per our agent-eval-on-hidden-tests lesson).
  - Compare `--image-max-tokens` 1024 vs 1600 to see whether the cap costs accuracy, and compare against PaddleOCR-VL-1.6 on the OCR subset.

**Step 2: make it the default if Step 1 passes**
- Fold the flags into the `qwen3.8` profile and update `scripts/README.md`, `day-2-ops.md:911` (stale "MTP doesn't support --mmproj"), and the mmproj row at `day-2-ops.md:584`.

**Step 3: router changes (TDD, in `router-app.py`)**
- Image-token admission estimate, a `data:`-only URL policy (SSRF), `video_url` to `input_video` rewrite with optional ffmpeg downscale, a 4xx/auto-swap when the active profile has no vision, the list-content guard in `should_strip_thinking`, and an explicit body cap.

**Step 4: clients**
- OpenCode: add `"attachment": true, "modalities": {"input": ["text","image"], "output": ["text"]}` to the qwen3.8 model entries in `opencode.json`, then restart.
- AnythingLLM: test an image upload with no config change.
- Agents: a small describe-then-reason MCP tool (`read_image(path, question)`) that posts to the router as `qwen3.8-nothink`.

**Step 5: fallback if the 27B projector is unstable on gfx1030**
- Option (a): `--no-mmproj-offload` (the ViT runs on the host CPU in LXC 151; slower but confirmed to avoid the ROCm MUL_MAT crash on gfx1201).
- Option (b): a separate small VLM/OCR llama-server, e.g. Qwen3.5-4B Q8 + F16 projector (about 5.2 GB) or PaddleOCR-VL-1.6 (about 2 GB), pinned to GPU1 or a CPU worker, behind a router alias such as `vision`.
- Option (c): enable the projector on the CPU workers' Qwen3.6-35B-A3B for batch OCR. Retest ik_llama.cpp image+MTP first.

---

## 7. Risks specific to ROCm 7.2.3 / gfx1030

1. **HIP flash-attention crash in the vision encoder on large images: highest risk.**
   - llama.cpp #28608 (opened Sep 8, 2026; **open**, labelled stale Oct 11, 2026; https://github.com/ggml-org/llama.cpp/issues/28608). Title: "ROCm/HIP: CLIP vision encoder crashes with HSA_STATUS_ERROR_MEMORY_APERTURE_VIOLATION in flash_attn_tile for large images".
   - Reported on **Qwen3.8-27B with mmproj-BF16**, 2x 7900 XTX (gfx1100), build 10454: "1280px JPEG" works, "1600px JPEG" is borderline, "2048px JPEG" is a fatal crash. The proposed patch skips FA for head dim 72 on HIP.
   - gfx1030 has no WMMA and uses the same tile FA kernels, so **assume we're affected**. [inferred]
   - At b11026 the projector inherits the main model's `flash_attn_type`, and **we can't turn FA off globally** because our q8_0 V-cache needs FA. [verified that the param is inherited; FA requirement from llama.cpp behaviour]
   - Mitigation: cap `--image-max-tokens` at about 1,280-1,600, which roughly matches the 1280 px that worked. Alternatives are `--no-mmproj-offload` or a locally patched clip.cpp.
2. **ROCm `MUL_MAT failed, ROCm error: invalid argument` on the first image** (gfx1201 R9700, Qwen3.8-27B, Aug 19, 2026; https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/discussions/69). The workaround `--no-mmproj-offload` was confirmed by the poster. [verified thread]
3. **BF16 projector on RDNA2.** No native BF16 hardware; the auto-pick would be BF16. Use F16. Risk magnitude is unverified.
4. **Idle GPU pinned at max clock while the projector is loaded on HIP** (#23965, R9700, closed Jun 11, 2026). The fix PR #24237 ("default GPU_MAX_HW_QUEUES=1 on HIP") was **closed unmerged** Jul 15, 2026. If idle power or junction temperature rises after enabling vision, set `GPU_MAX_HW_QUEUES=1` in the unit environment. This matters for our 180 W cap and blower thermal ceiling.
5. **Tensor split plus vision warmup assert** (Unsloth Studio #6415) [secondary]. A fallback to `--split-mode layer` costs about 30% decode (27.2 to 19.1 t/s measured 2026-08-19).
6. **GPU0 headroom.** Tensor split skips auto-fit, and the projector follows `--device` (GPU0) unless `-mmdev` moves it. An OOM would kill the chat unit; the memory note says a shrunk LXC 151 RAM ceiling looks like a GPU fault, so check `pct config 151` too.
7. **Older ROCm gibberish class** (#17797, gfx1151, Qwen3-VL; closed Dec 2025 via PR #17817) was a MUL_MAT_ID f16 kernel bug, not vision-specific. It is a reminder to sanity-check outputs (the CER eval) rather than trust "it didn't crash".
8. **The "//////" repetition reports** (Level1Techs, Aug 21-25, 2026, dual R9700 Qwen3.8) were later attributed by the poster to a VRAM overclock, "a red herring of sorts". Low risk, but watch for degenerate output in the quality set.

---

## Sources
- Qwen3.8-27B model card: https://huggingface.co/Qwen/Qwen3.8-27B (raw README read 2026-10-12)
- Qwen3.8 collection: https://huggingface.co/collections/Qwen/qwen38
- unsloth GGUF repo and file API: https://huggingface.co/unsloth/Qwen3.8-27B-GGUF, https://huggingface.co/api/models/unsloth/Qwen3.8-27B-GGUF/tree/main
- ggml-org / bartowski Qwen3.8 GGUF: https://huggingface.co/ggml-org/Qwen3.8-27B-GGUF, https://huggingface.co/bartowski/Qwen3.8-27B-GGUF
- Qwen3.6-27B card (proxy benchmarks): https://huggingface.co/Qwen/Qwen3.6-27B; Qwen3.5-9B card: https://huggingface.co/Qwen/Qwen3.5-9B
- llama.cpp source at tag b11026: https://github.com/ggml-org/llama.cpp/tree/b11026 (server-context.cpp, server-common.cpp, common/arg.cpp, common/common.h, common/download.cpp, tools/mtmd/{clip.cpp,mtmd.cpp,mtmd-helper.cpp,CMakeLists.txt})
- llama.cpp multimodal docs: https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md; server README: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
- Video PR #24269: https://github.com/ggml-org/llama.cpp/pull/24269; frame-merge PR #21858: https://github.com/ggml-org/llama.cpp/pull/21858
- MTP+vision: https://github.com/ggml-org/llama.cpp/issues/22867, https://github.com/ggml-org/llama.cpp/issues/23233, https://github.com/unslothai/unsloth/issues/5534
- ROCm: https://github.com/ggml-org/llama.cpp/issues/28608, https://github.com/ggml-org/llama.cpp/issues/23965, https://github.com/ggml-org/llama.cpp/pull/24237, https://github.com/ggml-org/llama.cpp/issues/17797, https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/discussions/69, https://forum.level1techs.com/t/dual-r9700-llama-cpp-rocm-tensor-split-working-p2p-on-proxmox-qwen3-8-27b/254183
- Tensor split + vision: https://github.com/unslothai/unsloth/issues/6415 (search summary only)
- PaddleOCR-VL-1.6 GGUF: https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF
- AnythingLLM Generic OpenAI provider: https://github.com/Mintplex-Labs/anything-llm/blob/master/server/utils/AiProviders/genericOpenAi/index.js
- OpenCode schema: https://opencode.ai/config.json; issue: https://github.com/anomalyco/opencode/issues/20802
- mcp-local-vision: https://glama.ai/mcp/servers/jshsakura/mcp-local-vision (repo https://github.com/jshsakura/mcp-local-vision)
- Blog confirming that unsloth Qwen3.8 + mmproj-BF16 "just works" (CUDA/unspecified): https://www.emcken.dk/programming/2026/09/10/giving-local-ai-with-llamacpp-vision/
