# Image generation, editing and video on 2x V620 (gfx1030, ROCm 7.2.3)

Researched 2026-10-12. **[V]** = verified on the source page today. **[U]** = unverified, a third-party claim, or my own estimate.
No gfx1030 or V620 measurement of Qwen-Image exists in any source I found. **Every speed figure for our cards below is an estimate.** Treat it as something to measure, not a fact.

---

## 0. Bottom line

- **Most likely to work: stable-diffusion.cpp (sd.cpp) built from source with HIP for gfx1030 against our ROCm 7.2.3.** It is the same ggml/HIP stack our llama.cpp already runs on these cards. Its OpenAI-shaped server has `/v1/images/generations` and `/v1/images/edits`. It can place the text encoder and DiT on different cards, or split one module across both.
- **ComfyUI/PyTorch on gfx1030 works, but it is the slow path for Qwen.** ComfyUI excludes fp16 for Qwen-Image. RDNA2 has no bf16. So it computes Qwen-Image in **fp32**, and gfx1030 also gets no flash or SDPA attention (details in section 5).
- **Model choice:**
  - **Qwen-Image-2.1-Turbo (7B, 8 steps)** is the fastest Qwen and small enough to share the GPUs with the chat model. Its licence is **research and non-commercial only**.
  - **Qwen-Image-2512 / Qwen-Image-Edit-2511 (20B) with the 4-step Lightning LoRA** are Apache-2.0. They are about 3x heavier, and in practice the chat model has to be unloaded.
- **Video:** short 480p clips with Wan2.2-TI2V-5B or Wan2.1-1.3B are feasible as batch jobs taking minutes each. 720p, 5 s and 14B are impractical except as overnight jobs.

---

## 1. The Qwen-Image family (as of 2026-10-12)

| Model | Released | Size | Text encoder | VAE | Licence | Notes |
|---|---|---|---|---|---|---|
| Qwen-Image | 2025-08-04 [V] | 20B MMDiT [V] | Qwen2.5-VL-7B [V via sd.cpp/Comfy file layout] | `qwen_image_vae.safetensors`, 254 MB [V] | Apache-2.0 [V] | Strong at text rendering, "especially for Chinese" [V] |
| Qwen-Image-Edit / Edit-2509 | 2025-08 / 2025-09-22 [U date] | 20B | Qwen2.5-VL-7B and its vision tower | same | Apache-2.0 | 2509 added multi-image input (1-3 images) [U] |
| **Qwen-Image-Edit-2511** | 2025-12-23/24 [V via GIGAZINE 2025-12-24] | 20B | same | same | Apache-2.0 | "mitigating image drift, improved character consistency, integrated LoRA capabilities" [V]. sd.cpp needs `--model-args qwen_image_zero_cond_t=true` or "editing quality will degrade significantly" [V] |
| **Qwen-Image-2512** | Dec 2025, "the December update" [V] | 20B [V] | Qwen2.5-VL-7B | same | Apache-2.0 [V] | Reduces the "AI-generated" look and improves text [V]. The card example uses 50 steps, true CFG 4.0 [V] |
| Qwen-Image-2.0 / 2.0 Pro / 3.0 | 2026-02-10 / 2026-07 | ~7B DiT + 8B encoder [U] | — | — | **API-only, no weights** [U, several sources agree] | Not usable locally |
| **Qwen-Image-2.1** | **2026-09-20** [V via Comfy blog] | **7B, "32 Single-Stream DiT layers"** [V] | **Qwen3-VL-8B** [V, sd.cpp doc] | its own 64-channel RGBA VAE, 0.68 GB [V/U] | **Qwen Research License** [V]: "FOR NON-COMMERCIAL PURPOSES ONLY", defined as "research or evaluation purposes only" [U, quoted by codersera] | One model does both generation and editing. Up to 10 reference images, native RGBA, 2K output [V/U]. Default 40 steps [V] |
| **Qwen-Image-2.1-Turbo** | Sep 2026 | 7B [V] | Qwen3-VL-8B | 2.1 VAE | Qwen Research [V] | **8 steps, CFG 1** with a fixed sigma schedule [V]. In sd.cpp: `--sigmas qwen-image-2.1-turbo --steps 8 --cfg-scale 1.0` [V] |

**Lightning (distilled few-step) LoRAs, lightx2v/ModelTC, Apache-2.0 [V]:**
- Qwen-Image: 4-step V1/V2 and 8-step V1/V1.1/V2.
- Edit: 4/8-step.
- Edit-2509: 4/8-step, 2025-10-09.
- **Edit-2511: 4-step, 2025-12-22**, plus a fused fp8 model.
- **2512: 4-step, 2026-01-01**.

Recommended settings are 4 or 8 steps with true CFG 1.0 [V]. Source: https://github.com/ModelTC/Qwen-Image-Lightning

**Quality positioning [U, vendor/press claims]:**
- The Qwen-Image line is best-in-class among open models for text and typography in images.
- 2.0/2.1 reportedly match or beat Nano Banana in Alibaba's own arena.
- I found no independent benchmark of 2.1 against FLUX.2-dev or Qwen-Image-2512.
- Known sd.cpp issue for 2.1: "periodic 8px/4px grid artifacts at native 2K resolutions (CUDA and Vulkan, Q8_0 and Q6_K)" (#2041, open) [V title].

### GGUF sizes [V, from the HF file listings]

| Weights | Q4_K_M | Q6_K | Q8_0 | BF16 |
|---|---|---|---|---|
| Qwen-Image-2512 (unsloth) | 13.2 GB | 16.8 GB | 21.8 GB | 40.9 GB |
| Qwen-Image-Edit-2511 (unsloth) | 13.2 GB | 16.9 GB | 21.8 GB | 40.9 GB |
| Qwen-Image-2.1 (leejet) | 4.2 GB (Q4_K) | 6.0 GB | 7.69 GB | ~14.2 GB |
| Qwen2.5-VL-7B text encoder (Comfy) | — | — | fp8 9.38 GB | 16.6 GB |
| Qwen3-VL-8B text encoder | ~5 GB (Q4_K_M) [V via blog] | — | — | 17.5 GB [U] |

Add these to the totals:
- The vision projector (mmproj, ~1.1-1.3 GB) for editing.
- The VAE (0.25 or 0.68 GB).
- About 2-4 GB of compute buffers at 1024². sd.cpp's auto-fit reserves 2 GiB per diffusion/text-encoder module and 1 GiB for the VAE [V].

---

## 2. Running on gfx1030 with ROCm ≤ 7.2

### Platform facts [V]
- The Radeon PRO V620 (gfx1030) is a **supported target in ROCm 7.2.3**, but only on "Ubuntu 24.04.4 and Ubuntu 22.04.5". It is not marked deprecated.
  - Source: https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.3/reference/system-requirements.html
  - So **`HSA_OVERRIDE_GFX_VERSION=10.3.0` is not needed for the V620.** It is only for gfx1031/1032 consumer cards.
- PyTorch for ROCm 7.2.3:
  - AMD's validated Docker tags are `rocm/pytorch:rocm7.2.3_ubuntu24.04_py3.12_pytorch_release_2.9.1` (and 2.8.0 / 2.7.1) [V].
  - The pip route is `--index-url https://download.pytorch.org/whl/nightly/rocm7.2` [V].
  - rocm/pytorch images list gfx1030 among their targets [V via search summary of Docker Hub].
- **No hardware bf16 on RDNA2.** An AMD engineer on vLLM #38107 (2026-03-26), about **2x V620**, wrote: "RDNA 2 does not have hardware support for bfloat16 (hardware bf16 was introduced in RDNA 3)". In that thread a bf16 Qwen3-8B ran at about 4 tok/s on our exact cards [V].
  - Lesson: **never let a diffusion stack default to bf16 on these cards.**

### stable-diffusion.cpp (recommended engine)

**Model support [V]:**
- Qwen-Image since 2025-10-12.
- Qwen-Image-Edit/2509 since 2025-10-13.
- Edit-2511 documented.
- **"Day-0 support for Qwen-Image-2.1"** on 2026-09-20.
- Wan2.1/2.2 since 2025-09-06.
- LTX-2.3 and LTX-2.5.
- Z-Image and FLUX.2-klein.

**HIP build [V, docs/build.md]:**
- Configure with `-DSD_HIPBLAS=ON -DGPU_TARGETS=gfx1030 -DAMDGPU_TARGETS=gfx1030` using clang from ROCm.
- The docs say: "you have to set `$GFX_NAME` manually to `gfx1030` for consumer RDNA2 cards".
- **The prebuilt release binaries are for ROCm 10.1.0** (`...-rocm-10.1.0.zip`, master-959, 2026-10-12) [V]. They are **unusable for us** because the driver caps us at ROCm 7.2. **Build from source**, the same way as llama.cpp.
- A Vulkan prebuilt exists too. However, an sd.cpp contributor with an **RX 6800 (gfx1030)** found Qwen-Image "noticeably worse on Vulkan than on ROCm", and the output was deterministic (PR #910, 2025-10-25) [V]. **Use HIP.**
- That same thread is the best evidence that **Qwen-Image runs on gfx1030 under ROCm** in sd.cpp [V, indirect].

**Multi-GPU placement [V, docs/backend.md]:**
- `--backend te=<dev0>,diffusion=<dev1>,vae=<dev1>` puts the text encoder on one card and the DiT on the other.
- `"diffusion=dev0&dev1"` does a layer split across devices. `--split-mode row` gives tensor-parallel-like row splitting.
- `--max-vram dev0=N,dev1=M` sets per-device budgets.
- `--offload-to-cpu` / `--params-backend cpu|disk` keeps weights in RAM and stages them in segments.
- The docs claim CPU offload saves VRAM "without reducing generation speed". **[U]** Over PCIe, expect some slowdown for a 20B model.
- **Note:** auto-fit "is not a hard physical VRAM cap", because driver contexts are not counted [V]. Leave headroom next to llama.cpp.

**Precision and NaN handling [V, docs/troubleshooting.md]:**
- If you get black or white images or NaNs, add `--linear-scale 0.0078125 --attn-scale 0.0078125`.
- `--attn-scale` works only with `--diffusion-fa`/`--fa`.

**Server API [V, examples/server/routes_openai.cpp]:**
- `GET /v1/models`
- `POST /v1/images/generations` (JSON: `prompt`, `n`, `size` "WxH", `output_format` png/jpeg/webp)
- `POST /v1/images/edits` (multipart: `image[]`, optional `mask`, `prompt`, `n`, `size`)
- The API **always returns `b64_json`** and does not read `response_format`.
- Extra sampler parameters go in the prompt as `<sd_cpp_extra_args>{...}</sd_cpp_extra_args>`.
- There is also an A1111-style `/sdapi/v1/...` API and a native async `/sdcpp/v1/...` API. Models are fixed at startup.
- Known server footgun [V, Ruibal 2026-09-23]: without `--lora-model-dir` and `--hires-upscalers-dir`, the server scans the container root on every request and fails on `/proc`.

**Speed features [V]:**
- `--diffusion-fa`.
- Step caches (`--cache-mode cache-dit | easycache | spectrum`).
- For 2.1, a prefix KV cache: text and reference-image K/V are computed once. It costs about 2 GiB in f16 for a 4096-token prefix [V]. This makes multi-reference edits much cheaper per step.

**Known slow case [V]:** sd.cpp #893 (2025-10) on a **gfx900 (Vega, no WMMA)** 16 GB card, Q3_K_S.
- Qwen-Image: **"39s/it sd.cpp 24s/it ComfyUI"**.
- Qwen-Image-Edit: 134 s/it in sd.cpp vs 64 s/it in ComfyUI.
- sd.cpp flash attention gave black images on gfx900.
- This is the closest pre-WMMA AMD data point. gfx1030 has about 1.5x Vega's FP16/FP32 throughput, and ggml FA already works for us in llama.cpp on these cards. **Re-measure FA on/off on our cards.**

### ComfyUI on ROCm, gfx1030 (read from current ComfyUI master code, 2026-10-12 [V])

**`comfy/model_management.py`:**
- `AMD_RDNA2_AND_OLDER_ARCH = ["gfx1030", ...]`.
- PyTorch SDPA attention is auto-enabled only for gfx90a/942/950/1100/1101/115x/117x/120x. **gfx1030 falls back to split or sub-quadratic attention.**
- `pytorch_attention_enabled_vae()` returns False on AMD: "enabling pytorch attention on AMD currently causes crash when doing high res".
- `should_use_bf16`: "RDNA2 and older don't support bf16".
- No FP8 compute on gfx1030. FP8 weights work as storage only.

**`comfy/supported_models.py`:**
- `QwenImage` and `QwenImage21` have `supported_inference_dtypes = [torch.bfloat16, torch.float32]`. **fp16 is excluded.**
- Combined with the above, on gfx1030 ComfyUI computes Qwen in **fp32** (manual cast from bf16/fp8/GGUF storage). That is half the RDNA2 fp16 rate, with larger activations.
- **[U]** `--fp16-unet` forces fp16. Expect overflow or NaN risk, which is presumably why fp16 is excluded.
- Wan accepts fp16, so ComfyUI is less penalised for Wan.

**Other ComfyUI notes [U, third-party guides]:**
- For GGUF use city96 `ComfyUI-GGUF`.
- If you get black images, diagnose with `--cpu-vae` / `--fp32-vae`.
- One report of AMD "DynamicVRAM" corruption after eviction and reload on 2.1 suggests `--disable-dynamic-vram`.
- **There is no Qwen-Image speed report for RDNA2 anywhere.** Older Flux-dev numbers on RDNA2 under ZLUDA/ROCm were 10-20 s/it at 1024² (6800 XT, Aug 2024) [U].

### Speed estimates for one V620 at 1024² (**[U] — my estimates, not measured**)

**Method:**
- 1024² is about 4096 image tokens, so one DiT step costs about 2 × params × 4.1k FLOPs plus attention.
- V620 peak is about 20 TFLOPS fp32 / 40 TFLOPS fp16, lower under the 180 W cap.
- Cross-check: the gfx900 figures above (Vega peak is about 60-65% of the V620's).
- **Cheap calibration:** one DiT step takes roughly as long as our llama.cpp **prompt processing** of 4096 tokens on a dense LLM of similar size (pp rate × size ratio). Our existing qwen3.8-27B pp512 numbers give a better prior than mine.

| Workload (sd.cpp HIP) | per step | steps | est. per image |
|---|---|---|---|
| Qwen-Image-2512 Q6_K/Q8 (20B) + Lightning-4 (CFG 1) | 10-25 s | 4 | **~1-2 min** (+ ~10-20 s encoder/VAE) |
| same, Lightning-8 | 10-25 s | 8 | ~2-3.5 min |
| same, full 50 steps, true CFG 4 (2 passes/step) | 10-25 s ×2 | 50 | **~20-40 min — not practical** |
| Qwen-Image-Edit-2511 + Lightning-4 (reference tokens ≈ double the sequence) | 20-45 s | 4 | ~1.5-3.5 min |
| **Qwen-Image-2.1-Turbo Q8 (7B), CFG 1** | 4-9 s | 8 | **~45 s-1.5 min** |
| Qwen-Image-2.1 base, 40 steps, CFG 6 (prefix cache) | 5-12 s | 40 | ~5-10 min |

- ComfyUI on the same card: expect roughly **1.5-2x slower** for Qwen because of fp32 compute and no flash attention [U].
- Splitting the text encoder and DiT across two cards does not speed up sampling. It only frees VRAM.
- A row split across both cards might, but our PCIe-only link limits it [U].

---

## 3. Video

**Open-weight status [U, multiple consistent sources]:**
- **Wan open weights stop at 2.2.** Wan 2.5/2.6/2.7/3.0 are API-only. The 2026-08-07 HF release was Wan2.2-Animate-2-14B (Apache-2.0).
- I found no Qwen video model with open weights.
- **LTX-2.5** (Lightricks, 2026-08-11 [U]): open weights, 22B DiT + Gemma-4-12B text encoder, free under $10M ARR. Community GGUF Q4_K_M is about 11.5-15 GB, and the text encoder is 15-26 GB. sd.cpp supports it (2026-08-20 [V]).

**Wan family [V, HF card]:**
- Wan2.2 A14B is MoE: two 14B experts, 27B total, 14B active.
- Wan2.2 **TI2V-5B** is dense. It has a 4×16×16 VAE (4×32×32 with patchify), and does 720p at 24 fps (1280×704). The card says: "can generate a 5-second 720P video in under 9 minutes on a single consumer-grade GPU", with a 24 GB minimum (RTX 4090). Apache-2.0.
- Wan2.1 1.3B and 14B are also available.
- QuantStack and city96 GGUFs exist for all of them. sd.cpp supports them all (`-M vid_gen`).
- sd.cpp warns: "Wan models vae requires really much VRAM! … please try tae instead" [V].
- The 4-step lightx2v LoRAs work with A14B [V].

**Feasibility on V620 [U, estimates]:**
- A 4090 does about 80 dense fp16 TFLOPS with fp32 accumulate, plus fast flash attention. A capped V620 does about 30 TFLOPS effective with no FA kernels in PyTorch.
- So expect **5-10x 4090 wall time.**

| Clip | Model | Estimate |
|---|---|---|
| 5 s 720p | TI2V-5B | **~45-90 min**. Overnight batch only |
| 33-49 frames at 832×480 | Wan2.1-1.3B or TI2V-5B | **~3-15 min**. Realistic for a "make a short clip" tool |
| 480p, 33 frames | Wan2.2 A14B + lightx2v 4+4 steps, Q4-Q6 (~9-12 GB per expert, swapped in and out) | ~5-15 min |

- Conclusion: **video is practical only as an occasional async batch job with the chat model unloaded.** It is not interactive.
- Long diffusion runs hold the cards at the 180 W cap for tens of minutes. That is a different thermal profile from LLM decode (see risks).

---

## 4. Deployment shape for our cluster

**Where it runs.** Run `sd-server` as another systemd unit in **LXC 151**, which already has ROCm 7.2.3, `/dev/kfd` and the GPUs. The alternative is a new LXC with the same GPU device passthrough and its own ROCm userspace.
- Multiple processes or LXCs can share `/dev/kfd`. **VRAM is not partitioned between them.**
- Build with the same `GPU_TARGETS=gfx1030` toolchain as llama.cpp, and pin the commit. sd.cpp ships about one master build a day.

**Two operating modes.**

| Mode | When | VRAM plan |
|---|---|---|
| **A. Co-resident** (Qwen-Image-2.1-Turbo) | interactive "/img" | Qwen3-VL-8B Q4_K_M encoder (~5 GB + mmproj 1.1 GB) on card 0, DiT Q8_0 7.7 GB + VAE 0.7 GB + ~3 GB compute on card 1. **About 7 GB on one card and 11-12 GB on the other.** Feasible only if `rocm-smi` shows that much free beside the chat model, embedding and reranker. Check with `pct exec 151 -- rocm-smi --showmeminfo vram`. Otherwise use `--params-backend te=cpu` and run the encoder on CPU (a few seconds per prompt on the Ryzen) [U]. |
| **B. Swap** (Qwen-Image-2512/Edit-2511 20B, any video) | batch or occasional | Use the existing swap webhook to unload chat, as the redteam-mode autoswitch already does. Then: DiT Q8_0 21.8 GB on card 1, Qwen2.5-VL-7B Q8 (~8 GB) + mmproj + VAE on card 0. Idle-revert to chat. Swap cost is about 25 s each way (our measurement). |

**Concurrency.**
- Both workloads are compute-bound on the same CUs. Running them concurrently cuts chat tok/s while an image renders. Prefill suffers most [U].
- Treat it like `CHAT_CONCURRENCY=1`: one image job at a time, queued in the router.

**Router.** Add `/v1/images/generations` and `/v1/images/edits` passthrough to sd-server.
- Inject defaults (steps, sigmas, size cap of 1024² or 1328²) via `<sd_cpp_extra_args>`.
- Enforce a scoped key for image routes.
- Convert `b64_json` to a URL if clients need one.
- Decide co-resident vs swap per model alias.

**Clients.**
- **Open WebUI:** native "OpenAI" image engine (`IMAGES_OPENAI_API_BASE_URL`) or ComfyUI engine. Image editing needs the "Edit Image" engine and URL set separately [U, docs disagree].
- **AnythingLLM** (docs v1.17.0): built-in Image Generation under Settings → AI Providers. Providers are OpenAI, OpenRouter, Ollama and **Lemonade**. Lemonade supports `/v1/images/edits`. Users run it with `/img`. It is **unverified** whether a custom base URL for OpenAI or Lemonade can point at our router. Test Lemonade first, since it is a local-server provider.
- **OpenCode / agents:** a ~50-line MCP tool (`generate_image`, `edit_image`) that calls the router and writes the PNG to a path. Several ComfyUI MCP servers exist (joenorton/comfyui-mcp-server, MatthewSnow2/comfyui-mcp-server and others) if we ever go the ComfyUI route. Remember the pinned-dependency lesson.

---

## 5. Known gfx1030 problems and workarounds

| Problem | Evidence | Workaround |
|---|---|---|
| No hardware bf16; bf16 runs as slow emulation | vLLM #38107 on 2x V620 [V]; ComfyUI `should_use_bf16` [V] | Use fp16 or quantized GGUF (sd.cpp). Never default to bf16 |
| ComfyUI runs Qwen-Image in fp32 on RDNA2 (fp16 not in supported dtypes) | ComfyUI source [V] | Prefer sd.cpp. `--fp16-unet` is a NaN-risk experiment [U] |
| No AOTriton flash or mem-efficient SDPA for gfx1030 | ComfyUI enables SDPA only for other arches [V]. AOTriton 0.10 lists gfx950/1201 official, gfx110x/115x experimental [U] | ComfyUI uses sub-quadratic/split attention (memory-heavier at high res). In sd.cpp use ggml `--diffusion-fa` (works on our cards in llama.cpp). `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` helped gfx1151 but is untested on gfx1030 [U] |
| No WMMA or matrix cores | arch | Nothing to do. This is why per-step times are about 5x an RTX 4090 |
| No FP8 compute | arch / ComfyUI [V] | FP8 safetensors are storage only. Prefer GGUF Q6_K/Q8_0 |
| fp16 overflow gives black images or NaNs | sd.cpp troubleshooting [V]; #1020 black images on ROCm RX 7600 XT (Z-Image) [V] | `--linear-scale 0.0078125 --attn-scale 0.0078125`, and halve again if needed. VAE on fp32 or CPU |
| VAE decode OOM (especially Wan) | sd.cpp docs [V] | sd.cpp retries with tiling automatically. `--vae-tiling`, TAE previews, `--temporal-tiling` for video. ComfyUI falls back to tiled decode |
| Vulkan quality degradation on Qwen/Wan (ggml_chunk) | PR #910, RX 6800 [V] | Use HIP, not Vulkan |
| Qwen-2.1 Edit at half speed with references | #2031 (Vulkan) [V] | Prefix cache (default on in current master) |
| 2.1 grid artifacts at 2K | #2041 [V] | Stay at about 1-1.3 MP |
| GGUF very slow in ComfyUI on AMD (one 7800 XT report: 500 s/it, 120 W) | forum [U] | Another reason to prefer sd.cpp |
| ROCm "invalid argument" at first DiT MUL_MAT (gfx1201 Windows, rocBLAS) | #2012 [V] | `GGML_CUDA_FORCE_CUBLAS=1`. Keep in mind if a HIP build misbehaves |

---

## 6. Recommended plan

1. **Spike, about half a day, chat model unloaded (redteam-style swap window):**
   - Build sd.cpp at a pinned commit with `-DSD_HIPBLAS=ON -DGPU_TARGETS=gfx1030` in LXC 151.
   - Run `sd-cli` with **Qwen-Image-2512 Q6_K + Lightning-4step** (Apache-2.0, 20B), `--diffusion-fa`, `--backend te=<gpu0>,diffusion=<gpu1>,vae=<gpu1>`, at 1024².
   - Record s/step with FA on and off, peak VRAM per card (`rocm-smi`), and power and temperature.
   - Fallback if NaNs or black images appear: add the scale flags.
   - Check whether sd.cpp applies the Lightning LoRA to a GGUF base; it should use runtime LoRA mode [U]. If not, use a pre-merged Lightning checkpoint.
2. **A/B:** run **Qwen-Image-2.1-Turbo Q8_0** (8 steps) and Edit-2511 + Lightning-4 the same way. **Decide on the 2.1 licence first:** Qwen Research covers non-commercial "research or evaluation" use only.
3. **Expected results [U]:**
   - About **1-2 min per 1024² image** for 20B + Lightning-4.
   - About **0.75-1.5 min** for 2.1-Turbo.
   - About 1.5-3.5 min per edit.
   - Anything taking more than 5 min per image at 4-8 steps means something is wrong: CPU fallback, bf16 path, or no FA.
4. **If the numbers hold:** run `sd-server` as a systemd unit, add the router passthrough with swap-or-co-reside per alias, and wire up Open WebUI, AnythingLLM and an MCP tool.
5. **Video later:** Wan2.2-TI2V-5B / Wan2.1-1.3B at 480p, short clips, async jobs, swap mode only.

**No-go risks.**
- **VRAM collision at swap time.** If sd-server holds VRAM when the swap webhook reloads the chat model, llama.cpp fails to load and chat goes down. The router must serialise this: image server down or unloaded before chat loads, and the reverse. ggml's managed budget "is not a hard physical VRAM cap" [V].
- **LXC 151 RAM.** `--offload-to-cpu` stages 20-35 GB of weights in RAM, and LXC 151 has a 32 GB floor. Raise its ceiling or use a separate LXC. Remember that the existing ceilings total 54 GB of the host's 128 GB.
- **Thermals and power.** Diffusion keeps both cards at the 180 W cap for minutes at a time, and for video, tens of minutes. Watch junction temperature against the 90%-PWM profile we validated.
- **Licence.** Qwen-Image-2.1/Turbo is non-commercial research only. The Qwen-Image 20B line and Wan2.2 are Apache-2.0.
- **Stack churn.** sd.cpp changes daily and Qwen-2.1 support is three weeks old with open bugs. Pin the commit and keep a known-good binary, as we do for llama.cpp.
- **Hard no-gos:**
  - The official sd.cpp ROCm binaries need ROCm 10.1.
  - Anything that needs FP8 or bf16 compute, or AOTriton FA, on gfx1030.
  - Interactive 720p video.
  - Full 50-step, true-CFG Qwen-Image runs, at about 20-40 min each.

---

## Sources (accessed 2026-10-12)
- Qwen-Image card (2025-08-04, Apache-2.0, 20B): https://huggingface.co/Qwen/Qwen-Image
- Qwen-Image-2512: https://huggingface.co/Qwen/Qwen-Image-2512
- Qwen-Image-Edit-2511: https://huggingface.co/Qwen/Qwen-Image-Edit-2511 ; https://gigazine.net/gsc_news/en/20251224-qwen-image-edit-2511
- Qwen-Image-2.1 / Turbo: https://huggingface.co/Qwen/Qwen-Image-2.1 ; https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo ; https://blog.comfy.org/p/qwen-image-21-in-comfyui-open-weight (2026-09-20) ; https://codersera.com/blog/how-to-run-qwen-image-2-1-locally-2026/ (2026-09-30, licence and size table) [U]
- Qwen-Image-2.0 API-only: https://gigazine.net/gsc_news/en/20260212-qwen-image-2 ; https://www.llmreference.com/model/qwen-image-2.0 [U]
- Lightning: https://github.com/ModelTC/Qwen-Image-Lightning ; https://huggingface.co/lightx2v/Qwen-Image-Lightning
- GGUFs: https://huggingface.co/unsloth/Qwen-Image-2512-GGUF ; https://huggingface.co/unsloth/Qwen-Image-Edit-2511-GGUF ; https://huggingface.co/leejet/Qwen-Image-2.1-GGUF ; https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI
- sd.cpp: https://github.com/leejet/stable-diffusion.cpp (README news; docs/build.md, backend.md, performance.md, troubleshooting.md, caching.md, qwen_image*.md, wan.md; examples/server/routes_openai.cpp) ; issues #893, #2012, #2031, #2041, #2066 ; PR #910
- Qwen-Image 2.1 on sd.cpp/Vulkan (780M iGPU, 20 min/1024²): https://www.alonsoruibal.com/running-qwen-image-2-1-locally-with-stable-diffusion-cpp/ (2026-09-23)
- ROCm 7.2.3 system requirements (V620 supported): https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.3/reference/system-requirements.html ; PyTorch install: https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.3/install/3rd-party/pytorch-install.html
- vLLM bf16 on 2x V620: https://github.com/vllm-project/vllm/issues/38107 (2026-03-25/26)
- ComfyUI source: https://github.com/comfyanonymous/ComfyUI/blob/master/comfy/model_management.py and comfy/supported_models.py (master, 2026-10-12)
- Wan2.2: https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B ; Wan ≥2.5 not open: https://evolink.ai/blog/wan-3-0-release , https://www.atlascloud.ai/blog/tips/is-wan-3.0-open-source [U]
- LTX-2.5: https://runpod.io/blog/ltx-2-5-the-open-weights-world-model-built-for-speed-and-how-to-run-it-on-runpod ; https://www.gradually.ai/en/ai-models/ltx-2-5/ [U]
- RDNA2 Flux speeds (2024): https://cal1.lr.ggtyler.dev/r/FluxAI/comments/1ektvxl/flux_and_amd_gpus [U]
- Open WebUI image gen: https://docs.openwebui.com/features/image-generation-and-editing/comfyui ; AnythingLLM: https://docs.anythingllm.com/features/image-generation , https://docs.anythingllm.com/mcp-compatibility/overview
- AOTriton/gfx1151 experimental flag: https://huggingface.co/axjns/strix-halo-kernels [U]
