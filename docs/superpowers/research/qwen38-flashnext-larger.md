# Qwen3.8 Flash-Next re-evaluation, and larger/higher-quant options (research 2026-10-12)

Scope: web + Hugging Face research only. Nothing here was run on our hardware. Where I give
throughput for our 2x V620, it is an estimate with the assumptions stated, not a measurement.
"UNVERIFIED" marks claims I could not confirm from a primary source.

## TL;DR

- **(a) Flash-Next: worth one cheap re-test, but expect another rejection on quality per token.**
  The 51.5 t/s prefill that killed it on 2026-08-29 was very likely the HIP bug where the QSA
  indexer's TOP_K fell back to the CPU. That was fixed upstream on 2026-08-31, and b11026 includes the fix.
  On other hardware the same model prefills at 300 to 2,500 t/s. A quant now exists that puts
  **39.2 GB** on the GPU, with the 28.8 GB n-gram table in RAM (ISTA-DASLab IQ2_XS). Against that:
  the weights have not changed since 2026-08-27, MTP is still not in mainline, and every quant that
  fits is heavily degraded at the token level. Unsloth's UD-Q2_K_XL has a mean KLD of 0.225 and 82.7% top-1;
  our 27B UD-Q6_K_XL is around 0.001 KLD and about 98.7% top-1. Flash-Next also emits about 1.6x more
  tokens per task (AA: 108k vs 67k). On AA-LCR (long context, which matters for RAG) the 27B is ahead, 82% vs 80%.
- **(b) The Qwen3.8 open family has only three models:** 27B dense, Flash-Next (180B MoE), and
  2.4T-A95B. No 32B/72B/122B/235B, Coder, or Plus model exists in open weights. 2.4T-A95B is out of reach at any
  quant. So "bigger" means Flash-Next or nothing.
- **Higher 27B quants:** Q8_0 (29 GB) or UD-Q8_K_XL (31.5 GB) fit easily, but the quality gain is
  within noise (KLD about 0.0014 to 0.0006). They would cost about 13-20% of generation speed. BF16
  (54.7 GB) does not fit next to embed + rerank with useful context, and would roughly halve
  generation speed. **Recommendation: keep UD-Q6_K_XL.**

---

## 1. Flash-Next since late August 2026

### 1.1 Model status: no new version
- Official repo `Qwen/Qwen3.8-Flash-Next`: createdAt 2026-08-24, **lastModified 2026-08-27T05:03:36Z**,
  license `qwen-community-1.0`, safetensors param count **179,999,981,459**
  (https://huggingface.co/api/models/Qwen/Qwen3.8-Flash-Next, fetched 2026-10-12).
- Architecture per the Unsloth card: "125B total, 6B activated, plus 51B n-gram embedding and 4B MTP",
  48 layers, hidden size 2560, 512 experts with "10 routed and 1 shared". GGUF arch is `qwen4exp`
  (https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF).
- I found no newer Flash-Next checkpoint. The hosted "Qwen3.8-Flash" is API-only
  (https://codersera.com/blog/qwen-3-8-model-lineup-2026/, updated 2026-10-05).

### 1.2 GGUF quants available now
**Unsloth** (`unsloth/Qwen3.8-Flash-Next-GGUF`, repo lastModified **2026-10-06**; adds MTP draft GGUFs,
including "shared" variants). Its KLD table, quoted from https://unsloth.ai/docs/models/qwen3.8-next:

| Quant | GB | Top-1 | Mean KLD | 99.9% KLD |
|---|---|---|---|---|
| UD-IQ1_S | 72.5 | 77.325 | 0.396070 | 7.2126 |
| UD-IQ1_M | 74.5 | 79.691 | 0.314739 | 6.1965 |
| UD-Q2_K_XL | 78.9 | 82.715 | 0.224607 | 4.9121 |
| UD-IQ3_XXS | 82.0 | 85.414 | 0.165120 | 4.0375 |
| UD-Q3_K_XL | 90.0 | 88.315 | 0.106504 | 3.0538 |
| UD-IQ4_XS | 93.7 | 89.554 | 0.083630 | 2.3677 |
| UD-Q4_K_XL | 111.3 | 92.255 | 0.046893 | 1.5468 |
| UD-Q6_K_XL | 169.2 | 94.089 | 0.027091 | 0.8416 |
| Q8_0 | 188.2 | 94.122 | 0.026574 | 0.8118 |

The 1-bit quant "uses 4-bit for the Ngram / PLE". Even Q8_0 sits at 0.027 KLD, which suggests a
reference or PLE floor effect, so read these relative to each other. For comparison, the 27B Q6/Q8
files sit at 0.0005-0.0024 (section 4).

**ISTA-DASLab GSQ-RCO** (https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF; upload
date not shown, collection "Updated 16 days ago", so about late September). These are the only
quants with a clean GPU/RAM split:

| Dir | bpw | Shard 1 (weights) | Shard 2 (n-gram, IQ4_NL) | Total | Task avg (BF16 = 93.12) |
|---|---|---|---|---|---|
| Q2_0 | 2.40 | **37.6 GB** | 28.8 GB | 66.4 GB | 89.07 |
| IQ2_XS | 2.50 | **39.2 GB** | 28.8 GB | 68.0 GB | 89.16 |
| IQ3_XXS | 3.00 | 47.0 GB | 28.8 GB | 75.8 GB | 92.57 |
| IQ3_S | 3.50 | 54.8 GB | 28.8 GB | 83.6 GB | 93.26 |

- Quality, quoted: "All three models exceed the BF16 base on the zero-shot average (100.3% to 101.4%)",
  which they say "should be read as parity". Task avg (AIME25 / GPQA-D / LCB v6) drops about 4 points at
  2.4-2.5 bpw. LCB v6 goes from 87.43 to 81.14 (Q2_0) or 83.43 (IQ2_XS). IQ3_XXS is near parity (92.57).
- Speed (hardware not stated): Q2_0 "Prompt 367.49 t/s, Decode 93.79 t/s"; IQ2_XS 108.19 / 70.30.
- **UNVERIFIED:** whether a `Q2_0` ggml type exists in mainline llama.cpp, and in b11026 in particular.
  I found no source for it. The page says the files "run unmodified in llama.cpp" but cites
  `--lazy-mode on` / `llama serve` flags I could not tie to a build. IQ2_XS and IQ3_XXS are long-standing
  types and safe on b11026.

Other publishers exist: AtomicChat (84.9-110.5 GB), Kubesimplify's write-up of UD-IQ1_S, and
`kingjones777/...ROCmFP4-STRIX_LEAN` (not evaluated). Atomic Chat warns that some "Qwen3.8-Flash-Next"
repos (abliterated/jailbreak) existed with zero files before the weights did.

### 1.3 llama.cpp changes since our 2026-08-29 test
- **#27742** "model: add Qwen3.8-Flash-Next (qwen4exp)": **merged 2026-08-27**, without MTP
  (https://github.com/ggml-org/llama.cpp/pull/27742).
- **Likely cause of our 51 t/s prefill: issue #27856** (opened 2026-08-28, closed 2026-09-07). Quote:
  "without `GGML_CUDA_USE_CUB`, the HIP backend's `supports_op` caps `TOP_K`/`ARGSORT` at ne[0] <= 1024
  columns". The QSA indexer's top-k then runs on the CPU with a GPU→CPU→GPU sync per QSA layer, and
  decode fell from 19.6 to 6.1 t/s past about 1K context on gfx1151.
  Fix: **#27466 "ROCm: add radix TOP_K for long rows", merged 2026-08-31**
  (https://github.com/ggml-org/llama.cpp/pull/27466). Reported results: "Long-context decode collapse gone".
  gfx1151 went from 8.8 to 20.9 t/s at N_KV 2176. That is **two days after our test**, and b11026 (mid-September) should
  contain it. Tested archs were gfx90a/908/1100/1201/1151; **gfx1030 is not mentioned (UNVERIFIED on
  RDNA2)**. The alternative fix is to build with `-DGGML_CUDA_USE_CUB=ON` plus hipcub/rocprim.
  Our test parameters are not recorded here. If the indexer was on the CPU, it fully explains the
  prefill number.
- **#28040** (merged 2026-09-01): O(log n) n-gram prev-token lookup. Decode +3.9% at 55k and +12% at 132k.
  Prefill was unchanged (RTX PRO 6000).
- **#27992, #28130, #28349** (n-gram index, gather-based QSA decode, sparse FA in QSA) were all
  **closed unmerged**. #28349 showed M5 Max pp2048@64k going from 388 to 702 t/s, but did not land.
- **MTP for qwen4exp is not in mainline.** #28623 (draft-mtp head, chunked QSA prefill,
  `LLAMA_PLE_RESIDENT`) was **closed unmerged 2026-09-13**, author: "not complete", CUDA-only testing.
  Unsloth's guide still builds from `danielhanchen/llama.cpp --branch qwen4exp/mtp`. A Reddit mirror says
  ik_llama.cpp merged qwen4exp MTP (#2369); UNVERIFIED, and ik_llama is CUDA/CPU-focused. Unsloth's
  "170 t/s" is RTX PRO 6000 vs a 100 t/s baseline. Community reports on other GPUs are 30-60 t/s.
- **Open HIP issue #29149** (2026-09-19): qwen4exp **livelocks during model load on HIP** (gfx1201,
  ROCm 7.2.4) on the mmap path when tensor overrides to CPU are used. It loads fine on Vulkan. The
  reporter's workaround patch (staging uploads through a 64 MiB buffer) is not submitted. This matters
  directly to us because we would put the n-gram table on the CPU. Try `--load-mode` variants and, if
  needed, Vulkan.
- Open: #30005 "indexer cache duplicated under `-sm tensor`" (2026-10-05). **Use `-sm layer`
  for Flash-Next, not our tensor split.**

### 1.4 Published speed on comparable setups
From https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/discussions/3 and the cited pages:
- 1x RTX 5090 32 GB + 128 GB DDR5-5200, UD-Q2_K_XL, `-ncmoe 37`, PLE on CPU, fork:
  "819 at 2K, 885 at 128K" prefill, 47.2 t/s gen (stock build: 12.2 t/s at 239K).
- 1x RTX 5090 + 2x32 GB DDR5-5600, UD-Q2_K_XL, `-ncmoe 40`: 965 t/s prefill at 32k (#27992 comment).
- 2x RTX 4090D (48 GB), Q4_K_XL, PLE on CPU: 1796.81 pp / 44.66 tg.
- 4x RTX 3090, UD-IQ4_XS: 1029 pp (36K) / 49.8 tg (short) and 26.4 tg (36K).
- AMD: Strix Halo ROCm, with the TOP_K fix: about 426 pp peak at 4K, 284 at 26K, 20.6-15.3 tg
  (https://sleepingrobots.com/dreams/qwen38-flash-next-strix-halo/, 2026-08-29). R9700 + 395 Vulkan:
  532.87 pp512 / 23.25 tg128. No gfx1030/V620 data anywhere.
- Artificial Analysis hosted output speed: 51 t/s (Flash-Next) vs 46 t/s (27B).

### 1.5 Quality vs 27B (full precision, vendor + AA)
- Vendor (Unsloth card): SWE-bench Pro 62.5 vs 61.7; DeepSWE 58.7 vs 42.2; Toolathlon Verified 73.5
  vs 67.1; LiveCodeBench v6 91.9 vs 90.3; GPQA-D 91.7 vs 89.2.
- Artificial Analysis (https://artificialanalysis.ai/models/comparisons/qwen3-8-flash-next-vs-qwen3-8-27b):
  Intelligence Index **40 vs 34** (27B Xhigh); Terminal-Bench 4.0 **25% vs 6%**; SciCode 51% vs 47%;
  **AA-LCR v1.1 80% vs 82%** (27B better at long context); output tokens per task **108k vs 67k**.
- These figures are for full precision. Our fitting quants (2-3 bpw) lose about 4 task points (ISTA) and
  have about 100x the 27B-Q6 KLD (Unsloth). Our own 2026-08-29 result was **parity on 5 hard agentic
  tasks**, consistent with the quant eating the base-model advantage.

### 1.6 Fit and speed estimate for our box
Budget: 64 GB VRAM − about 7 GB (embed + rerank) = **about 57 GB usable**. User threshold is ≤45 GB for weights.
RAM is about 74 GB free.

| Config | GPU | RAM | Fits? | Est. gen | Est. prefill |
|---|---|---|---|---|---|
| ISTA IQ2_XS, all weights on GPU, n-gram in RAM | 39.2 GB + KV/compute ~3-5 GB | 28.8 GB | **Yes (≤45)** | 40-60 t/s | 300-800 t/s if TOP_K fix works on gfx1030 |
| ISTA Q2_0 (if type supported) | 37.6 GB + KV | 28.8 GB | Yes, if loadable | similar or slightly faster | |
| ISTA IQ3_XXS | 47.0 GB + KV | 28.8 GB | Over 45, under 57 | 35-55 t/s | similar |
| Unsloth UD-Q4_K_XL, experts partly in RAM (`-ncmoe`) | ~45 GB | ~66 GB | Tight on RAM | 20-35 t/s | 400-900 t/s (PCIe-bound weight streaming) |

Assumptions: 6B active params; about 2.5-3 GB of weights read per token at 2.5-3 bpw (experts at
low bpw, attention/shared at higher); V620 at 512 GB/s with about 50-60% achieved in llama.cpp MoE decode on
`-sm layer` (cards in sequence, not parallel). That gives a ceiling around 100 t/s, and real llama.cpp
MoE overhead plus host-side n-gram hashing lands at about half. Our measured 32.7 t/s on 2026-08-29 was on
a 57 GB build before #27466 and #28040. Partial-offload gen is bounded by DDR5-3600 dual channel
(about 45 GB/s effective) carrying about 40% of routed-expert reads. Prefill estimates come from
Strix Halo (426) and 5090 hybrid (819-965) data, scaled by judgment. **No RDNA2 measurement exists.**

Without MTP (not in mainline), Flash-Next's per-task wall time vs the 27B (48 t/s with MTP) is about
1.6x tokens ÷ (gen ratio). Even at 50 t/s it is roughly **1.5x slower per task**, versus 6.6x before.

---

## 2. Qwen3.8 family (complete open-weight list)
HF API `author=Qwen&search=Qwen3.8` returns exactly six repos: 27B, 27B-FP8, Flash-Next,
Flash-Next-FP8, 2.4T-A95B, 2.4T-A95B-FP8 (https://huggingface.co/api/models?author=Qwen&search=Qwen3.8).

| Model | Type | Open? | Position vs 27B | GGUF | Fits us? |
|---|---|---|---|---|---|
| Qwen3.8-27B | dense hybrid GDN, 64 layers | Apache 2.0 (2026-08-14) | baseline; AA 34 (Xhigh) | many | in production |
| Qwen3.8-Flash-Next | 180B MoE, 6B active (+51B n-gram, 4B MTP) | Qwen Community License 1.0 (~2026-08-26) | AA 40; SWE-Pro 62.5 vs 61.7 | Unsloth, ISTA, AtomicChat | only at 2-3 bpw |
| Qwen3.8-2.4T-A95B | 2.4T MoE, 95B active | open, "Qwen3.8-Max License" (2026-08-12) | SWE-Pro 67.7; AA 39.89 | n/a for us | **No**: about 450 GB even at ~1.5 bpw |
| Qwen3.8-Max / Max-0902 / Max-Prime, Qwen3.8-Flash, Omni-Flash(-Realtime) | API only | No | Max-0902 AA 45.42 | none | n/a |

No Qwen3.8 32B/72B/122B/235B, Coder, or VL-specific open release exists (27B and Flash-Next already
include vision). The "Qwen3.8 27B MoE" in search results is a community conversion (Whittle MoE
27B-A17.8B, https://vramcalculator.com/qwen3-8-27b-moe/). It is not official, has about the same size and
quality class, so skip it.

## 3. Candidates that fit 64 GB (+ RAM offload)
Only Flash-Next (section 1.6) and higher 27B quants (section 4) qualify. Both archs (`qwen4exp`, Qwen3.8 dense) load on
b11026: #27742 merged 2026-08-27, before b11026. Caveat: HIP load livelock #29149, unverified on gfx1030.

## 4. Higher quants of the 27B
Sizes (https://huggingface.co/unsloth/Qwen3.8-27B-GGUF): UD-Q6_K_XL **25.3 GB**, Q8_0 **29 GB**,
UD-Q8_K_XL **31.5 GB**, BF16 **54.7 GB**.

Quality (no single source covers all three; all are BF16-referenced):
- kingy.ai, quoting AtomicChat (vendor, not independent; refreshed 2026-08-22):
  LM Studio Q6_K 22.43 GB **KLD 0.00242 / 97.93% top-1**; Atomic AD-Q6_K 25.01 GB 0.00107 / 98.67%;
  Atomic **Q8_0 28.89 GB 0.00064 / 98.93%**; Unsloth UD-Q8_K_XL 31.46 GB 0.00051 / 99.07%
  (https://kingy.ai/blog/qwen3-8-27b-best-quantization-gguf/).
- tooltd README ("unsloth old" files): **UD-Q6_K_XL 24.14 GB KLD 0.001380**; UD-Q8_K_XL 29.30 GB
  0.000850 / 98.970% (https://huggingface.co/tooltd/Qwen3.8-27B-IQ4-XS-16GB-VRAM-GGUF/blob/main/README.md).
- HF discussion #65 (2026-08-15): "past 25 GB it stops mattering whose file you grab, Q6 and Q8 from
  any publisher agree within noise" (https://huggingface.co/Qwen/Qwen3.8-27B/discussions/65).
- No published task-accuracy delta Q6_K_XL → Q8_0/BF16 found. At a KLD delta of about 0.0008, expect <0.5
  pt on any task, which is below our eval noise (we saturated at 30/32).

Speed (dense, bandwidth-bound; scale our 48 t/s by size ratio, MTP acceptance assumed unchanged):
- Q8_0: 25.3/29 → **about 42 t/s (-13%)**. Q8_0 MMQ on HIP is efficient, so prefill is about flat (~540-580).
- UD-Q8_K_XL: 25.3/31.5 → **about 38-39 t/s (-20%)**.
- BF16: 25.3/54.7 → **about 22 t/s (-54%)**. 54.7 GB + 7 GB embed/rerank = 61.7 GB, which leaves about 2 GB
  for KV, the MTP head and compute buffers. **It does not fit at our context sizes.** gfx1030 BF16 matmul
  support in HIP llama.cpp is also weak (UNVERIFIED for b11026). Skip.

---

## 5. Ranked recommendation

1. **Benchmark first: Flash-Next ISTA-DASLab IQ2_XS** (39.2 GB GPU + 28.8 GB n-gram RAM; about 68 GB download).
   Run on b11026 with `-sm layer`, `-ub 2048 -b 2048`, n-gram/PLE on the CPU via mmap, and q8_0 KV.
   - Step 0 (cheap, about 10 minutes): `llama-bench -p 512,4096,16384 -n 128` and check decode at depth 8K. If decode collapses past
     about 1K context, the TOP_K CPU fallback is still active on gfx1030. Rebuild with `-DGGML_CUDA_USE_CUB=ON`
     or stop.
   - If load hangs (#29149): try `--load-mode` variants, then Vulkan, then stop.
   - **Go/no-go (all three required, all measured on our box):**
     (a) prefill ≥ 300 t/s at 16K;
     (b) gen ≥ 35 t/s at 16K without MTP;
     (c) on our hidden-test agentic eval it **beats the 27B by ≥ 2 tasks or clears a discriminating
     task the 27B fails**, with wall time per task ≤ 1.5x the 27B. Parity means no-go, as on 2026-08-29.
     Also run one RAG/long-context check, because the 27B leads on AA-LCR.
   - If IQ2_XS passes (a) and (b) but ties on quality, try ISTA IQ3_XXS (47 GB, above the 45 GB line but within our
     ~57 GB usable) as the last attempt, since it is near BF16 on task avg. Otherwise stop.
2. **Optional, low value: 27B Q8_0** (29 GB). Benchmark only if a specific regression is suspected
   to be quant-related. Expect about -13% gen and no measurable eval gain. Go only if the
   discriminating eval improves by ≥ 2 tasks (unlikely).
3. **Skip:** 27B BF16 (doesn't fit, -54% speed); 27B UD-Q8_K_XL (-20% for a 0.0003 KLD gain);
   Flash-Next Unsloth 1-bit/2-bit (KLD 0.22-0.40); Flash-Next UD-Q4_K_XL with expert offload (RAM-bound,
   20-35 t/s est., ~66 GB RAM); Flash-Next MTP forks (unmerged, CUDA-only testing); Qwen3.8-2.4T-A95B
   (impossible); Whittle 27B MoE (community, no upside).

## Sources (fetched 2026-10-12)
- https://huggingface.co/api/models/Qwen/Qwen3.8-Flash-Next
- https://huggingface.co/api/models?author=Qwen&search=Qwen3.8
- https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF and https://huggingface.co/api/models/unsloth/Qwen3.8-Flash-Next-GGUF
- https://unsloth.ai/docs/models/qwen3.8-next
- https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF
- https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/discussions/3 and /discussions/56
- https://blog.kubesimplify.com/running-qwen3-8-flash-next-on-dgx-spark-and-rtx-pro-6000 (2026-08-27)
- https://sleepingrobots.com/dreams/qwen38-flash-next-strix-halo/ (2026-08-29)
- llama.cpp: PR 27742, 27466, 28040, 27992, 28130, 28349, 28623; issues 27856, 29149 (github.com/ggml-org/llama.cpp)
- https://artificialanalysis.ai/models/comparisons/qwen3-8-flash-next-vs-qwen3-8-27b
- https://codersera.com/blog/qwen-3-8-model-lineup-2026/ (updated 2026-10-05)
- https://github.com/QwenLM/Qwen3.8
- https://huggingface.co/unsloth/Qwen3.8-27B-GGUF
- https://kingy.ai/blog/qwen3-8-27b-best-quantization-gguf/
- https://huggingface.co/tooltd/Qwen3.8-27B-IQ4-XS-16GB-VRAM-GGUF/blob/main/README.md
- https://huggingface.co/Qwen/Qwen3.8-27B/discussions/65
- https://vramcalculator.com/qwen3-8-27b-moe/
