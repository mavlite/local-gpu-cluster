# Local GPU Cluster v2 — Technical Reference

**ASUS ProArt X870E-Creator · Ryzen 7600 · 2× AMD V620 32 GB · Proxmox VE 9.x · llama.cpp · AnythingLLM · MCP**

![Platform](https://img.shields.io/badge/platform-Proxmox%20VE%209-informational)
![Hypervisor](https://img.shields.io/badge/containers-LXC%20%2B%20Docker-blue)
![GPU-AMD](https://img.shields.io/badge/GPU-2%C3%97%20V620%2032GB-red)
![Runtime](https://img.shields.io/badge/runtime-llama.cpp-000000)
![Models](https://img.shields.io/badge/models-Qwen%203.6%20family-blue)
![RAG](https://img.shields.io/badge/RAG-AnythingLLM%20%2B%20LanceDB-0a6cf5)
![SpecDecode](https://img.shields.io/badge/feature-speculative%20decoding-orange)
![Revision](https://img.shields.io/badge/revision-2-lightgrey)
![License](https://img.shields.io/badge/license-CC%20BY--SA%204.0-yellow)

> ### V620-only pivot — complete
> This v2 reference originally described "2× V620 + 1× RTX 3060" with the 3060 acting as a separate workload-isolation tier. **The 3060 has been removed.** The cluster runs everything on the two V620s as additional `llama-server` processes pinned per-card via `--main-gpu`. See §1.3 for the rationale; §6 (the old CUDA stack) has been retired.
>
> For the operational deployment commands and per-phase verification, see [`setup-runbook.md`](./setup-runbook.md). For the bootstrap scripts that automate the runbook, see [`scripts/README.md`](./scripts/README.md).

End-to-end build of the second-generation local LLM cluster. Replaces the v1 setup (Dell T7910 + 3× RTX 3060) with a purpose-built workstation: ASUS ProArt X870E-Creator, Ryzen 7600, 64 GB pooled VRAM across two AMD V620 server cards. With the V620-only pivot, all inference (chat + embedder + reranker) runs on the V620 pool — there is no longer a separate NVIDIA tier.

The big architectural shifts vs. v1: Proxmox VE host with one LXC per service (replacing bare-metal Ubuntu), llama.cpp replacing Ollama for performance and speculative-decoding support, and a request-aware router replacing the dual-port no-think proxy.

> **Scope note.** This document describes one verified configuration on the author's hardware as of the revision date. Paths, IPs, and ports are illustrative — adapt to your environment. Versions move quickly; check upstream documentation before acting on version-specific details here. For the previous-generation setup on dual Xeon hardware with NVIDIA-only GPUs, see [`local-gpu-cluster-reference.md`](./local-gpu-cluster-reference.md) (v1).

---

## Table of Contents

- [1. Hardware](#1-hardware)
  * [1.1 Host: ASUS ProArt X870E-Creator + Ryzen 7600](#11-host-asus-proart-x870e-creator--ryzen-7600)
  * [1.2 GPUs: 2× AMD Radeon Pro V620](#12-gpus-2-amd-radeon-pro-v620--1-nvidia-rtx-3060)
  * [1.3 Why this GPU mix](#13-why-this-gpu-mix)
  * [1.4 V620 active cooling: shroud kit + 80 mm fans + 12 V Molex](#14-v620-active-cooling-shroud-kit--80-mm-fans--12-v-molex)
  * [1.5 GPU support brackets](#15-gpu-support-brackets)
- [2. Proxmox VE Host Setup](#2-proxmox-ve-host-setup)
  * [2.1 Install Proxmox VE 9.x](#21-install-proxmox-ve-9x)
  * [2.2 BIOS configuration](#22-bios-configuration)
  * [2.3 IOMMU and kernel modules](#23-iommu-and-kernel-modules)
  * [2.4 ZFS storage layout for shared model files](#24-zfs-storage-layout-for-shared-model-files)
  * [2.5 Network bridge](#25-network-bridge)
- [3. PCIe Topology and Link Verification](#3-pcie-topology-and-link-verification)
- [4. LXC Provisioning Strategy](#4-lxc-provisioning-strategy)
  * [4.1 Why LXC for GPU workloads](#41-why-lxc-for-gpu-workloads)
  * [4.2 Bare-metal in LXC vs Docker-in-LXC](#42-bare-metal-in-lxc-vs-docker-in-lxc)
  * [4.3 GPU device passthrough to LXC via cgroups](#43-gpu-device-passthrough-to-lxc-via-cgroups)
- [5. llama.cpp ROCm LXC (V620 Stack)](#5-llamacpp-rocm-lxc-v620-stack)
  * [5.1 Container creation](#51-container-creation)
  * [5.2 GPU device passthrough configuration](#52-gpu-device-passthrough-configuration)
  * [5.3 ROCm 6.x install on Ubuntu 24.04](#53-rocm-6x-install-on-ubuntu-2404)
  * [5.4 llama.cpp build with HIP](#54-llamacpp-build-with-hip)
  * [5.5 Model selection and download](#55-model-selection-and-download)
  * [5.6 llama-server systemd unit](#56-llama-server-systemd-unit)
  * [5.7 Tensor split tuning across the two V620s](#57-tensor-split-tuning-across-the-two-v620s)
- [6. (removed) llama.cpp CUDA LXC — 3060 stack retired](#6-removed-llamacpp-cuda-lxc--3060-stack-retired)
- [7. Router LXC (Per-Request Decision + Keepalive)](#7-router-lxc-per-request-decision--keepalive)
  * [7.1 Role and design](#71-role-and-design)
  * [7.2 Routing logic](#72-routing-logic)
  * [7.3 Per-request thinking-block decision](#73-per-request-thinking-block-decision)
  * [7.4 SSE keepalive](#74-sse-keepalive)
  * [7.5 Implementation](#75-implementation)
  * [7.6 systemd unit](#76-systemd-unit)
- [8. AnythingLLM LXC (Docker, Clean Install)](#8-anythingllm-lxc-docker-clean-install)
  * [8.1 Container creation](#81-container-creation)
  * [8.2 Docker install in LXC](#82-docker-install-in-lxc)
  * [8.3 AnythingLLM compose stack](#83-anythingllm-compose-stack)
  * [8.4 Provider configuration: point at the router](#84-provider-configuration-point-at-the-router)
  * [8.5 Workspace tuning (clean install)](#85-workspace-tuning-clean-install)
  * [8.6 Re-ingest VCF and SDG corpora from markdown](#86-re-ingest-vcf-and-sdg-corpora-from-markdown)
- [9. MCP Server Stack (Migrated)](#9-mcp-server-stack-migrated)
- [10. VCF Documentation Auto-Updater (Migrated)](#10-vcf-documentation-auto-updater-migrated)
- [Appendix A — IP and Service Port Map](#appendix-a--ip-and-service-port-map)
- [Appendix B — Directory Layout (Host and LXCs)](#appendix-b--directory-layout-host-and-lxcs)
- [Appendix C — Smoke Tests](#appendix-c--smoke-tests)
- [Appendix D — LXC Provisioning Scripts (community-scripts style)](#appendix-d--lxc-provisioning-scripts-community-scripts-style)

---

## Revision history

| Rev | Date | Notes |
| --- | --- | --- |
| 1 | TBD | Initial v2 reference: ASUS ProArt X870E-Creator + Ryzen 7600 host, 2× V620 + 1× 3060 GPU mix, Proxmox VE 9.x with LXC services, llama.cpp replacing Ollama, request-aware router with per-request thinking-strip decision, AnythingLLM clean install with re-ingest from existing markdown sources. |

---

## 1. Hardware

### 1.1 Host: ASUS ProArt X870E-Creator + Ryzen 7600

| Component | Spec |
| --- | --- |
| Form factor | Mid-tower (Lian Li Lancool 217 Black, SKU LAN217X — walnut wood front accent, non-RGB, steel side panel, built-in 6-channel PWM fan hub, built-in adjustable GPU support bracket, dual PSU mount positions) |
| Motherboard | ASUS ProArt X870E-Creator WiFi (ATX, X870E chipset) |
| CPU | AMD Ryzen 7600 (6c/12t, 65 W TDP, 88 W PPT) |
| CPU cooler | Thermalright Phantom Spirit 120 EVO (dual tower, 7 heatpipes, twin 120 mm PWM) |
| RAM | DDR5 (existing — re-used from previous build) |
| PSU | be quiet! Power Zone 2 1200 W (80+ Platinum efficiency, 140 mm Pure Wings 3 rifle-bearing fan, semi-passive zero-RPM mode, dual 12V-2x6 connectors, HEC OEM platform) |
| Storage | NVMe boot drive (M.2_1, PCIe 5.0 x4 from CPU) + secondary NVMe for `/tank` (M.2_3, chipset PCIe 4.0 x4) |
| Chassis fans | **Stock (pre-installed in Lancool 217):** 2× 170mm front intake (1550 RPM, 142.56 CFM each, 3.34 mmH₂O — feed cool air directly to GPU stack in "GPU mode" lower position), 2× 120mm reverse-blade bottom intake on PSU shroud (1950 RPM, 71.1 CFM, 1.99 mmH₂O — push air UP into bottom of GPU stack), 1× 140mm rear exhaust (1800 RPM). **Added:** 3× ARCTIC P14 Pro PST 140mm as top exhaust (PST-chained to single motherboard header). 2× P14 PSTs held as cold spares. Total 8 fans deployed, all connected via the case's built-in 6-channel PWM hub. |

The ProArt X870E-Creator is the right board choice for this build for three specific reasons that matter to a Proxmox + multi-GPU deployment:

1. **Three usable PCIe slots with adequate bandwidth for all three GPUs.** The CPU-attached x16 slot bifurcates to x8/x8 when both are populated. The chipset-attached third slot is PCIe 4.0 x4. Every GPU gets at least PCIe 4.0 x4 = PCIe 3.0 x8-equivalent, which is plenty for inference. See §3 for actual link verification.
2. **Clean IOMMU groupings.** Multiple Proxmox forum reports confirm this board has unusually well-isolated IOMMU groups for a consumer motherboard — no `pcie_acs_override` needed, no kernel patches. Confirmed working with simultaneous multi-GPU passthrough.
3. **All-black workstation aesthetic, no on-board RGB.** The board has Aura Sync headers but no on-board lighting itself, so it stays dark inside the closed case.

The 7600 is intentionally a lightweight CPU. For LLM inference, the CPU is just a coordinator — the GPUs do the work — and the 65 W chip lets the Phantom Spirit cooler run nearly silent. If the host is later repurposed as a daily driver where CPU compute matters, swap to a 7950X (170 W TDP) and step the cooler up to a 360 mm AIO.

### 1.2 GPUs: 2× AMD Radeon Pro V620

| Spec | V620 |
| --- | --- |
| Architecture | RDNA 2 (Navi 21, `gfx1030`) |
| VRAM | 32 GB GDDR6 |
| Memory bandwidth | 512 GB/s |
| FP16 compute | ~38 TFLOPS |
| TDP | 300 W (passive — needs forced air); measured sustained ~225 W under llama.cpp workloads |
| Form factor | Dual-slot, full-height, **passive** (server card) |
| PCIe | 4.0 x16 → operates at 4.0 x8 on this board's CPU-attached slots (PCIE_1 + PCIE_2) |
| Pool | **64 GB** combined across the two V620s |

The V620 is AMD's professional inference card based on the same Navi 21 silicon as the 6900 XT but configured for high-density server deployments — passive cooling, ECC GDDR6 (configurable), full-height dual-slot. ROCm 6.x and 7.x officially support it (`gfx1030` target).

### 1.3 Why this GPU mix (V620-only)

**Why V620s for primary inference.** The previous v1 setup pooled 36 GB across three 3060s and was VRAM-limited — large models at high context filled the pool with no headroom for concurrent requests. Two V620s pool 64 GB, nearly double, while reducing card count from three to two — fewer slots, fewer power connectors, fewer thermal hot-spots. The V620's higher FP16 throughput and 512 GB/s bandwidth (vs. 360 GB/s on the 3060) helps with the Qwen 3.5 35B target model.

**Why no NVIDIA tier.** Earlier revisions of this document described a "2× V620 + 1× RTX 3060" hybrid where the 3060 ran the embedder + reranker + a small fast-chat model as a workload-isolation tier. **That tier has been removed.** All embedding, reranking, and chat now run as separate `llama-server` processes inside a single V620 LXC, with per-card pinning via `--main-gpu`:

- **Chat** (Qwen3.6-35B-A3B UD-Q4_K_M, single in-flight request at full 256K ctx; spec-decode disabled — see note below): tensor-split across both V620s (`--tensor-split 1,1`).
- **Embedder** (Qwen3-Embedding-0.6B Q8_0): pinned to V620 #1 via `--main-gpu 0` and `HIP_VISIBLE_DEVICES=0`. Uses `--pooling last` (Qwen3-Embedding uses the final `<|endoftext|>` token; `cls` produces wrong embeddings).
- **Reranker** (BGE Reranker v2-m3 OR Qwen3-Reranker-0.6B): pinned to V620 #2 via `--main-gpu 1` and `HIP_VISIBLE_DEVICES=1`. Uses `--embeddings --pooling rank --reranking`.

VRAM budget (steady state, 256K chat context, q8_0 KV, `--parallel 1` on chat, `--parallel 4` on embed): chat 22 GB weights + ~11 GB single-slot KV split across cards + embedder 1.2 GB on card #1 + reranker 1.5 GB on card #2 ≈ **~35 GB used of the 64 GB pool**. Per-card ≈ 17-18 GB. ~29 GB headroom. No draft model: spec-decoding is disabled — see the note below.

**Trade-off accepted.** The 3060 used to isolate bulk RAG ingest from interactive chat at the hardware level. With the V620-only topology, that isolation moves up the stack to the router (LXC 153): `asyncio.Semaphore`-based admission control, per-IP rate limiting via `slowapi`, fail-open SSE on upstream 5xx, and a priority lane that throttles bulk embed when chat has in-flight requests. A 5K-document re-embed will briefly contend with chat for compute (~15–25 minutes total wall-clock on V620 with `--parallel 8`, vs the prior 30–60 minutes on the 3060). For typical workloads where bulk ingest is rare, this is the right trade.

**Future expansion.** PCIE_3 (bottom slot, PCIe 4.0 x4 from chipset) is now empty. Prioritized future-use options: 10 GbE NIC for cluster federation (Intel X710), HBA if `/tank` outgrows two NVMes (LSI 9300-8i), PCIe audio capture for a future Whisper LXC, or an OCuLink adapter for an external GPU. Adding a third V620 is power-feasible (~340 W headroom at 80% derate of the 1200 W PSU; matches the user's memory note that 3060s drew ~120 W measured vs 170 W spec → V620s similarly under-draw vs 300 W TDP), but slot count and cooling are the constraints, not power.

> **Note on speculative decoding architecture:** llama.cpp's speculative decoding requires both target and draft models in the **same** `llama-server` process (via `-md` / `--model-draft` flag) — they cannot communicate across LXCs or over the network. **Spec-decode is currently disabled on Qwen3.6** because the 35B-A3B target has vocab 248,320 while available small variants (Qwen3-0.6B at vocab 151,936) have an incompatible tokenizer that llama.cpp refuses to load as a draft. Until a vocab-matched Qwen3.6 small variant ships, the chat unit boots with the 22 GB target alone and logs `common_speculative_init: no implementations specified for speculative decoding` (expected behavior, not a bug). Qwen3.6-35B-A3B is MoE with ~3 B active params/token, so it's already fast — the ~1.5-2× throughput hit from disabled spec-decode is an accepted trade-off. Re-enable via `LLAMA_DRAFT_REPO` in `scripts/config.env` once a compatible draft is available.

### 1.4 V620 active cooling: shroud kit + 80 mm fans + 12 V Molex

V620s ship without integral fans — they expect a server chassis with high-static-pressure 60–80 mm fans pushing air through their dense passive heatsinks along the length of the card. In a tower case like the Lancool 217, ambient airflow alone won't cool a ~225 W passive card; the cards will thermal-throttle within minutes. The 80mm shroud kit converts each V620 into a self-contained active-cooled card, and the case's 2× 170mm front fans plus 2× 120mm bottom-shroud fans feed it abundant cool air to push through.

The fix is an aftermarket shroud kit that bolts an 80 mm fan to the rear of each card. The kit used here is the [GF Computers V620/V520/BC-160 cooling fan shroud](https://www.ebay.com/itm/285399696488) — a 3D-printed adapter that mounts a standard 80×80×25 mm fan to the rear of the card and channels its airflow through the heatsink fins.

| Component | Pick | Purpose |
| --- | --- | --- |
| Shroud kit | GF Computers V620 80 mm shroud (×2) | Channels air through heatsink |
| Shroud fans | Noctua NF-A8 PWM (×2 + 1 spare) | Quiet, high static pressure 80 mm |
| Power | SATA-to-3-pin fan adapter @ 12 V (constant on) | Independent of motherboard control |

Historical note: earlier revisions of this design used a SATA-to-fan adapter for constant-12V power to the V620 shroud fans. This was based on the originally-specified Define 7 XL case which lacked an integrated fan hub. With the Lancool 217 case selection, V620 fan power and control is now handled through the case's built-in 6-channel PWM hub, with motherboard PWM signal driven by the software bridge described above. The constant-12V SATA approach remains a valid fallback (Approach C in the runbook) if the software bridge proves unreliable.

For powering and **controlling speed** of the V620 shroud fans, the recommended approach is:

**Approach A (recommended): Built-in fan hub + software bridge.** The Lancool 217 includes a 6-channel PWM fan hub pre-installed behind the motherboard tray. Plug all 4× NF-A8 PWM fans into spare channels on the hub (the case's own 5 stock fans use other channels). The hub's master PWM input plugs into a single CHA_FAN header on the motherboard. On the Proxmox host, run a systemd service that reads V620 temperatures from inside the V620 LXC (via a shared bind-mount file) and writes PWM duty cycle to that motherboard hwmon endpoint — which the hub then mirrors to all connected fans, including the NF-A8s.

Power budget check: NF-A8 PWM @ 0.08A each × 4 = 0.32A; case stock fans add ~0.6A; the Lancool 217 hub is rated for 24W (2A) at 12V, well within budget for all fans combined. The hub itself draws power from a dedicated SATA connector.

This gives you V620-temperature-driven control for the entire fan ecosystem: idle is whisper-quiet (~25% PWM ≈ low-RPM operation across all fans), and fans ramp to full only when V620 edge temp exceeds 80°C. Implementation details are in the runbook (Step 5.13).

**Approach B: Aquacomputer Quadro fan controller (~$70).** A dedicated USB-controlled hardware fan controller with thermistor temperature inputs. Stick a thermistor on each V620 heatsink, wire the V620 fans to one Quadro PWM channel, and configure curves via `liquidctl`. Independent of OS state — works from boot regardless of ROCm/Proxmox health. Bypasses the case's hub entirely.

**Approach C (fallback): SATA constant 12V.** Power the V620 fans from a SATA-to-fan splitter at constant 12V (Noctua NA-FH1 hub, third-party SATA splitter, or NA-SAC5+NA-SYC1 combo). Reliable but always full RPM (24 dB(A) combined for 4× NF-A8). Use this only if Approach A fails or you don't want the software complexity.

### 1.5 GPU support brackets

V620 cards with the shroud installed weigh ~1.4–1.6 kg each. Two heavy GPUs in a vertical-tower orientation still create real cantilever stress on the PCIe slots over time. The Lancool 217 includes a built-in adjustable GPU support bracket on the motherboard tray — useful for the top GPU (V620 #1 in PCIE_1). For the second V620 (PCIE_2) and as redundant support for the top card, two **upHere G205** GPU brace supports (~$10 each, anodized aerospace aluminum, height-adjustable jack-stand design, supports single or dual-slot cards) sit on the PSU shroud floor and prop up the far end of each card. Using 2× G205 + the built-in bracket gives redundant support for the heaviest card and consistent fitment for the second. The G205 has a magnetic + rubber-pad base so it doesn't require screws into the PSU shroud, and adjusts via a telescopic screw to dial in exact height per card. Pre-pivot builds that ran a 3060 in PCIE_3 used a third G205 for it — no longer needed.

---

## 2. Proxmox VE Host Setup

### 2.1 Install Proxmox VE 9.x

Proxmox VE 9.x (released Q3 2025) is the recommended baseline — it ships kernel 6.14, which has mature support for the X870E chipset, AMD AGESA 1.2.x, and the V620 (`gfx1030`) via the upstream `amdgpu` driver. Earlier 8.x releases on kernel 6.8 work but lack some sensor monitoring and have less-tested AGESA handling for 9000-series Ryzen.

Standard Proxmox install via USB:

1. Download `proxmox-ve_9.x.iso` from `https://www.proxmox.com/downloads`.
2. Write to USB with `dd` or Rufus (DD mode, not ISO mode).
3. Boot the installer with the USB plugged into a rear USB-A port (front-panel USB on this case routes through the chassis controller and occasionally has init-order issues).
4. Use **ZFS RAID0** on the boot NVMe for the root filesystem. Single-disk ZFS gives you snapshots, dataset hierarchies, and `zfs send` for backup at trivial cost.
5. Set hostname `gpu-cluster.local` (or per local convention) and a static management IP on the 10 GbE Marvell port (`enp4s0` typically).
6. After first boot, disable the Proxmox enterprise repo, enable the no-subscription repo, and run `apt update && apt full-upgrade`.

```bash
# /etc/apt/sources.list.d/pve-no-subscription.list
deb http://download.proxmox.com/debian/pve bookworm pve-no-subscription
```

### 2.2 BIOS configuration

Before the first Proxmox boot, configure BIOS for virtualization and GPU passthrough. From the ProArt X870E-Creator UEFI:

| Setting | Path | Value |
| --- | --- | --- |
| SVM Mode | Advanced → CPU Configuration | **Enabled** |
| IOMMU | Advanced → AMD CBS → NBIO Common Options | **Enabled** |
| ACS | Advanced → AMD CBS → NBIO Common Options → IOMMU → ACS | **Enabled** |
| Above 4G Decoding | Advanced → PCI Subsystem Settings | **Enabled** |
| Re-Size BAR Support | Advanced → PCI Subsystem Settings | **Enabled** |
| PCIe ASPM (I226-V NIC) | Advanced → PCI Subsystem | **Disabled** |
| Resizable BAR / SR-IOV | Advanced → PCI | **Enabled** |
| Secure Boot | Boot → Secure Boot | **Disabled** (simplifies kernel module signing) |
| CSM | Boot → CSM | **Disabled** (UEFI only) |

Confirm BIOS revision is **1605 or later** before installing. Earlier revisions have known PCIe enumeration quirks with the X870E AGESA on multi-GPU configurations.

### 2.3 IOMMU and kernel modules

Edit the GRUB cmdline to enable AMD-Vi and pre-load VFIO modules:

```bash
# /etc/default/grub
GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt"
```

Then:

```bash
update-grub
proxmox-boot-tool refresh
```

For LXC GPU passthrough (which is what we use here, not VFIO), VFIO modules aren't strictly required — but loading them anyway makes future VM-based passthrough trivial.

```bash
# /etc/modules
vfio
vfio_iommu_type1
vfio_pci
```

Reboot. Confirm IOMMU is active:

```bash
dmesg | grep -e DMAR -e IOMMU -e AMD-Vi
# Expect: AMD-Vi: Interrupt remapping enabled
#         AMD-Vi: Virtual APIC enabled
#         perf/amd_iommu: Detected AMD IOMMU #0
```

And confirm IOMMU groups are clean:

```bash
for d in /sys/kernel/iommu_groups/*/devices/*; do
  n=${d#*/iommu_groups/*}; n=${n%%/*}
  printf 'IOMMU Group %s ' "$n"
  lspci -nns "${d##*/}"
done | sort -V
```

Each GPU should appear in its own group with only its associated audio-function device. If a GPU shares a group with unrelated chipset devices, IOMMU isolation is broken — verify the BIOS settings above. The ProArt X870E-Creator should produce clean groupings out of the box.

### 2.4 ZFS storage layout for shared model files

llama.cpp gguf model files are large (the Qwen 3.5 35B at Q4 is ~22 GB). Storing the same model in three different LXC root filesystems triples the disk usage and complicates updates. Instead, create a host-level ZFS dataset and bind-mount it read-only into each GPU LXC.

```bash
# Assuming the secondary NVMe at /dev/nvme1n1 is unused
zpool create -o ashift=12 tank /dev/nvme1n1
zfs create tank/models
zfs create tank/anythingllm
zfs create tank/mcp

# Recommended properties for model files (large, sequential reads, infrequent writes)
zfs set compression=lz4 tank/models
zfs set atime=off tank/models
zfs set recordsize=1M tank/models
```

Resulting layout:

```
/tank/
├── models/                      # gguf files, mounted ro into both llama.cpp LXCs
│   └── .cache/                       # HuggingFace cache layout (llama-server writes here via --hf-repo)
│       └── models--<org>--<repo>/snapshots/<rev>/<file>.gguf
│   # Currently-deployed (verified live):
│   #   unsloth/Qwen3.6-35B-A3B-GGUF       -> Qwen3.6-35B-A3B-UD-Q4_K_M.gguf  (chat target ~22 GB)
│   #   Qwen/Qwen3-Embedding-0.6B-GGUF     -> Qwen3-Embedding-0.6B-Q8_0.gguf  (embedder ~1 GB)
│   #   gpustack/bge-reranker-v2-m3-GGUF   -> bge-reranker-v2-m3-Q4_K_M.gguf  (reranker ~1.5 GB)
├── anythingllm/                 # AnythingLLM persistent storage
│   └── storage/
└── mcp/                         # MCP container working state
    └── vcf-doc-updater/
```

Bind-mounting into LXCs is configured per-container in `/etc/pve/lxc/<vmid>.conf`:

```
mp0: /tank/models,mp=/opt/models,ro=1
```

### 2.5 Network bridge

LXCs are on a flat bridge (`vmbr0`) with each container getting its own DHCP-assigned IP from the LAN. This is the same pattern as the v1 setup and keeps things simple — every service is reachable from any other host on the LAN.

```
# /etc/network/interfaces (excerpt)
auto vmbr0
iface vmbr0 inet static
    address 192.168.6.150/24       # host management IP
    gateway 192.168.6.1
    bridge-ports enp4s0            # 10 GbE Marvell AQC113CS
    bridge-stp off
    bridge-fd 0
```

Reserve static DHCP leases on your router for each LXC IP so MCP/AnythingLLM endpoints don't move. Suggested IP plan:

| Service | LXC name | IP |
| --- | --- | --- |
| llama.cpp ROCm V620 (chat + embed + rerank, three llama-server units) | `llamacpp-amd` | 192.168.6.151 |
| Router (auth + admission + Prometheus) | `llm-router` | 192.168.6.153 |
| AnythingLLM | `anythingllm` | 192.168.6.154 |
| MCP stack | `mcp-stack` | 192.168.6.155 |
| SearXNG (optional) | `searxng` | 192.168.6.156 |

Note: LXC 152 (`llamacpp-nv`, the old 3060 LXC) was destroyed in the V620-only pivot. Pre-pivot builds had a separate NVIDIA LXC at `192.168.6.152` for embedder + reranker — that role is now served by additional `llama-server` instances inside LXC 151 (ports 8082 and 8083).

---

## 3. PCIe Topology and Link Verification

The ProArt X870E-Creator presents three PCIe slots:

| Slot | Source | Lanes | Speed | Card |
| --- | --- | --- | --- | --- |
| PCIE_1 (top) | CPU | x16 (drops to x8 when slot 2 populated) | PCIe 5.0 | V620 #1 |
| PCIE_2 | CPU | x8 (active when slot 1 also populated) | PCIe 5.0 | V620 #2 |
| PCIE_3 (bottom) | X870E chipset | x4 | PCIe 4.0 | (empty — V620-only build; pre-pivot held RTX 3060) |

V620 is a PCIe 4.0 card, so PCIe 5.0 negotiation drops to 4.0 — expected and fine. The 3060 is also PCIe 4.0, also drops to 4.0 in the chipset slot.

Verify enumeration:

```bash
# V620s are class 0380 "Display controller" (headless server cards) — not class 0300 VGA or
# 0302 3D controller, so the naive `vga|3d` filter misses them. Filter by Navi 21 ID instead.
lspci -nn | grep -iE "navi 21|\[1002:73a1\]"
# Expect: two AMD Radeon Pro V620 entries [1002:73a1]. Pre-pivot builds also showed an
# NVIDIA GA106 (10de:2504) in PCIE_3; the 3060 was removed in the V620-only pivot.
```

Verify link width and speed:

```bash
for bus in $(lspci -D | grep -iE "vga|3d controller" | awk '{print $1}'); do
  echo "=== $bus ==="
  lspci -vvv -s $bus | grep -E "LnkCap:|LnkSta:"
done
```

Expected:

- V620 #1 in PCIE_1: `LnkSta: Speed 16GT/s, Width x8` (PCIe 4.0 x8)
- V620 #2 in PCIE_2: `LnkSta: Speed 16GT/s, Width x8`
- PCIE_3 (bottom): empty in V620-only builds. Pre-pivot 3060 in this slot showed `LnkSta: Speed 16GT/s, Width x4`.

If any card shows `Width x1` or `Speed 2.5GT/s`, the slot is misconfigured. Check that **Above 4G Decoding** and **Re-Size BAR** are enabled (§2.2) and that no M.2 drive is stealing lanes from the slot in question. On this board, populating M.2_2 drops PCIE_2 from x8 to x4 — leave M.2_2 empty unless you accept that bandwidth hit on V620 #2.

### Driver-level verification

After ROCm and CUDA are installed in their respective LXCs (§5 and §6), repeat the link check from each container:

```bash
# In llamacpp-amd LXC (the only GPU LXC in the V620-only build)
rocm-smi --showpcie
# Expect both V620s at PCIe 4.0 x8
```

### Known X870E PCIe quirk

There is a documented X870E firmware bug where, after a VFIO Function Level Reset, the slot permanently downgrades from x16 to x8 until the next cold boot. This affects **VFIO passthrough to VMs**, not LXC cgroup-based passthrough, which doesn't issue FLRs. Since this build uses LXC for all GPU stacks, the bug doesn't bite. If a future workload needs VFIO-based GPU passthrough to a VM (e.g., a Bazzite gaming VM in PCIE_3), be aware: cold-boot recovery is required to restore x16 width after each VM stop.

---

## 4. LXC Provisioning Strategy

### 4.1 Why LXC for GPU workloads

LXC containers share the host kernel, which means a GPU passed through via cgroup device permissions appears in the container as the same `/dev/dri/render*` (AMD) or `/dev/nvidia*` (NVIDIA) node it would on bare metal — no virtualization overhead, no FLR-based reset issues, no driver-version mismatch between guest and host. For inference workloads where every percent of GPU throughput matters and where the workload is Linux-native, LXC beats VMs by a meaningful margin.

The trade-off is that LXC containers can't isolate GPUs from the host — the host kernel sees them too, and you have to be careful that the host doesn't try to bind a driver before the LXC takes over. In practice this means letting the host load `amdgpu` and the NVIDIA kernel module normally; the LXCs just get cgroup access to the device nodes.

### 4.2 Bare-metal in LXC vs Docker-in-LXC

Different services in this stack get different treatment:

**Bare-metal in LXC (no Docker layer):**
- `llamacpp-amd` — the V620 inference stack (chat + embed + rerank on ports 8080/8082/8083)
- `llm-router` — the request-aware router

These workloads either need direct GPU device access (the llama.cpp stacks) or are tiny and benefit from minimal abstraction (the router). Adding a Docker layer between LXC and the application would introduce a second cgroup translation for GPU devices, complicate driver/library version matching, and add nothing in return.

**Docker-in-LXC:**
- `anythingllm` — Docker is how AnythingLLM is officially distributed
- `mcp-stack` — the three custom MCP containers benefit from Docker's reproducibility
- `searxng` — official Docker image

These services have no GPU dependency, so the nesting overhead is negligible, and Docker buys real value for image distribution and reproducible rebuilds. Inside the LXC, install Docker as you would on bare-metal Ubuntu.

### 4.3 GPU device passthrough to LXC via cgroups

For an LXC to use a GPU, two things have to happen:

1. The host's cgroup must permit the LXC to access the device's major/minor numbers
2. The device node must appear inside the LXC's filesystem

Both are configured per-LXC in `/etc/pve/lxc/<vmid>.conf`. Examples in §5 (AMD) and §6 (NVIDIA).

To find the major/minor numbers for your devices on the host:

```bash
# AMD render and KFD nodes
ls -l /dev/dri/render* /dev/kfd
# crw-rw---- 1 root render 226, 128 ... /dev/dri/renderD128   (V620 #1)
# crw-rw---- 1 root render 226, 129 ... /dev/dri/renderD129   (V620 #2)
# crw-rw---- 1 root render 226, 130 ... /dev/dri/renderD130   (3060 — yes, the 3060 also has a render node)
# crw-rw-rw- 1 root render 234, 0   ... /dev/kfd

# NVIDIA character devices
ls -l /dev/nvidia*
# crw-rw-rw- 1 root root 195,   0 ... /dev/nvidia0
# crw-rw-rw- 1 root root 195, 255 ... /dev/nvidiactl
# crw-rw-rw- 1 root root 195, 254 ... /dev/nvidia-modeset
# crw-rw-rw- 1 root root 240,   0 ... /dev/nvidia-uvm
# crw-rw-rw- 1 root root 240,   1 ... /dev/nvidia-uvm-tools
# crw-rw-rw- 1 root root 506,   0 ... /dev/nvidia-caps/nvidia-cap1
```

Major numbers vary by kernel build, so check on your specific host. The render node enumeration order (which GPU is `renderD128` vs `renderD129`) corresponds to PCI bus order — always double-check with `lspci`.

---

## 5. llama.cpp ROCm LXC (V620 Stack)

### 5.1 Container creation

Create an unprivileged Ubuntu 24.04 LXC. From the Proxmox host:

```bash
pct create 151 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname llamacpp-amd \
  --cores 8 \
  --memory 32768 \
  --swap 8192 \
  --rootfs local-zfs:64 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=1 \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 0
```

Notes on these choices:
- `cores 8` — llama.cpp benefits from real cores for non-GPU layers (tokenization, sampling, scheduler). 8 of the 7600's 12 threads is a comfortable allocation.
- `memory 32768` — llama.cpp memory-maps gguf files; 32 GB host RAM is more than enough for swap-free loading and KV cache spillover handling.
- `rootfs local-zfs:64` — 64 GB root disk. Models live on the bind-mounted `/tank/models`, not in the LXC root.
- `features nesting=1` — required for some GPU monitoring tools that need access to kernel cgroups.

### 5.2 GPU device passthrough configuration

Edit `/etc/pve/lxc/151.conf` on the host:

```
# Bind-mount shared model storage (read-only)
mp0: /tank/models,mp=/opt/models,ro=1

# AMD render nodes (V620 #1 and #2)
lxc.cgroup2.devices.allow: c 226:128 rwm
lxc.cgroup2.devices.allow: c 226:129 rwm
lxc.mount.entry: /dev/dri/renderD128 dev/dri/renderD128 none bind,optional,create=file
lxc.mount.entry: /dev/dri/renderD129 dev/dri/renderD129 none bind,optional,create=file

# AMD KFD (compute) interface
lxc.cgroup2.devices.allow: c 234:* rwm
lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file

# Required for ROCm to work in unprivileged LXC
lxc.apparmor.profile: unconfined
lxc.cap.drop:
```

The `apparmor.profile: unconfined` line is unfortunate but currently required — the default Proxmox AppArmor profile blocks some syscalls ROCm uses for inter-GPU communication.

**Reliability fallback:** if you observe KFD-handshake failures, intermittent `rocm-smi` errors, or ROCm crashes after a few hours of uptime, the most reliable workaround is to switch this LXC to **privileged** mode. Privileged is *less* hardened (root inside the container is root on the host), but it bypasses several namespace edge cases that have caused issues with multi-GPU ROCm workloads on V620/CDNA hardware. The Strix Halo guide on the Proxmox forum explicitly requires privileged for the KFD handshake; experience varies for V620, so try unprivileged first and fall back to privileged only if needed.

To switch to privileged: stop the container, edit `/etc/pve/lxc/151.conf`, change `unprivileged: 1` to `unprivileged: 0`, and remove the `lxc.apparmor.profile: unconfined` line (privileged containers don't need it). Then restart.

Start the container:

```bash
pct start 151
pct enter 151
```

Verify GPU visibility from inside the LXC:

```bash
ls -l /dev/dri/ /dev/kfd
# Expect: renderD128, renderD129, and kfd present
```

### 5.3 ROCm 6.x install on Ubuntu 24.04

Inside the LXC:

```bash
apt update && apt install -y wget gnupg2 build-essential cmake git
mkdir --parents --mode=0755 /etc/apt/keyrings
wget https://repo.radeon.com/rocm/rocm.gpg.key -O - | \
  gpg --dearmor | tee /etc/apt/keyrings/rocm.gpg > /dev/null

# ROCm 6.2 for Ubuntu 24.04 (noble)
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/6.2 noble main" \
  | tee /etc/apt/sources.list.d/rocm.list

apt update
apt install -y rocm-dev rocm-libs rocminfo rocm-smi-lib

# Add user to required groups (LXC uses root by default; if you create a non-root user, add it to render and video)
usermod -a -G render,video root
```

Verify ROCm sees both V620s:

```bash
rocminfo | grep -A2 "gfx1030"
# Expect two Agent blocks with Name: gfx1030

rocm-smi
# Expect tabular output listing GPU 0 and GPU 1
```

If `rocminfo` returns no GPUs but `ls /dev/dri` shows the render nodes, the most common cause is missing `kfd` permissions — re-check the cgroup allow line for major 234 and that `/dev/kfd` is mounted into the LXC.

### 5.4 llama.cpp build with HIP

llama.cpp has an explicit HIP/ROCm backend that's well-maintained. Build from source so you get the right `gfx1030` target:

```bash
cd /opt
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp

# Configure with HIP backend, targeting V620's gfx1030 architecture
cmake -B build \
  -DGGML_HIP=ON \
  -DAMDGPU_TARGETS=gfx1030 \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_BUILD_SERVER=ON

cmake --build build --config Release -j 8

# Resulting binary: ./build/bin/llama-server
```

Smoke test:

```bash
./build/bin/llama-server --help | grep -i hip
# Expect to see HIP-related options confirming the backend is built in
```

Build time is ~10 minutes on the 7600.

### 5.5 Model selection and download

The V620 stack runs **three `llama-server` processes** inside LXC 151, all sharing the same llama.cpp binary built in §5.4:

| Service | Model | Size | Notes |
| --- | --- | --- | --- |
| `llamacpp-chat.service` (port 8080) | Qwen3.6-35B-A3B UD-Q4_K_M (`unsloth/Qwen3.6-35B-A3B-GGUF`) | ~22 GB | Primary chat / RAG / coding target. MoE with ~3 B active params/token. Tensor-split across both V620s, single in-flight request at full trained ctx (`--ctx-size 262144 --parallel 1`). |
| `llamacpp-embed.service` (port 8082) | Qwen3-Embedding-0.6B Q8_0 (`Qwen/Qwen3-Embedding-0.6B-GGUF`) | ~1 GB | Embedding generation (1024-dim). Pinned to V620 #1 via `--main-gpu 0`. **`--pooling last`** is critical — `cls` produces semantically wrong embeddings and silently invalidates the vector DB. |
| `llamacpp-rerank.service` (port 8083) | BGE Reranker v2-m3 Q4_K_M (`gpustack/bge-reranker-v2-m3-GGUF`) | ~1.5 GB | Cross-encoder reranking for vector-search results. Pinned to V620 #2 via `--main-gpu 1`. Alt: `Qwen/Qwen3-Reranker-0.6B-GGUF` (gated on HF — requires `HF_TOKEN`). |

**VRAM budget at deployed config** (chat: 256K ctx, q8_0 KV, `--parallel 1`):

| Component | VRAM |
| --- | --- |
| Chat weights + KV split across both V620s | ~32 GB total (~16 GB per card) |
| Embedder pinned to V620 #1 | ~1.2 GB |
| Reranker pinned to V620 #2 | ~1.5 GB |
| **Total** | **~35 GB of 64 GB pool, ~29 GB headroom** |

**Speculative decoding is disabled by default** — see §1.3 note. Re-enable via `LLAMA_DRAFT_REPO` in `scripts/config.env` if a vocab-matched Qwen3.6 small variant becomes available.

Models are downloaded on demand by llama-server's `--hf-repo` flag into `/tank/models/.cache/` (HuggingFace cache layout: `models--<org>--<repo>/snapshots/<rev>/<file>.gguf`). The `/tank/models/` directory is RW bind-mounted into LXC 151 at `/opt/models/` so the cache survives container recreation. To pre-fetch from the host:

```bash
# From the Proxmox host
huggingface-cli download unsloth/Qwen3.6-35B-A3B-GGUF Qwen3.6-35B-A3B-UD-Q4_K_M.gguf \
    --local-dir /tank/models/Qwen3.6-35B-A3B-GGUF/
# Then swap the unit's --hf-repo flag for --model /opt/models/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf
```

The provisioning script [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) and [`setup-runbook.md`](./setup-runbook.md) §5.11 are the canonical source of truth for which models are deployed and how their systemd units are configured.

### 5.6 llama-server systemd units

LXC 151 hosts **three** systemd units, not one. Each is a separate `llama-server` process with per-card pinning:

- `llamacpp-chat.service` (port 8080) — Qwen3.6-35B-A3B target tensor-split across both V620s
- `llamacpp-embed.service` (port 8082) — Qwen3-Embedding-0.6B pinned to V620 #1 (`--main-gpu 0`, `HIP_VISIBLE_DEVICES=0`)
- `llamacpp-rerank.service` (port 8083) — BGE Reranker v2-m3 pinned to V620 #2 (`--main-gpu 1`, `HIP_VISIBLE_DEVICES=1`)

The canonical unit shapes are generated by [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) and documented in detail in [`setup-runbook.md`](./setup-runbook.md) §5.11.3 (chat), §5.11.4 (embed), §5.11.5 (rerank). Key features common to all three:

- Bearer auth via `--api-key ${LLAMACPP_API_KEY}` (key loaded from `EnvironmentFile=/etc/llamacpp.env`, never inline in the unit file).
- `--hf-repo` for on-demand HuggingFace download into `LLAMA_CACHE=/opt/models/.cache`.
- `--cache-type-k q8_0 --cache-type-v q8_0` to halve KV cost vs. f16 with imperceptible quality impact.
- `--mlock` pins weights in VRAM, prevents demand paging under bulk-embed contention.
- `--metrics` exposes Prometheus instrumentation on each port.
- ROCm tuning env vars: `HIP_FORCE_DEV_KERNARG=1`, `GPU_MAX_HW_QUEUES=2`, `HSA_OVERRIDE_GFX_VERSION=10.3.0`, `GGML_HIP_UMA=0`.

Chat-specific tuning:
- `--ctx-size 262144 --parallel 1` gives a single in-flight request the full Qwen3.6 trained context window (256K). The router's `CHAT_CONCURRENCY=1` matches this — multi-agent sub-calls queue at the router (which can emit SSE keepalives while waiting) rather than inside llama.cpp.
- `--cache-reuse 1024 --cache-ram 16384` enables 16 GB host-RAM prompt-prefix KV cache so repeat prefixes (OpenCode's long system prompt every turn) skip re-tokenization.
- `--reasoning-format deepseek` moves `<think>...</think>` into `message.reasoning_content`; `--jinja` enables the chat template kwargs the router uses to toggle `enable_thinking` per alias.
- `--no-mmproj` skips the vision projector — text-only RAG/coding doesn't need it.
- **Speculative decoding is disabled by default** (see §1.3 note and runbook §5.11.3).

Embed-specific: `--embeddings --pooling last` (NOT `cls` — wrong pooling silently invalidates the vector DB). Rerank-specific: `--reranking --embeddings --pooling rank` per llama.cpp upstream best practice.

Flash Attention (`--flash-attn`) has known performance regressions on V620 (`gfx1030`) — sometimes slower than the default path. The chat unit ships with `--flash-attn auto`; **benchmark both** per §5.7 before pinning to `on` or `off` in production.

Don't hand-author these unit files from this document — they drift fast as llama.cpp's flag surface evolves. Use `scripts/51-lxc-amd.sh` (Phase 5 of the runbook) as the source of truth.

### 5.7 Tensor split tuning across the two V620s

`--tensor-split 1,1` distributes layers evenly. For perfectly identical cards in identical slots, this is right. But:

- If the two cards are in slots with different bandwidth (e.g., one at PCIe 4.0 x8, the other at x4 because M.2_2 is populated), the slower-slot card becomes the bottleneck. Skewing the split toward the faster-slot card (`--tensor-split 6,5` for example) sometimes improves throughput.
- If one card is hotter than the other and thermal-throttling more, the throttled card becomes the bottleneck. Fix airflow first, then revisit the split.

Measure with:

```bash
# In one terminal:
rocm-smi --showuse --showtemp -d 0 1

# In another, hit the server with a long generation:
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"local","messages":[{"role":"user","content":"Write a 2000-word essay on TCP slow start."}],"stream":false}' \
  > /dev/null

# Watch GPU util — both cards should pin near 100% during generation, with similar temperatures.
```

If GPU 0 hits 100% and GPU 1 sits at 70%, layers are over-allocated to GPU 0 — try `--tensor-split 4,5` (more layers on GPU 1).

---

## 6. (removed) llama.cpp CUDA LXC — 3060 stack retired

The original v2 design ran a separate NVIDIA-backed LXC at VMID 152 (`llamacpp-nv`, IP 192.168.6.152) for the embedder + reranker + a small "fast" chat model. The 3060 has been removed from the build (see §1.3 for rationale). The embedder and reranker moved into LXC 151 as additional `llama-server` processes pinned per-card via `--main-gpu` (V620 #1 → embed, V620 #2 → rerank); the fast-chat tier was deleted entirely.

This section previously documented:
- LXC 152 container creation
- NVIDIA driver + CUDA install in an unprivileged LXC
- `llama.cpp` build with CUDA backend
- The draft / embedder / reranker model loadout (now on V620 #1 and V620 #2)
- Three CUDA `llama-server` systemd units (`llamacpp-draft`, `llamacpp-embed-old`, `llamacpp-rerank-old`)

For the equivalent V620-only configuration that replaced it, see §5 above and [`setup-runbook.md`](./setup-runbook.md) Phase 5 (which documents all three V620 llama-server units in one place).

### Note on speculative decoding

Speculative decoding requires target + draft in the **same `llama-server` process** (llama.cpp's API mandates shared tokenizer/KV cache state — no remote-draft-model API). The pre-pivot design described running the draft on the 3060 LXC; that was never workable. Both V620s tensor-split the target model and the draft is loaded in the same process via `-md`. As of the Qwen3.6 cutover, speculative decoding is **disabled** because Qwen3.6 uses a different tokenizer (vocab 248,320) than Qwen3 small variants (vocab 151,936) — no vocab-compatible draft exists in the smaller Qwen3.6 lineup yet. The 35B-A3B target activates only 3 B params per token so it's already fast; the lost spec-decode is a ~1.5-2× throughput penalty we accept.

## 7. Router LXC (Per-Request Decision + Keepalive)

### 7.1 Role and design

The router is a thin FastAPI service sitting between clients (AnythingLLM, OpenCode, curl) and the two llama.cpp stacks. It does four jobs:

1. **Endpoint multiplexing** — one URL for clients, dispatched to the right backend
2. **Per-request thinking-block decision** — strip `<think>...</think>` for RAG traffic, preserve for agentic-coding traffic, decided per request
3. **SSE keepalive** — emit `: ping` comment frames during upstream silence so client idle-timers don't fire
4. **OpenAI API compatibility** — single `/v1/chat/completions`, `/v1/embeddings`, `/v1/rerank` shape regardless of backend

This replaces the v1 dual-port no-think proxy (one port stripped thinking, the other didn't). The new design uses a single port and a per-request signal to decide.

### 7.2 Routing logic

```
Request                                     Backend
──────────────────────────────────────────  ────────────────────────────────
POST /v1/chat/completions                   llamacpp-amd:8080 (V620 stack)
POST /v1/embeddings                         llamacpp-amd:8082 (V620 embedder, --main-gpu 0)
POST /v1/rerank                             llamacpp-amd:8083 (V620 reranker, --main-gpu 1)
GET  /v1/models                             aggregated from both backends
```

Speculative decoding is invisible to the router — it's local within the V620 LXC's `llama-server` process via the `-md` / `--model-draft` flag (not a cross-LXC URL). The router just talks to the V620's main port; the V620 process itself loads both target and draft models.

### 7.3 Per-request thinking-block decision

The router decides whether to strip `<think>...</think>` from responses based on signals in the request, in this priority order:

1. **Explicit header** — `X-Strip-Thinking: true` or `X-Strip-Thinking: false`. If present, this is authoritative.
2. **Explicit body field** — `"strip_thinking": true` or `"strip_thinking": false` in the JSON body.
3. **System prompt sniff** — if the first message has `role: system` and the content matches `/no_think|hide thinking|strip reasoning/i`, strip is enabled.
4. **Model name heuristic** — if the model name in the request matches `/^rag-|-rag$/i`, strip is enabled (the model itself signals its intent).
5. **Default** — preserve thinking blocks.

This means:

- **AnythingLLM** uses model `rag-qwen3.6` (router alias) → thinking disabled via `chat_template_kwargs.enable_thinking=false` and any leaked `<think>` blocks regex-stripped from the response, lower latency to first answer token.
- **OpenCode** uses model `qwen3.6-think` → thinking enabled, multi-turn reasoning continuity works.
- **`curl` testing** can pass either header explicitly.

### 7.4 SSE keepalive

The keepalive logic is identical to v1 §4.3 — emit `: ping\n\n` (SSE comment frame) every 12 seconds during upstream silence. This is independent of the strip-thinking decision; both behaviors apply to streaming responses.

### 7.5 Implementation

Create the router LXC (VMID 153) using the same pattern as the GPU LXCs but smaller — 2 cores, 4 GB RAM, no GPU passthrough.

```bash
pct create 153 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname llm-router \
  --cores 2 \
  --memory 4096 \
  --rootfs local-zfs:8 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=0 \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 1
```

Inside the LXC, install Python and create the router:

```bash
apt update && apt install -y python3 python3-venv
useradd -r -m -d /opt/llm-router -s /usr/sbin/nologin router
mkdir -p /opt/llm-router && chown router:router /opt/llm-router
su - router -s /bin/bash -c '
  python3 -m venv /opt/llm-router/venv
  /opt/llm-router/venv/bin/pip install fastapi uvicorn[standard] httpx
'
```

`/opt/llm-router/app.py`:

```python
"""
LLM cluster router.
- /v1/chat/completions       -> V620 stack (with optional per-request <think> strip)
- /v1/embeddings             -> 3060 embedder
- /v1/rerank                 -> 3060 reranker
- SSE keepalive on streaming responses
"""

import asyncio
import json
import os
import re
import time

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import StreamingResponse, JSONResponse

V620_URL    = os.environ.get("V620_URL",    "http://192.168.6.151:8080")
EMBED_URL   = os.environ.get("EMBED_URL",   "http://192.168.6.151:8082")
RERANK_URL  = os.environ.get("RERANK_URL",  "http://192.168.6.151:8083")
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "12"))

THINK_RE       = re.compile(r"<think>.*?</think>", re.DOTALL)
NOTHINK_HINT   = re.compile(r"/no_think|hide thinking|strip reasoning", re.IGNORECASE)
RAG_MODEL_RE   = re.compile(r"(^rag-|-rag$)", re.IGNORECASE)

app = FastAPI()


# -------- Strip-thinking decision logic --------

def should_strip_thinking(
    body: dict,
    header_value: str | None,
) -> bool:
    """
    Decide whether to strip <think>...</think> blocks from the response.
    Priority: explicit header > explicit body field > system-prompt sniff
              > model-name heuristic > default (preserve).
    """
    # 1. Explicit header
    if header_value is not None:
        return header_value.lower() in ("true", "1", "yes")

    # 2. Explicit body field
    if "strip_thinking" in body:
        return bool(body["strip_thinking"])

    # 3. System prompt sniff
    msgs = body.get("messages", [])
    if msgs and msgs[0].get("role") == "system":
        if NOTHINK_HINT.search(msgs[0].get("content", "")):
            return True

    # 4. Model name heuristic
    model = body.get("model", "")
    if RAG_MODEL_RE.search(model):
        return True

    # 5. Default: preserve
    return False


# -------- SSE proxying with keepalive --------

async def sse_stream_with_keepalive(
    upstream_url: str,
    payload: dict,
    strip_thinking: bool,
):
    """
    Proxy an upstream SSE stream, emitting `: ping` comments during silence,
    and optionally stripping <think>...</think> blocks from data: lines.

    NOTE on think-stripping correctness: a <think>...</think> block can span
    multiple SSE chunks. Naive per-chunk regex would miss blocks split at
    chunk boundaries. We track an `in_think` state across chunks and buffer
    partial tokens to handle this correctly.
    """
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", upstream_url, json=payload) as r:
            queue: asyncio.Queue = asyncio.Queue()

            async def reader():
                async for chunk in r.aiter_text():
                    await queue.put(chunk)
                await queue.put(None)

            task = asyncio.create_task(reader())
            in_think = False
            buf = ""    # carry partial "<think" or "</think" tokens across chunks

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            queue.get(), timeout=KEEPALIVE_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
                        continue
                    if chunk is None:
                        # Flush any leftover buffer
                        if buf and not in_think:
                            yield buf.encode()
                        break

                    if not strip_thinking:
                        yield chunk.encode()
                        continue

                    # Stateful think-stripping across chunk boundaries
                    text = buf + chunk
                    out = []
                    i = 0
                    while i < len(text):
                        if not in_think:
                            j = text.find("<think>", i)
                            if j == -1:
                                # Reserve last 6 chars in case "<think" arrives at boundary
                                if len(text) - i > 6:
                                    out.append(text[i:len(text) - 6])
                                    i = len(text) - 6
                                buf = text[i:]
                                break
                            out.append(text[i:j])
                            in_think = True
                            i = j + len("<think>")
                        else:
                            j = text.find("</think>", i)
                            if j == -1:
                                # Reserve last 7 chars in case "</think" arrives at boundary
                                buf = text[max(i, len(text) - 7):]
                                break
                            in_think = False
                            i = j + len("</think>")
                    else:
                        buf = ""

                    if out:
                        yield "".join(out).encode()
            finally:
                task.cancel()


# -------- /v1/chat/completions --------

@app.post("/v1/chat/completions")
async def chat(
    request: Request,
    x_strip_thinking: str | None = Header(default=None, alias="X-Strip-Thinking"),
):
    body = await request.json()
    strip = should_strip_thinking(body, x_strip_thinking)
    stream = body.get("stream", False)
    url = f"{V620_URL}/v1/chat/completions"

    if stream:
        return StreamingResponse(
            sse_stream_with_keepalive(url, body, strip),
            media_type="text/event-stream",
        )

    # Non-streaming
    async with httpx.AsyncClient(timeout=None) as c:
        r = await c.post(url, json=body)
        data = r.json()
        if strip:
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if "content" in msg:
                    msg["content"] = THINK_RE.sub("", msg["content"])
        return JSONResponse(data)


# -------- /v1/embeddings --------

# Qwen3 Embedding requires <|endoftext|> appended to each input.
# Per Qwen's model card: llama-server does NOT auto-append this token.
QWEN3_EOT = "<|endoftext|>"


def _ensure_eot(text: str) -> str:
    """Append <|endoftext|> if not already present at end of input."""
    if not text:
        return text
    return text if text.endswith(QWEN3_EOT) else text.rstrip() + QWEN3_EOT


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()

    # Inject <|endoftext|> for Qwen3 Embedding compliance.
    # OpenAI-compatible /v1/embeddings accepts `input` as str OR list[str].
    inp = body.get("input")
    if isinstance(inp, str):
        body["input"] = _ensure_eot(inp)
    elif isinstance(inp, list):
        body["input"] = [_ensure_eot(x) if isinstance(x, str) else x for x in inp]

    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{EMBED_URL}/v1/embeddings", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)


# -------- /v1/rerank --------

@app.post("/v1/rerank")
async def rerank(request: Request):
    body = await request.json()

    # Reranker also benefits from <|endoftext|> on documents+query if using a Qwen3-based reranker.
    # bge-reranker-v2-m3 uses XLMRoBERTa tokenizer and does NOT need this — leave body unchanged.
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(f"{RERANK_URL}/v1/rerank", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)


# -------- /v1/models (aggregated) --------

@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=10.0) as c:
        try:
            v620 = (await c.get(f"{V620_URL}/v1/models")).json().get("data", [])
        except Exception:
            v620 = []
        try:
            embed = (await c.get(f"{EMBED_URL}/v1/models")).json().get("data", [])
        except Exception:
            embed = []
    return {"object": "list", "data": v620 + embed}


# -------- Health --------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": time.time()}
```

### 7.6 systemd unit

`/etc/systemd/system/llm-router.service`:

```ini
[Unit]
Description=LLM cluster router (per-request strip + keepalive)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=router
WorkingDirectory=/opt/llm-router
Environment="V620_URL=http://192.168.6.151:8080"
Environment="EMBED_URL=http://192.168.6.151:8082"
Environment="RERANK_URL=http://192.168.6.151:8083"
Environment="KEEPALIVE_INTERVAL=12"
ExecStart=/opt/llm-router/venv/bin/uvicorn app:app \
    --host 0.0.0.0 --port 8000 \
    --timeout-keep-alive 300 \
    --workers 1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now llm-router
```

Smoke test:

```bash
curl http://192.168.6.153:8000/healthz
# {"ok": true, ...}

curl http://192.168.6.153:8000/v1/models
# Aggregated model list from both backends

# Test strip-thinking via header
curl -N http://192.168.6.153:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Strip-Thinking: true" \
  -d '{"model":"rag-qwen3.6","stream":true,"messages":[{"role":"user","content":"think briefly, then say hi"}]}'
# Expect <think> blocks absent from data: payloads
```

---

## 8. AnythingLLM LXC (Docker, Clean Install)

### 8.1 Container creation

```bash
pct create 154 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname anythingllm \
  --cores 4 \
  --memory 8192 \
  --rootfs local-zfs:32 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 1
```

`features keyctl=1` is required for Docker storage drivers in unprivileged LXC.

Bind-mount the AnythingLLM persistent storage from host ZFS into the LXC. Edit `/etc/pve/lxc/154.conf`:

```
mp0: /tank/anythingllm,mp=/opt/anythingllm-data
```

### 8.2 Docker install in LXC

Inside the LXC:

```bash
apt update && apt install -y ca-certificates curl gnupg lsb-release

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  gpg --dearmor -o /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker run --rm hello-world                    # smoke test
```

### 8.3 AnythingLLM compose stack

`/opt/anythingllm/docker-compose.yml`:

```yaml
services:
  anythingllm:
    image: mintplexlabs/anythingllm:latest
    container_name: anythingllm
    restart: unless-stopped
    cap_add:
      - SYS_ADMIN
    ports:
      - "3001:3001"
    volumes:
      - /opt/anythingllm-data/storage:/app/server/storage
      - /opt/anythingllm/.env:/app/server/.env
    environment:
      STORAGE_DIR: /app/server/storage
```

```bash
mkdir -p /opt/anythingllm /opt/anythingllm-data/storage
touch /opt/anythingllm/.env
chmod 600 /opt/anythingllm/.env
cd /opt/anythingllm
docker compose up -d
docker logs anythingllm -f
```

Wait for the "Server listening on 0.0.0.0:3001" line, then browse to `http://192.168.6.154:3001/` and complete the initial setup (admin user, etc.).

### 8.4 Provider configuration: point at the router

In AnythingLLM Settings:

**Chat LLM**
- Provider: **Generic OpenAI**
- Base URL: `http://192.168.6.153:8000/v1`
- Model: `rag-qwen3.6` (the router alias for thinking-off RAG synthesis; use `qwen3.6-think` for thinking-on coding agents — both resolve to the same backend, see [`scripts/files/router-app.py`](./scripts/files/router-app.py) `ALIAS_MAP`)
- Token context window: `131072`
- API key: `sk-anything` (llama.cpp doesn't validate, AnythingLLM requires a non-empty value)

**Embedder**
- Provider: **Generic OpenAI**
- Base URL: `http://192.168.6.153:8000/v1`
- Model: `qwen3-embedding`
- Embedding dimension: **1024** (Qwen3 embedder native — same as v1)
- Max embed chunk length: matches AnythingLLM's character chunk size

Because the router exposes `/v1/embeddings` separately from `/v1/chat/completions`, AnythingLLM's "Generic OpenAI" embedder handles this correctly. No need to point the embedder at a different backend.

To get RAG-style strip-thinking behavior from AnythingLLM without modifying its config, name the chat model with the `rag-` prefix in the llama.cpp `--alias` flag, or use a system prompt that includes `/no_think`. The router's heuristics will catch either signal.

### 8.5 Workspace tuning (clean install)

Same per-workspace API tuning as v1 §5.5, with the URL pointing at the new AnythingLLM IP:

```bash
export ALLM_URL="http://192.168.6.154:3001"
export ALLM_KEY="<workspace API key>"
export WS="vcf-reference"

curl -X POST "$ALLM_URL/api/v1/workspace/$WS/update" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "similarityThreshold": 0.0,
    "topN": 10,
    "chatMode": "query",
    "vectorSearchMode": "rerank",
    "openAiTemp": 0.3,
    "chatModel": "rag-qwen3.6",
    "queryRefusalResponse": "Not in the provided VCF documents.",
    "openAiPrompt": "You are a technical reference assistant for VMware Cloud Foundation (VCF). Answer questions using ONLY the content retrieved from the attached VCF documentation. If the answer is not in the retrieved context, say so — do not fall back on general VMware knowledge. Cite which document each claim comes from when possible."
  }'
```

Repeat for `sdg-documentation` (Keycloak) workspace with appropriate `openAiPrompt` and `queryRefusalResponse` values.

### 8.6 Re-ingest VCF and SDG corpora from markdown

The clean install means every document needs to be re-uploaded and re-embedded. The original markdown files from the v1 ingestion pipeline still exist at `/opt/vcf-ingest/out/` and `/opt/keycloak-ingest/output/keycloak-26.3.3/` on the old host. Copy them over:

```bash
# From new Proxmox host
ssh old-host 'tar czf - /opt/vcf-ingest/out /opt/keycloak-ingest/output' | \
  ssh anythingllm-lxc 'tar xzf - -C /tmp/'

# Inside the AnythingLLM LXC
cd /tmp
ls /tmp/opt/vcf-ingest/out | wc -l           # confirm file count matches expectations
```

Bulk upload to AnythingLLM:

```bash
WS=vcf-reference
for f in /tmp/opt/vcf-ingest/out/*.md; do
  curl -s -X POST "$ALLM_URL/api/v1/document/upload" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -F "file=@$f" \
    -F "addToWorkspaces=$WS"
done

# Trigger embeddings via the router → V620 embedder (LXC 151, --main-gpu 0)
curl -s -X POST "$ALLM_URL/api/v1/workspace/$WS/update-embeddings" \
  -H "Authorization: Bearer $ALLM_KEY"

# Same for sdg-documentation
WS=sdg-documentation
for f in /tmp/opt/keycloak-ingest/output/keycloak-26.3.3/**/*.md; do
  curl -s -X POST "$ALLM_URL/api/v1/document/upload" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -F "file=@$f" \
    -F "addToWorkspaces=$WS"
done
curl -s -X POST "$ALLM_URL/api/v1/workspace/$WS/update-embeddings" \
  -H "Authorization: Bearer $ALLM_KEY"
```

The embedding step runs on V620 #1's pinned `llamacpp-embed` service (`--main-gpu 0`, `--parallel 4`); router admission control (`EMBED_CONCURRENCY=4`) prevents bulk re-embed from stalling chat traffic that shares the V620 pool.

Apply the chunk-size fix from v1 §8 before re-embedding: Settings → Text Splitter & Chunking → chunk size **2500**, overlap **500**.

---

## 9. MCP Server Stack (Migrated)

The three MCP containers from v1 (`anythingllm-search-mcp`, `broadcom-techdocs-mcp`, `sdg-docs-mcp`) carry over unchanged structurally. Migration is mostly an IP-address swap.

Create the LXC:

```bash
pct create 155 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname mcp-stack \
  --cores 2 \
  --memory 4096 \
  --rootfs local-zfs:16 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --start 1
```

Install Docker (same procedure as §8.2), then copy the three MCP project trees from the old host:

```bash
ssh old-host 'tar czf - /opt/anythingllm-mcp /opt/broadcom-techdocs-mcp /opt/sdg-mcp' | \
  ssh mcp-stack-lxc 'tar xzf - -C /'
```

Inside each project's `docker-compose.yml`, update environment variables to point at the new AnythingLLM IP:

```yaml
environment:
  ANYTHINGLLM_BASE_URL: "http://192.168.6.154:3001"
  ANYTHINGLLM_API_KEY: "${ANYTHINGLLM_API_KEY}"
  # ...
```

(Update `.env` files with new API keys if they were rotated during AnythingLLM setup.)

Bring them up:

```bash
cd /opt/anythingllm-mcp && docker compose up -d --build
cd /opt/broadcom-techdocs-mcp && docker compose up -d --build
cd /opt/sdg-mcp && docker compose up -d --build
```

Verify each is reachable:

```bash
for port in 3002 3003 3004; do
  echo "=== $port ==="
  timeout 3 curl -sN -H "Accept: text/event-stream" \
    "http://192.168.6.155:$port/sse" | head -3
done
# Expect each to start with "event: endpoint" lines
```

Update the OpenCode client config to point at the new MCP IPs:

```json
{
  "mcp": {
    "vcf-reference": {
      "type": "remote",
      "url": "http://192.168.6.155:3002/sse",
      "timeout": 120000
    },
    "broadcom-live": {
      "type": "remote",
      "url": "http://192.168.6.155:3003/sse",
      "timeout": 120000
    },
    "sdg-docs": {
      "type": "remote",
      "url": "http://192.168.6.155:3004/sse",
      "timeout": 120000
    }
  }
}
```

The tool docstrings, `format_source()` helper, and routing-aware tool naming from v1 §6 carry over unchanged — they're business logic not coupled to the underlying inference stack.

---

## 10. VCF Documentation Auto-Updater (Migrated)

The auto-updater service from v1 §9 also moves over with minimal change. It lives in the `mcp-stack` LXC alongside the MCP containers (same Docker host).

```bash
ssh old-host 'tar czf - /opt/vcf-doc-updater' | \
  ssh mcp-stack-lxc 'tar xzf - -C /'
```

Update `/opt/vcf-doc-updater/.env` with the new AnythingLLM URL:

```
ANYTHINGLLM_URL=http://192.168.6.154:3001
ANYTHINGLLM_API_KEY=<key>
```

Update the bind-mount to the AnythingLLM JSON cache from `docker-compose.yml`. The original v1 architecture mounted `/opt/anythingllm/storage/documents/custom-documents/` read-only into the updater container. In the new setup, that path is on the AnythingLLM LXC, not the same host as the auto-updater. Two options:

**Option A: NFS mount.** Export `/opt/anythingllm-data/storage/documents/custom-documents` from the AnythingLLM LXC via NFS, mount it on the MCP LXC, and bind-mount into the updater Docker container.

**Option B: Move the auto-updater into the AnythingLLM LXC.** Run it as a Docker container on the AnythingLLM host directly. The auto-updater is small enough that nesting Docker on the same LXC as AnythingLLM has minimal cost.

**Recommendation: Option B.** Less moving parts, no NFS to maintain.

```bash
# Move project to AnythingLLM LXC instead
pct exec 155 -- bash -c 'cd /opt/vcf-doc-updater && docker compose down'
ssh anythingllm-lxc 'mkdir -p /opt'
pct exec 155 -- bash -c 'tar czf - /opt/vcf-doc-updater' | \
  pct exec 154 -- bash -c 'tar xzf - -C /'
# Edit docker-compose.yml: bind-mount /opt/anythingllm-data/storage/documents/custom-documents:/allm-storage:ro
pct exec 154 -- bash -c 'cd /opt/vcf-doc-updater && docker compose up -d'
```

Run the same SQLite health check as v1:

```bash
sqlite3 /opt/vcf-doc-updater/state/state.sqlite \
  "SELECT id, datetime(started_at,'unixepoch','localtime') AS started,
          discovered, unchanged, updated, new, deleted, errors, aborted
   FROM runs ORDER BY id DESC LIMIT 5;"
```

---

## Appendix A — IP and Service Port Map

| LXC | IP | Port | Service | Notes |
| --- | --- | --- | --- | --- |
| `llamacpp-amd` (151) | 192.168.6.151 | 8080 | `llamacpp-chat.service` (V620 tensor-split, Qwen3.6-35B-A3B UD-Q4_K_M, 256K ctx, `--parallel 1`) | Speculative decoding disabled by default — vocab mismatch with available drafts, see [`setup-runbook.md`](./setup-runbook.md) §5.11.3; Bearer auth via `LLAMACPP_API_KEY` |
| `llamacpp-amd` (151) | 192.168.6.151 | 8082 | `llamacpp-embed.service` (V620 #1 via `--main-gpu 0`) | Qwen3-Embedding-0.6B Q8_0, `--pooling last`, dim 1024 |
| `llamacpp-amd` (151) | 192.168.6.151 | 8083 | `llamacpp-rerank.service` (V620 #2 via `--main-gpu 1`) | BGE-Reranker-v2-m3 or Qwen3-Reranker-0.6B fallback |
| `llm-router` (153) | 192.168.6.153 | 8000 | FastAPI router | per-request strip + keepalive |
| `anythingllm` (154) | 192.168.6.154 | 3001 | AnythingLLM | Docker |
| `anythingllm` (154) | 192.168.6.154 | (n/a) | vcf-doc-updater | internal cron, no port |
| `mcp-stack` (155) | 192.168.6.155 | 3002 | anythingllm-search-mcp (VCF) | SSE |
| `mcp-stack` (155) | 192.168.6.155 | 3003 | broadcom-techdocs-mcp | SSE |
| `mcp-stack` (155) | 192.168.6.155 | 3004 | sdg-docs-mcp | SSE |
| `searxng` (156) | 192.168.6.156 | 8888 | SearXNG | Docker |

Client-facing endpoint: **`http://192.168.6.153:8000/v1`** (the router). All clients (AnythingLLM, OpenCode, curl) use this single base URL.

---

## Appendix B — Directory Layout (Host and LXCs)

### Proxmox host
```
/tank/                                       # ZFS mirror across two NVMes
├── models/                                  # gguf model files (RW bind-mounted into LXC 151 — HF cache target)
│   └── .cache/                              # HuggingFace cache layout (llama-server downloads here via --hf-repo)
│       └── models--<org>--<repo>/snapshots/<rev>/<file>.gguf
│   # Currently-deployed files (verified live):
│   #   unsloth/Qwen3.6-35B-A3B-GGUF       -> Qwen3.6-35B-A3B-UD-Q4_K_M.gguf  (~22 GB chat target)
│   #   Qwen/Qwen3-Embedding-0.6B-GGUF     -> Qwen3-Embedding-0.6B-Q8_0.gguf  (~1 GB embedder)
│   #   gpustack/bge-reranker-v2-m3-GGUF   -> bge-reranker-v2-m3-Q4_K_M.gguf  (~1.5 GB reranker)
├── anythingllm/                             # AnythingLLM persistent storage
│   └── storage/
│       └── documents/
│           └── custom-documents/            # ~7000+ ingested .json files across both workspaces
├── rag-state/                               # RAG corpus refresh state (scripts/rag/)
│   ├── <source-id>/{manifest,documents}.json
│   └── _proposals/                          # safety-threshold-halted plans for review
└── mcp/                                     # MCP container working state
```

### llamacpp-amd LXC (151)
```
/opt/
├── models/                                  # bind-mounted from /tank/models (ro)
└── llama.cpp/
    └── build/bin/llama-server
```

### llm-router LXC (153)
```
/opt/llm-router/
├── app.py
└── venv/
```

### anythingllm LXC (154)
```
/opt/
├── anythingllm/
│   ├── docker-compose.yml
│   └── .env
├── anythingllm-data/                        # bind-mounted from /tank/anythingllm
│   └── storage/
└── vcf-doc-updater/
    ├── docker-compose.yml
    └── state/state.sqlite
```

### mcp-stack LXC (155)
```
/opt/
├── anythingllm-mcp/
├── broadcom-techdocs-mcp/
└── sdg-mcp/
```

---

## Appendix C — Smoke Tests

Run these in sequence after any rebuild or migration step.

```bash
# === 1. Host-level GPU enumeration ===
# V620 = class 0380 "Display controller" (headless server card; not VGA/3D).
lspci -nn | grep -iE "navi 21|\[1002:73a1\]"
# Expect: 2× AMD Radeon Pro V620 entries (Navi 21)

# === 2. Host IOMMU groups are clean ===
for d in /sys/kernel/iommu_groups/*/devices/*; do
  n=${d#*/iommu_groups/*}; n=${n%%/*}
  printf 'IOMMU Group %s ' "$n"
  lspci -nns "${d##*/}"
done | sort -V | grep -iE "navi|amd.*audio"
# Each V620 should be in its own group. V620s have NO audio function (headless),
# so the only AMD audio you'll see is the Raphael iGPU's HDMI audio at 7e:00.1/7e:00.6.

# === 3. ROCm sees both V620s (from llamacpp-amd LXC) ===
pct exec 151 -- rocm-smi --showtemp
# Expect tabular output with GPU 0 and GPU 1

# === 4. (removed) CUDA 3060 check — LXC 152 destroyed in V620-only pivot ===

# === 5. V620 chat llama-server is responding ===
LLAMACPP_KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)
curl -sf -H "Authorization: Bearer $LLAMACPP_KEY" http://192.168.6.151:8080/v1/models | jq '.data[].id'
# Expect: "rag-qwen3.6"

# === 6. V620 embed + rerank services are responding ===
for port in 8082 8083; do
  echo "=== port $port ==="
  curl -sf -H "Authorization: Bearer $LLAMACPP_KEY" "http://192.168.6.151:$port/v1/models" | jq '.data[].id'
done
# Expect: "qwen3-embed" on 8082, "bge-rerank" on 8083

# === 7. Speculative decoding state (expected: DISABLED for Qwen3.6) ===
pct exec 151 -- journalctl -u llamacpp-chat --since "30 min ago" | grep -i "no implementations specified for speculative decoding"
# Expect: a line confirming spec-decode is off (vocab mismatch — see runbook §5.11.3).
# Re-enable by setting LLAMA_DRAFT_REPO in config.env once a vocab-matched
# Qwen3.6 small variant becomes available.

# === 8. Router is healthy and aggregates models ===
curl -s http://192.168.6.153:8000/healthz
curl -s http://192.168.6.153:8000/v1/models | jq '.data[].id'
# Expect: aggregated list from both backends

# === 9. Router strips thinking when asked ===
curl -sN http://192.168.6.153:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Strip-Thinking: true" \
  -d '{"model":"rag-qwen3.6","stream":true,"messages":[{"role":"user","content":"think then say hi"}]}' \
  | grep -c '<think>'
# Expect: 0 (rag-qwen3.6 alias disables thinking via chat_template_kwargs.enable_thinking=false
# AND the router regex-strips any leaked <think> blocks as a belt-and-suspenders backup)

# === 10. Router preserves thinking by default ===
curl -sN http://192.168.6.153:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -d '{"model":"qwen3.6-think","stream":true,"messages":[{"role":"user","content":"think then say hi"}]}' \
  | grep -c '<think>'
# Expect: > 0 (thinking blocks present — qwen3.6-think alias enables thinking)

# === 11. Router keepalive works during long generation ===
curl -sN http://192.168.6.153:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -d '{"model":"rag-qwen3.6","stream":true,"messages":[{"role":"user","content":"Write a 2000-word essay on ZFS internals."}]}' \
  | grep -E "^: ping" | head -3
# Expect: at least one ": ping" line during slow generation. The first ": ping" is
# emitted immediately, before upstream connect, so clients see a byte right away
# instead of waiting through the prompt-processing window.

# === 12. Embedding produces 1024-dim vectors ===
curl -s http://192.168.6.153:8000/v1/embeddings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ROUTER_KEY" \
  -d '{"model":"qwen3-embed","input":"dimension probe"}' \
  | jq '.data[0].embedding | length'
# Expect: 1024

# === 13. AnythingLLM API reachable ===
ALLM_KEY=<your key>
curl -s -H "Authorization: Bearer $ALLM_KEY" \
  http://192.168.6.154:3001/api/v1/workspaces | jq '.workspaces[].slug'
# Expect: vcf-reference, sdg-documentation

# === 14. AnythingLLM refusal path works ===
curl -s -X POST "http://192.168.6.154:3001/api/v1/workspace/vcf-reference/chat" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"What is the capital of France?","mode":"query"}' \
  | jq -r '.textResponse'
# Expect: "Not in the provided VCF documents."

# === 15. AnythingLLM positive retrieval ===
curl -s -X POST "http://192.168.6.154:3001/api/v1/workspace/vcf-reference/chat" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"What prefix lengths are valid for edge uplink subnets?","mode":"query"}' \
  | jq -r '.textResponse'
# Expect: reference to /29 or /30 with VCF-NET-014/VCF-IP-015 citation

# === 16. MCP containers are listening ===
for port in 3002 3003 3004; do
  printf "Port $port: "
  timeout 3 curl -sN -H "Accept: text/event-stream" \
    "http://192.168.6.155:$port/sse" 2>&1 | head -1
done
# Expect each: "event: endpoint" line

# === 17. Auto-updater health ===
pct exec 154 -- sqlite3 /opt/vcf-doc-updater/state/state.sqlite \
  "SELECT id, datetime(started_at,'unixepoch','localtime') AS started,
          discovered, unchanged, updated, new, deleted, errors, aborted
   FROM runs ORDER BY id DESC LIMIT 3;"
# Expect: discovered ~4900, errors=0, aborted=0
```

---

## Appendix D — LXC Provisioning Scripts (community-scripts style)

Three custom helper scripts, modeled after [`community-scripts/ProxmoxVE`](https://github.com/community-scripts/ProxmoxVE) layout. Place these at `/usr/local/bin/` on the Proxmox host.

### llamacpp-amd-lxc.sh

```bash
#!/usr/bin/env bash
# llamacpp-amd-lxc.sh — Provision the V620 ROCm llama.cpp LXC
# Style follows community-scripts/ProxmoxVE pattern.

set -euo pipefail

VMID="${VMID:-151}"
HOSTNAME="${HOSTNAME:-llamacpp-amd}"
TEMPLATE="${TEMPLATE:-local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
CORES="${CORES:-8}"
MEMORY="${MEMORY:-32768}"
ROOTFS="${ROOTFS:-local-zfs:64}"
BRIDGE="${BRIDGE:-vmbr0}"
MODELS_DIR="${MODELS_DIR:-/tank/models}"

echo "==> Creating LXC $VMID ($HOSTNAME)"
pct create "$VMID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" \
  --memory "$MEMORY" \
  --rootfs "$ROOTFS" \
  --net0 "name=eth0,bridge=$BRIDGE,firewall=0,ip=dhcp,type=veth" \
  --features nesting=1 \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 0

echo "==> Adding GPU passthrough config"
CONF="/etc/pve/lxc/${VMID}.conf"
cat <<EOF >> "$CONF"

# Shared model storage
mp0: ${MODELS_DIR},mp=/opt/models,ro=1

# AMD V620 render nodes (verify majors with: ls -l /dev/dri/render*)
lxc.cgroup2.devices.allow: c 226:128 rwm
lxc.cgroup2.devices.allow: c 226:129 rwm
lxc.mount.entry: /dev/dri/renderD128 dev/dri/renderD128 none bind,optional,create=file
lxc.mount.entry: /dev/dri/renderD129 dev/dri/renderD129 none bind,optional,create=file

# AMD KFD compute interface
lxc.cgroup2.devices.allow: c 234:* rwm
lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file

# Required for ROCm in unprivileged LXC
lxc.apparmor.profile: unconfined
lxc.cap.drop:
EOF

echo "==> Starting LXC"
pct start "$VMID"
sleep 5

echo "==> Installing ROCm 6.x and llama.cpp inside the LXC"
pct exec "$VMID" -- bash -c '
  set -e
  apt update && apt install -y wget gnupg2 build-essential cmake git
  mkdir --parents --mode=0755 /etc/apt/keyrings
  wget https://repo.radeon.com/rocm/rocm.gpg.key -O - | \
    gpg --dearmor | tee /etc/apt/keyrings/rocm.gpg > /dev/null
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/6.2 noble main" \
    > /etc/apt/sources.list.d/rocm.list
  apt update
  apt install -y rocm-dev rocm-libs rocminfo rocm-smi-lib

  cd /opt
  git clone https://github.com/ggerganov/llama.cpp.git
  cd llama.cpp
  cmake -B build -DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1030 \
    -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_SERVER=ON
  cmake --build build --config Release -j 8
'

echo "==> Done. Verify GPUs with: pct exec $VMID -- rocminfo | grep gfx1030"
echo "==> Then create /etc/systemd/system/llama-server.service per the doc."
```

### ~~llamacpp-nv-lxc.sh~~ — removed in V620-only pivot

The pre-pivot design provisioned an NVIDIA 3060 LXC (`llamacpp-nv`, VMID 152) for the embedder + reranker + a fast-chat tier. **LXC 152 has been destroyed and the 3060 removed from the build.** The embedder and reranker workloads moved into LXC 151 as additional `llama-server` processes pinned per-card via `--main-gpu` — see [`scripts/51-lxc-amd.sh`](./scripts/51-lxc-amd.sh) for the current canonical provisioning. The old NVIDIA driver install, CUDA toolkit, and CUDA-backed llama.cpp build are gone.

### llm-router-lxc.sh

```bash
#!/usr/bin/env bash
# llm-router-lxc.sh — Provision the FastAPI request router

set -euo pipefail

VMID="${VMID:-153}"
HOSTNAME="${HOSTNAME:-llm-router}"
TEMPLATE="${TEMPLATE:-local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"

pct create "$VMID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores 2 --memory 4096 \
  --rootfs local-zfs:8 \
  --net0 "name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth" \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 1

sleep 5

pct exec "$VMID" -- bash -c '
  set -e
  apt update && apt install -y python3 python3-venv
  useradd -r -m -d /opt/llm-router -s /usr/sbin/nologin router || true
  mkdir -p /opt/llm-router && chown router:router /opt/llm-router
  su - router -s /bin/bash -c "
    python3 -m venv /opt/llm-router/venv
    /opt/llm-router/venv/bin/pip install fastapi uvicorn[standard] httpx
  "
'

echo "==> Done. Copy app.py and llm-router.service per the doc, then:"
echo "    pct exec $VMID -- systemctl enable --now llm-router"
```

---

## Contributing and feedback

Issues and pull requests welcome. This document describes a single author's verified configuration. Corrections and additions from other setups are especially useful — please include hardware/software versions and a specific reproduction path for any contradicting behavior.

## License

This document is published under [Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)](https://creativecommons.org/licenses/by-sa/4.0/). Code snippets are MIT-licensed for ease of reuse.

## Acknowledgements

- **Proxmox** — the hypervisor that makes this whole architecture practical
- **llama.cpp / ggerganov** — the inference runtime that replaced Ollama for performance and speculative-decoding support
- **AMD ROCm team** — for the RDNA 2 (`gfx1030`) target support that makes V620s viable for local inference
- **Qwen team (Alibaba Cloud)** — open Qwen3.6-35B-A3B (MoE, ~3 B active params/token) for the chat target plus Qwen3-Embedding-0.6B / Qwen3-Reranker-0.6B for the RAG retrieval pipeline
- **AnythingLLM (Mintplex Labs)** — RAG frontend, unchanged from v1
