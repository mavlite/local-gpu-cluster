# Migration Phase 2: Hardware Swap to 2× V620 + 1× 3060

**Companion to [`migration-ollama-to-llama-cpp.md`](./migration-ollama-to-llama-cpp.md) (Phase 1) and [`local-gpu-cluster-reference.md`](./local-gpu-cluster-reference.md). Phase 1 must be complete and stable before starting Phase 2 — this document assumes llama.cpp + llama-swap is already running at `:11434` on the 3× RTX 3060 cluster.**

![Status](https://img.shields.io/badge/status-draft-orange)
![Risk](https://img.shields.io/badge/risk-high-red)
![Reversibility](https://img.shields.io/badge/reversibility-staged-yellow)
![Revision](https://img.shields.io/badge/revision-3-lightgrey)
![Deployment](https://img.shields.io/badge/deployment-LXC-blue)

End-to-end procedure for replacing two of the three RTX 3060s with two AMD Radeon Pro V620 (32 GB) cards, keeping one 3060 as a CUDA helper. Final pool is 76 GB across mixed-vendor hardware. Architecture is **per-card model assignment** — no cross-card tensor split — driven by current llama.cpp limitations: ROCm + Qwen MoE on gfx1030 is broken (issue [#19880](https://github.com/ggml-org/llama.cpp/issues/19880)), and Vulkan multi-GPU layer-split has open regressions ([#15974](https://github.com/ggml-org/llama.cpp/issues/15974)). Staged rollout in two phases (one V620 first, validate, then the second) so a defective card or a software regression doesn't strand you with no working chat backend.

> **Reality check.** This is a non-trivial hardware project: an external PSU (or aggressive power-limiting), 3D-printed cooling shrouds with 80mm fans, mixed-vendor driver coexistence on Ubuntu 24.04, and two parallel llama.cpp build trees. The migration is reversible at every stage — you keep the 3060s in storage and can restore the Phase 1 config — but it is not fast. Budget two weekends end to end.

---

## Table of Contents

- [1. Decision and scope](#1-decision-and-scope)
- [2. Target architecture](#2-target-architecture)
- [3. Prerequisites](#3-prerequisites)
- [4. Hardware prep](#4-hardware-prep)
- [5. Software prep](#5-software-prep)
- [6. Per-card configurations](#6-per-card-configurations)
- [7. llama-swap configuration](#7-llama-swap-configuration)
- [8. Staged rollout](#8-staged-rollout)
- [9. Parity gates per stage](#9-parity-gates-per-stage)
- [10. Rollback per stage](#10-rollback-per-stage)
- [11. Post-cutover validation](#11-post-cutover-validation)
- [12. Known issues and mitigations](#12-known-issues-and-mitigations)
- [13. What changes vs Phase 1](#13-what-changes-vs-phase-1)
- [Appendix A — systemd units](#appendix-a--systemd-units)
- [Appendix B — file layout](#appendix-b--file-layout)

---

## Revision history

| Rev | Date | Notes |
|---|---|---|
| 3 | May 2026 | LXC deployment alignment with Phase 1 Rev 3. Phase 2 reuses the existing `llama-cpp` LXC container — no new container provisioning, just GPU passthrough reconfiguration (remove pulled-3060 entries, add V620 entries by PCI BDF) plus AMD userspace install inside the container. Build steps, llama-swap config, parity gates, and rollback procedures all use LXC primitives (`lxc snapshot`, `lxc config device`, `lxc exec`, `lxc restore`). AMD kernel driver stays on host; userspace inside container. Power-cap script stays on host (sysfs writes need host scope, not container DRM-passthrough scope). Appendix A split into A.1 (in-container llama-swap unit) and A.2 (host-side power cap). Appendix B split into host filesystem, container filesystem, and LXC snapshot inventory. Stage 1 and Stage 2 procedures rewritten to interleave host-side hardware ops with container-side software work. TurboQuant verification confirms the deferral remains correct: PR #21089 still open and CPU-only, naming is `tbq3_0`/`tbq4_0` (not `turbo3`/`turbo4`), and screenshots showing those flags against `ghcr.io/ggml-org/llama.cpp:server-cuda` are misleading — the official mainline image doesn't accept them. |
| 2 | May 2026 | Multi-reviewer pass + measurement-first power approach. Power planning reframed: Stage 1 measures actual V620 sustained draw before deciding between Option A (external PSU) and Option B (`pp_power_cap` derate); existing PDB is likely sufficient given the user's measured 3060 draw of ~120W vs 170W spec. Stage 1 transitional config rewritten as concrete YAML (rag-qwen25 ↔ coder-36 exclusive on the V620, no layer-split CUDA, slot-3 3060 idle). `coder-192k` further dropped to `coder-160k` for safer Vulkan-allocator margin (~1.9 GB). Reranker integration honest: AnythingLLM has no UI for external reranker; service is hosted on the 3060 but consumption requires a sidecar or MCP-side work — Phase 2 ships the service, not the integration. Reranker GGUF source corrected (`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` — the `Qwen/`-org GGUF repo doesn't exist). Reranker invocation uses the explicit triple `--reranking --pooling rank --embedding`. ROCm install switched to `--usecase=rocm` (workstation deprecated). gfx1030 noted as not in ROCm 7.x supported matrix. AMD installer URL fixed (no more LATEST placeholder). Power-cap script hardened with min/max bounds check, proper glob (`card*` not `card[1-9]`), and a hardened systemd companion unit. AMDVLK→RADV ICD pinning added. Vulkan device-name pinning recommended over numeric IDs. Build-tree git-rev parity check added. ~6× tok/s claim softened to "30–70 tok/s, measure don't predict." §12 known-issues table expanded with new rows from review findings. |
| 1 | May 2026 | Initial Phase 2 draft. Per-card model assignment (no cross-card tensor split). Vulkan on V620s (ROCm broken on gfx1030 for Qwen MoE per #19880). CUDA on 3060. Staged rollout in two stages. External-PSU power plan documented; alternative `pp_power_cap` derate path also covered. Reranker entry on the 3060 added as a Phase 1 → Phase 2 capability gain. `coder-256k` renamed to `coder-160k` because 256K on a single 32 GB V620 has zero margin. |

---

## 1. Decision and scope

### 1.1 What this phase delivers

- **~6× generation throughput on chat models.** Vulkan on a single V620 (RDNA 2, ~512 GB/s memory bandwidth) is roughly an order of magnitude faster than 3-card layer-split CUDA on RTX 3060s (360 GB/s × 1, gated by PCIe 3.0 cross-shard hops). Extrapolating from R9700 (RDNA 4) Vulkan benchmarks of ~178 tok/s on Qwen3.5-35B-A3B, V620 should land near ~90 tok/s per card on the same model. Vs the current ~15 tok/s on layer-split 3× 3060.
- **No more chat-model thrashing.** With `rag-qwen25` permanently resident on V620 #0 and the coder family on V620 #1, AnythingLLM and OpenCode users never see a cold-load pause when context-switching between RAG and agentic coding.
- **Per-stack failure isolation.** Embedder failure can't take down chat; chat failure can't take down RAG. Three independent failure domains.
- **Capacity for a reranker on the 3060.** 12 GB on a fast small CUDA card is currently underutilized as just an embedder host. Adding a Qwen3-Reranker (cross-encoder) improves RAG retrieval quality measurably — separate win from the throughput story.

### 1.2 What this phase does NOT deliver

- **Qwen 2.5 72B Q4 (or any other 70B-class dense model) is still out of reach.** ~40 GB weights don't fit on a single 32 GB V620, and Vulkan multi-GPU tensor split is currently broken. If 70B-class becomes load-bearing, that's a Phase 3 problem requiring different cards or a fixed Vulkan multi-GPU path.
- **Native ROCm performance.** ROCm + Qwen MoE on gfx1030 crashes (#19880). You're on Vulkan even though you've installed two AMD cards. The ROCm install is still useful for `rocm-smi` (monitoring, power capping) but doesn't carry inference.
- **More native context.** `coder-256k` becomes `coder-160k` — at 256K on a single 32 GB V620 you're at exactly 32 GB / 32 GB with zero margin, which is operationally unsafe under any concurrent allocator pressure.

### 1.3 What stays unchanged from Phase 1

- llama-swap binds `:11434`, fronts everything.
- All four AnythingLLM workspace configurations.
- The no-think proxy on `:11435` / `:11436`.
- All three MCP containers.
- LanceDB collections (assuming embedding parity gate from Phase 1 §6.1 still holds — and it must, see §9.1).
- Modelfile-equivalent flags per Phase 1 §10.

---

## 2. Target architecture

### 2.1 Final-state diagram

```text
AnythingLLM (3001) ─┐
OpenCode           ─┤
curl              ──┼──► no-think proxy (11435/11436) ──► llama-swap (11434) ──┐
AnythingLLM embed ──┘                                                          │
                                                            ┌──────────────────┴──────────────────┐
                                                            │ routes by request "model" field      │
                                                            └──────────────────┬──────────────────┘
                              ┌─────────────────────────────────────────────────┼─────────────────────────────────────────┐
                              ▼                                                 ▼                                         ▼
                  ┌─────────────────────────┐                     ┌─────────────────────────┐                 ┌─────────────────────────┐
                  │  Stack A — CUDA / 3060  │                     │  Stack B1 — Vulkan /    │                 │  Stack B2 — Vulkan /    │
                  │  /opt/llama.cpp-cuda    │                     │  V620 #0  (32 GB)       │                 │  V620 #1  (32 GB)       │
                  │  CUDA_VISIBLE_DEVICES=0 │                     │  GGML_VK device 0       │                 │  GGML_VK device 1       │
                  ├─────────────────────────┤                     ├─────────────────────────┤                 ├─────────────────────────┤
                  │  qwen3-embed (1.4 GB)   │  always loaded      │  rag-qwen25  (~19 GB)   │  always loaded  │  coder-36   (~25.5 GB)  │  one
                  │  qwen3-reranker (~2 GB) │  always loaded      │                         │                 │  coder-long (~27.5 GB)  │  at a
                  │  (room for 1–2 helpers) │                     │  --parallel 4           │                 │  coder-160k (~30 GB)    │  time
                  └─────────────────────────┘                     └─────────────────────────┘                 └─────────────────────────┘
                              ▲                                                 ▲                                         ▲
                              │                                                 │                                         │
                          PCIe 3.0 x16                                      PCIe 3.0 x16                              PCIe 3.0 x16
                                                                                                                          
                                                                  3060 powered by T7910 PDB (170 W)
                                                                  V620s powered by external 1200W PSU (300 W each)
                                                                  Add2PSU bridge ties power-on
```

### 2.2 Component responsibility map

| Layer | Phase 1 (3× 3060) | Phase 2 (2× V620 + 1× 3060) |
|---|---|---|
| Chat backend | One stack, layer-split across 3 cards via `--tensor-split 1,1,1` | Two stacks, single-GPU per stack, no tensor split |
| RAG model placement | Evicted on coder request (`exclusive: true`) | Persistent on V620 #0 |
| Coder model placement | Evicted on RAG request | Swap-among-themselves on V620 #1 |
| Embedder placement | Always loaded (`persistent: true`), shared with chat group on layer split | Dedicated to 3060 (Stack A) |
| Reranker | Not present | New on 3060 |
| Build target | Single binary, `-DGGML_CUDA=ON` | Two binaries: `-DGGML_CUDA=ON` (3060) and `-DGGML_VULKAN=ON` (V620s) |
| GPU isolation | `CUDA_VISIBLE_DEVICES=0,1,2` global | Per-model: `CUDA_VISIBLE_DEVICES=0` for Stack A, `GGML_VK_VISIBLE_DEVICES=N` for Stack B1/B2 |
| Power | 3× 170W = 510W on PDB | 1× 170W on PDB + 2× 300W on external PSU |
| Vendor surface | NVIDIA only | NVIDIA + AMD |

---

## 3. Prerequisites

> **Deployment continues from Phase 1 — same LXC container.** Phase 1 provisioned a `llama-cpp` LXC container with privileged mode, GPU passthrough for all three RTX 3060s, and a host bind-mount of `/opt/gguf`. Phase 2 reuses that container. The hardware swap reconfigures GPU passthrough on the host (replace 3060 device entries with V620 entries by PCI BDF). The software work (build the Vulkan tree alongside Phase 1's CUDA tree, install AMD userspace) happens **inside the existing `llama-cpp` container**. Paths like `/opt/llama.cpp-cuda` and `/opt/llama.cpp-vulkan` are inside the container's filesystem, not the host's. The host gets the AMD kernel driver (`amdgpu` module); the container gets the userspace libraries.

Don't start Phase 2 unless all of these are green:

1. Phase 1 has been live for at least two weeks under real workload — no chat-model thrashing complaints, no embedding-parity drift, no llama-swap restarts due to crashes. The `llama-cpp` LXC container is healthy and serving on `:11434`.
2. The reference doc's Appendix C smoke tests pass clean. Specifically the VCF positive-retrieval test, which proves the LanceDB embedding pipeline still works end-to-end.
3. You have **two V620 cards in hand** with cooling shrouds and 80mm fans assembled. Don't order hardware partway through this procedure.
4. You have an **external 1000W+ PSU** (HX1000, HX1200, RM1000x, or equivalent) and an Add2PSU adapter on hand if Stage 1 measurement requires it — *or* you've decided to take the `pp_power_cap` derate path (§4.3) and accept ~10–15% throughput loss on V620s.
5. **Storage and labeling.** Label the three 3060s before pulling them so you remember which card was in which slot, in case you need to restore the Phase 1 layout. Anti-static bag, sealed box, off-shelf.
6. **LXC snapshot taken.** Before any hardware change: `lxc snapshot llama-cpp pre-phase2-stage1`. Filesystem-level rollback insurance.

---

## 4. Hardware prep

> **Read this first — the power-budget framing is measurement-driven, not spec-driven.** The naive arithmetic says 2× V620 (300W spec) + 1× 3060 (170W spec) = 770W against a 675W PDB ceiling. That math is conservative. LLM inference is memory-bandwidth-bound; SMs/CUs sit partially idle waiting on VRAM and the GPU drops clocks accordingly. **Measured 3× RTX 3060 sustained draw on this cluster under Qwen inference is ~360W (vs 510W spec) — about 70% of TDP.** V620 actual draw under sustained Qwen 3.6 inference is unknown until you measure it; reports on the closely related RDNA 2 W6800 (same gfx1030, similar TDP class) suggest ~180–220W sustained.
>
> Stage 1 of the rollout (§8) installs **one** V620 on the existing PDB and measures actual sustained draw. The external PSU (§4.2) and the power-cap derate (§4.3) are documented as the two options for Stage 2 — but the decision between them (or neither) happens **after** measurement, not before. If V620 measures ~200W sustained, neither is needed and the existing PDB carries everything.

### 4.1 Physical layout on the T7910

PCIe slot assignment after the swap. Power source for the V620s is decided after Stage 1 measurement (§8 step 8); table shows both possible end states.

| T7910 slot | Connection (Stage 1) | Connection (Stage 2 if PDB sufficient) | Connection (Stage 2 if external PSU) | Device |
|---|---|---|---|---|
| 1 (CPU0, x16) | PDB 8-pin | PDB 8-pin | PDB 8-pin | RTX 3060 12 GB |
| 3 (CPU1, x16) | (empty) | PDB 8-pin | External PSU 8-pin | V620 #0 (rag-qwen25) |
| 5 (CPU1, x16) | PDB 8-pin (V620 #1 here in Stage 1) | PDB 8-pin | External PSU 8-pin | V620 #1 (coder-*) |

The 3060 stays on the T7910 PDB regardless. The choice for Stage 2 is whether the V620s also stay on the PDB or move to a dedicated external PSU.

### 4.2 Option A — External PSU and Add2PSU bridge (install only if Stage 1 measurement requires)

Reach for this path only if Stage 1 measurement (§8 step 8) shows V620 sustained draw above ~260W per card, or if you want headroom for a future hardware cycle. Otherwise the PDB carries everything; skip to §4.4.

Mount a second ATX PSU (HX1000 or similar, ~$150–200) in the upper drive bay or external case. Wire:

- Both V620 8-pin PCIe leads from the second PSU only.
- An [Add2PSU](https://www.amazon.com/s?k=add2psu) adapter ($10) connects:
  - Primary PSU's 24-pin via SATA passthrough or a spare Molex
  - Secondary PSU's 24-pin "PS_ON#" line bridged to ground when primary is on
- Both PSUs share chassis ground via the same case ground.

When the T7910 powers on, the primary PSU spins up, Add2PSU bridges PS_ON to the secondary, and the V620s come up together. Power-off is automatic on system shutdown.

> Test this *before* installing the V620s. Plug a dummy load (an old 850W-tested PSU tester or even a spare HDD) into the secondary PSU, power on the T7910, and confirm both PSUs energize. If Add2PSU is wired wrong, the secondary PSU is dead at boot — better to discover that without two $250 GPUs hanging off it.

### 4.3 Option B — Power-limit V620s, no external PSU (install only if Stage 1 measurement is in the marginal band)

Reach for this path only if Stage 1 measurement shows V620 sustained draw between ~230–260W and you'd rather not run a second PSU. Derate each V620 from 300W → 250W using the kernel `amdgpu` sysfs interface. Total spec draw becomes 500W + 170W = 670W — just inside the 675W PDB ceiling. **Real-world headroom is wider** (the 3060 at 120W measured = 50W under spec; V620s under inference are similarly under-spec) but you're betting on transient spikes also staying under-budget.

```bash
#!/bin/bash
# /usr/local/bin/v620-powercap.sh
set -euo pipefail
for card in /sys/class/drm/card*/device; do
  vendor=$(cat "$card/vendor" 2>/dev/null) || continue
  [ "$vendor" = "0x1002" ] || continue   # AMD only
  for cap in "$card"/hwmon/hwmon*/power1_cap; do
    [ -e "$cap" ] || continue
    echo 250000000 > "$cap"               # 250 W in microwatts (default 300 W)
  done
done
```

Persist via the systemd one-shot in Appendix A. Throughput cost on Qwen MoE generation is roughly 10–15% (memory-bandwidth-bound with the power cap not deeply biting on bandwidth).

> The earlier draft of this doc (Rev 1) recommended external PSU by default. With the 3060 measured at ~120W actual against 170W spec, the V620 spec→actual ratio is likely similar; the PDB is probably sufficient for both V620s without intervention. Stage 1 measurement is the only way to know — don't pre-commit to either Option A or B.

### 4.4 Cooling

You've got this. For reference, the V620's intended airflow is ~30 CFM front-to-back through a passive heatsink fed by chassis fans. The 3D-printed shroud + 80mm fan setup needs to maintain that flow under sustained load — measure GPU edge temp during a 30-minute sustained generation and confirm it stays below 85 °C. Throttling kicks in around 95 °C; you don't want to even approach it.

```bash
# After install, watch all three cards under sustained load:
watch -n 1 'rocm-smi --showtemp; nvidia-smi --query-gpu=temperature.gpu --format=csv'
```

If any V620 hits 90 °C in steady state, fan flow is insufficient — increase fan speed (PWM controller), upgrade to a 92mm or 120mm fan, or stop and reconsider the shroud geometry. Don't run for weeks at the edge of throttle.

---

## 5. Software prep

### 5.1 AMD driver and ROCm install — host kernel driver, container userspace

This installs in two places. **The kernel driver (`amdgpu` module) goes on the host** because LXC containers share the host kernel. **The userspace libraries (`rocm-smi`, Vulkan ICD, HIP runtime) go inside the container** because that's where llama.cpp builds and runs.

ROCm itself is for `rocm-smi`, `pp_power_cap`, and HIP runtime — **not** for inference. Important context: **gfx1030 (V620) is no longer in the official ROCm 7.x supported-GPU compatibility matrix.** ROCm 7 production wheels target gfx1100/gfx1101 (RX 7000 series) and gfx120x (RX 9000). gfx1030 builds exist only via TheRock community/nightly. This means:
- `rocm-smi` and `pp_power_cap` work (kernel `amdgpu` driver supports gfx1030 fine on stock Ubuntu).
- HIP-based inference (i.e., llama.cpp built with `-DGGML_HIP=ON`) is unsupported on gfx1030 in ROCm 7.x — orthogonal to issue #19880's Qwen MoE breakage. Either way, **Vulkan is the only viable inference path** on this hardware.

#### 5.1.1 Host install — kernel driver only

Stock Ubuntu 24.04's kernel includes `amdgpu` for gfx1030. In most cases no AMD packages are needed on the host — the kernel module loads on first boot after the V620s are physically installed. Verify post-install:

```bash
# --- HOST shell ---
lspci -nn | grep -E 'VGA|Display' | grep -i amd
# Expect 1× or 2× AMD Navi 21 entries (Stage 1 has one V620; Stage 2 has two)

dmesg | grep -i amdgpu | grep -iE 'fatal|error|fail' \
  && echo "AMD driver errors on host!" || echo "AMD driver clean on host"

# Confirm DRM render nodes exist for the V620(s)
ls -l /dev/dri/renderD*
```

If the kernel didn't auto-bind to the V620 (rare — the device ID has been in the kernel for years), install AMD's installer **on the host** to pull in DKMS:

```bash
# --- HOST shell, only if kernel-default amdgpu didn't bind ---
INSTALLER=$(curl -s https://repo.radeon.com/amdgpu-install/latest/ubuntu/noble/ \
  | grep -oE 'amdgpu-install_[0-9.]+-[0-9]+_all\.deb' | tail -1)
cd /tmp
wget "https://repo.radeon.com/amdgpu-install/latest/ubuntu/noble/${INSTALLER}"
sha256sum "$INSTALLER"   # verify against AMD's published hash
sudo apt install "./${INSTALLER}"
sudo apt update
sudo amdgpu-install --usecase=dkms   # kernel-only install, no userspace
```

#### 5.1.2 Container — pass through V620 DRM nodes and install userspace

The existing `llama-cpp` LXC has CUDA passthrough for the 3060s from Phase 1. Phase 2 adds passthrough for the V620 DRM render nodes. Stage 1 adds one V620; Stage 2 adds the second. Detail in §8.

```bash
# --- HOST shell — example for Stage 2 (both V620s installed) ---
# Find PCI BDFs of the V620s:
lspci -nn | grep -i 'amd.*navi 21'
# Example output (slot 3 and slot 5):
#   83:00.0 VGA compatible controller [...] AMD/ATI Navi 21 [Radeon Pro V620]
#   05:00.0 VGA compatible controller [...] AMD/ATI Navi 21 [Radeon Pro V620]

# Remove the old 3060 GPU passthrough entries that are no longer present:
lxc config device remove llama-cpp gpu1   # was 81:00.0 — pulled in Stage 1
lxc config device remove llama-cpp gpu2   # was 83:00.0 — pulled in Stage 2

# Add V620 passthrough by PCI BDF:
lxc config device add llama-cpp v620_0 gpu vendorid=1002 pci=0000:83:00.0
lxc config device add llama-cpp v620_1 gpu vendorid=1002 pci=0000:05:00.0

lxc restart llama-cpp

# Verify inside the container
lxc exec llama-cpp -- ls -l /dev/dri/
# Expect renderD128 (3060), renderD129 and renderD130 (V620s) — actual numbers vary
```

Now install AMD userspace **inside the container**:

```bash
# --- inside llama-cpp container ---
lxc exec llama-cpp -- bash

INSTALLER=$(curl -s https://repo.radeon.com/amdgpu-install/latest/ubuntu/noble/ \
  | grep -oE 'amdgpu-install_[0-9.]+-[0-9]+_all\.deb' | tail -1)
cd /tmp
wget "https://repo.radeon.com/amdgpu-install/latest/ubuntu/noble/${INSTALLER}"
sha256sum "$INSTALLER"   # verify
apt install "./${INSTALLER}"
apt update

# Userspace only — kernel driver is already on host (--no-dkms forces userspace-only)
amdgpu-install --usecase=rocm --no-dkms

# Vulkan ICD + tools
apt install -y libvulkan-dev mesa-vulkan-drivers vulkan-tools

# Add the ollama service account to render and video groups inside container
usermod -aG render,video ollama

# Verify
rocm-smi
vulkaninfo --summary | grep -E 'deviceName|driverID'
# Expect 1× NVIDIA RTX 3060 and 1× or 2× AMD Navi 21
```

> Membership in `render` is equivalent to full GPU compute access (DRM render-node ioctl). The `ollama` service account inside the container now has read+write on `/dev/dri/renderD*` for both V620s — same privilege class as its existing `/dev/nvidia*` access on the 3060.

### 5.2 NVIDIA driver coexistence and Vulkan ICD priority

NVIDIA kernel driver from Phase 0 stays on the host; NVIDIA userspace from Phase 1's container provisioning stays in the `llama-cpp` LXC. Phase 2 layers AMD userspace alongside it inside the same container. The gotchas are Vulkan ICD priority and NVIDIA's OpenGL libraries.

```bash
# --- inside llama-cpp container ---
vulkaninfo --summary | grep -E 'driverName|driverID'

# Expected ICDs in /usr/share/vulkan/icd.d/ (inside container):
#   nvidia_icd.json
#   radeon_icd.x86_64.json   # Mesa RADV (default for AMD)
#   amd_icd64.json            # AMDVLK if installed
```

**If both Mesa RADV and AMDVLK are installed, AMDVLK takes precedence by default.** RADV is the better-tested path for llama.cpp Vulkan on RDNA 2, so pin it explicitly. Set the env var both at the container level (so any process started inside inherits it) and per-model in the llama-swap config:

```bash
# --- HOST shell ---
lxc config set llama-cpp environment.AMD_VULKAN_ICD RADV
lxc restart llama-cpp
```

Per-model override in §7's llama-swap config also sets `env: ["AMD_VULKAN_ICD=RADV", ...]` on Vulkan-backed entries — defense-in-depth.

If the NVIDIA driver was installed with `--no-opengl-files` originally (the standard pattern for compute-only NVIDIA on the host), nothing to change. If it was installed standard, redo the host install with `--no-opengl-files` to keep Mesa as the default OpenGL provider for the V620 side.

**Driver upgrade hygiene.** Pin both vendors' packages — kernel-side on host, userspace-side inside container — to known-good versions during the migration:

```bash
# --- HOST shell ---
sudo apt-mark hold amdgpu-dkms nvidia-driver-XXX   # kernel modules

# --- inside container ---
lxc exec llama-cpp -- apt-mark hold \
  amdgpu-core rocm-smi-lib rocm-dev \
  nvidia-utils-XXX libnvidia-compute-XXX
```

Unpin and upgrade in a planned maintenance window with parity gates re-run. Note: host kernel and container userspace upgrades must be coordinated — version skew between host's NVIDIA kernel module and container's NVIDIA userspace is a perennial source of "Failed to initialize NVML" errors.

### 5.3 Build llama.cpp with two backends — inside the container

Two parallel build trees inside the `llama-cpp` LXC. The CUDA build from Phase 1 lives at `/opt/llama.cpp` (or `/opt/llama.cpp-cuda` if you renamed it; either works). The Vulkan tree is added alongside.

```bash
# --- inside llama-cpp container ---
lxc exec llama-cpp -- bash

# Rename the existing CUDA tree for clarity (optional but helps readability)
ls /opt/llama.cpp/build/bin/llama-server   # already exists from Phase 1
mv /opt/llama.cpp /opt/llama.cpp-cuda
ln -s /opt/llama.cpp-cuda /opt/llama.cpp   # legacy symlink for any Phase 1 scripts

# Vulkan build — fresh tree
git clone https://github.com/ggml-org/llama.cpp.git /opt/llama.cpp-vulkan
cd /opt/llama.cpp-vulkan
git fetch --tags

# Use the SAME release tag as Phase 1's CUDA build — drift between trees breaks
# OpenAI-compat behavior subtly. Get Phase 1's tag:
PHASE1_TAG=$(git -C /opt/llama.cpp-cuda describe --tags --exact-match 2>/dev/null \
            || git -C /opt/llama.cpp-cuda rev-parse HEAD)
echo "Phase 1 CUDA tree at: $PHASE1_TAG"
git checkout "$PHASE1_TAG"

cmake -S /opt/llama.cpp-vulkan -B /opt/llama.cpp-vulkan/build \
  -DGGML_VULKAN=ON -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build /opt/llama.cpp-vulkan/build --config Release -j$(nproc)

/opt/llama.cpp-vulkan/build/bin/llama-server --version
/opt/llama.cpp-vulkan/build/bin/llama-server --list-devices
# Expect entries for the V620(s) and possibly the 3060 via Vulkan

# Pin both build trees to the same commit — verify before any parity gate:
cuda_v=$(git -C /opt/llama.cpp-cuda rev-parse --short HEAD)
vulkan_v=$(git -C /opt/llama.cpp-vulkan rev-parse --short HEAD)
[ "$cuda_v" = "$vulkan_v" ] && echo "OK: trees match ($cuda_v)" \
  || { echo "WARN: CUDA=$cuda_v vs Vulkan=$vulkan_v — reconcile"; }

chown -R ollama:ollama /opt/llama.cpp-vulkan/build/bin
```

Drift between CUDA and Vulkan trees is a debugging nightmare — when something breaks, you want to be sure it's not a version mismatch.

### 5.4 GPU isolation — verify before going further (inside container)

```bash
# --- inside llama-cpp container ---

# CUDA-side: 3060 should be the only visible device
CUDA_VISIBLE_DEVICES=0 /opt/llama.cpp-cuda/build/bin/llama-server --list-devices
# Expected: one CUDA device, GeForce RTX 3060

# Vulkan-side: should see V620 #0 and V620 #1 (and possibly the 3060 via Vulkan,
# but we're not going to use it that way).
GGML_VK_VISIBLE_DEVICES=0 /opt/llama.cpp-vulkan/build/bin/llama-server --list-devices
# Expected: one Vulkan device — V620 #0

GGML_VK_VISIBLE_DEVICES=1 /opt/llama.cpp-vulkan/build/bin/llama-server --list-devices
# Expected: one Vulkan device — V620 #1
```

**Use `--device <stable-name>` rather than numeric IDs.** Vulkan device enumeration order can shift across reboots, kernel updates, or driver upgrades — physical V620 #0 (slot 3) may end up as Vulkan device 1, swapping with V620 #1 (slot 5). The first symptom is rag-qwen25 loading on the wrong card and VRAM math failing on the next coder model. Read the canonical device names from `vulkaninfo --summary | grep -E 'deviceName|GPU id'` (run inside the container) and reference those in `cmd:` blocks. `GGML_VK_VISIBLE_DEVICES=N` works for ad-hoc testing but is not stable enough for production llama-swap config.

> **`mlock` and IPC_LOCK in privileged LXC.** Privileged LXC containers already inherit `CAP_IPC_LOCK` from the host's default capability set — no Docker-style `--cap-add=IPC_LOCK` translation is needed. If you ever need higher memlock rlimits inside the container (e.g., to support `--mlock` on a fork that mlocks model weights), set `lxc.prlimit.memlock = unlimited` in the container's raw LXC config. None of the flags currently in §7's llama-swap config require this.

---

## 6. Per-card configurations

### 6.1 Stack A — 3060 (CUDA)

Same llama-server flags as Phase 1 §4.3 for the embedder. New: a reranker entry — but read §6.1.1 below carefully, the integration story has caveats.

```bash
# Embedder (unchanged from Phase 1)
/opt/llama.cpp-cuda/build/bin/llama-server \
  --model /opt/gguf/qwen3-embedding-0.6B-f16.gguf \
  --host 127.0.0.1 --port 9001 \
  --n-gpu-layers 999 \
  --embedding --pooling last \
  --ctx-size 2048 \
  --batch-size 512 --ubatch-size 512

# Reranker (new in Phase 2) — note all three flags are required
/opt/llama.cpp-cuda/build/bin/llama-server \
  --model /opt/gguf/qwen3-reranker-0.6B-q8_0.gguf \
  --host 127.0.0.1 --port 9002 \
  --n-gpu-layers 999 \
  --reranking --pooling rank --embedding \
  --ctx-size 2048
```

**Reranker GGUF source matters.** The `Qwen/`-org HuggingFace repo ships safetensors only; there is no official `Qwen/Qwen3-Reranker-0.6B-GGUF`. Use a verified third-party conversion:
- [`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF`](https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF) — recommended (official-ish ggml-org community, Q8_0).
- [`Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp`](https://huggingface.co/Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp) — verified-correct conversion specifically for llama-server.
- [`Mungert/Qwen3-Reranker-0.6B-GGUF`](https://huggingface.co/Mungert/Qwen3-Reranker-0.6B-GGUF) — f16 if you specifically want it.

Mis-converted reranker GGUFs return scores ~4.5e-23 because the metadata (`cls.output.weight`, `pooling_type=RANK`, `output_labels=["yes","no"]`) isn't right. The triple-flag invocation `--reranking --pooling rank --embedding` is mandatory; using just `--reranking` will silently produce garbage on some GGUFs.

`bge-reranker-v2-m3-GGUF` is a fallback if Qwen3-Reranker has trouble.

VRAM math for Stack A: ~1.4 GB embedder + ~2 GB reranker = ~3.4 GB on the 12 GB 3060. ~8.5 GB headroom for future small helpers.

#### 6.1.1 Reranker integration with AnythingLLM — gap to be aware of

**AnythingLLM as currently shipped does not have a UI for an external reranker provider.** The reference doc's `vectorSearchMode: "rerank"` setting uses LanceDB's *built-in* cross-encoder (Transformers.js, runs on the AnythingLLM Node process), not an HTTP call to an external endpoint. So hosting the reranker on the 3060 doesn't automatically improve AnythingLLM RAG quality.

Three integration options:

1. **Defer integration to Phase 3** — host the reranker now (so the GPU and config are ready), but don't wire it into AnythingLLM until either the project ships an external-reranker provider, or you write a sidecar.
2. **Sidecar pattern** — add a small reverse proxy (FastAPI shim) between AnythingLLM and the LLM endpoint that intercepts the retrieval response, calls `/v1/rerank` against `qwen3-reranker`, reorders the top-K, and forwards. Real engineering, ~1 day of work.
3. **Use via MCP** — write a custom MCP retrieval tool that hits LanceDB directly, calls the reranker, returns reranked chunks. Useful if you're already using MCP for retrieval; bypasses AnythingLLM's reranker phase entirely.

Phase 2's deliverable is the **hosted service**, not the AnythingLLM integration. The §1.1 "improves RAG quality" claim is conditional on (1), (2), or (3) shipping. Mark this as planned-but-not-integrated.

### 6.2 Stack B1 — V620 #0 (Vulkan), `rag-qwen25`

```bash
/opt/llama.cpp-vulkan/build/bin/llama-server \
  --model /opt/gguf/qwen2.5-32b-instruct-q4_K_M.gguf \
  --host 127.0.0.1 --port 9003 \
  --n-gpu-layers 999 \
  -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --ctx-size 32768 \
  --n-predict 4096 \
  --parallel 4 --cont-batching \
  --temp 0.3 --top-k 20 --top-p 0.95 --min-p 0 \
  --repeat-penalty 1 --presence-penalty 0 \
  --jinja
```

VRAM: ~18 GB weights + 4 × 1.1 GB KV at 32K = ~22.5 GB. ~9.5 GB headroom on a 32 GB V620. `--parallel 4` is worth using — RAG queries are short and typically interleaved across multiple users; serializing them through one slot wastes the bandwidth headroom the V620 has over the 3060.

### 6.3 Stack B2 — V620 #1 (Vulkan), coder family

Three llama-swap entries, one model loaded at a time on V620 #1 (`exclusive: true` within their own group). Same Modelfile-equivalent sampler settings as Phase 1.

| Variant | `--ctx-size` | `--parallel` | Weights + KV (q8_0) | + Vulkan overhead (~1.5 GB) | Margin on 32 GB |
|---|---|---|---|---|---|
| `coder-36` | 65536 | 2 | 23 GB + 4.5 GB = 27.5 GB | ~29 GB | ~3 GB |
| `coder-long` | 131072 | 1 | 23 GB + 4.5 GB = 27.5 GB | ~29 GB | ~3 GB |
| `coder-160k` | 163840 | 1 | 23 GB + 5.6 GB = 28.6 GB | ~30.1 GB | ~1.9 GB |

`coder-256k` and `coder-160k` (the Rev-1 draft entry) are **both dropped** in Phase 2. The Vulkan allocator overhead (RADV command buffers ~200–600 MB, SPIR-V pipeline cache ~200–400 MB during first compile, llama.cpp scratch buffers, plus driver staging) typically runs 1.0–1.5 GB beyond the model+KV math. At 192K you'd have ~0.5 GB free, which is one shader recompilation away from `VK_ERROR_OUT_OF_DEVICE_MEMORY` — and the failure mode on llama.cpp's Vulkan backend is currently a hard process exit, not graceful degradation. **160K is the realistic ceiling** on a single 32 GB V620. If you need more, it's a Phase 3 problem (Vulkan multi-GPU split fixed, or a card with ≥40 GB VRAM).

### 6.4 Sample llama-server invocation for a coder model

```bash
/opt/llama.cpp-vulkan/build/bin/llama-server \
  --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf \
  --host 127.0.0.1 --port 9004 \
  --n-gpu-layers 999 \
  -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --ctx-size 65536 \
  --n-predict 16384 \
  --parallel 2 --cont-batching \
  --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0 \
  --repeat-penalty 1 --presence-penalty 0 \
  --jinja
```

Run by llama-swap with `GGML_VK_VISIBLE_DEVICES=1` in `env:` to pin to V620 #1.

---

## 7. llama-swap configuration

`/opt/llama-swap/config.yaml`. Replaces the Phase 1 config.

```yaml
healthCheckTimeout: 240   # Vulkan cold-load + V620 KV pre-allocate is slower than Phase 1
logLevel: info

# apiKeys: required only if --listen 0.0.0.0; see Phase 1 §5.1
# apiKeys:
#   - "REPLACE_WITH_OPENSSL_RAND_HEX_32_VALUE"

models:
  # ─── Stack A: CUDA on 3060 ───
  qwen3-embed:
    cmd: |
      /opt/llama.cpp-cuda/build/bin/llama-server
      --model /opt/gguf/qwen3-embedding-0.6B-f16.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      --embedding --pooling last
      --ctx-size 2048
      --batch-size 512 --ubatch-size 512
    env:
      - "CUDA_VISIBLE_DEVICES=0"
    proxy: http://127.0.0.1:${PORT}

  qwen3-reranker:
    cmd: |
      /opt/llama.cpp-cuda/build/bin/llama-server
      --model /opt/gguf/qwen3-reranker-0.6B-q8_0.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      --reranking --pooling rank --embedding
      --ctx-size 2048
    env:
      - "CUDA_VISIBLE_DEVICES=0"
    proxy: http://127.0.0.1:${PORT}

  # ─── Stack B1: Vulkan on V620 #0 ───
  rag-qwen25:
    cmd: |
      /opt/llama.cpp-vulkan/build/bin/llama-server
      --model /opt/gguf/qwen2.5-32b-instruct-q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 32768
      --n-predict 4096
      --parallel 4 --cont-batching
      --temp 0.3 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    env:
      - "GGML_VK_VISIBLE_DEVICES=0"
    proxy: http://127.0.0.1:${PORT}

  # ─── Stack B2: Vulkan on V620 #1 (exclusive among themselves) ───
  coder-36:
    cmd: |
      /opt/llama.cpp-vulkan/build/bin/llama-server
      --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 65536
      --n-predict 16384
      --parallel 2 --cont-batching
      --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    env:
      - "GGML_VK_VISIBLE_DEVICES=1"
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

  coder-long:
    cmd: |
      /opt/llama.cpp-vulkan/build/bin/llama-server
      --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 131072
      --n-predict 16384
      --parallel 1 --cont-batching
      --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    env:
      - "GGML_VK_VISIBLE_DEVICES=1"
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

  coder-160k:
    cmd: |
      /opt/llama.cpp-vulkan/build/bin/llama-server
      --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf
      --host 127.0.0.1 --port ${PORT}
      --n-gpu-layers 999
      -fa on
      --cache-type-k q8_0 --cache-type-v q8_0
      --ctx-size 163840
      --n-predict 16384
      --parallel 1 --cont-batching
      --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0
      --repeat-penalty 1 --presence-penalty 0
      --jinja
    env:
      - "GGML_VK_VISIBLE_DEVICES=1"
    proxy: http://127.0.0.1:${PORT}
    ttl: 1800

groups:
  # Embedder is persistent; reranker stays separate during Stage 1 bringup so a
  # bad reranker GGUF doesn't take down the embed group's health check.
  # Promote reranker into the embed group only after §9.2 passes.
  embed:
    persistent: true
    members: [qwen3-embed]

  rerank:
    members: [qwen3-reranker]   # ttl-evictable until validated; promote to persistent later

  rag:
    persistent: true            # rag-qwen25 always loaded on V620 #0
    members: [rag-qwen25]

  coder:
    exclusive: true             # one coder model at a time on V620 #1
    members: [coder-36, coder-long, coder-160k]
```

Key design changes vs Phase 1:

- **Three groups, not two.** RAG separated from the coder group; both V620s host different things, so the chat group's `exclusive: true` from Phase 1 no longer makes sense.
- **`persistent: true` on rag** — the V620 has 32 GB to itself, no reason to ever evict.
- **`exclusive: true` on coder only** — one coder model at a time on V620 #1.
- **`healthCheckTimeout: 240`** (up from 180). Vulkan cold-loads include shader compilation; first-time-on-a-build can take 60+ seconds on top of the GGUF read.

---

## 8. Staged rollout

The hardware swap is sequenced so you always have a working chat backend.

### Stage 0 — Phase 1 baseline (current state)

3× RTX 3060, single CUDA stack, llama-swap routing all models to layer-split backends. Confirmed stable. Proceed.

### Stage 1 — first V620 install (drop one 3060)

Goal: prove out V620 cooling, power, drivers, and Vulkan llama.cpp on real workload before doubling the variable count. **Critical: the Stage 1 measurement (step 11 below) decides whether Stage 2 needs Option A (external PSU) or can stay on the existing PDB.**

1. **Take a pre-Stage-1 LXC snapshot** for filesystem-level rollback insurance:
   ```bash
   # --- HOST shell ---
   lxc snapshot llama-cpp pre-phase2-stage1
   ```

2. **Stop the `llama-cpp` LXC** so it isn't holding the soon-to-be-pulled 3060:
   ```bash
   lxc stop llama-cpp
   ```

3. **Power off the T7910, ground yourself, pull one 3060** (the slot-5 card — leaves the other two CUDA cards in slots 1 and 3). Bag and shelve.

4. **Install one V620** in slot 5 with the 3D-printed shroud + 80mm fan. Wire its 8-pin to **the same PDB connector the pulled 3060 used** — no external PSU yet, we're going to measure first.

5. **Boot. Verify hardware enumeration on the host:**
   ```bash
   # --- HOST shell ---
   lspci -nn | grep -E 'VGA|Display'
   # Expect: 2× NVIDIA GA106, 1× AMD Navi 21
   nvidia-smi
   dmesg | grep -i amdgpu | grep -iE 'fatal|error|fail' \
     && echo "AMD driver errors!" || echo "AMD driver clean"
   ls -l /dev/dri/renderD*
   ```
   If the kernel didn't auto-bind to the V620, run §5.1.1's host-side AMD installer with `--usecase=dkms`.

6. **Reconfigure LXC GPU passthrough.** Remove the entry for the pulled 3060, add the V620:
   ```bash
   # --- HOST shell ---
   # The pulled 3060 was at PCI 0000:83:00.0 (slot 5) per reference doc §2.2.
   # The new V620 takes its physical slot. Confirm new BDF:
   lspci -nn | grep -i 'amd.*navi 21'
   # Example: 83:00.0 — same BDF since slot didn't change. Vendor changed from
   # 10de (NVIDIA) to 1002 (AMD).

   lxc config device remove llama-cpp gpu2          # was the slot-5 3060
   lxc config device add llama-cpp v620_0 gpu vendorid=1002 pci=0000:83:00.0

   lxc start llama-cpp
   lxc exec llama-cpp -- ls -l /dev/dri/
   # Expect 3060s' renderD nodes (still 2 of them) plus the V620's renderD
   ```

7. **Install AMD userspace inside the container per §5.1.2.** Then verify visibility:
   ```bash
   lxc exec llama-cpp -- vulkaninfo --summary | grep -E 'deviceName|driverID'
   # Expect 2× NVIDIA RTX 3060 + 1× AMD Navi 21
   lxc exec llama-cpp -- rocm-smi
   ```

8. **Verify Phase 1 llama-swap config still works on the 2 remaining 3060s.** It will be running with `--tensor-split 1,1` instead of `1,1,1` — that's fine, llama.cpp auto-distributes.
   ```bash
   lxc exec llama-cpp -- systemctl status llama-swap
   curl -s http://127.0.0.1:11434/v1/models | jq
   ```
   If anything regressed, stop and triage before continuing.

9. **Build the Vulkan llama.cpp tree inside the container per §5.3.** Verify `--list-devices` (run inside container) shows V620 #0 by name.

10. **Stage 1 transitional llama-swap config** — concrete YAML, deliberately conservative. `coder-long` and `coder-160k` are not available in Stage 1 (the V620 alone can't hold both rag-qwen25 and a long-context coder simultaneously, and we don't want to layer-split CUDA across the two remaining 3060s for one model). They return in Stage 2.

   Push this config into the container at `/opt/llama-swap/config.stage1.yaml`:

   ```yaml
   # /opt/llama-swap/config.stage1.yaml — DO NOT use Phase 2 §7 yet
   healthCheckTimeout: 240
   logLevel: info

   models:
     # Stack A on slot-1 3060 (CUDA, device 0)
     qwen3-embed:
       cmd: |
         /opt/llama.cpp-cuda/build/bin/llama-server
         --model /opt/gguf/qwen3-embedding-0.6B-f16.gguf
         --host 127.0.0.1 --port ${PORT}
         --n-gpu-layers 999 --embedding --pooling last --ctx-size 2048
       env: ["CUDA_VISIBLE_DEVICES=0"]
       proxy: http://127.0.0.1:${PORT}

     qwen3-reranker:
       cmd: |
         /opt/llama.cpp-cuda/build/bin/llama-server
         --model /opt/gguf/qwen3-reranker-0.6B-q8_0.gguf
         --host 127.0.0.1 --port ${PORT}
         --n-gpu-layers 999 --reranking --pooling rank --embedding --ctx-size 2048
       env: ["CUDA_VISIBLE_DEVICES=0"]
       proxy: http://127.0.0.1:${PORT}

     # V620 #0 on slot 5 (Vulkan) — exclusive between rag-qwen25 and coder-36
     # only, while we validate. coder-long/coder-160k come back in Stage 2.
     rag-qwen25:
       cmd: |
         /opt/llama.cpp-vulkan/build/bin/llama-server
         --model /opt/gguf/qwen2.5-32b-instruct-q4_K_M.gguf
         --host 127.0.0.1 --port ${PORT}
         --n-gpu-layers 999 -fa on
         --cache-type-k q8_0 --cache-type-v q8_0
         --ctx-size 32768 --n-predict 4096 --parallel 2 --cont-batching
         --temp 0.3 --top-k 20 --top-p 0.95 --min-p 0
         --repeat-penalty 1 --presence-penalty 0 --jinja
       env: ["AMD_VULKAN_ICD=RADV", "GGML_VK_VISIBLE_DEVICES=0"]
       proxy: http://127.0.0.1:${PORT}
       ttl: 1800

     coder-36:
       cmd: |
         /opt/llama.cpp-vulkan/build/bin/llama-server
         --model /opt/gguf/qwen3.6-35b-a3b-UD-Q4_K_M.gguf
         --host 127.0.0.1 --port ${PORT}
         --n-gpu-layers 999 -fa on
         --cache-type-k q8_0 --cache-type-v q8_0
         --ctx-size 65536 --n-predict 16384 --parallel 1 --cont-batching
         --temp 0.6 --top-k 20 --top-p 0.95 --min-p 0
         --repeat-penalty 1 --presence-penalty 0 --jinja
       env: ["AMD_VULKAN_ICD=RADV", "GGML_VK_VISIBLE_DEVICES=0"]
       proxy: http://127.0.0.1:${PORT}
       ttl: 1800

   groups:
     embed:
       persistent: true
       members: [qwen3-embed]
     rerank:
       members: [qwen3-reranker]
     v620_slot5:
       exclusive: true            # rag-qwen25 OR coder-36, not both
       members: [rag-qwen25, coder-36]
   ```

   Stage 1 deliberately accepts chat-model thrashing (rag-qwen25 ↔ coder-36 evict each other on the V620). That regression goes away when V620 #1 is installed in Stage 2. The remaining slot-3 3060 is idle in Stage 1; ~15W parasitic draw, document it as expected.

11. **Bind llama-swap to the new config inside the container** and restart:
    ```bash
    # --- HOST shell ---
    lxc file push /tmp/config.stage1.yaml llama-cpp/opt/llama-swap/config.stage1.yaml
    lxc exec llama-cpp -- ln -sf /opt/llama-swap/config.stage1.yaml /opt/llama-swap/config.yaml
    lxc exec llama-cpp -- systemctl restart llama-swap
    curl -s http://127.0.0.1:11434/v1/models | jq    # verify the four entries
    ```

12. **Run the §9 parity gates** against `:11434`. Skip §9.5 long-context tests at 131K and 160K — those models aren't loaded in Stage 1.

13. **Measure V620 sustained power draw.** Run a 30-minute sustained generation on `coder-36` at 64K context (the longest available in Stage 1) and capture per-card power. Run from the host since `rocm-smi` and `nvidia-smi` see all hardware regardless of LXC namespace:

    ```bash
    # --- HOST shell ---
    ( while true; do
        date +%s
        rocm-smi --showpower 2>/dev/null | grep -E 'GPU\['
        nvidia-smi --query-gpu=power.draw --format=csv,noheader
        sleep 1
      done ) | tee /tmp/v620-power-stage1.log
    ```

    Take the **median** sustained value (ignore startup spikes). Decision matrix for Stage 2:

    | V620 sustained (median) | Stage 2 power plan | Rationale |
    |---|---|---|
    | ≤ 230W | Stay on PDB. **No external PSU; no derate.** | 2× 230W + 120W (3060 measured) ≈ 580W. Fits 675W with 95W margin. |
    | 230–260W | Choose: (a) install external PSU per §4.2, OR (b) `pp_power_cap` derate to 250W per §4.3. | Marginal. Pick based on appetite for a second PSU vs 10–15% throughput hit. |
    | > 260W | **Install external PSU before Stage 2.** | 2× 260W + 120W = 640W is too close to PDB spike ceiling. |

14. **Run Stage 1 for at least one week under real workload** before proceeding to Stage 2. Watch thermals (`rocm-smi --showtemp` from host), watch for Vulkan kernel errors (`dmesg | grep amdgpu` from host, Vulkan errors `lxc exec llama-cpp -- journalctl -u llama-swap` for the user-space side), watch for any `coder-36`/`rag-qwen25` request anomalies.

### Stage 2 — second V620 install (drop the second 3060)

1. **Take a pre-Stage-2 LXC snapshot:**
   ```bash
   # --- HOST shell ---
   lxc snapshot llama-cpp pre-phase2-stage2
   lxc stop llama-cpp
   ```

2. Power off, pull the slot-3 3060, bag and shelve.

3. Install the second V620 with shroud + fan. Wire 8-pin to **the power source the Stage 1 measurement (§8 step 13) selected** — PDB if ≤230W, external PSU if >260W, your call if marginal.

4. If installing the external PSU now: mount, bridge with Add2PSU, **test PS_ON sync with a dummy load before plugging in V620 power leads.**

5. Boot, verify enumeration on host (3 GPUs total: 1× 3060, 2× V620):
   ```bash
   # --- HOST shell ---
   lspci -nn | grep -E 'VGA|Display'
   ls -l /dev/dri/renderD*
   ```

6. **Reconfigure LXC GPU passthrough** — remove the second pulled 3060 (slot 3, BDF was 0000:81:00.0), add the second V620:
   ```bash
   # --- HOST shell ---
   lspci -nn | grep -i 'amd.*navi 21'   # confirm BDF of the second V620

   lxc config device remove llama-cpp gpu1            # was the slot-3 3060
   lxc config device add llama-cpp v620_1 gpu vendorid=1002 pci=0000:81:00.0
   lxc start llama-cpp
   lxc exec llama-cpp -- vulkaninfo --summary | grep deviceName
   # Expect 1× NVIDIA RTX 3060 + 2× AMD Navi 21
   ```

7. **Promote llama-swap to the §7 final-state config:**
   ```bash
   # --- HOST shell ---
   lxc file push /tmp/config.yaml llama-cpp/opt/llama-swap/config.yaml
   lxc exec llama-cpp -- ln -sf /opt/llama-swap/config.yaml /opt/llama-swap/config.yaml.active
   lxc exec llama-cpp -- systemctl restart llama-swap
   curl -s http://127.0.0.1:11434/v1/models | jq
   # Expect six models: qwen3-embed, qwen3-reranker, rag-qwen25, coder-36, coder-long, coder-160k
   ```

8. Re-run §9 parity gates including the now-available `coder-long` and `coder-160k` long-context tests.

9. Promote `qwen3-reranker` from its standalone group into the `embed` group (set `persistent: true`) once §9.2 has passed cleanly.

10. Run for two weeks under real workload before declaring Phase 2 stable.

### Stage 3 — long-term operations

- The two pulled 3060s stay shelved as rollback hardware for at least 90 days.
- The external PSU and Add2PSU stay in place; they're load-bearing infrastructure now.
- Cooling thermals get monitored — `rocm-smi --showtemp` in a Grafana panel or a periodic systemd timer that alerts on >85 °C sustained.

---

## 9. Parity gates per stage

### 9.1 Embedding parity (still the hard gate)

The embedder moves from a 3060 in a layer-split arrangement to a 3060 in a single-GPU Stack A arrangement — same hardware family, same CUDA backend, same model. Vector parity should be straightforward, but **verify it anyway** because the alternative (silently corrupted LanceDB) is the one Phase 1 finding that doesn't roll back cleanly.

Reuse the script from Phase 1 §6.1, comparing pre-Phase-2 (current Phase 1) llama-swap on `:11434` to a temporary post-Phase-2 llama-swap on `:11440`. Cosine ≥ 0.9999 after L2-normalize. Hard gate.

### 9.2 Reranker endpoint smoke test (Phase 2 hosts the service; integration is separate — see §6.1.1)

This gate validates the reranker endpoint **as a service**. It does NOT validate AnythingLLM RAG quality, because AnythingLLM as currently shipped doesn't consume an external reranker (see §6.1.1). The integration is a Phase 3 / sidecar problem.

```bash
# Smoke test — should return a reranked list with relevance scores
curl -s http://127.0.0.1:11440/v1/rerank \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-reranker",
    "query": "edge uplink subnets",
    "documents": [
      "VCF-NET-014: edge uplink subnets must use prefix length /29 or /30.",
      "Keycloak realm settings include token lifespans.",
      "the quick brown fox"
    ]
  }' | jq
```

Expect: scores in the 0–1 range, with the VCF chunk scoring highest. **If scores cluster around ~4.5e-23, the reranker GGUF was mis-converted** (missing `cls.output.weight` or wrong `pooling_type`). Replace the GGUF with one of the verified sources in §6.1 and re-test.

Truncate documents to ≤1500 tokens before submission. The reranker context is 2048 (`--ctx-size 2048` in §7) and longer documents either error or are silently truncated depending on the llama.cpp commit. Don't pipe raw scraped pages directly to `/v1/rerank` without length bounding.

### 9.3 No-think proxy regex check

Same as Phase 1 §6.2. The Vulkan-served Qwen 3.6 should emit identical `<think>...</think>` delimiters as the CUDA-served version. Verify with one curl.

### 9.4 OpenCode tool-call parity

This is the highest-risk gate in Phase 2. Vulkan and CUDA may render the chat template slightly differently — chat-template Jinja rendering is portable but the underlying tokenization paths can drift. Run OpenCode pointed at `:11440` with a known tool-call test path. Compare against the Phase 1 baseline. If tool calls drift, pin the Vulkan llama.cpp build to a specific known-good commit.

### 9.5 Long-context smoke test

```bash
for ctx in 32768 65536 131072 163840; do
  echo "=== context fill: $ctx ==="
  python /opt/migration/longctx-smoke.py --port 11440 \
    --model "$([ "$ctx" -le 65536 ] && echo coder-36 || \
              [ "$ctx" -le 131072 ] && echo coder-long || echo coder-160k)" \
    --fill "$ctx"
done
```

Vulkan multi-GPU split is broken (#15974) but we're not using multi-GPU split — each model is on a single V620. So this gate is checking single-GPU Vulkan correctness at long context, which is generally well-tested. Expect green.

### 9.6 Throughput regression check

Capture tok/s on the same prompt against:
- Phase 1 (3× 3060 layer-split CUDA) — baseline measurement before pulling cards.
- Stage 1 (V620 single-GPU Vulkan) — should be substantially faster than Phase 1.
- Stage 2 (final-state, both V620s) — should match Stage 1 on `rag-qwen25` and `coder-36`.

The Rev-1 doc projected ~90 tok/s by extrapolating from R9700 (RDNA 4) Vulkan benchmarks. **That extrapolation doesn't back-port cleanly** — RDNA 4 has substantial wavefront and matrix-engine improvements that gfx1030 (RDNA 2) lacks, and many of the Vulkan shader optimizations land RDNA-4-first. Realistic range on V620 is 30–70 tok/s on Qwen 3.6 35B-A3B; **measure, don't predict.**

Halt criteria: if Stage 1 throughput is **less than 2× Phase 1 baseline**, do not proceed to Stage 2. Diagnose first: check `rocm-smi` for thermal/power throttle, `lspci -vv | grep -i lnksta` for PCIe link-width degradation, `dmesg | grep amdgpu` for GPU faults, and verify with `--verbose` output that flash-attention engaged (note: Vulkan FA fast-path requires NVIDIA coopmat2; on RDNA 2 you'll get the non-fused FA variant — that's expected, not a regression).

### 9.7 Power and thermal validation

30-minute sustained generation on `coder-long` at 131K. Watch:

```bash
watch -n 1 'rocm-smi --showtemp --showpower; nvidia-smi --query-gpu=temperature.gpu,power.draw --format=csv'
```

Targets:
- V620 edge temp < 85 °C steady state
- V620 power draw < 300 W (or < 250 W if power-capped)
- 3060 temp unchanged from Phase 1 baseline
- Total system power draw on the wall (kill-a-watt or smart plug) within PSU specs

If V620 hits 90 °C, fan flow is insufficient. Stop and improve cooling before continuing.

---

## 10. Rollback per stage

The destination of any rollback is the previous stable stage, not all the way back to Ollama (which has been gone since Phase 1).

### 10.1 Stage 1 rollback (one V620 installed → back to Phase 1)

```bash
# --- HOST shell ---

# 1. Stop the LXC
lxc exec llama-cpp -- systemctl stop llama-swap
lxc stop llama-cpp

# 2. Power off, pull the V620, reinstall the original 3060 in slot 5.
#    If you installed an external PSU during Stage 2, disconnect it now.

# 3. Boot the host, verify 3× 3060 enumeration via host-side nvidia-smi.

# 4. Restore the LXC GPU passthrough — remove the V620 entry, re-add the 3060:
lxc config device remove llama-cpp v620_0
lxc config device add llama-cpp gpu2 vendorid=10de pci=0000:83:00.0

# 5. Restore Phase 1 config from snapshot OR re-link the Phase 1 config.yaml:
#    Option A (clean) — restore from snapshot:
lxc restore llama-cpp pre-phase2-stage1
#    Option B (keep Vulkan tree but flip config) — re-link:
# lxc exec llama-cpp -- ln -sf /opt/llama-swap/config.phase1.yaml /opt/llama-swap/config.yaml

# 6. Start
lxc start llama-cpp
lxc exec llama-cpp -- systemctl start llama-swap

# 7. Run Phase 1 Appendix C smoke tests from the reference doc.
```

The Vulkan llama.cpp build at `/opt/llama.cpp-vulkan` (inside the container) can stay installed — it's inert without AMD GPUs. The AMD userspace inside the container is similarly inert. Both are removed cleanly only by the `lxc restore` snapshot path.

### 10.2 Stage 2 rollback (both V620s installed → back to Stage 1)

```bash
# --- HOST shell ---

# 1. Stop LXC, pull the second V620, reinstall slot-3 3060.
lxc exec llama-cpp -- systemctl stop llama-swap
lxc stop llama-cpp
# (physical hardware swap)

# 2. Reconfigure LXC GPU passthrough — replace v620_1 with the slot-3 3060
lxc config device remove llama-cpp v620_1
lxc config device add llama-cpp gpu1 vendorid=10de pci=0000:81:00.0

# 3. Restore Stage 1 state — option A: restore the LXC snapshot
lxc restore llama-cpp pre-phase2-stage2
#    option B: keep filesystem changes but re-link Stage 1 config
# lxc exec llama-cpp -- ln -sf /opt/llama-swap/config.stage1.yaml /opt/llama-swap/config.yaml

# 4. Start and verify
lxc start llama-cpp
curl -s http://127.0.0.1:11434/v1/models | jq
```

### 10.3 Full rollback to Phase 1 from final state

Pull both V620s, reinstall both 3060s, disconnect external PSU if installed. Cumulative result is a return to Phase 1's 3-card CUDA setup.

```bash
# --- HOST shell, after physical swap ---
lxc stop llama-cpp
lxc config device remove llama-cpp v620_0
lxc config device remove llama-cpp v620_1
lxc config device add llama-cpp gpu1 vendorid=10de pci=0000:81:00.0
lxc config device add llama-cpp gpu2 vendorid=10de pci=0000:83:00.0
lxc restore llama-cpp pre-phase2-stage1   # rolls back container filesystem too
lxc start llama-cpp
```

~30 minutes of work plus the physical reseating. The Phase-1 LXC snapshot (`pre-phase2-stage1`) is the clean recovery point.

---

## 11. Post-cutover validation

After Stage 2 has run for two weeks under real workload:

- **Throughput** — `coder-36` measured tok/s should be substantially higher than Phase 1 baseline. Target ~90 tok/s; if you're below 50 tok/s, something is wrong (PCIe link degradation, power cap, Vulkan kernel falling back to a slow path).
- **VRAM utilization** — `rocm-smi --showmeminfo vram` per card. V620 #0 should sit at ~22 GB used (rag-qwen25 + 4 slots). V620 #1 swings between ~25 GB (coder-36 loaded) and ~30 GB (coder-160k loaded) and ~0 GB (idle, between swaps).
- **Thermal stability** — `rocm-smi --showtemp` 30-day max should be below 85 °C.
- **Auto-updater** — runs nightly through the embedder on the 3060. Same SQLite pattern as before (errors=0, aborted=0 in steady state).
- **Reranker quality** — pre-Phase-2 vs post-Phase-2 RAG retrieval quality should improve measurably. Run a small eval set of 20–50 known VCF queries and compare top-1/top-3 hit rates with and without reranker.
- **External PSU** — verify Add2PSU bridge holds across multiple reboots and sleep cycles. A failed PS_ON bridge = no V620 power = chat backends down.

---

## 12. Known issues and mitigations

| Issue | Severity | Mitigation |
|---|---|---|
| ROCm + Qwen MoE on gfx1030 crashes (#19880) | **HARD CONSTRAINT** | Use Vulkan, not ROCm, for inference. Re-evaluate if/when #19880 closes |
| gfx1030 not in ROCm 7.x official supported-GPU matrix | **HARD CONSTRAINT** | Same conclusion: Vulkan only. ROCm install is for `rocm-smi` and `pp_power_cap`, not inference |
| Vulkan multi-GPU layer-split regressions (#15974) | **HARD CONSTRAINT** | Per-card model assignment only; no `--split-mode layer --tensor-split` across V620s |
| Vulkan multi-GPU general slowdown (#16767) | structural | Reinforces no-tensor-split decision. Pooled-VRAM operations are off-limits |
| ROCm 7.2 + Qwen3.5-35B-A3B infinite wait (#20545) | informational | Even if #19880 closes, related ROCm bugs hit `coder-*` family. Stay Vulkan |
| Spec-TDP math (770W) exceeds 675W PDB — actual likely lower | check at Stage 1 | Measured 3× 3060 actual is ~360W (vs 510W spec). V620 actual unknown until §8 step 9 measures it. Decision matrix in §8 chooses external PSU vs derate vs neither |
| `coder-192k` (Rev 1) had insufficient Vulkan-allocator margin | structural | Replaced with `coder-160k`; 256K is a Phase 3 problem |
| Vulkan cold-load includes SPIR-V shader compilation (~60s first run) | LOW | `healthCheckTimeout: 240`. Subsequent loads from same llama.cpp build are cached |
| External PSU PS_ON bridge as SPOF for both V620s (Option A only) | MEDIUM | Test Add2PSU during install; monitor secondary PSU 12V via smart plug; keep spare bridge dongle on hand |
| Mis-converted reranker GGUF returns ~4.5e-23 scores | MEDIUM | Use a verified source (`ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` or Voodisss conversion); §9.2 catches it |
| AnythingLLM has no UI for external reranker provider | structural | §6.1.1 — Phase 2 hosts the service; integration is sidecar/MCP work or deferred |
| Mixed-vendor driver coexistence breakage on kernel/driver upgrade | MEDIUM | `apt-mark hold` both vendors' packages; test upgrades in maintenance window |
| AMDVLK takes precedence over RADV when both ICDs are installed | LOW | `Environment="AMD_VULKAN_ICD=RADV"` in systemd unit and llama-swap `env:` |
| Vulkan device enumeration order non-deterministic across reboots | MEDIUM | Use `--device <stable-name>` in production `cmd:` blocks, not numeric IDs |
| V620 thermal throttle in still air | MEDIUM | 3D-printed shroud + 80mm fan setup must maintain <85 °C steady |
| OpenCode tool-call template drift between CUDA and Vulkan builds | MEDIUM | Same llama.cpp commit on both build trees; §5.3 git rev-parse check enforces; §9.4 catches regressions |
| Vulkan FA fast-path requires NVIDIA coopmat2 (not on RDNA 2) | informational | `-fa on` on V620s uses non-fused FA variant. Validate FA actually engages via `--verbose` log |
| `--no-dkms` may leave amdgpu unbound on older kernels | LOW | Verify with `dmesg | grep amdgpu`; fall back to install without `--no-dkms` if binding fails |
| TurboQuant CUDA forks don't help — still deferred | informational | §11 of Phase 1 still applies. AMD-side TurboQuant ports (Pascal-SAPUI5, domvox) are RDNA3+ only — not gfx1030 |
| Loss of pooled 76 GB capacity for 70B-class models | structural | Phase 3 problem. Either bigger single cards or fixed Vulkan multi-GPU |

---

## 13. What changes vs Phase 1

A condensed summary for the Phase 1 reader:

| Phase 1 (3× 3060) | Phase 2 (2× V620 + 1× 3060) |
|---|---|
| Single binary `/opt/llama.cpp/build/bin/llama-server` | Two binaries: `/opt/llama.cpp-cuda/...` and `/opt/llama.cpp-vulkan/...` (pinned to same git commit) |
| `CUDA_VISIBLE_DEVICES=0,1,2` global env | Per-model `env:` in llama-swap config; `--device <name>` in `cmd:` for stable Vulkan device pinning |
| `--split-mode layer --tensor-split 1,1,1` | (removed — per-card model assignment) |
| `groups: chat: exclusive: true` includes all 4 chat models | Four groups: `embed` persistent, `rerank` (graduating to embed group post-validation), `rag` persistent, `coder` exclusive |
| `coder-256k` viable | Replaced by `coder-160k` (single 32 GB V620 limit + Vulkan allocator overhead) |
| No reranker | Reranker hosted on the 3060 (integration with AnythingLLM is a separate sidecar/MCP question — see §6.1.1) |
| One PSU (T7910 PDB) | T7910 PDB + optional external PSU bridged via Add2PSU — measurement-driven decision in §8 step 9 |
| `healthCheckTimeout: 180` | `healthCheckTimeout: 240` (Vulkan SPIR-V shader compile adds ~60s on cold loads) |
| `nvidia-smi` for monitoring | `nvidia-smi` + `rocm-smi` |
| Single vendor (NVIDIA) | Mixed-vendor — `apt-mark hold` both vendors; `AMD_VULKAN_ICD=RADV` to pin Mesa over AMDVLK |

Anything not in this table is unchanged — including AnythingLLM workspace configs, the no-think proxy, the MCP servers, the auto-updater, the LanceDB collections, and OpenCode's base URL.

---

## Appendix A — systemd units

Two layers, matching the LXC pattern from Phase 1: the **in-container** llama-swap unit (modified for Phase 2's mixed-vendor environment), and an **optional host-level** power-cap unit (only if taking the derate path from §4.3). The host-side LXC autostart is handled by `lxc config set llama-cpp boot.autostart true` (already done in Phase 1) — no additional host systemd unit needed.

### A.1 Inside the `llama-cpp` container — `llama-swap.service`

Updated from Phase 1: removed the `CUDA_VISIBLE_DEVICES=0,1,2` line (GPU isolation is now per-model in `config.yaml`); added `AMD_VULKAN_ICD=RADV` for the Vulkan side.

```ini
# Inside the llama-cpp LXC container at /etc/systemd/system/llama-swap.service
[Unit]
Description=llama-swap — model router for llama-server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ollama
Group=ollama
WorkingDirectory=/opt/llama-swap
# CUDA_VISIBLE_DEVICES intentionally omitted — set per-model in config.yaml
# AMD_VULKAN_ICD=RADV pins Mesa over AMDVLK (which would otherwise take precedence
# if both ICDs are installed). Per-model env: in config.yaml redundantly sets this
# for Vulkan backends; the unit-level value is defense-in-depth.
Environment="AMD_VULKAN_ICD=RADV"
ExecStart=/opt/llama-swap/llama-swap \
  --config /opt/llama-swap/config.yaml \
  --listen 127.0.0.1:11434
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

# Hardening (matches Phase 1 with PrivateDevices=no for both /dev/nvidia* and /dev/dri/*)
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=no            # required for /dev/nvidia* AND /dev/dri/*
ReadWritePaths=/opt/llama-swap /opt/gguf /run/llama-swap
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=no    # required for both CUDA JIT and Vulkan SPIR-V JIT
RestrictSUIDSGID=yes
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
AmbientCapabilities=
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

Push and reload from host:

```bash
# --- HOST shell ---
lxc file push /tmp/llama-swap.service llama-cpp/etc/systemd/system/llama-swap.service
lxc exec llama-cpp -- systemctl daemon-reload
lxc exec llama-cpp -- systemctl enable llama-swap
```

`Conflicts=ollama.service` from Phase 1's earlier draft is still removed — Ollama is in a separate LXC and isn't running anyway. `PrivateDevices=no` covers both `/dev/nvidia*` (3060) and `/dev/dri/*` (V620s). `MemoryDenyWriteExecute=no` is required for both CUDA JIT and Vulkan SPIR-V shader compilation.

### A.2 On the host — V620 power-cap (Option B from §4.3 only)

The V620 power cap runs **on the host**, not inside the container — sysfs writes to `/sys/class/drm/cardN/device/.../power1_cap` need access to host's GPU device nodes, and the LXC container only sees GPUs through DRM render-node passthrough (no `/sys/class/drm` write permissions). This is the one piece of Phase 2 that lives at host scope.

`/usr/local/bin/v620-powercap.sh` (on host):

```bash
#!/bin/bash
set -euo pipefail
TARGET_UW=250000000   # 250 W in microwatts
for card in /sys/class/drm/card*/device; do
  vendor=$(cat "$card/vendor" 2>/dev/null) || continue
  [ "$vendor" = "0x1002" ] || continue   # AMD only
  for cap in "$card"/hwmon/hwmon*/power1_cap; do
    [ -e "$cap" ] || continue
    cap_min=$(cat "${cap}_min" 2>/dev/null || echo 0)
    cap_max=$(cat "${cap}_max" 2>/dev/null || echo 999999999)
    if [ "$TARGET_UW" -lt "$cap_min" ] || [ "$TARGET_UW" -gt "$cap_max" ]; then
      logger -p daemon.warning "v620-powercap: $TARGET_UW out of range [$cap_min, $cap_max] for $cap; skipping"
      continue
    fi
    echo "$TARGET_UW" > "$cap"
  done
done
```

The hardened systemd companion unit:

```ini
# /etc/systemd/system/v620-powercap.service
[Unit]
Description=Set V620 power cap on boot
After=systemd-udev-settle.service multi-user.target
ConditionPathExistsGlob=/sys/class/drm/card*/device/hwmon/hwmon*/power1_cap

[Service]
Type=oneshot
ExecStart=/usr/local/bin/v620-powercap.sh
RemainAfterExit=yes
# Hardening — script needs root to write sysfs but nothing else
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=no
ReadWritePaths=/sys/class/drm
NoNewPrivileges=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
```

---

## Appendix B — file layout

Two filesystems, matching the Phase 1 LXC layout — the **host** filesystem (bind-mounted GGUFs, host-side scripts) and the **`llama-cpp` LXC container's** filesystem (build artifacts, llama-swap config, in-container systemd units).

### B.1 Host filesystem

```text
/opt/                              # host paths
├── gguf/                          # bind-mounted into the llama-cpp LXC at /opt/gguf
│   ├── qwen2.5-32b-instruct-q4_K_M.gguf
│   ├── qwen3.6-35b-a3b-UD-Q4_K_M.gguf
│   ├── qwen3-embedding-0.6B-f16.gguf
│   └── qwen3-reranker-0.6B-q8_0.gguf       (new in Phase 2)
├── migration/                     # host-side migration tooling
│   ├── test-embedding-parity.py   # Phase 1 §6.1, reused
│   └── longctx-smoke.py           # Phase 1 §6.4, reused
└── backups/                       # mode 700, owned root

/usr/local/bin/                    # host scripts
└── v620-powercap.sh               # only if Option B (§4.3) is chosen at Stage 1 step 13

/etc/systemd/system/               # host systemd units
└── v620-powercap.service          # only if Option B (§4.3) is chosen

/var/lib/lxd/containers/           # (or /var/lib/incus/, depending on stack)
├── ollama/                        # stopped/disabled since Phase 1 cutover; kept for rollback
├── anythingllm/                   # unchanged
├── nothink-proxy/                 # unchanged
├── (other existing LXCs)/         # unchanged
└── llama-cpp/                     # provisioned in Phase 1 §3.2.1; reconfigured in Phase 2
```

### B.2 Inside the `llama-cpp` LXC container

```text
/opt/                              # paths inside the container
├── llama.cpp-cuda/                # Phase 1 build, renamed in §5.3
│   └── build/bin/llama-server     # CUDA, runs on 3060
├── llama.cpp-vulkan/              # NEW in Phase 2 §5.3
│   └── build/bin/llama-server     # Vulkan, runs on V620s
├── llama.cpp -> llama.cpp-cuda    # legacy symlink for Phase 1 scripts
├── llama-swap/
│   ├── llama-swap
│   ├── config.yaml                # symlink to whichever phase/stage config is active
│   ├── config.phase1.yaml         # Phase 1 baseline (preserved for rollback)
│   ├── config.stage1.yaml         # Phase 2 Stage 1 transitional (§8 step 10)
│   └── config.yaml.phase2-final   # Phase 2 final state (§7) — symlink target post-Stage-2
└── gguf/                          # bind-mount of host's /opt/gguf — same files

/etc/systemd/system/               # inside-container systemd units
└── llama-swap.service             # Appendix A.1
```

### B.3 LXC snapshots (host-managed)

```text
lxc info llama-cpp | grep -A 10 Snapshots
# Expected entries (timestamps will vary):
#   pre-phase2-stage1   — taken §3 step 6, restoration target for Stage 1 rollback
#   pre-phase2-stage2   — taken §8 Stage 2 step 1, restoration target for Stage 2 rollback
```

Existing service directories (`/opt/anythingllm`, `/opt/nothink-proxy`, the three MCP containers, `/opt/vcf-doc-updater`, `/opt/searxng`) are unchanged.

---

## License

This document inherits the licensing of the parent reference: text under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), embedded code snippets under [MIT](https://opensource.org/licenses/MIT).
