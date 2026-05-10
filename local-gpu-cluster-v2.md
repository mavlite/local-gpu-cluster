# Local GPU Cluster v2 — Technical Reference

**ASUS ProArt X870E-Creator · Ryzen 7600 · 2× AMD V620 32 GB + 1× NVIDIA RTX 3060 12 GB · Proxmox VE 9.x · llama.cpp · AnythingLLM · MCP**

![Platform](https://img.shields.io/badge/platform-Proxmox%20VE%209-informational)
![Hypervisor](https://img.shields.io/badge/containers-LXC%20%2B%20Docker-blue)
![GPU-AMD](https://img.shields.io/badge/GPU-2%C3%97%20V620%2032GB-red)
![GPU-NV](https://img.shields.io/badge/GPU-1%C3%97%20RTX%203060%2012GB-76b900)
![Runtime](https://img.shields.io/badge/runtime-llama.cpp-000000)
![Models](https://img.shields.io/badge/models-Qwen%203.5%20family-blue)
![RAG](https://img.shields.io/badge/RAG-AnythingLLM%20%2B%20LanceDB-0a6cf5)
![SpecDecode](https://img.shields.io/badge/feature-speculative%20decoding-orange)
![Revision](https://img.shields.io/badge/revision-1-lightgrey)
![License](https://img.shields.io/badge/license-CC%20BY--SA%204.0-yellow)

End-to-end build of the second-generation local LLM cluster. Replaces the v1 setup (Dell T7910 + 3× RTX 3060) with a purpose-built workstation: ASUS ProArt X870E-Creator, Ryzen 7600, 64 GB pooled VRAM across two AMD V620 server cards, and a 3060 acting as **assistive infrastructure** for the V620 cluster — running the speculative-decoding draft model, the embedder, and the reranker.

The big architectural shifts vs. v1: Proxmox VE host with one LXC per service (replacing bare-metal Ubuntu), llama.cpp replacing Ollama for performance and speculative-decoding support, and a request-aware router replacing the dual-port no-think proxy.

> **Scope note.** This document describes one verified configuration on the author's hardware as of the revision date. Paths, IPs, and ports are illustrative — adapt to your environment. Versions move quickly; check upstream documentation before acting on version-specific details here. For the previous-generation setup on dual Xeon hardware with NVIDIA-only GPUs, see [`local-gpu-cluster-reference.md`](./local-gpu-cluster-reference.md) (v1).

---

## Table of Contents

- [1. Hardware](#1-hardware)
  * [1.1 Host: ASUS ProArt X870E-Creator + Ryzen 7600](#11-host-asus-proart-x870e-creator--ryzen-7600)
  * [1.2 GPUs: 2× AMD Radeon Pro V620 + 1× NVIDIA RTX 3060](#12-gpus-2-amd-radeon-pro-v620--1-nvidia-rtx-3060)
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
- [6. llama.cpp CUDA LXC (3060 Stack)](#6-llamacpp-cuda-lxc-3060-stack)
  * [6.1 Container creation](#61-container-creation)
  * [6.2 NVIDIA driver + CUDA in LXC](#62-nvidia-driver--cuda-in-lxc)
  * [6.3 llama.cpp build with CUDA](#63-llamacpp-build-with-cuda)
  * [6.4 Model loadout: draft, embedder, reranker](#64-model-loadout-draft-embedder-reranker)
  * [6.5 Three llama-server systemd units](#65-three-llama-server-systemd-units)
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
| Form factor | Full tower (Fractal Design Define 7 XL, sound-dampened, no RGB) |
| Motherboard | ASUS ProArt X870E-Creator WiFi (ATX, X870E chipset) |
| CPU | AMD Ryzen 7600 (6c/12t, 65 W TDP, 88 W PPT) |
| CPU cooler | Thermalright Phantom Spirit 120 EVO (dual tower, 7 heatpipes, twin 120 mm PWM) |
| RAM | DDR5 (existing — re-used from previous build) |
| PSU | be quiet! Power Zone 2 1200 W (80+ Platinum efficiency, 140 mm Pure Wings 3 rifle-bearing fan, semi-passive zero-RPM mode, dual 12V-2x6 connectors, HEC OEM platform) |
| Storage | NVMe boot drive (M.2_1, PCIe 5.0 x4 from CPU) + secondary NVMe for `/tank` (M.2_3, chipset PCIe 4.0 x4) |
| Chassis fans | 5× ARCTIC P14 Pro PST 140mm (replacing/supplementing the 3× stock Dynamic X2 GP-14): 3× front intake, 2× top exhaust. PST Y-cable chains all 5 fans on a single motherboard header. 400-2500 RPM, 72 CFM, 2.4 mm H₂O static pressure, FDB bearing, zero-RPM <5% PWM |

The ProArt X870E-Creator is the right board choice for this build for three specific reasons that matter to a Proxmox + multi-GPU deployment:

1. **Three usable PCIe slots with adequate bandwidth for all three GPUs.** The CPU-attached x16 slot bifurcates to x8/x8 when both are populated. The chipset-attached third slot is PCIe 4.0 x4. Every GPU gets at least PCIe 4.0 x4 = PCIe 3.0 x8-equivalent, which is plenty for inference. See §3 for actual link verification.
2. **Clean IOMMU groupings.** Multiple Proxmox forum reports confirm this board has unusually well-isolated IOMMU groups for a consumer motherboard — no `pcie_acs_override` needed, no kernel patches. Confirmed working with simultaneous multi-GPU passthrough.
3. **All-black workstation aesthetic, no on-board RGB.** The board has Aura Sync headers but no on-board lighting itself, so it stays dark inside the closed case.

The 7600 is intentionally a lightweight CPU. For LLM inference, the CPU is just a coordinator — the GPUs do the work — and the 65 W chip lets the Phantom Spirit cooler run nearly silent. If the host is later repurposed as a daily driver where CPU compute matters, swap to a 7950X (170 W TDP) and step the cooler up to a 360 mm AIO.

### 1.2 GPUs: 2× AMD Radeon Pro V620 + 1× NVIDIA RTX 3060

| Spec | V620 | 3060 |
| --- | --- | --- |
| Architecture | RDNA 2 (Navi 21, `gfx1030`) | Ampere (GA106, `sm_86`) |
| VRAM | 32 GB GDDR6 | 12 GB GDDR6 |
| Memory bandwidth | 512 GB/s | 360 GB/s |
| FP16 compute | ~38 TFLOPS | ~12.7 TFLOPS |
| TDP | 300 W (passive — needs forced air) | 170 W |
| Form factor | Dual-slot, full-height, **passive** (server card) | Dual-slot, axial fan |
| PCIe | 4.0 x16 → operates at 4.0 x8 (CPU slots 1+2) or x4 (chipset slot 3) on this board |
| Pool | **64 GB** combined across the two V620s | Standalone |

The V620 is AMD's professional inference card based on the same Navi 21 silicon as the 6900 XT but configured for high-density server deployments — passive cooling, ECC GDDR6 (configurable), full-height dual-slot. ROCm 6.x officially supports it (`gfx1030` target). The 3060 is unchanged from v1 — a known-good Ampere card with mature CUDA support.

### 1.3 Why this GPU mix

Two design decisions drove this configuration:

**Why V620s for primary inference.** The previous setup pooled 36 GB across three 3060s and was VRAM-limited at the high end — large models at high context filled the pool with no headroom for concurrent requests (see v1 §3.6). Stepping up to two V620s nearly doubles pooled VRAM to 64 GB while reducing card count from three to two — fewer slots, fewer power connectors, fewer thermal hot-spots. The V620's higher FP16 throughput and 512 GB/s bandwidth (vs. 360 GB/s on the 3060) also help with the Qwen 3.5 35B target model.

**Why keep a 3060 instead of a third V620.** Two reasons. First, V620s require active cooling that isn't built into the card — adding a third means another shroud, another fan, another 300 W of heat. Second, dedicating the 3060 to **embedding and reranking workloads** isolates them from the V620 chat pool. RAG ingestion (5,000-document re-embeds) never queues behind interactive chat traffic, and the embedder + reranker have low VRAM requirements that leave the 3060 with substantial headroom for ad-hoc workloads (Whisper transcription, small vision models, etc.).

The 3060 effectively becomes "infrastructure GPU" for the V620 cluster — a workload-isolation tier rather than a competitor for the same workloads.

> **Note on speculative decoding architecture:** llama.cpp's speculative decoding requires both target and draft models in the **same** `llama-server` process (via `-md` / `--model-draft` flag) — they cannot communicate across LXCs or over the network. The corrected architecture loads both Qwen 3.5 35B (target, ~22 GB) and Qwen 3.5 0.8B (draft, ~600 MB) into the V620 LXC together. The 0.8B draft is small enough to coexist with the 35B target in the V620 VRAM pool without measurable cost.

### 1.4 V620 active cooling: shroud kit + 80 mm fans + 12 V Molex

V620s ship without integral fans — they expect a server chassis with high-static-pressure 60–80 mm fans pushing air through their dense passive heatsinks along the length of the card. In a tower like the Define 7 XL, ambient airflow alone won't cool a 300 W passive card; the cards will thermal-throttle within minutes.

The fix is an aftermarket shroud kit that bolts an 80 mm fan to the rear of each card. The kit used here is the [GF Computers V620/V520/BC-160 cooling fan shroud](https://www.ebay.com/itm/285399696488) — a 3D-printed adapter that mounts a standard 80×80×25 mm fan to the rear of the card and channels its airflow through the heatsink fins.

| Component | Pick | Purpose |
| --- | --- | --- |
| Shroud kit | GF Computers V620 80 mm shroud (×2) | Channels air through heatsink |
| Shroud fans | Noctua NF-A8 PWM (×2 + 1 spare) | Quiet, high static pressure 80 mm |
| Power | SATA-to-3-pin fan adapter @ 12 V (constant on) | Independent of motherboard control |

Power is supplied directly from the PSU at 12 V via a SATA-to-3-pin adapter, bypassing the motherboard's fan controller entirely. Rationale: the V620s draw constant cooling demand whenever they're powered (even at idle they generate ~30 W per card and rely on forced airflow). Running the shroud fans at constant 12 V is simpler than trying to thread PWM through Proxmox's fan-curve story, and the Noctua NF-A8 PWM at 12 V tops out at ~22 dB(A) per fan — completely inaudible inside the dampened Define 7 XL.

For powering and **controlling speed** of the V620 shroud fans, the recommended approach is:

**Approach A (recommended): Motherboard PWM with software bridge.** Plug all 4× NF-A8 PWM fans into a single motherboard CHA_FAN header via a 4-way PWM splitter (~$5). On the Proxmox host, run a systemd service that reads V620 temperatures from inside the V620 LXC (via a shared bind-mount file) and writes PWM duty cycle to the motherboard's hwmon PWM endpoint. Power budget check: 4× NF-A8 PWM @ 0.08A each = 0.32A, well within the X870E-Creator's 1A-per-header rating.

This gives you V620-temperature-driven fan control: idle is whisper-quiet (~25% PWM ≈ 1000 RPM), and fans ramp to full only when V620 edge temp exceeds 80°C. Implementation details are in the runbook (Step 5.13).

**Approach B: Aquacomputer Quadro fan controller (~$70).** A dedicated USB-controlled hardware fan controller with thermistor temperature inputs. Stick a thermistor on each V620 heatsink, wire fans to one Quadro PWM channel, and configure curves via `liquidctl`. Independent of OS state — works from boot regardless of ROCm/Proxmox health.

**Approach C (fallback): SATA constant 12V.** Power the fans from a SATA-to-fan splitter at constant 12V (Noctua NA-FH1 hub, third-party SATA splitter, or NA-SAC5+NA-SYC1 combo). Reliable but always full RPM (24 dB(A) combined for 4× NF-A8). Use this only if Approach A fails or you don't want the software complexity.

### 1.5 GPU support brackets

V620 cards with the shroud installed weigh ~1.4–1.6 kg each. Three heavy GPUs in a vertical-tower orientation create real cantilever stress on the PCIe slots over time. Three **upHere G205** GPU brace supports (~$10 each, anodized aerospace aluminum, height-adjustable jack-stand design, supports single or dual-slot cards) sit on the PSU shroud floor of the Define 7 XL and prop up the far end of each card. The G205 has a magnetic + rubber-pad base so it doesn't require screws into the PSU shroud, and adjusts via a telescopic screw to dial in exact height per card.

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
│   ├── qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf
│   ├── qwen3.5-0.8b-q4_k_m.gguf  (draft model)
│   ├── qwen3-embedding-0.6b.gguf
│   └── bge-reranker-v2-m3.gguf
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
| llama.cpp ROCm (V620) | `llamacpp-amd` | 192.168.6.151 |
| llama.cpp CUDA (3060) | `llamacpp-nv` | 192.168.6.152 |
| Router | `llm-router` | 192.168.6.153 |
| AnythingLLM | `anythingllm` | 192.168.6.154 |
| MCP stack | `mcp-stack` | 192.168.6.155 |
| SearXNG | `searxng` | 192.168.6.156 |

---

## 3. PCIe Topology and Link Verification

The ProArt X870E-Creator presents three PCIe slots:

| Slot | Source | Lanes | Speed | Card |
| --- | --- | --- | --- | --- |
| PCIE_1 (top) | CPU | x16 (drops to x8 when slot 2 populated) | PCIe 5.0 | V620 #1 |
| PCIE_2 | CPU | x8 (active when slot 1 also populated) | PCIe 5.0 | V620 #2 |
| PCIE_3 (bottom) | X870E chipset | x4 | PCIe 4.0 | RTX 3060 |

V620 is a PCIe 4.0 card, so PCIe 5.0 negotiation drops to 4.0 — expected and fine. The 3060 is also PCIe 4.0, also drops to 4.0 in the chipset slot.

Verify enumeration:

```bash
lspci -nn | grep -iE "vga|3d controller"
# Expect three entries: two AMD V620 (1002:73a3 or similar Navi 21 ID) and one NVIDIA GA106 (10de:2504)
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
- 3060 in PCIE_3: `LnkSta: Speed 16GT/s, Width x4`

If any card shows `Width x1` or `Speed 2.5GT/s`, the slot is misconfigured. Check that **Above 4G Decoding** and **Re-Size BAR** are enabled (§2.2) and that no M.2 drive is stealing lanes from the slot in question. On this board, populating M.2_2 drops PCIE_2 from x8 to x4 — leave M.2_2 empty unless you accept that bandwidth hit on V620 #2.

### Driver-level verification

After ROCm and CUDA are installed in their respective LXCs (§5 and §6), repeat the link check from each container:

```bash
# In llamacpp-amd LXC
rocm-smi --showpcie
# Expect both V620s at PCIe 4.0 x8

# In llamacpp-nv LXC
nvidia-smi --query-gpu=index,name,pci.bus_id,pcie.link.gen.current,pcie.link.width.current --format=csv
# Expect: 0, NVIDIA GeForce RTX 3060, ..., 4, 4
```

### Known X870E PCIe quirk

There is a documented X870E firmware bug where, after a VFIO Function Level Reset, the slot permanently downgrades from x16 to x8 until the next cold boot. This affects **VFIO passthrough to VMs**, not LXC cgroup-based passthrough, which doesn't issue FLRs. Since this build uses LXC for all GPU stacks, the bug doesn't bite. If a future workload needs VFIO-based GPU passthrough to a VM (e.g., a Windows daily-driver VM with the 3060), be aware: cold-boot recovery is required to restore x16 width after each VM stop.

---

## 4. LXC Provisioning Strategy

### 4.1 Why LXC for GPU workloads

LXC containers share the host kernel, which means a GPU passed through via cgroup device permissions appears in the container as the same `/dev/dri/render*` (AMD) or `/dev/nvidia*` (NVIDIA) node it would on bare metal — no virtualization overhead, no FLR-based reset issues, no driver-version mismatch between guest and host. For inference workloads where every percent of GPU throughput matters and where the workload is Linux-native, LXC beats VMs by a meaningful margin.

The trade-off is that LXC containers can't isolate GPUs from the host — the host kernel sees them too, and you have to be careful that the host doesn't try to bind a driver before the LXC takes over. In practice this means letting the host load `amdgpu` and the NVIDIA kernel module normally; the LXCs just get cgroup access to the device nodes.

### 4.2 Bare-metal in LXC vs Docker-in-LXC

Different services in this stack get different treatment:

**Bare-metal in LXC (no Docker layer):**
- `llamacpp-amd` — the V620 inference stack
- `llamacpp-nv` — the 3060 stack
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

Per the Qwen 3.5 model family, the V620 stack runs the **35B target model**. The 0.8B draft variant goes on the 3060 (§6.4).

Model | Size | Quant | Context | Use
--- | --- | --- | --- | ---
qwen3.5:35b-a3b | 24 GB | Q4_K_M | 256 K theoretical, 128 K practical | Primary chat / coding
qwen3.5:27b | 17 GB | Q4_K_M | 256 K theoretical, 128 K practical | Optional alternate (more headroom)

VRAM budget at 128 K context on 64 GB pool (2× V620 32 GB):
- Weights (35B Q4_K_M): ~24 GB
- KV cache @ 128 K context: ~10 GB
- Activations + overhead: ~3 GB
- **Total: ~37 GB**, leaves ~27 GB pooled headroom for concurrent requests or context expansion

At 256 K context the KV cache balloons to ~20 GB and total occupancy hits ~47 GB — still fits but with much less headroom. For routine use, 128 K is the sweet spot.

Download via Ollama-style registry pulls (which produce gguf files we can use with llama.cpp directly), or fetch from Hugging Face in gguf format. Place in `/tank/models/` on the host; the LXC sees them at `/opt/models/`:

```bash
# From the Proxmox host (not inside the LXC)
cd /tank/models
wget https://huggingface.co/.../qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf
wget https://huggingface.co/.../qwen3.5-0.8b-q4_k_m.gguf
wget https://huggingface.co/.../qwen3-embedding-0.6b.gguf
wget https://huggingface.co/.../bge-reranker-v2-m3.gguf
```

(Exact URLs depend on the model maintainer's gguf release; substitute the specific community quant of choice. Bartowski and TheBloke produce reliable Qwen quants on Hugging Face.)

### 5.6 llama-server systemd unit

`/etc/systemd/system/llama-server.service` inside the `llamacpp-amd` LXC:

```ini
[Unit]
Description=llama.cpp server (V620 ROCm — Qwen 3.5 35B + 0.8B draft for spec decode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
Environment="HIP_VISIBLE_DEVICES=0,1"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="GGML_HIP_UMA=0"
ExecStart=/opt/llama.cpp/build/bin/llama-server \
    --model /opt/models/qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf \
    --model-draft /opt/models/qwen3.5-0.8b-q4_k_m.gguf \
    --host 0.0.0.0 \
    --port 8080 \
    --ctx-size 131072 \
    --n-gpu-layers all \
    --n-gpu-layers-draft all \
    --tensor-split 1,1 \
    --batch-size 512 \
    --ubatch-size 512 \
    --threads 8 \
    --parallel 2 \
    --cont-batching \
    --cache-type-k q8_0 \
    --cache-type-v q8_0 \
    --spec-draft-n-max 16 \
    --spec-draft-n-min 0 \
    --metrics
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Key flag rationale:
- `--model-draft /opt/models/qwen3.5-0.8b-q4_k_m.gguf` — local draft model for speculative decoding. Both target and draft must be loaded in the same `llama-server` process (this is how llama.cpp implements spec decode).
- `--n-gpu-layers-draft all` — offload all draft model layers to GPU. The 0.8B draft is tiny (~600 MB) and shares VRAM with the target model.
- `--spec-draft-n-max 16` — maximum draft tokens per iteration. Default is 16, fine for most workloads.
- `--n-gpu-layers all` — offload all target model layers. 999 is a common idiom for "all".
- `--tensor-split 1,1` — split layers evenly between V620 #1 and V620 #2. Tune in §5.7.
- `--ctx-size 131072` — 128 K context, fits comfortably with Q8 KV cache.
- `--cache-type-k q8_0 --cache-type-v q8_0` — quantize the KV cache to 8-bit. Halves KV memory cost vs. f16 with imperceptible quality impact.
- `--cont-batching` — continuous batching for concurrent request efficiency.
- `--parallel 2` — allow 2 concurrent sequences. Increase only after measuring VRAM headroom.

Note: Flash Attention (`--flash-attn`) has known performance issues on V620 (`gfx1030`) — it can be slower than the default attention path on this generation. **Benchmark both before enabling it in production.** See §5.7 for the benchmark procedure.

```bash
systemctl daemon-reload
systemctl enable --now llama-server
systemctl status llama-server
```

Confirm it's serving:

```bash
curl http://localhost:8080/v1/models
# Expect a JSON response listing the loaded model
```

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

## 6. llama.cpp CUDA LXC (3060 Stack)

### 6.1 Container creation

Same pattern as the AMD LXC, with different VMID and resource sizing:

```bash
pct create 152 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname llamacpp-nv \
  --cores 4 \
  --memory 16384 \
  --swap 4096 \
  --rootfs local-zfs:48 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=1 \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 0
```

The 3060 stack runs three small models (draft, embedder, reranker), so resource footprint is much smaller than the V620 stack.

### 6.2 NVIDIA driver + CUDA in LXC

NVIDIA passthrough to LXC is more involved than AMD because the userspace driver and kernel module versions must match exactly between host and container.

**Step 1: Install the NVIDIA driver on the Proxmox host.** Use the `nvidia-driver` package from Debian non-free (or run the official `.run` installer with `--no-kernel-module` to keep drivers but skip module install if you already have one). The host must load `nvidia` kernel modules at boot.

```bash
# On host
apt install -y pve-headers-$(uname -r)
apt install -y nvidia-driver firmware-misc-nonfree
nvidia-smi                                   # confirm host sees the 3060
```

**Step 2: Find device nodes and configure cgroup passthrough.** Edit `/etc/pve/lxc/152.conf`:

```
# Bind-mount shared model storage (read-only)
mp0: /tank/models,mp=/opt/models,ro=1

# NVIDIA character devices
lxc.cgroup2.devices.allow: c 195:* rwm
lxc.cgroup2.devices.allow: c 240:* rwm
lxc.cgroup2.devices.allow: c 506:* rwm
lxc.mount.entry: /dev/nvidia0 dev/nvidia0 none bind,optional,create=file
lxc.mount.entry: /dev/nvidiactl dev/nvidiactl none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-modeset dev/nvidia-modeset none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-uvm dev/nvidia-uvm none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-uvm-tools dev/nvidia-uvm-tools none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-caps dev/nvidia-caps none bind,optional,create=dir

lxc.apparmor.profile: unconfined
lxc.cap.drop:
```

Major numbers (195, 240, 506) vary by kernel — verify with `ls -l /dev/nvidia*` on the host.

**Step 3: Install the NVIDIA userspace inside the LXC** without installing the kernel module (the host kernel already has it):

```bash
pct start 152
pct enter 152

apt update && apt install -y wget build-essential cmake git
wget https://us.download.nvidia.com/XFree86/Linux-x86_64/<DRIVER_VERSION>/NVIDIA-Linux-x86_64-<DRIVER_VERSION>.run
chmod +x NVIDIA-Linux-x86_64-<DRIVER_VERSION>.run
./NVIDIA-Linux-x86_64-<DRIVER_VERSION>.run --no-kernel-module

# CUDA toolkit (separate package)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
dpkg -i cuda-keyring_1.1-1_all.deb
apt update
apt install -y cuda-toolkit-12-6
```

`<DRIVER_VERSION>` must match the host's NVIDIA driver version exactly — get it from `nvidia-smi` on the host. Mismatch causes `Failed to initialize NVML: Driver/library version mismatch`.

Verify:

```bash
nvidia-smi
# Expect: 1× RTX 3060 listed
```

### 6.3 llama.cpp build with CUDA

Inside the LXC:

```bash
cd /opt
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp

cmake -B build \
  -DGGML_CUDA=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLAMA_BUILD_SERVER=ON \
  -DCMAKE_CUDA_ARCHITECTURES=86

cmake --build build --config Release -j 4
```

`CMAKE_CUDA_ARCHITECTURES=86` targets Ampere (`sm_86`, the 3060's compute capability). Setting this explicitly (vs. leaving it default) avoids building bloat for unused GPU generations and shaves build time.

### 6.4 Model loadout: draft, embedder, reranker

The 3060's 12 GB hosts three models simultaneously:

| Model | Size | VRAM | Role |
| --- | --- | --- | --- |
| qwen3.5:0.8b Q4_K_M | ~600 MB | ~1.5 GB (with KV cache) | Draft model for V620's 35B target |
| qwen3-embedding:0.6b | ~600 MB | ~1.2 GB | Embeddings for AnythingLLM |
| bge-reranker-v2-m3 | ~600 MB | ~1.5 GB | Reranker for RAG retrieval |
| **Total simultaneous load** | | **~4.2 GB** | Leaves ~7 GB headroom |

All three load simultaneously with comfortable margin. llama.cpp serves each as a separate `llama-server` instance on a different port.

### 6.5 Two llama-server systemd units

The 3060 stack runs **two** services: embedder and reranker. (The speculative-decoding draft model is *not* here — it runs on the V620 LXC alongside the target model. See §5.6 and the note at the end of §6.4.)

**`/etc/systemd/system/llama-embed.service`** — embedding service:

```ini
[Unit]
Description=llama.cpp embedding server (qwen3-embedding 0.6B)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
Environment="CUDA_VISIBLE_DEVICES=0"
ExecStart=/opt/llama.cpp/build/bin/llama-server \
    --model /opt/models/qwen3-embedding-0.6b.gguf \
    --host 0.0.0.0 \
    --port 8082 \
    --embedding \
    --pooling cls \
    --ctx-size 8192 \
    --n-gpu-layers all \
    --batch-size 512 \
    --threads 4
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**`/etc/systemd/system/llama-rerank.service`** — reranker:

```ini
[Unit]
Description=llama.cpp reranker (bge-reranker-v2-m3)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
Environment="CUDA_VISIBLE_DEVICES=0"
ExecStart=/opt/llama.cpp/build/bin/llama-server \
    --model /opt/models/bge-reranker-v2-m3-q4_k_m.gguf \
    --host 0.0.0.0 \
    --port 8083 \
    --reranking \
    --ctx-size 8192 \
    --n-gpu-layers all \
    --threads 4
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable both:

```bash
systemctl daemon-reload
systemctl enable --now llama-embed llama-rerank
```

Verify VRAM usage on the 3060 after all three are running:

```bash
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
# Expect: ~3500-4500 MiB used of 12288 MiB total
```

The 3060 has substantial VRAM headroom (~7-8 GB free) for future workloads — Whisper transcription, small vision models, alternative reranker variants, etc.

### Note on speculative decoding

Speculative decoding for the V620 stack is configured **entirely within the V620 LXC**. Both target (Qwen 3.5 35B) and draft (Qwen 3.5 0.8B) models load in the same `llama-server` process via `-md` flag — see §5.6. The 3060 LXC plays no role in spec decode and runs only embedder/reranker workloads. This architectural choice was driven by llama.cpp's API: spec decode requires both models to share the same process for tokenizer access and KV cache coordination. There is no remote-draft-model API.

---

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
POST /v1/embeddings                         llamacpp-nv:8082 (3060 embedder)
POST /v1/rerank                             llamacpp-nv:8083 (3060 reranker)
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

- **AnythingLLM** sets `X-Strip-Thinking: true` (or uses a model named `rag-qwen3.5`) → reasoning stripped, lower latency to first answer token.
- **OpenCode** sends no header and uses model `coder-qwen3.5` → reasoning preserved, multi-turn thinking continuity works.
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
EMBED_URL   = os.environ.get("EMBED_URL",   "http://192.168.6.152:8082")
RERANK_URL  = os.environ.get("RERANK_URL",  "http://192.168.6.152:8083")
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
Environment="EMBED_URL=http://192.168.6.152:8082"
Environment="RERANK_URL=http://192.168.6.152:8083"
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
  -d '{"model":"qwen3.5","stream":true,"messages":[{"role":"user","content":"think briefly, then say hi"}]}'
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
- Model: `qwen3.5-35b` (or whatever name llama.cpp reports — check `curl http://192.168.6.151:8080/v1/models`)
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
    "chatModel": "qwen3.5-35b",
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

# Trigger embeddings via the router → 3060 embedder
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

The embedding step is dramatically faster than v1 because it runs on the dedicated 3060 embedder service rather than competing with chat traffic on the GPU pool.

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
| `llamacpp-amd` (151) | 192.168.6.151 | 8080 | llama-server (V620, 35B target + 0.8B draft) | Speculative decoding native via `-md` flag |
| `llamacpp-nv` (152) | 192.168.6.152 | 8082 | llama-server (3060, embedder) | qwen3-embedding 0.6B |
| `llamacpp-nv` (152) | 192.168.6.152 | 8083 | llama-server (3060, reranker) | bge-reranker-v2-m3 |
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
/tank/                                       # ZFS pool on secondary NVMe
├── models/                                  # gguf model files (mounted ro into both llama.cpp LXCs)
│   ├── qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf
│   ├── qwen3.5-0.8b-q4_k_m.gguf
│   ├── qwen3-embedding-0.6b.gguf
│   └── bge-reranker-v2-m3-q4_k_m.gguf
├── anythingllm/                             # AnythingLLM persistent storage
│   └── storage/
│       └── documents/
│           └── custom-documents/            # ~5,000 ingested .json files
└── mcp/                                     # MCP container working state
```

### llamacpp-amd LXC (151)
```
/opt/
├── models/                                  # bind-mounted from /tank/models (ro)
└── llama.cpp/
    └── build/bin/llama-server
```

### llamacpp-nv LXC (152)
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
lspci -nn | grep -iE "vga|3d controller"
# Expect: 2× AMD V620 + 1× NVIDIA RTX 3060

# === 2. Host IOMMU groups are clean ===
for d in /sys/kernel/iommu_groups/*/devices/*; do
  n=${d#*/iommu_groups/*}; n=${n%%/*}
  printf 'IOMMU Group %s ' "$n"
  lspci -nns "${d##*/}"
done | sort -V | grep -iE "vga|3d|audio.*nvidia|audio.*amd"
# Each GPU + audio function should be in its own group

# === 3. ROCm sees both V620s (from llamacpp-amd LXC) ===
pct exec 151 -- rocm-smi --showtemp
# Expect tabular output with GPU 0 and GPU 1

# === 4. CUDA sees the 3060 (from llamacpp-nv LXC) ===
pct exec 152 -- nvidia-smi --query-gpu=index,name,memory.total --format=csv
# Expect: 0, NVIDIA GeForce RTX 3060, 12288 MiB

# === 5. V620 llama-server is responding ===
curl -s http://192.168.6.151:8080/v1/models | jq '.data[].id'
# Expect: model identifier(s) including qwen3.5-35b

# === 6. 3060 services are responding ===
for port in 8082 8083; do
  echo "=== port $port ==="
  curl -s "http://192.168.6.152:$port/v1/models" | jq '.data[].id'
done

# === 7. Speculative decoding is wired (both target + draft on V620 LXC) ===
pct exec 151 -- journalctl -u llama-server --since "10 min ago" | grep -iE "draft|spec"
# Expect lines indicating draft acceptance statistics during recent generations

# === 8. Router is healthy and aggregates models ===
curl -s http://192.168.6.153:8000/healthz
curl -s http://192.168.6.153:8000/v1/models | jq '.data[].id'
# Expect: aggregated list from both backends

# === 9. Router strips thinking when asked ===
curl -sN http://192.168.6.153:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Strip-Thinking: true" \
  -d '{"model":"qwen3.5","stream":true,"messages":[{"role":"user","content":"think then say hi"}]}' \
  | grep -c '<think>'
# Expect: 0

# === 10. Router preserves thinking by default ===
curl -sN http://192.168.6.153:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5","stream":true,"messages":[{"role":"user","content":"think then say hi"}]}' \
  | grep -c '<think>'
# Expect: > 0 (thinking blocks present)

# === 11. Router keepalive works during long generation ===
curl -sN http://192.168.6.153:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5","stream":true,"messages":[{"role":"user","content":"Write a 2000-word essay on ZFS internals."}]}' \
  | grep -E "^: ping" | head -3
# Expect: at least one ": ping" line during slow generation

# === 12. Embedding produces 1024-dim vectors ===
curl -s http://192.168.6.153:8000/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-embedding","input":"dimension probe"}' \
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

### llamacpp-nv-lxc.sh

```bash
#!/usr/bin/env bash
# llamacpp-nv-lxc.sh — Provision the 3060 CUDA llama.cpp LXC

set -euo pipefail

VMID="${VMID:-152}"
HOSTNAME="${HOSTNAME:-llamacpp-nv}"
TEMPLATE="${TEMPLATE:-local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
CORES="${CORES:-4}"
MEMORY="${MEMORY:-16384}"
ROOTFS="${ROOTFS:-local-zfs:48}"
BRIDGE="${BRIDGE:-vmbr0}"
MODELS_DIR="${MODELS_DIR:-/tank/models}"
DRIVER_VERSION="${DRIVER_VERSION:?must specify DRIVER_VERSION matching host nvidia-smi}"

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

CONF="/etc/pve/lxc/${VMID}.conf"
cat <<EOF >> "$CONF"

mp0: ${MODELS_DIR},mp=/opt/models,ro=1

lxc.cgroup2.devices.allow: c 195:* rwm
lxc.cgroup2.devices.allow: c 240:* rwm
lxc.cgroup2.devices.allow: c 506:* rwm
lxc.mount.entry: /dev/nvidia0 dev/nvidia0 none bind,optional,create=file
lxc.mount.entry: /dev/nvidiactl dev/nvidiactl none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-modeset dev/nvidia-modeset none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-uvm dev/nvidia-uvm none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-uvm-tools dev/nvidia-uvm-tools none bind,optional,create=file
lxc.mount.entry: /dev/nvidia-caps dev/nvidia-caps none bind,optional,create=dir

lxc.apparmor.profile: unconfined
lxc.cap.drop:
EOF

pct start "$VMID"
sleep 5

pct exec "$VMID" -- bash -c "
  set -e
  apt update && apt install -y wget build-essential cmake git
  cd /tmp
  wget -q https://us.download.nvidia.com/XFree86/Linux-x86_64/${DRIVER_VERSION}/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run
  chmod +x NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run
  ./NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run --no-kernel-module -s

  wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
  dpkg -i cuda-keyring_1.1-1_all.deb
  apt update
  apt install -y cuda-toolkit-12-6

  cd /opt
  git clone https://github.com/ggerganov/llama.cpp.git
  cd llama.cpp
  cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 \
    -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_SERVER=ON
  cmake --build build --config Release -j 4
"

echo "==> Done. Verify with: pct exec $VMID -- nvidia-smi"
```

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
- **Qwen team (Alibaba Cloud)** — open model weights including the 0.8B/35B family pairing that enables speculative decoding
- **AnythingLLM (Mintplex Labs)** — RAG frontend, unchanged from v1
