# Local GPU Cluster v2 — Deployment Runbook

**Sequential, step-by-step deployment of the v2 cluster: ASUS ProArt X870E-Creator + Ryzen 7600 + 2× V620 + 1× RTX 3060 on Proxmox VE 9.x.**

This runbook is the operational companion to [`local-gpu-cluster-v2.md`](./local-gpu-cluster-v2.md). The v2 doc explains *why*; this runbook is the exact *how*. Every command is meant to be run in order. Each phase ends with a verification step — do not proceed if it fails.

> **Architecture correction notes (vs. v2 doc rev 1):**
>
> 1. Earlier versions described speculative decoding via a cross-LXC `--draft-url` flag. That flag does not exist in upstream llama.cpp. Speculative decoding requires both target and draft models in the same `llama-server` process (via `-md` / `--model-draft`). Corrected: both models live on the V620 LXC; the 3060 LXC handles embeddings and reranking exclusively.
>
> 2. The "Qwen 3.5 35B" model is technically **Qwen3.5-35B-A3B** — a Mixture-of-Experts model with 3 B active parameters per token out of 35 B total. Inference speed is closer to a 3 B model while VRAM cost matches the full 35 B weight set. Speculative decoding still works but speedup may be modest since the target model is already fast.
>
> 3. Proxmox VE 9.1 (released Nov 2025) defaults to Linux kernel 6.17, which has known incompatibilities with the current Debian NVIDIA DKMS packages. This runbook pins the host kernel to 6.14 for stability with NVIDIA workloads (Phase 4).
>
> 4. Modern Proxmox LXC GPU passthrough uses the `dev0:` configuration syntax (or `pct set ... -dev0`), which is more robust to host restarts than raw `lxc.cgroup2.devices.allow` + `lxc.mount.entry` directives. This runbook uses `dev0:` syntax throughout.

---

## Table of Contents

- [Pre-deployment Checklist](#pre-deployment-checklist)
- [Phase 1 — Hardware Assembly](#phase-1--hardware-assembly)
- [Phase 2 — BIOS Configuration](#phase-2--bios-configuration)
- [Phase 3 — Proxmox VE 9.x Installation](#phase-3--proxmox-ve-9x-installation)
- [Phase 4 — Host Configuration](#phase-4--host-configuration)
- [Phase 5 — Provision the V620 LXC (151 — `llamacpp-amd`)](#phase-5--provision-the-v620-lxc-151--llamacpp-amd)
- [Phase 6 — Provision the 3060 LXC (152 — `llamacpp-nv`)](#phase-6--provision-the-3060-lxc-152--llamacpp-nv)
- [Phase 7 — Deploy the Router LXC (153 — `llm-router`)](#phase-7--deploy-the-router-lxc-153--llm-router)
- [Phase 8 — Deploy AnythingLLM LXC (154 — `anythingllm`)](#phase-8--deploy-anythingllm-lxc-154--anythingllm)
- [Phase 9 — Deploy MCP Stack LXC (155 — `mcp-stack`)](#phase-9--deploy-mcp-stack-lxc-155--mcp-stack)
- [Phase 10 — Data Migration from v1](#phase-10--data-migration-from-v1)
- [Phase 11 — Final Verification](#phase-11--final-verification)
- [Backup Procedures](#backup-procedures)
- [Monitoring Procedures](#monitoring-procedures)
- [Recovery Procedures](#recovery-procedures)
- [Troubleshooting Cheatsheet](#troubleshooting-cheatsheet)

---

## Pre-deployment Checklist

Before you begin, confirm you have:

**Hardware:**
- [ ] ASUS ProArt X870E-Creator WiFi motherboard ✅ (verified: 4 M.2 slots, dual 10GbE+2.5GbE, AM5)
- [ ] AMD Ryzen 7600 CPU
- [ ] DDR5 memory (32 GB minimum, 64 GB recommended) — **note: 4 DIMMs limit AMD-validated speed to 3600 MT/s**; 2× 32GB DIMMs run at 5600 MT/s if you want full speed
- [ ] 2× AMD Radeon Pro V620 (32 GB GDDR6 each)
- [ ] 1× NVIDIA RTX 3060 (12 GB)
- [ ] Thermalright Phantom Spirit 120 EVO CPU cooler ✅
- [ ] be quiet! Power Zone 2 1200W PSU ✅ (verified: 80+ Platinum, dual 12V-2x6, HEC-built)
- [ ] Fractal Design Define 7 XL case (FD-C-DEF7X-01) — supports up to 9× 140mm fans
- [ ] 2× GF Computers V620 80mm cooling shroud kits (eBay) — **shrouds only, no fans/power included**
- [ ] **2-4× Noctua NF-A8 PWM 80mm fans** — quantity depends on shroud (1 vs 2 fans per V620 shroud); confirm before ordering. If each V620 shroud holds 2 fans (typical), you need 4 total
- [ ] Fan power for V620 shrouds: one of (Noctua NA-FH1 fan hub, OR SATA → 3× 3-pin splitter cable, OR NA-SAC5 + NA-SYC1 Y-cable)
- [ ] **3× upHere G205 GPU brace supports** (anti-sag jack stands) — one per GPU
- [ ] **5× ARCTIC P14 Pro PST 140mm chassis fans** (recommended upgrade over Define 7 XL stock GP-14 fans; PST chain on a single motherboard header)
- [ ] Boot NVMe (1 TB+ recommended) for Proxmox — install in M.2_1 (CPU PCIe 5.0 x4)
- [ ] Secondary NVMe (2 TB+ recommended) for `/tank` (models + data) — install in M.2_3 or M.2_4 (chipset PCIe 4.0)
- [ ] Quality PCIe 4.0 x16 riser (LinkUp Ultra) — **only if using vertical mount** (not needed for standard 3-GPU horizontal layout)

**Reference data:**
- [ ] Old T7910 host accessible (for data migration from v1)
- [ ] Existing AnythingLLM API keys recorded
- [ ] Existing OpenCode config file backed up
- [ ] LAN DHCP reservations available for new LXC IPs (or static plan)
- [ ] Domain/hostname plan (default: `gpu-cluster.local`)

**Software downloads (do these in advance over fast internet):**
- [ ] Proxmox VE 9.x ISO (`proxmox-ve_9.x.iso`)
- [ ] Ubuntu 24.04 standard LXC template
- [ ] V620 BIOS update files (if applicable)

**Estimated total deployment time:** 6–10 hours over 1–2 days, including model downloads.

---

## Phase 1 — Hardware Assembly

**Estimated time:** 2–3 hours

### Step 1.1 — Prepare the case

1. Remove both side panels of the Define 7 XL.
2. Remove the HDD cage if present (frees up 467 mm GPU clearance, rather than 315 mm).
3. Install the PSU — Power Zone 2 mounts at the bottom, fan facing down. Use the included anti-vibration mounting screws.
4. Install the motherboard standoffs in the EE-ATX positions per the case manual. Confirm all 9 standoffs are present and seated.

### Step 1.2 — Install CPU and RAM

1. Open the ProArt X870E-Creator's CPU socket (lift the lever).
2. Seat the Ryzen 7600 — the gold triangle on the chip aligns with the triangle on the socket.
3. Lower the lever until it clicks home. Some force is normal.
4. Install RAM in slots **A2 and B2** (second from the left in each pair) for 2-DIMM configurations. For 4-DIMM, fill A1+A2+B1+B2.
5. **Important for AM5 + ECC:** AMD's official spec is 5600 MT/s with 2 DIMMs, dropping to 3600 MT/s with 4 DIMMs. If you have ECC RAM and 4 DIMMs, expect the slower speed.

### Step 1.3 — Install CPU cooler

1. Apply thermal paste — pea-sized drop in the center of the CPU IHS. Do not spread.
2. Install the Phantom Spirit 120 EVO mounting bracket per its instructions (AM5 mounting hardware is included in the box).
3. Mount the cooler. The fan should blow toward the rear of the case.
4. Connect the CPU fan PWM cable to the **CPU_FAN** header.

### Step 1.4 — Install motherboard

1. Install the I/O shield first (pre-mounted on this board — confirm it's correctly oriented).
2. Lower the motherboard onto the standoffs, aligning with the I/O shield.
3. Install all 9 motherboard screws. Snug, but don't overtighten.
4. Connect the 24-pin ATX and both 8-pin EPS power connectors.
5. Connect front-panel headers per the motherboard manual (use ASUS Q-Connector if included).

### Step 1.5 — Install storage

The X870E-Creator has **four** M.2 slots: two PCIe 5.0 (CPU-attached, M.2_1 and M.2_2) and two PCIe 4.0 (chipset-attached, M.2_3 and M.2_4). Populate them carefully:

1. **M.2_1** (top, near CPU socket — PCIe 5.0 x4 from CPU): Boot NVMe goes here. No lane-sharing penalty.
2. **M.2_3** or **M.2_4** (chipset, lower positions, PCIe 4.0 x4): Secondary NVMe for `/tank`. Either is fine; both are chipset-attached and don't affect GPU lanes.
3. **Leave M.2_2 empty** — per ASUS spec, populating M.2_2 drops PCIEX16(G5)_2 from x8 to x4, hurting V620 #2 bandwidth.
4. Install M.2 heatsinks on both drives (the board provides them; pull tabs are tool-free).

### Step 1.6 — Install GPUs (in this exact order)

1. **PCIE_1 (top slot):** V620 #1 — secure the slot retention clip
2. **PCIE_2 (middle slot):** V620 #2 — secure the slot retention clip
3. **PCIE_3 (bottom slot):** RTX 3060 — secure the slot retention clip

For each GPU, install screws to secure to the rear case bracket. **Do not power them on yet** — fan shrouds and supports come next.

### Step 1.7 — Install V620 fan shrouds

1. Mount each 80 mm Noctua NF-A8 to a V620 shroud per the shroud kit instructions.
2. Attach the shroud assembly to the rear of each V620 with the included screws. The fan should pull air **through** the heatsink toward the rear of the card.
3. Route the 3-pin fan cables along the bottom of the case toward where the SATA-to-fan adapter will attach.

### Step 1.8 — Install GPU support brackets

1. Position three upHere G205 GPU brace supports on the PSU shroud floor — one under the front edge of each GPU.
2. Adjust each bracket's telescopic screw so the rubber pad just touches the underside of the GPU's heatsink. The card should be level — neither sagging nor lifted.
3. The G205's magnetic + rubber base holds it in place without needing screws into the PSU shroud.

### Step 1.9 — Power connections

1. **24-pin ATX** → motherboard
2. **2× 8-pin EPS** → motherboard CPU power (both required)
3. **V620 #1:** 2× 8-pin PCIe (use 2 separate PSU cables, not one daisy-chain — 300 W on a single cable is at the safety edge)
4. **V620 #2:** 2× 8-pin PCIe (one cable acceptable here since you only have 3 PCIe cables in the box; daisy-chain the second connector)
5. **RTX 3060:** 1× 8-pin PCIe (daisy-chain off V620 #2's cable is fine — 170 W well under safety limit)
6. **Shroud fans (V620 cooling):** see Step 1.9.1 below for the recommended PWM control approach.

### Step 1.9.1 — V620 shroud fan power and control

The 4× NF-A8 PWM fans on the V620 shrouds need power AND ideally PWM-modulated speed control so they idle quietly when the GPUs aren't under load. There are three approaches; pick one:

**Approach A (recommended): Motherboard PWM with software bridge.**

This gives you V620-temperature-driven fan speed control. Idle is whisper-quiet (~25% PWM, ~1000 RPM, nearly inaudible). Under heavy inference, fans ramp to full automatically.

Hardware:
- 1× 4-way 4-pin PWM fan splitter (any brand, ~$5; or daisy-chain via NA-SYC1 Y-cables)
- All 4× NF-A8 PWM → splitter → CHA_FAN3 motherboard header

Power budget check: NF-A8 PWM draws 0.08A max @ 12V. 4 fans = 0.32A; ASUS X870E-Creator headers are rated 1A each. Well within budget.

BIOS configuration (Step 2.2 covers BIOS broadly):
- Q-Fan Configuration → CHA_FAN3 → Mode: **PWM**
- Speed control: **Manual**
- Set a flat 50% baseline curve as a fail-safe (if the software bridge dies, fans stay at safe speed)

Software setup (defer until after Phase 4 — LXC 151 must be running first):
- See Step 5.X below for the systemd services that read V620 temps and write motherboard PWM.

**Approach B: Aquacomputer Quadro fan controller (~$70).**

If you prefer hardware-based control independent of OS state:
- Buy 1× Aquacomputer Quadro (~$70) and 2× 10K thermistors (~$5 each)
- Stick one thermistor under each V620's shroud, near the heatsink fins
- Wire 4× NF-A8 to one Quadro PWM channel; thermistors to two Quadro temp inputs
- Configure curve via `liquidctl` on Linux: `liquidctl --match Quadro set fan1 speed 30 50 50 70 80 100`
- Quadro is USB-connected; runs from boot independent of OS

**Approach C (simplest, but loud): SATA constant 12V.**

Original plan from Round 8: power the fans from a SATA-to-3/4-fan splitter (Noctua NA-FH1, NA-SAC5+NA-SYC1, or third-party). All fans run at constant 12V = full 2200 RPM. Reliable but always loud (4× NF-A8 at full RPM ≈ 24 dB(A) measured at 1m). Use this only if Approach A's software bridge is not desired.

For the rest of this runbook, **Approach A is assumed**. The software bridge implementation appears in Step 5.X below.

### Step 1.10 — Install case fans

The Define 7 XL ships with 3× Dynamic X2 GP-14 fans pre-installed. **Replace/supplement with the 5× ARCTIC P14 Pro PST** for significantly higher airflow and static pressure (~72 CFM and 2.4 mm H₂O vs the GP-14's ~64 CFM and 1.16 mm H₂O). This matters because the V620s rely on the case airflow to feed cool air to the NF-A8s on their shrouds.

1. **Remove the 3× stock GP-14 fans** (or relocate two of them to the bottom intake positions if you want bonus airflow — Define 7 XL supports up to 9× 140mm).
2. **Front intake:** 3× ARCTIC P14 Pro PST — airflow direction into the case. These are your primary cool-air supply for the GPU stack.
3. **Top exhaust:** 2× ARCTIC P14 Pro PST — airflow direction out of the case. Removes hot air rising from GPUs and CPU.
4. **Rear exhaust:** keep the single stock GP-14 here, or leave the rear position empty if you've redeployed the stock fans elsewhere.
5. **PST chain:** the P14 Pro PST has a built-in Y-splitter (PWM Sharing Technology). Chain all 5 fans together — the first fan plugs into a single motherboard PWM header (CHA_FAN1 or similar), and the next 4 daisy-chain off it. The motherboard sees them as one fan with shared RPM signal from the master fan.
6. Configure BIOS fan curve in Step 2.2 to ramp up at 50°C+ and run silent below.

### Step 1.11 — Cable management

1. Route all cables behind the motherboard tray.
2. Use the case's included velcro straps generously.
3. Tuck unused PCIe and SATA connectors behind the tray, secured with velcro.
4. Confirm no cables interfere with fan blades or GPU airflow.

### Step 1.12 — Pre-flight check

Before powering on:

- [ ] All four PSU connectors firmly seated (24-pin, 2× EPS, GPU power)
- [ ] All three GPUs locked in slot retention clips
- [ ] No loose screws inside the case
- [ ] CPU fan and shroud fans connected and routed clear of blades
- [ ] Front panel cables connected (power button at minimum)
- [ ] PSU rocker switch off
- [ ] PSU mode switch (if Power Zone 2 has one) at default position

Now flip the PSU switch on. Press the front panel power button. Confirm:

- [ ] CPU fan spins
- [ ] V620 shroud fans spin (constant 12 V)
- [ ] All case fans spin
- [ ] No POST beeps or error LEDs on the motherboard's Q-LED indicators

If any Q-LED stays lit (CPU/DRAM/VGA/BOOT), power off and re-check the corresponding component. The most common issue is RAM not fully seated.

---

## Phase 2 — BIOS Configuration

**Estimated time:** 30 minutes

### Step 2.1 — Enter BIOS

Press `Delete` repeatedly during POST. The ProArt X870E-Creator drops to UEFI EZ Mode by default — press F7 to switch to Advanced Mode.

### Step 2.2 — Update BIOS first if needed

Confirm BIOS revision in the Main tab. **Must be 1605 or later** for stable X870E + multi-GPU configurations.

If older:
1. Download the latest BIOS from `https://www.asus.com/motherboards-components/motherboards/proart/proart-x870e-creator-wifi/helpdesk_bios/`
2. Copy the unzipped `.CAP` file to a FAT32-formatted USB drive
3. In BIOS: **Tool → ASUS EZ Flash 3 Utility** → select the file → flash → reboot
4. Re-enter BIOS after the reboot completes (it will reboot 1-2 times during the update)

### Step 2.3 — Set required BIOS values

Navigate to each setting and configure:

| Setting | Path | Value |
|---|---|---|
| SVM Mode | Advanced → CPU Configuration | **Enabled** |
| IOMMU | Advanced → AMD CBS → NBIO Common Options → IOMMU | **Enabled** |
| ACS Enable | Advanced → AMD CBS → NBIO Common Options → IOMMU → ACS | **Enabled** |
| Above 4G Decoding | Advanced → PCI Subsystem Settings | **Enabled** |
| Re-Size BAR Support | Advanced → PCI Subsystem Settings | **Enabled** |
| Secure Boot | Boot → Secure Boot | **Disabled** |
| CSM | Boot → CSM | **Disabled** |
| PCIe ASPM | Advanced → PCI Subsystem | **Disabled** (for I226-V NIC stability) |
| Fast Boot | Boot | **Disabled** (helps with BMC/IPMI debugging if anything goes wrong) |

Optionally:
- **DOCP / EXPO** for memory: Apply your RAM kit's profile if you have one
- **PBO** (Precision Boost Overdrive): Leave at Auto unless you specifically want to tune

### Step 2.4 — Verify GPU presence

Navigate to **Advanced → Onboard Devices Configuration → PCIe slots** and confirm:

- PCIE_1: V620 #1 detected
- PCIE_2: V620 #2 detected
- PCIE_3: NVIDIA GeForce RTX 3060 detected

If any slot shows empty, power off and re-seat that GPU.

### Step 2.5 — Save and exit

Press F10 → Yes → reboot. From here, leave the system at the BIOS POST until Phase 3.

---

## Phase 3 — Proxmox VE 9.x Installation

**Estimated time:** 30 minutes

### Step 3.1 — Prepare installation media

On a different machine:

```bash
# Download Proxmox VE 9.x ISO (verify current version on https://www.proxmox.com/downloads)
wget https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso

# Verify checksum (replace with current published checksum)
sha256sum proxmox-ve_9.1-1.iso

# Write to USB (replace /dev/sdX with the USB device — DESTRUCTIVE)
# On Linux:
sudo dd if=proxmox-ve_9.1-1.iso of=/dev/sdX bs=4M status=progress conv=fsync
sync

# On Windows: use Rufus in DD mode (NOT ISO mode)
```

> **Kernel version note:** Proxmox VE 9.1 (Nov 2025+) ships with kernel **6.17** as default. Kernel 6.17 has known DKMS build failures with the current Debian non-free NVIDIA driver packages (550.163.x line). This runbook **pins the kernel to 6.14** in Phase 4 for NVIDIA compatibility. If you're installing fresh PVE 9.0 (kernel 6.14), no pinning needed; if 9.1+ (kernel 6.17), follow the pinning steps in §4.1.

### Step 3.2 — Boot the installer

1. Insert the USB into a **rear** USB-A port (front-panel USB occasionally has init-order issues during Proxmox boot)
2. Power on, press F8 to choose boot device, select the USB
3. At the Proxmox boot menu, select "Install Proxmox VE (Graphical)"

### Step 3.3 — Storage configuration

1. Accept EULA
2. Target hard disk: select your **boot NVMe** (M.2_1, the smaller drive for Proxmox itself)
3. Click "Options"
4. Filesystem: **zfs (RAID0)** for single-disk setup
5. Compress: **on**, ashift: **12**, copies: **1**
6. Hdsize: leave default (full disk)
7. OK to confirm

### Step 3.4 — Country / timezone / keyboard

Set per your location. Typical:
- Country: United States
- Time zone: America/Chicago (or your TZ)
- Keyboard layout: U.S. English

### Step 3.5 — Administrator account

- Password: a strong password (you'll use this for the web UI)
- Email: a valid email address (used for system notifications)

### Step 3.6 — Network configuration

- Management interface: select the **10 GbE Marvell** port (likely `enp4s0`, but verify against the wired connection actually plugged in)
- Hostname (FQDN): `gpu-cluster.local` (or per your local convention)
- IP address (CIDR): `192.168.6.150/24` (or your chosen static IP)
- Gateway: `192.168.6.1`
- DNS server: your local DNS (e.g., `192.168.6.1` or `1.1.1.1`)

### Step 3.7 — Install

Click "Install" and wait ~5 minutes. The system will reboot when complete.

### Step 3.8 — First boot verification

After reboot, access the web UI from another machine:

```
https://192.168.6.150:8006
```

Log in as `root@pam` with the password set during install. Accept the self-signed certificate warning.

### Step 3.9 — Switch to no-subscription repo

SSH into the host:

```bash
ssh root@192.168.6.150
```

Disable the enterprise repos and enable no-subscription:

```bash
# Disable enterprise repos
sed -i 's/^deb/#deb/' /etc/apt/sources.list.d/pve-enterprise.list 2>/dev/null
sed -i 's/^deb/#deb/' /etc/apt/sources.list.d/ceph.list 2>/dev/null

# Enable no-subscription repo
cat > /etc/apt/sources.list.d/pve-no-subscription.list <<EOF
deb http://download.proxmox.com/debian/pve trixie pve-no-subscription
EOF

apt update
apt full-upgrade -y
```

Reboot if any kernel updates were applied:

```bash
[ -f /var/run/reboot-required ] && reboot
```

### Step 3.10 — Verification

```bash
# Confirm PVE version
pveversion --verbose

# Confirm kernel version
uname -r
# Expect: 6.14.x-pve or similar

# Confirm CPU is recognized
lscpu | grep -i "model name"
# Expect: AMD Ryzen 5 7600 6-Core Processor

# Confirm all RAM detected
free -h | head -2
```

**Stop and verify before proceeding:**
- [ ] Web UI accessible at https://192.168.6.150:8006
- [ ] CPU correctly identified
- [ ] All RAM detected
- [ ] No-subscription repo working (`apt update` succeeds)

---

## Phase 4 — Host Configuration

**Estimated time:** 1 hour

### Step 4.1 — Pin kernel to 6.14 (if running PVE 9.1+ with kernel 6.17)

PVE 9.1+ defaults to kernel 6.17, which has DKMS build failures with current Debian NVIDIA driver packages. Pin to 6.14 before installing the NVIDIA stack:

```bash
# Check current running kernel
uname -r

# If kernel is 6.17.x: pin to 6.14 series
# First, ensure 6.14 kernel and headers are installed
apt install -y proxmox-kernel-6.14 proxmox-headers-6.14

# Pin the latest 6.14 as default
proxmox-boot-tool kernel list
# Find a 6.14.x-pve entry like "6.14.11-5-pve"

proxmox-boot-tool kernel pin 6.14.11-5-pve   # adjust to actual version

# Reboot to switch to pinned kernel
reboot

# After reboot, verify
uname -r
# Expect: 6.14.x-pve
```

If you want to remove the 6.17 kernel later (after confirming 6.14 stability):

```bash
# DON'T do this until 6.14 is verified working with all your services
apt purge proxmox-kernel-6.17 proxmox-headers-6.17
```

If you're on PVE 9.0 with kernel 6.14 already, skip this step.

### Step 4.2 — Enable IOMMU and load VFIO modules

```bash
# Edit GRUB cmdline
sed -i 's|GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt"|' /etc/default/grub

# Update bootloader
update-grub
proxmox-boot-tool refresh

# Pre-load VFIO modules (used for future VM-based passthrough; harmless for LXC)
cat >> /etc/modules <<EOF
vfio
vfio_iommu_type1
vfio_pci
EOF

update-initramfs -u -k all
reboot
```

After reboot, reconnect via SSH and verify:

```bash
# IOMMU should be active
dmesg | grep -e DMAR -e IOMMU -e AMD-Vi
# Expect lines containing "AMD-Vi: Interrupt remapping enabled"
#                       "AMD-Vi: Virtual APIC enabled"
#                       "perf/amd_iommu: Detected AMD IOMMU"
```

If IOMMU is not active, return to BIOS and re-verify Step 2.3.

### Step 4.3 — Verify IOMMU groups are clean

```bash
# Print IOMMU groups, sorted
for d in /sys/kernel/iommu_groups/*/devices/*; do
  n=${d#*/iommu_groups/*}; n=${n%%/*}
  printf 'IOMMU Group %s ' "$n"
  lspci -nns "${d##*/}"
done | sort -V | grep -iE "vga|3d|audio.*nvidia|amd.*audio"
```

Expected: each GPU and its audio function in its own dedicated IOMMU group, with no unrelated devices co-grouped. The ProArt X870E-Creator should produce clean groupings.

If groups are bad (multiple GPUs sharing a group with chipset devices), recheck BIOS settings — particularly **ACS Enable**.

### Step 4.4 — Install AMD firmware on host

Required for `amdgpu` kernel module to find current firmware blobs:

```bash
apt install -y firmware-amd-graphics
```

### Step 4.5 — Verify host sees all GPUs

```bash
lspci -nn | grep -iE "vga|3d controller|audio.*nvidia"
# Expect three GPU entries (2x AMD + 1x NVIDIA) plus their audio functions
```

```bash
# AMD GPUs should have render nodes after the next reboot
# After amdgpu loads, check:
ls -l /dev/dri/
# Expect: card0, card1, renderD128, renderD129 (one pair per V620)
```

If the AMD GPUs aren't showing render nodes, reboot once more — the kernel module load order on a fresh install sometimes needs a reboot to settle.

### Step 4.6 — Install NVIDIA driver on host

The host must have the NVIDIA driver loaded for LXC passthrough to work. Two options:

**Option A: Debian non-free package (simpler, but version may lag and DKMS can fail on newer kernels).**

```bash
# Add non-free-firmware to sources if not present
cat > /etc/apt/sources.list.d/non-free-firmware.list <<EOF
deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
EOF

apt update

# Install kernel headers (needed for DKMS module build)
apt install -y proxmox-headers-$(uname -r)

# Install NVIDIA driver (this builds DKMS modules; takes a few minutes)
apt install -y nvidia-driver firmware-nvidia-graphics
```

If DKMS fails (common on kernel 6.17 with current Debian package 550.163.x), confirm the kernel pin from §4.1 took effect: `uname -r` should show `6.14.x-pve`. If still failing, try Option B.

**Option B: NVIDIA `.run` installer with the 580.x driver line (recommended for PVE 9.x).**

```bash
# Pick a current 580.x driver — verified working: 580.95.05, 580.105.06, 580.126.09
DRIVER_VERSION=580.105.06   # or newer; check https://www.nvidia.com/en-us/drivers/

# Install kernel headers
apt install -y proxmox-headers-$(uname -r)

# Download and run installer
cd /tmp
wget https://us.download.nvidia.com/XFree86/Linux-x86_64/${DRIVER_VERSION}/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run
chmod +x NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run
./NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run --silent --dkms

# Save the .run file — you'll need the same version for the LXC
mkdir -p /root/nvidia-installer
mv NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run /root/nvidia-installer/
```

When the installer prompts (if not running `--silent`):
- Kernel module type: **NVIDIA Proprietary** (better Wayland/desktop support) or **MIT/GPL** (open kernel module — works for compute, recommended for headless servers)
- Sign kernel module: **Yes** (if you have Secure Boot enabled — but our BIOS step disabled it, so this can be **No**)
- Register DKMS module: **Yes** (rebuilds on kernel updates)
- Run nvidia-xconfig: **No** (headless host)

Reboot to load the new module:

```bash
reboot
```

After reboot:

```bash
# Confirm NVIDIA module loaded
lsmod | grep nvidia
# Expect: nvidia, nvidia_modeset, nvidia_uvm, nvidia_drm

# Confirm the GPU is visible
nvidia-smi
# Expect: tabular output listing the RTX 3060

# Note the driver version — you'll need to match it exactly inside the 3060 LXC
nvidia-smi --query-gpu=driver_version --format=csv,noheader
# Save this version (e.g., 580.105.06)
```

**Save the NVIDIA driver version and `.run` file** — the LXC needs the same userspace version as the host kernel module.

### Step 4.7 — Identify device major numbers

```bash
# AMD render and KFD nodes (note the major numbers on each line)
ls -l /dev/dri/render* /dev/kfd

# Example output:
# crw-rw---- 1 root render 226, 128 ... /dev/dri/renderD128   (V620 #1)
# crw-rw---- 1 root render 226, 129 ... /dev/dri/renderD129   (V620 #2)
# crw-rw---- 1 root render 226, 130 ... /dev/dri/renderD130   (3060 — yes, NVIDIA also has a render node)
# crw-rw-rw- 1 root render 234,   0 ... /dev/kfd

# NVIDIA character devices
ls -l /dev/nvidia*
# Example:
# crw-rw-rw- 1 root root 195,   0 ... /dev/nvidia0
# crw-rw-rw- 1 root root 195, 255 ... /dev/nvidiactl
# crw-rw-rw- 1 root root 195, 254 ... /dev/nvidia-modeset
# crw-rw-rw- 1 root root 240,   0 ... /dev/nvidia-uvm
# crw-rw-rw- 1 root root 240,   1 ... /dev/nvidia-uvm-tools
```

**Record the major numbers from your system** — they vary by kernel build:
- AMD render: typically `226`
- AMD KFD: typically `234`
- NVIDIA character: typically `195`
- NVIDIA UVM: typically `240`
- NVIDIA caps: typically `506` (varies more)

You'll need these in Phases 5 and 6.

### Step 4.8 — Set up ZFS storage for shared models

```bash
# Identify your secondary NVMe (the one for /tank, NOT the boot drive)
lsblk -d -o NAME,SIZE,MODEL
# Look for the larger NVMe that's not currently mounted

# Create the pool (replace nvme1n1 with your actual device)
SECONDARY_NVME=/dev/nvme1n1   # ADJUST THIS

zpool create -o ashift=12 tank $SECONDARY_NVME

# Create datasets
zfs create tank/models
zfs create tank/anythingllm
zfs create tank/mcp
zfs create tank/backups

# Set properties tuned for model files (large, sequential reads)
zfs set compression=lz4 tank/models
zfs set atime=off tank/models
zfs set recordsize=1M tank/models

# Verify
zfs list
# Expect: tank, tank/models, tank/anythingllm, tank/mcp, tank/backups
```

### Step 4.9 — Add ZFS storage to Proxmox

Make `tank` available as Proxmox storage for backups and ISO uploads:

```bash
# Add the directory to PVE
pvesm add dir tank-backups --path /tank/backups --content backup,iso,vztmpl

# Verify
pvesm status
# Expect: tank-backups (active)
```

### Step 4.10 — Download the Ubuntu 24.04 LXC template

```bash
# Update template list
pveam update

# Download Ubuntu 24.04 standard
pveam download local ubuntu-24.04-standard_24.04-2_amd64.tar.zst

# Verify
pveam list local | grep ubuntu-24
```

### Step 4.11 — Configure firewall (optional but recommended)

```bash
# Through web UI: Datacenter → Firewall → Options → Firewall: Yes
# Then add rules to allow SSH/HTTPS from your management subnet
# This runbook assumes firewall is OFF on individual containers (firewall=0 on net device)
# but the host firewall protects everything.

# CLI alternative — keep host firewall off for simplicity:
pve-firewall stop
sed -i 's/^enable: 1/enable: 0/' /etc/pve/firewall/cluster.fw 2>/dev/null
```

**Stop and verify before proceeding:**
- [ ] IOMMU active in `dmesg`
- [ ] Clean IOMMU groups (each GPU isolated)
- [ ] `nvidia-smi` works on host, shows the 3060
- [ ] `ls /dev/dri/` shows renderD128, renderD129, renderD130
- [ ] `zfs list` shows `tank/models`
- [ ] Ubuntu 24.04 template downloaded

---

## Phase 5 — Provision the V620 LXC (151 — `llamacpp-amd`)

**Estimated time:** 1.5 hours (mostly compile time)

### Step 5.1 — Create the LXC

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

### Step 5.2 — Configure GPU passthrough (modern `dev0:` syntax)

Modern Proxmox (PVE 8.2+) supports a `dev0:`-style passthrough syntax that's more robust to host restarts than raw `lxc.cgroup2.devices.allow` + `lxc.mount.entry` directives. Use it via `pct set` from the host:

```bash
# Find the GIDs that own the device files (typical: render=104 or 993, video=44)
RENDER_GID=$(getent group render | cut -d: -f3)
VIDEO_GID=$(getent group video | cut -d: -f3)
echo "RENDER_GID=$RENDER_GID VIDEO_GID=$VIDEO_GID"

# Apply mount and devices
pct set 151 --mp0 /tank/models,mp=/opt/models,ro=1
pct set 151 --dev0 /dev/kfd,gid=$RENDER_GID
pct set 151 --dev1 /dev/dri/renderD128,gid=$RENDER_GID
pct set 151 --dev2 /dev/dri/renderD129,gid=$RENDER_GID
```

Verify the resulting config:

```bash
pct config 151 | grep -E "^(dev|mp)"
# Expect:
# dev0: /dev/kfd,gid=104
# dev1: /dev/dri/renderD128,gid=104
# dev2: /dev/dri/renderD129,gid=104
# mp0: /tank/models,mp=/opt/models,ro=1
```

**Fallback to legacy raw syntax if `dev0:` style fails** (older PVE versions, unusual hardware): edit `/etc/pve/lxc/151.conf` directly and add:

```
mp0: /tank/models,mp=/opt/models,ro=1
lxc.cgroup2.devices.allow: c 226:128 rwm
lxc.cgroup2.devices.allow: c 226:129 rwm
lxc.cgroup2.devices.allow: c 234:* rwm
lxc.mount.entry: /dev/dri/renderD128 dev/dri/renderD128 none bind,optional,create=file
lxc.mount.entry: /dev/dri/renderD129 dev/dri/renderD129 none bind,optional,create=file
lxc.mount.entry: /dev/kfd dev/kfd none bind,optional,create=file
lxc.apparmor.profile: unconfined
lxc.cap.drop:
```

Major numbers (226, 234) are typical but vary by kernel — verify with `ls -l /dev/dri/render* /dev/kfd` from §4.7. The legacy syntax requires `apparmor.profile: unconfined` to be unblocked; the modern `dev0:` syntax usually doesn't.

### Step 5.3 — Start the LXC and verify GPU visibility

```bash
pct start 151
pct enter 151
```

You're now inside the LXC. Verify:

```bash
# Should show /dev/dri/renderD128, /dev/dri/renderD129, /dev/kfd
ls -l /dev/dri/ /dev/kfd

# Hostname check
hostname
# Expect: llamacpp-amd
```

If `/dev/kfd` is missing or render nodes aren't visible, exit the container, fix `/etc/pve/lxc/151.conf`, and `pct restart 151`.

### Step 5.4 — Install ROCm in the LXC

Inside the LXC, install ROCm using AMD's `amdgpu-install` script. As of early 2026 the current stable is ROCm 7.2.x (replacing the older 6.x line). Use the `latest` URL alias to always get the current release.

```bash
apt update
apt install -y wget gnupg2 build-essential cmake git curl

# Download and install the amdgpu-install package (latest stable)
wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/noble/amdgpu-install_*_all.deb -O /tmp/amdgpu-install.deb
apt install -y /tmp/amdgpu-install.deb

# Install ROCm WITHOUT the kernel module (the host kernel has it already)
amdgpu-install -y --usecase=rocm --no-dkms

# Add root to required groups (and the Render/Video group lookup ensures matching GID)
usermod -aG render,video root
```

**Why `--no-dkms`:** LXC containers share the host kernel. The kernel-mode AMDGPU driver is loaded once on the host (Phase 4); inside the LXC, only userspace ROCm libraries are needed. Installing DKMS inside the LXC would attempt to build a kernel module against a kernel that the LXC can't actually load modules into.

**If you need a specific ROCm version** (e.g. 6.4.x for stability with older code): pin it explicitly:

```bash
# ROCm 6.4.x example
wget https://repo.radeon.com/amdgpu-install/6.4/ubuntu/noble/amdgpu-install_6.4.60400-1_all.deb -O /tmp/amdgpu-install.deb
```

### Step 5.5 — Verify ROCm sees both V620s

```bash
rocminfo | grep -A2 "gfx1030"
# Expect: TWO Agent blocks showing Name: gfx1030

rocm-smi
# Expect: tabular output listing GPU 0 and GPU 1
```

If `rocminfo` returns no GPUs but `ls /dev/dri/` shows the render nodes:

1. Verify `/dev/kfd` is mounted: `ls -l /dev/kfd`
2. Verify the cgroup allow line for major 234 in `/etc/pve/lxc/151.conf`
3. Restart the LXC: `pct restart 151`
4. If still failing, try setting the LXC to **privileged** (remove `--unprivileged 1` and recreate, or set `unprivileged: 0` in the conf file)

### Step 5.6 — Build llama.cpp with HIP

Still inside the LXC:

```bash
cd /opt
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp

# Build with HIP backend, targeting V620's gfx1030 architecture
HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
cmake -S . -B build \
    -DGGML_HIP=ON \
    -DGPU_TARGETS=gfx1030 \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLAMA_CURL=ON

# Build (takes ~10 minutes)
cmake --build build --config Release -j$(nproc)
```

If build fails with `cannot find ROCm device library`, try setting `HIP_DEVICE_LIB_PATH` explicitly:

```bash
# Find the path containing oclc_abi_version_400.bc
find /opt/rocm -name "oclc_abi_version_400.bc" 2>/dev/null

# Use that path:
HIP_DEVICE_LIB_PATH=/opt/rocm/amdgcn/bitcode \
HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -p)" \
cmake -S . -B build -DGGML_HIP=ON -DGPU_TARGETS=gfx1030 -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
```

Verify the build:

```bash
./build/bin/llama-server --version
# Expect: version info and build info

./build/bin/llama-server --list-devices 2>&1 | head -20
# Expect: lines mentioning ROCm and gfx1030
```

### Step 5.7 — Download models

Models live on the host's `/tank/models` (mounted read-only as `/opt/models` inside the LXC). Download them from the Proxmox host, not the LXC:

**Exit the LXC first:**

```bash
exit
# You're back on the Proxmox host
```

**Download from host:**

The Qwen 3.5 family includes both target (35B-A3B, MoE with 3B active params) and draft (0.8B dense). Both are needed for speculative decoding. Recommended quantizations:

| File | Repo | Approx size |
| --- | --- | --- |
| Target: Qwen3.5-35B-A3B Q4_K_M | `unsloth/Qwen3.5-35B-A3B-GGUF` | ~22 GB |
| Draft: Qwen3.5-0.8B Q4_K_M | `unsloth/Qwen3.5-0.8B-GGUF` | ~600 MB |

```bash
cd /tank/models

# Use llama-server's built-in HF downloader (recommended — handles multi-shard automatically)
# Or wget directly:

# Target: Qwen 3.5 35B-A3B (MoE) Q4_K_M
# Note: this is multi-shard. Verify exact filenames at https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF
wget -O qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf \
  "https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF/resolve/main/Qwen3.5-35B-A3B-Q4_K_M-00001-of-00002.gguf"
wget -O qwen3.5-35b-a3b-q4_k_m-00002-of-00002.gguf \
  "https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF/resolve/main/Qwen3.5-35B-A3B-Q4_K_M-00002-of-00002.gguf"

# Draft: Qwen 3.5 0.8B Q4_K_M
wget -O qwen3.5-0.8b-q4_k_m.gguf \
  "https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF/resolve/main/Qwen3.5-0.8B-Q4_K_M.gguf"

# Verify checksums against published values on the model card
sha256sum *.gguf

chmod 644 *.gguf
```

**Alternative: use llama-server's built-in HF downloader.** Modern llama.cpp can download directly:

```bash
# Inside the LXC, the systemd unit can reference a HF repo:
# --hf-repo unsloth/Qwen3.5-35B-A3B-GGUF:Q4_K_M --hf-repo-draft unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M
# This auto-downloads to LLAMA_CACHE on first start.
```

**URL stability caveat:** Hugging Face URLs and exact quant filenames change as new quantization algorithms (e.g. Unsloth Dynamic 2.0) are released. Verify the current file list at `https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF/tree/main` before running `wget`. The `Q4_K_M` quant variant is recommended as a quality/size sweet spot.

**About Qwen 3.5 35B-A3B specifically:**
- It's an **MoE model**: 35 B total parameters, 3 B activated per token. Inference is closer to a 3 B model in throughput while VRAM cost is still ~22 GB at Q4_K_M.
- Default behavior: **thinking mode enabled** (generates `<think>...</think>` blocks). The 0.8 B draft defaults to non-thinking.
- The `mmproj` vision projector file is optional — only needed if you want vision input. Skip it for text-only RAG/coding.
- Default context: 262 K tokens (256 K). At 128 K, KV cache @ Q8 ≈ 5–6 GB.

### Step 5.8 — Smoke-test the V620 stack

Re-enter the LXC:

```bash
pct enter 151
```

Run llama-server with both models for a quick test:

```bash
cd /opt/llama.cpp

./build/bin/llama-server \
    --model /opt/models/qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf \
    --model-draft /opt/models/qwen3.5-0.8b-q4_k_m.gguf \
    --host 0.0.0.0 \
    --port 8080 \
    --ctx-size 32768 \
    --n-gpu-layers all \
    --n-gpu-layers-draft all \
    --tensor-split 1,1 \
    --threads 8 \
    --batch-size 512 \
    --ubatch-size 512 \
    --cache-type-k q8_0 \
    --cache-type-v q8_0 \
    --cont-batching \
    --metrics &

# Wait for "server is listening" log line — usually 30-60 seconds for model load
sleep 60

# Test it
curl -s http://localhost:8080/v1/models | head -20

# Test inference (with spec decode automatically active because both models are loaded)
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5",
    "messages": [{"role":"user","content":"In one sentence: what is the speed of light?"}],
    "max_tokens": 50
  }' | jq '.choices[0].message.content'

# Look at the logs for spec decode acceptance stats
# Expected: lines like "spec_decode acceptance rate: 0.65" — anything above 0.5 is good
```

Stop the test server:

```bash
pkill llama-server
```

### Step 5.9 — Tune tensor split

Run a generation with both V620s monitored:

**Terminal 1:**
```bash
watch -n 1 rocm-smi --showuse --showmemuse --showtemp
```

**Terminal 2:**
```bash
cd /opt/llama.cpp
./build/bin/llama-server \
    --model /opt/models/qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf \
    --tensor-split 1,1 \
    --n-gpu-layers all \
    --port 8080 &
sleep 60

# Generate a long response
curl -X POST http://localhost:8080/v1/chat/completions \
  -d '{"model":"qwen3.5","messages":[{"role":"user","content":"Write 1000 words about the OSI model."}]}' \
  > /dev/null
```

In Terminal 1, observe:
- Both GPUs should pin at 90-100% utilization during generation
- VRAM usage should be roughly equal across both GPUs
- Temperatures should be similar (within 5°C)

If usage is uneven (e.g., GPU 0 at 100%, GPU 1 at 70%), adjust `--tensor-split` — try `4,5` to push more layers to GPU 1, or `5,4` for the reverse. Iterate until usage matches.

Stop the test:
```bash
pkill llama-server
```

### Step 5.10 — Note on Flash Attention for V620

llama.cpp's Flash Attention has known performance issues on V620 (`gfx1030`) — it can be SLOWER than the default attention path. **Test both with and without `--flash-attn`** and use whichever is faster:

```bash
# Benchmark without flash attention
./build/bin/llama-bench \
    --model /opt/models/qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf \
    -ngl all \
    -fa 0

# Benchmark with flash attention
./build/bin/llama-bench \
    --model /opt/models/qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf \
    -ngl all \
    -fa 1
```

Compare the `tg128` row's t/s value. Use whichever is higher in the production systemd unit. (As of llama.cpp main branch in early 2026, FA is often slower on V620 — but this evolves with each release, so verify on your specific build.)

### Step 5.11 — Create production systemd unit

Inside the LXC, decide based on your benchmark (§5.10) whether to set `--flash-attn` to `on`, `off`, or leave at the default `auto` (which lets llama.cpp decide):

```bash
cat > /etc/systemd/system/llama-server.service <<'EOF'
[Unit]
Description=llama.cpp server (V620 ROCm — Qwen 3.5 35B-A3B + 0.8B draft)
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
    --model /opt/models/qwen3.5-35b-a3b-q4_k_m.gguf \
    --model-draft /opt/models/qwen3.5-0.8b-q4_k_m.gguf \
    --alias rag-qwen3.5 \
    --host 0.0.0.0 \
    --port 8080 \
    --ctx-size 131072 \
    --n-gpu-layers all \
    --n-gpu-layers-draft all \
    --tensor-split 1,1 \
    --threads 8 \
    --batch-size 512 \
    --ubatch-size 512 \
    --cache-type-k q8_0 \
    --cache-type-v q8_0 \
    --cont-batching \
    --parallel 2 \
    --spec-draft-n-max 16 \
    --spec-draft-n-min 0 \
    --flash-attn auto \
    --metrics
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now llama-server
systemctl status llama-server --no-pager
```

Key flag rationale (verified against upstream `tools/server/README.md`):

- `--n-gpu-layers all` is the modern idiom; older docs use `999` which still works.
- `--flash-attn auto` (default) lets llama.cpp pick. Override to `off` if §5.10 benchmark shows FA hurts on V620.
- `--alias rag-qwen3.5` — model name reported via `/v1/models`. Names matching `^rag-|-rag$` trigger the router's automatic strip-thinking heuristic (§7.3).
- `--cache-type-k q8_0 --cache-type-v q8_0` — quantize KV cache to 8-bit. Supported types: `f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1`.
- `--metrics` — exposes Prometheus-compatible `/metrics` endpoint (used in monitoring layer).
- **Optional flag worth considering:** `--reasoning-format deepseek` — moves `<think>...</think>` content to a separate `message.reasoning_content` field in the response, simplifying the router's strip logic. Default is `auto` which leaves them in `message.content`.
- **Optional flag for power saving:** `--sleep-idle-seconds 600` unloads the model from RAM/VRAM after 10 minutes of inactivity; next request reloads. Useful if the cluster is shared with a desktop workload.

**Why no `--api-key`:** llama-server does support native API key validation via `--api-key KEY`; we omit it because the router LXC enforces access control upstream and the V620 LXC isn't directly LAN-exposed. Add it if your network model requires per-backend auth.

Watch logs to confirm clean startup:

```bash
journalctl -u llama-server -f
# Wait for: "main: server is listening on http://0.0.0.0:8080"
# Then Ctrl-C
```

### Step 5.12 — Verify from outside the LXC

Find the LXC's IP:

```bash
pct exec 151 -- ip -4 addr show eth0 | grep inet | awk '{print $2}'
# Note this IP — likely 192.168.6.151 or DHCP-assigned
```

From your local machine:

```bash
LLAMACPP_AMD_IP=192.168.6.151   # adjust to your actual IP

curl http://$LLAMACPP_AMD_IP:8080/v1/models
# Expect: JSON listing the model
```

**Stop and verify before proceeding:**
- [ ] `rocminfo` shows two `gfx1030` agents
- [ ] llama-server systemd unit is active
- [ ] V620 stack responds on port 8080
- [ ] Both GPUs split work during a test generation
- [ ] Speculative decoding acceptance rate >0.5 in logs

### Step 5.13 — V620 fan control software bridge (Approach A from Step 1.9.1)

This sets up the systemd services that read V620 temperatures from inside LXC 151 and translate them into motherboard PWM duty cycle, giving you V620-temp-driven fan speed control.

**Architecture:**
- Inside LXC 151: a small writer service polls `rocm-smi` every 5s and writes the max V620 edge temp to a shared file
- On the Proxmox host: a reader service polls that file and writes PWM duty cycle to the motherboard's hwmon endpoint
- Bind mount: a host directory `/var/lib/v620-temps/` is mounted into the LXC at the same path

**Step 5.13.1 — Set up the bind mount.** From the Proxmox host:

```bash
# Create the host directory
mkdir -p /var/lib/v620-temps
chmod 755 /var/lib/v620-temps

# Stop the LXC, add the bind mount, restart
pct stop 151
pct set 151 --mp0 /var/lib/v620-temps,mp=/var/lib/v620-temps
pct start 151
```

**Step 5.13.2 — Install the writer inside LXC 151.**

```bash
pct enter 151

cat > /usr/local/bin/v620-temp-publish.sh <<'EOF'
#!/bin/bash
# Publish max V620 edge temp every 5 seconds to a shared file.
# Read by the Proxmox host's fan control bridge.

mkdir -p /var/lib/v620-temps

while true; do
    # Capture both GPU edge temps; format depends on rocm-smi version
    T1=$(rocm-smi -d 0 --showtemp 2>/dev/null | awk '/Temperature.*edge/ {print int($NF); exit}')
    T2=$(rocm-smi -d 1 --showtemp 2>/dev/null | awk '/Temperature.*edge/ {print int($NF); exit}')

    # Default to 60 (safe-warm) if rocm-smi failed
    [ -z "$T1" ] && T1=60
    [ -z "$T2" ] && T2=60

    # Take the max
    MAX=$(( T1 > T2 ? T1 : T2 ))
    echo "$MAX" > /var/lib/v620-temps/current-temp.tmp
    mv /var/lib/v620-temps/current-temp.tmp /var/lib/v620-temps/current-temp

    sleep 5
done
EOF

chmod +x /usr/local/bin/v620-temp-publish.sh

cat > /etc/systemd/system/v620-temp-publish.service <<'EOF'
[Unit]
Description=V620 GPU temperature publisher
After=network-online.target llama-server.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/v620-temp-publish.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now v620-temp-publish.service
sleep 10
cat /var/lib/v620-temps/current-temp  # should print a number 30-80
exit
```

**Step 5.13.3 — Discover the motherboard PWM hwmon path on the host.**

```bash
# Back on Proxmox host
apt install -y lm-sensors
sensors-detect --auto  # answer yes to all defaults; identifies nuvoton/IT8xxx superio chip

# After detection, find the right pwm endpoint
sensors  # lists all hwmon devices
ls /sys/class/hwmon/

# Typically on ASUS X870E boards you'll see hwmon entries like:
#   nct6798-isa-0290 (the superio fan/sensor chip)
# CHA_FAN3 maps to one of the pwm files. Confirm by changing it temporarily:
echo 1 > /sys/class/hwmon/hwmon3/pwm4_enable   # adjust hwmon3/pwm4
echo 64 > /sys/class/hwmon/hwmon3/pwm4         # 25% — listen for fans slowing
echo 255 > /sys/class/hwmon/hwmon3/pwm4        # 100% — listen for fans speeding up
```

When you've found the pwm file that visibly changes the V620 shroud fans' speed, note its full path. Below we assume `/sys/class/hwmon/hwmon3/pwm4` — substitute yours.

**Step 5.13.4 — Install the host fan control bridge.**

```bash
cat > /usr/local/bin/v620-fan-bridge.sh <<'EOF'
#!/bin/bash
# Read V620 max edge temp from LXC 151 (via shared bind mount)
# and write PWM duty cycle to motherboard fan header.

TEMP_FILE="/var/lib/v620-temps/current-temp"
PWM="/sys/class/hwmon/hwmon3/pwm4"          # ADJUST after sensors-detect
ENABLE="${PWM}_enable"

# Set manual mode
echo 1 > "$ENABLE"

while true; do
    if [ -r "$TEMP_FILE" ]; then
        TEMP=$(cat "$TEMP_FILE" 2>/dev/null)
        TEMP=${TEMP:-65}  # safe default if read fails

        # Map temp → PWM duty (0-255)
        if   [ "$TEMP" -lt 50 ]; then PWM_VAL=64    # ~25% — quiet idle
        elif [ "$TEMP" -lt 60 ]; then PWM_VAL=102   # ~40%
        elif [ "$TEMP" -lt 70 ]; then PWM_VAL=153   # ~60%
        elif [ "$TEMP" -lt 80 ]; then PWM_VAL=204   # ~80%
        else                          PWM_VAL=255   # 100% — full cooling
        fi

        echo "$PWM_VAL" > "$PWM"
    else
        # File missing → safe fail-over to 75%
        echo 192 > "$PWM"
    fi
    sleep 5
done
EOF

chmod +x /usr/local/bin/v620-fan-bridge.sh

cat > /etc/systemd/system/v620-fan-bridge.service <<'EOF'
[Unit]
Description=V620 GPU temperature -> motherboard PWM bridge
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/v620-fan-bridge.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now v620-fan-bridge.service
```

**Step 5.13.5 — Validate end-to-end.**

```bash
# On host
journalctl -u v620-fan-bridge -f &
cat /var/lib/v620-temps/current-temp   # current V620 max temp
cat /sys/class/hwmon/hwmon3/pwm4       # current PWM duty (0-255)

# Stress test: trigger heavy V620 load and watch fans ramp
pct exec 151 -- bash -c 'cd /opt/llama.cpp && ./build/bin/llama-bench -m /opt/models/qwen3.5-35b-a3b-q4_k_m.gguf -ngl 999 -t 4 -n 128 -p 512'
# In another shell: watch fans ramp from ~25% to ~80-100% as V620s heat up
# After test ends, watch fans return to ~25% within a minute
```

**Validation checks:**

- [ ] LXC 151 writes a fresh temp value to `/var/lib/v620-temps/current-temp` every 5s
- [ ] Host bridge service is active and writing to PWM endpoint
- [ ] Fans audibly slow at idle (V620 < 50°C) and speed up under load
- [ ] If you stop `v620-fan-bridge.service`, fans return to BIOS fail-safe (50% flat)
- [ ] If you `rm /var/lib/v620-temps/current-temp` on host, bridge falls back to 75% within 5s

**Caveats:**

- **hwmon path stability across reboots:** The `hwmon3` index can shuffle if devices are added/removed. If this happens, write a udev rule to bind a stable name, OR have the bridge script auto-detect by chip name. For now, hardcoded path is fine; just re-verify after BIOS updates.
- **BIOS may try to reclaim PWM control:** Some ASUS BIOSes resume default fan curves after S3/S4 sleep. Since this is a server, sleep should be disabled (Step 2.2 BIOS config disables S3). If you observe fans returning to BIOS-controlled behavior unexpectedly, restart `v620-fan-bridge.service`.
- **`rocm-smi` field name varies by ROCm version:** ROCm 7.x reports temperature as "Temperature (Sensor edge)" or similar. The `awk` filter looks for `/Temperature.*edge/`; if your ROCm output differs, adjust the regex.

---

## Phase 6 — Provision the 3060 LXC (152 — `llamacpp-nv`)

**Estimated time:** 1 hour

### Step 6.1 — Create the LXC

```bash
# Back on the Proxmox host
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

### Step 6.2 — Configure NVIDIA passthrough (modern `dev0:` syntax)

Same approach as the V620 LXC — use `pct set` with `dev0:` style entries from the host:

```bash
# Find the GID owning the NVIDIA character devices on the host (typically 'video' = 44, but verify)
NVIDIA_GID=$(stat -c '%g' /dev/nvidia0)
echo "NVIDIA_GID=$NVIDIA_GID"

# Apply mount and devices
pct set 152 --mp0 /tank/models,mp=/opt/models,ro=1
pct set 152 --dev0 /dev/nvidia0,gid=$NVIDIA_GID
pct set 152 --dev1 /dev/nvidiactl,gid=$NVIDIA_GID
pct set 152 --dev2 /dev/nvidia-uvm,gid=$NVIDIA_GID
pct set 152 --dev3 /dev/nvidia-uvm-tools,gid=$NVIDIA_GID

# nvidia-caps subdir contents (may vary by kernel — check with `ls /dev/nvidia-caps/`)
if [ -d /dev/nvidia-caps ]; then
    for cap in /dev/nvidia-caps/nvidia-cap*; do
        idx=$((idx + 1))
        # Use a high index to avoid collision with the dev[0-3] above
        case $cap in
            *cap1) pct set 152 --dev4 ${cap},gid=$NVIDIA_GID ;;
            *cap2) pct set 152 --dev5 ${cap},gid=$NVIDIA_GID ;;
        esac
    done
fi

# nvidia-modeset (only if your driver build includes it)
[ -e /dev/nvidia-modeset ] && pct set 152 --dev6 /dev/nvidia-modeset,gid=$NVIDIA_GID
```

Verify:

```bash
pct config 152 | grep -E "^(dev|mp)"
# Expect dev0..dev5 (or dev6) entries pointing at /dev/nvidia* and /dev/nvidia-caps/*
```

**Fallback to legacy syntax if `dev0:` fails:** Edit `/etc/pve/lxc/152.conf` and add:

```
mp0: /tank/models,mp=/opt/models,ro=1
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

Major numbers vary by kernel (195, 240, 506 are typical) — verify with `ls -l /dev/nvidia*` on the host (recorded in §4.7).

### Step 6.3 — Start and enter the LXC

```bash
pct start 152
pct enter 152
```

### Step 6.4 — Install matching NVIDIA userspace

You need the **exact same driver version** inside the LXC as on the host. The simplest path: copy the `.run` file you saved in §4.6 into the LXC.

```bash
# Get the version from the host
DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
echo "Host driver: $DRIVER_VERSION"

# Push the saved installer into the LXC (run from host)
exit  # exit LXC if you're in it
pct push 152 /root/nvidia-installer/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run \
    /root/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run

pct enter 152

# In LXC: install userspace ONLY (no kernel module — host has it)
DRIVER_VERSION=$(cat /proc/driver/nvidia/version 2>/dev/null | grep -oP 'NVRM.*?\K[0-9.]+' | head -1)
chmod +x /root/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run
/root/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run --no-kernel-module --silent --no-questions

# Or interactively (more control over install options):
# /root/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run --no-kernel-module
```

If the `.run` file isn't available on the host (you used Option A in §4.6), download it directly inside the LXC:

```bash
apt update && apt install -y wget build-essential cmake git curl

# Match the host version exactly
DRIVER_VERSION=580.105.06   # CHANGE TO MATCH YOUR HOST'S nvidia-smi output

wget "https://us.download.nvidia.com/XFree86/Linux-x86_64/${DRIVER_VERSION}/NVIDIA-Linux-x86_64-${DRIVER_VERSION}.run" -O /tmp/nvidia.run
chmod +x /tmp/nvidia.run

# Install userspace only — host has the kernel module
/tmp/nvidia.run --no-kernel-module --silent --no-questions
```

Confirm:

```bash
nvidia-smi
# Expect: same output as on the host, listing the RTX 3060
```

If you get `Failed to initialize NVML: Driver/library version mismatch`, the userspace and kernel module versions don't match. Re-check both with:

```bash
# Userspace version (from inside LXC)
cat /proc/driver/nvidia/version
nvidia-smi -q | grep "Driver Version"

# Kernel module version (from host)
modinfo nvidia | grep ^version
```

These must be identical. If not, reinstall the userspace at the matching version.

### Step 6.5 — Install CUDA toolkit

```bash
# Download CUDA repository keyring
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb
dpkg -i /tmp/cuda-keyring.deb
apt update

# Install CUDA toolkit (without driver — we have userspace already)
apt install -y cuda-toolkit-12-6

# Add CUDA to PATH
cat >> ~/.bashrc <<EOF
export PATH=/usr/local/cuda-12.6/bin:\$PATH
export LD_LIBRARY_PATH=/usr/local/cuda-12.6/lib64:\$LD_LIBRARY_PATH
EOF
source ~/.bashrc
```

### Step 6.6 — Verify NVIDIA setup

```bash
nvidia-smi
# Expect: tabular output listing the RTX 3060 with driver version matching the host
```

If you get "Failed to initialize NVML: Driver/library version mismatch" — the LXC's userspace driver version doesn't exactly match the host's kernel module. Reinstall with the precise version.

If you get "no devices were found" — the cgroup allow lines or the device passthrough are wrong. Re-check Step 6.2.

### Step 6.7 — Build llama.cpp with CUDA

```bash
cd /opt
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp

cmake -B build \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES=86 \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLAMA_CURL=ON

# Build (takes ~5 minutes on the 7600's 4 allocated cores)
cmake --build build --config Release -j$(nproc)
```

`CMAKE_CUDA_ARCHITECTURES=86` targets the 3060's Ampere `sm_86`. This avoids building bloat for unused GPU generations.

Verify:

```bash
./build/bin/llama-server --version
./build/bin/llama-server --list-devices 2>&1 | head -10
# Expect: line mentioning CUDA0 (RTX 3060)
```

### Step 6.8 — Download embedder and reranker models

**On the Proxmox host:**

```bash
cd /tank/models

# Embedder (qwen3-embedding 0.6B, ~600 MB)
wget -O qwen3-embedding-0.6b.gguf \
  "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/qwen3-embedding-0.6b-q8_0.gguf"

# Reranker (BGE Reranker v2 m3, ~600 MB)
wget -O bge-reranker-v2-m3-q4_k_m.gguf \
  "https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q4_K_M.gguf"

chmod 644 qwen3-embedding-0.6b.gguf bge-reranker-v2-m3-q4_k_m.gguf
```

### Step 6.9 — Create two llama-server systemd units

Inside the LXC. Note that **per upstream `tools/server/README.md`, the reranking endpoint requires `--embedding --pooling rank` together** — `--reranking` is the alias that opens the endpoint, but the underlying mechanism uses pooling type `rank`. We set both explicitly to be safe.

**Embedder service** (Qwen 3 Embedding 0.6B, produces 1024-dim vectors):

```bash
cat > /etc/systemd/system/llama-embed.service <<'EOF'
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
    --alias qwen3-embedding \
    --host 0.0.0.0 \
    --port 8082 \
    --embeddings \
    --pooling last \
    --ctx-size 32768 \
    --n-gpu-layers all \
    --batch-size 512 \
    --ubatch-size 512 \
    --threads 4 \
    --metrics
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

**Two important Qwen3 Embedding quirks** (from the official model card):

1. **Pooling type must be `last`, not `cls`** — Qwen3 Embedding uses the final `<|endoftext|>` token as the sentence representation. Setting `--pooling cls` will produce incorrect embeddings that don't match published benchmarks.

2. **`<|endoftext|>` token must be appended manually** — Qwen3 Embedding expects the input to end with the special `<|endoftext|>` token. The router (or any client) must append it. In FastAPI router code:

   ```python
   # When forwarding embedding requests to the 3060 LXC
   if "input" in body:
       if isinstance(body["input"], str):
           body["input"] = body["input"].rstrip() + "<|endoftext|>"
       elif isinstance(body["input"], list):
           body["input"] = [s.rstrip() + "<|endoftext|>" for s in body["input"]]
   ```

3. **Client-side normalization required** — `llama-server` does not currently support the `--embd-normalize` flag; embeddings come back unnormalized. Either normalize client-side (L2 norm) or accept that cosine-similarity comparisons need explicit norm division. AnythingLLM normalizes by default, so this is mostly transparent for our pipeline, but is important to know if you build custom RAG code against this endpoint.

**Reranker service** (bge-reranker-v2-m3):

```bash
cat > /etc/systemd/system/llama-rerank.service <<'EOF'
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
    --alias bge-reranker-v2-m3 \
    --host 0.0.0.0 \
    --port 8083 \
    --embeddings \
    --pooling rank \
    --reranking \
    --ctx-size 8192 \
    --n-gpu-layers all \
    --threads 4 \
    --metrics
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Notes on the flags:

- `--embeddings` (plural) is the canonical form per upstream docs; `--embedding` is also accepted as alias.
- `--pooling cls` for the embedder (Qwen 3 Embedding uses `[CLS]` token pooling; some embedders use `mean` or `last` — check the model card).
- `--pooling rank` is required for reranking even with `--reranking` set, per the server README.
- `--alias` sets the model name reported by `/v1/models`.
- `--metrics` enables the Prometheus `/metrics` endpoint on each service.

Enable both:

```bash
systemctl daemon-reload
systemctl enable --now llama-embed llama-rerank
systemctl status llama-embed llama-rerank --no-pager
```

### Step 6.10 — Smoke test embedder and reranker

```bash
# Embedding produces 1024-dim vectors
curl -s http://localhost:8082/v1/embeddings \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen3-embedding","input":"dimension probe"}' | \
  jq '.data[0].embedding | length'
# Expect: 1024

# Reranker
curl -s http://localhost:8083/v1/rerank \
    -H "Content-Type: application/json" \
    -d '{
        "model": "bge-reranker-v2-m3",
        "query": "what is the capital of France?",
        "documents": ["Paris is the capital", "Berlin is in Germany", "London is in England"]
    }' | jq
# Expect: scored results with Paris ranked highest
```

```bash
# Verify VRAM usage on the 3060
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv
# Expect: ~3000-4500 MiB used out of 12288 (lots of headroom)
```

### Step 6.11 — Note the LXC IP

```bash
exit  # Exit back to host
pct exec 152 -- ip -4 addr show eth0 | grep inet | awk '{print $2}'
# Note this IP — will be needed for router configuration
```

**Stop and verify before proceeding:**
- [ ] `nvidia-smi` works inside the LXC
- [ ] llama-embed and llama-rerank both active
- [ ] Embedding endpoint returns 1024-dim vectors
- [ ] Reranker endpoint returns scored results
- [ ] VRAM utilization is reasonable (~30% of 12 GB)

---

## Phase 7 — Deploy the Router LXC (153 — `llm-router`)

**Estimated time:** 30 minutes

### Step 7.1 — Create the LXC

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

### Step 7.2 — Set up Python environment

```bash
pct enter 153

apt update
apt install -y python3 python3-venv python3-pip

# Create dedicated user
useradd -r -m -d /opt/llm-router -s /usr/sbin/nologin router
mkdir -p /opt/llm-router
chown router:router /opt/llm-router

# Create venv and install dependencies
sudo -u router bash -c '
  python3 -m venv /opt/llm-router/venv
  /opt/llm-router/venv/bin/pip install --upgrade pip
  /opt/llm-router/venv/bin/pip install fastapi "uvicorn[standard]" httpx
'
```

### Step 7.3 — Deploy the router application

Create `/opt/llm-router/app.py` with the corrected logic (note: removed cross-LXC speculative decoding, since spec decode is now fully local on the V620 LXC):

```bash
cat > /opt/llm-router/app.py <<'PYEOF'
"""
LLM cluster router.
- /v1/chat/completions       -> V620 stack (port 8080) with speculative decoding native
- /v1/embeddings             -> 3060 embedder (port 8082)
- /v1/rerank                 -> 3060 reranker (port 8083)
- SSE keepalive on streaming responses
- Per-request <think> block decision (header > body > system prompt > model name > default)
"""

import asyncio
import os
import re
import time

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import StreamingResponse, JSONResponse

V620_URL    = os.environ.get("V620_URL",   "http://192.168.6.151:8080")
EMBED_URL   = os.environ.get("EMBED_URL",  "http://192.168.6.152:8082")
RERANK_URL  = os.environ.get("RERANK_URL", "http://192.168.6.152:8083")
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "12"))

THINK_RE     = re.compile(r"<think>.*?</think>", re.DOTALL)
NOTHINK_HINT = re.compile(r"/no_think|hide thinking|strip reasoning", re.IGNORECASE)
RAG_MODEL_RE = re.compile(r"(^rag-|-rag$)", re.IGNORECASE)

app = FastAPI(title="LLM Cluster Router")


def should_strip_thinking(body: dict, header_value: str | None) -> bool:
    """Decide whether to strip <think>...</think> blocks from response."""
    if header_value is not None:
        return header_value.lower() in ("true", "1", "yes")
    if "strip_thinking" in body:
        return bool(body["strip_thinking"])
    msgs = body.get("messages", [])
    if msgs and msgs[0].get("role") == "system":
        if NOTHINK_HINT.search(msgs[0].get("content", "")):
            return True
    if RAG_MODEL_RE.search(body.get("model", "")):
        return True
    return False


async def sse_stream_with_keepalive(upstream_url: str, payload: dict, strip_thinking: bool):
    """Proxy upstream SSE stream with keepalive + optional think-block stripping."""
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", upstream_url, json=payload) as r:
            queue: asyncio.Queue = asyncio.Queue()

            async def reader():
                async for chunk in r.aiter_text():
                    await queue.put(chunk)
                await queue.put(None)

            task = asyncio.create_task(reader())
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
                        continue
                    if chunk is None:
                        break
                    out = THINK_RE.sub("", chunk) if strip_thinking else chunk
                    yield out.encode()
            finally:
                task.cancel()


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

    async with httpx.AsyncClient(timeout=None) as c:
        r = await c.post(url, json=body)
        data = r.json()
        if strip:
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if "content" in msg:
                    msg["content"] = THINK_RE.sub("", msg["content"])
        return JSONResponse(data, status_code=r.status_code)


# Qwen3 Embedding requires <|endoftext|> appended to each input.
# llama-server does NOT auto-append this token.
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
    inp = body.get("input")
    if isinstance(inp, str):
        body["input"] = _ensure_eot(inp)
    elif isinstance(inp, list):
        body["input"] = [_ensure_eot(x) if isinstance(x, str) else x for x in inp]

    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{EMBED_URL}/v1/embeddings", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/rerank")
async def rerank(request: Request):
    body = await request.json()
    # bge-reranker-v2-m3 uses XLMRoBERTa tokenizer — no EOT injection needed.
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(f"{RERANK_URL}/v1/rerank", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=10.0) as c:
        v620, embed = [], []
        try:
            v620 = (await c.get(f"{V620_URL}/v1/models")).json().get("data", [])
        except Exception:
            pass
        try:
            embed = (await c.get(f"{EMBED_URL}/v1/models")).json().get("data", [])
        except Exception:
            pass
    return {"object": "list", "data": v620 + embed}


@app.get("/healthz")
async def healthz():
    """Health check including upstream availability."""
    async with httpx.AsyncClient(timeout=3.0) as c:
        upstream_status = {}
        for name, url in [("v620", V620_URL), ("embed", EMBED_URL), ("rerank", RERANK_URL)]:
            try:
                r = await c.get(f"{url}/v1/models")
                upstream_status[name] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
            except Exception as e:
                upstream_status[name] = f"unreachable: {type(e).__name__}"
    return {"ok": True, "ts": time.time(), "upstream": upstream_status}
PYEOF

chown router:router /opt/llm-router/app.py
```

### Step 7.4 — systemd unit

```bash
cat > /etc/systemd/system/llm-router.service <<EOF
[Unit]
Description=LLM cluster router
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
ExecStart=/opt/llm-router/venv/bin/uvicorn app:app \\
    --host 0.0.0.0 --port 8000 \\
    --timeout-keep-alive 300 \\
    --workers 1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now llm-router
systemctl status llm-router --no-pager
```

### Step 7.5 — Smoke test

```bash
# Health check including upstream verification
curl -s http://localhost:8000/healthz | jq
# Expect: {"ok": true, "ts": ..., "upstream": {"v620": "ok", "embed": "ok", "rerank": "ok"}}

# Aggregated model list
curl -s http://localhost:8000/v1/models | jq

# Test strip-thinking preserved by default
curl -sN http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5","stream":true,"messages":[{"role":"user","content":"think briefly then say hi"}]}' | head -20

# Test strip-thinking via header
curl -sN http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Strip-Thinking: true" \
  -d '{"model":"qwen3.5","stream":true,"messages":[{"role":"user","content":"think briefly then say hi"}]}' | head -20
# Differences: <think>...</think> blocks should be absent in the second test
```

```bash
# Note router IP for later
exit
pct exec 153 -- ip -4 addr show eth0 | grep inet | awk '{print $2}'
```

**Stop and verify before proceeding:**
- [ ] `/healthz` returns ok with all three upstreams reachable
- [ ] Strip-thinking via header works
- [ ] Streaming responses include `: ping` keepalive frames during slow generation

---

## Phase 8 — Deploy AnythingLLM LXC (154 — `anythingllm`)

**Estimated time:** 45 minutes

### Step 8.1 — Create the LXC

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
  --start 0
```

### Step 8.2 — Bind-mount AnythingLLM data

Edit `/etc/pve/lxc/154.conf`:

```bash
nano /etc/pve/lxc/154.conf
```

Append:

```
mp0: /tank/anythingllm,mp=/opt/anythingllm-data
```

Start the LXC:

```bash
pct start 154
pct enter 154
```

### Step 8.3 — Install Docker

```bash
apt update
apt install -y ca-certificates curl gnupg lsb-release

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null

apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

# Smoke test
docker run --rm hello-world
```

### Step 8.4 — Deploy AnythingLLM

```bash
mkdir -p /opt/anythingllm
mkdir -p /opt/anythingllm-data/storage

cat > /opt/anythingllm/docker-compose.yml <<'EOF'
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
EOF

# Initialize empty .env
touch /opt/anythingllm/.env
chmod 600 /opt/anythingllm/.env

# Start
cd /opt/anythingllm
docker compose up -d

# Watch logs
docker logs anythingllm -f
# Wait for "Server listening on 0.0.0.0:3001" then Ctrl-C
```

### Step 8.5 — Initial AnythingLLM setup

Find the LXC IP:

```bash
exit  # back to host
pct exec 154 -- ip -4 addr show eth0 | grep inet | awk '{print $2}'
# Note this IP — likely 192.168.6.154
```

Browse to `http://192.168.6.154:3001/`:

1. Create the admin user account
2. Skip the welcome wizard for now (we'll configure providers next)

### Step 8.6 — Configure LLM and embedder providers

In AnythingLLM UI: **Settings → AI Providers → LLM Preference**

- Provider: **Generic OpenAI**
- Base URL: `http://192.168.6.153:8000/v1` (the router)
- API Key: `sk-anything` (any non-empty string — llama.cpp doesn't validate)
- Chat Model Name: query the V620's actual model name from `curl http://192.168.6.151:8080/v1/models | jq -r .data[0].id`
- Token context window: `131072`
- Max Tokens: `8192`

Click **Save Changes**.

**Settings → AI Providers → Embedder Preference**:

- Provider: **Generic OpenAI**
- Base URL: `http://192.168.6.153:8000/v1` (router routes embeddings to 3060)
- API Key: `sk-anything`
- Embedding Model Name: query from `curl http://192.168.6.152:8082/v1/models | jq -r .data[0].id`
- Embedding dimension: **1024**
- Max embed chunk length: `2500`

Click **Save Changes**.

**Settings → AI Providers → Reranker** (if option exists):

- Configure to use the router's `/v1/rerank` endpoint

### Step 8.7 — Configure text splitting (chunk size fix from v1)

**Settings → Text Splitter & Chunking**:

- Text chunk size: **2500**
- Text chunk overlap: **500**

This is the v1 §8 chunk-size fix carrying over.

### Step 8.8 — Generate API keys

**Settings → API Keys**:

1. Click "Generate New API Key"
2. Save the key — you'll need it for workspace management and MCP servers

**Stop and verify before proceeding:**
- [ ] AnythingLLM web UI accessible
- [ ] LLM provider configured and tests successfully (use the in-app "Test Connection" if available)
- [ ] Embedder provider configured
- [ ] API key generated and saved

---

## Phase 9 — Deploy MCP Stack LXC (155 — `mcp-stack`)

**Estimated time:** 1 hour (mostly migration of source from old host)

### Step 9.1 — Create the LXC

```bash
pct create 155 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname mcp-stack \
  --cores 2 \
  --memory 4096 \
  --rootfs local-zfs:16 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 1
```

### Step 9.2 — Install Docker

```bash
pct enter 155
# Same Docker install as Step 8.3
```

(Repeat the Docker install from Step 8.3 — same commands.)

### Step 9.3 — Migrate MCP source trees from v1 host

From the **Proxmox host** (not the LXC):

```bash
# Verify SSH access to old host
ssh root@old-t7910-host hostname

# Get the MCP LXC's IP
MCP_IP=$(pct exec 155 -- ip -4 addr show eth0 | grep inet | awk '{print $2}' | cut -d/ -f1)
echo "MCP LXC IP: $MCP_IP"

# Stream the three MCP source trees from old host to new LXC
ssh root@old-t7910-host 'tar czf - /opt/anythingllm-mcp /opt/broadcom-techdocs-mcp /opt/sdg-mcp' | \
  pct exec 155 -- tar xzf - -C /
```

### Step 9.4 — Update MCP configurations

Inside LXC 155, edit each MCP's `.env` and/or `docker-compose.yml`:

```bash
pct enter 155

# For each MCP
for mcp in anythingllm-mcp broadcom-techdocs-mcp sdg-mcp; do
    cd /opt/$mcp
    
    # Update the environment to point at the new AnythingLLM
    if [ -f docker-compose.yml ]; then
        sed -i 's|http://[^:]*:3001|http://192.168.6.154:3001|g' docker-compose.yml
    fi
    if [ -f .env ]; then
        sed -i 's|ANYTHINGLLM_BASE_URL=.*|ANYTHINGLLM_BASE_URL=http://192.168.6.154:3001|' .env
        # Update the API key — use the one from Step 8.8
        # sed -i 's|ANYTHINGLLM_API_KEY=.*|ANYTHINGLLM_API_KEY=YOUR_NEW_KEY|' .env
    fi
done
```

Manually edit `.env` files with the new AnythingLLM API key from Step 8.8:

```bash
nano /opt/anythingllm-mcp/.env
nano /opt/broadcom-techdocs-mcp/.env  # if applicable
nano /opt/sdg-mcp/.env
```

### Step 9.5 — Build and start MCP containers

```bash
for mcp in anythingllm-mcp broadcom-techdocs-mcp sdg-mcp; do
    echo "=== Building $mcp ==="
    cd /opt/$mcp
    docker compose down 2>/dev/null
    docker compose up -d --build
done
```

### Step 9.6 — Verify MCPs are listening

```bash
# Each MCP listens on a different port (per v1 §6.1)
for port in 3002 3003 3004; do
    printf "Port $port: "
    timeout 3 curl -sN -H "Accept: text/event-stream" \
      "http://localhost:$port/sse" 2>&1 | head -1
done
# Expect each: "event: endpoint" line
```

### Step 9.7 — Update OpenCode client config

On your OpenCode client machine, update `config.json` to point at the new IPs:

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

Restart OpenCode and verify each MCP connects without "SSE error: Unable to connect" messages.

**Stop and verify before proceeding:**
- [ ] All three MCP containers running (`docker ps`)
- [ ] All three SSE endpoints respond
- [ ] OpenCode connects to all three without errors

---

## Phase 10 — Data Migration from v1

**Estimated time:** 2-4 hours (depending on corpus size)

### Step 10.1 — Migrate VCF documentation source

The original markdown source files at `/opt/vcf-ingest/out/` and Keycloak files come over to the AnythingLLM LXC:

```bash
# From Proxmox host
ssh root@old-t7910-host 'tar czf - /opt/vcf-ingest/out /opt/keycloak-ingest/output' | \
  pct exec 154 -- tar xzf - -C /tmp/
```

### Step 10.2 — Re-create workspaces in AnythingLLM

In the AnythingLLM UI:

1. Click **+ New Workspace**
2. Name: `vcf-reference`
3. Save

Repeat for `sdg-documentation`.

### Step 10.3 — Tune workspace settings via API

```bash
# Inside the AnythingLLM LXC
pct enter 154

# Set environment
export ALLM_URL="http://localhost:3001"
export ALLM_KEY="<your API key from Step 8.8>"

# Configure vcf-reference workspace
curl -X POST "$ALLM_URL/api/v1/workspace/vcf-reference/update" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "similarityThreshold": 0.0,
    "topN": 10,
    "chatMode": "query",
    "vectorSearchMode": "rerank",
    "openAiTemp": 0.3,
    "queryRefusalResponse": "Not in the provided VCF documents.",
    "openAiPrompt": "You are a technical reference assistant for VMware Cloud Foundation (VCF). Answer questions using ONLY the content retrieved from the attached VCF documentation. If the answer is not in the retrieved context, say so — do not fall back on general VMware knowledge. Cite which document each claim comes from when possible."
  }'

# Same for sdg-documentation
curl -X POST "$ALLM_URL/api/v1/workspace/sdg-documentation/update" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "similarityThreshold": 0.0,
    "topN": 12,
    "chatMode": "query",
    "vectorSearchMode": "rerank",
    "openAiTemp": 0.3,
    "queryRefusalResponse": "Not in the provided SDG documents.",
    "openAiPrompt": "You are a technical reference assistant for SDG infrastructure (Keycloak 26.3.3 and related self-hosted tools). Answer questions using ONLY the content retrieved from the attached documentation. Each document has a source: <tool-name> field — name the originating tool when citing."
  }'
```

### Step 10.4 — Bulk upload markdown to AnythingLLM

The `/api/v1/document/upload` endpoint accepts an `addToWorkspaces` parameter, but a known AnythingLLM bug (Mintplex-Labs/anything-llm#5271) intermittently uploads to `custom-documents/` without actually attaching to the workspace. To work around this we use a two-step process: upload first, then attach via `update-embeddings` with the explicit `adds` array.

```bash
# Inside LXC 154
WS=vcf-reference

# Track uploaded filenames for the second step
UPLOADED_FILES=()

count=0
for f in /tmp/opt/vcf-ingest/out/*.md; do
    count=$((count+1))
    base=$(basename "$f")
    printf "[$count] Uploading $base... "
    response=$(curl -s -X POST "$ALLM_URL/api/v1/document/upload" \
        -H "Authorization: Bearer $ALLM_KEY" \
        -F "file=@$f" \
        -F "addToWorkspaces=$WS")
    
    success=$(echo "$response" | jq -r '.success' 2>/dev/null)
    
    # Extract the document location (e.g., "custom-documents/foo-uuid.json")
    doc_location=$(echo "$response" | jq -r '.documents[0].location' 2>/dev/null)
    if [ "$success" = "true" ] && [ -n "$doc_location" ] && [ "$doc_location" != "null" ]; then
        UPLOADED_FILES+=("$doc_location")
        echo "OK ($doc_location)"
    else
        echo "FAILED: $response"
    fi
done
echo "Total: $count files uploaded to $WS"

# Step 2: Explicitly attach all uploaded documents to the workspace
# (works around AnythingLLM bug where addToWorkspaces silently fails)
echo "=== Attaching $count documents to workspace $WS ==="
adds_json=$(printf '%s\n' "${UPLOADED_FILES[@]}" | jq -R . | jq -s .)
curl -s -X POST "$ALLM_URL/api/v1/workspace/$WS/update-embeddings" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"adds\": $adds_json}" | jq

# Repeat for SDG
WS=sdg-documentation
UPLOADED_FILES=()
count=0
shopt -s globstar  # enable ** pattern
for f in /tmp/opt/keycloak-ingest/output/keycloak-26.3.3/**/*.md; do
    [ -f "$f" ] || continue
    count=$((count+1))
    response=$(curl -s -X POST "$ALLM_URL/api/v1/document/upload" \
        -H "Authorization: Bearer $ALLM_KEY" \
        -F "file=@$f" \
        -F "addToWorkspaces=$WS")
    doc_location=$(echo "$response" | jq -r '.documents[0].location' 2>/dev/null)
    [ -n "$doc_location" ] && [ "$doc_location" != "null" ] && UPLOADED_FILES+=("$doc_location")
done
echo "Total: $count SDG files uploaded"

adds_json=$(printf '%s\n' "${UPLOADED_FILES[@]}" | jq -R . | jq -s .)
curl -s -X POST "$ALLM_URL/api/v1/workspace/$WS/update-embeddings" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"adds\": $adds_json}" | jq
```

**Verify documents are actually attached** to each workspace before proceeding:

```bash
for ws in vcf-reference sdg-documentation; do
    count=$(curl -s -H "Authorization: Bearer $ALLM_KEY" \
        "$ALLM_URL/api/v1/workspace/$ws" | \
        jq '.workspace[0].documents | length')
    echo "$ws: $count documents attached"
done
# Expect non-zero counts matching what you uploaded
```

If the count is 0 or much smaller than expected, the workspace-attach step failed — re-run the second `update-embeddings` call manually with the `adds` array.

### Step 10.5 — Verify embedding completion

The `/v1/workspace/$ws/update-embeddings` calls in §10.4 trigger immediate embedding via the router → 3060 LXC. Embedding runs synchronously per request, so the curl returns once embeddings are persisted. For a 5,000-document corpus, expect 30–60 minutes total wall-clock time on a 3060.

While embedding runs, monitor the 3060 in another shell:

```bash
# In another shell on the Proxmox host
pct exec 152 -- watch -n 1 nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv
# Expect: utilization spikes during embedding, modest VRAM growth
```

Verify embedding completion:

```bash
for ws in vcf-reference sdg-documentation; do
    count=$(curl -s -H "Authorization: Bearer $ALLM_KEY" \
        "$ALLM_URL/api/v1/workspace/$ws" | \
        jq '[.workspace[0].documents[] | select(.pinned == false)] | length')
    echo "$ws: $count embedded documents"
done
```

If embedding errors out partway through (typically due to a malformed source doc), AnythingLLM logs will show which document failed. Retry just the failed ones with another targeted `update-embeddings` call.

### Step 10.6 — Migrate VCF auto-updater

The auto-updater goes alongside AnythingLLM:

```bash
# From Proxmox host
ssh root@old-t7910-host 'tar czf - /opt/vcf-doc-updater' | \
  pct exec 154 -- tar xzf - -C /

# Inside LXC 154
pct enter 154
cd /opt/vcf-doc-updater

# Update environment for new AnythingLLM URL
sed -i 's|ANYTHINGLLM_URL=.*|ANYTHINGLLM_URL=http://192.168.6.154:3001|' .env
# Update API key
nano .env  # set ANYTHINGLLM_API_KEY=<new key>

# Update bind-mount path in docker-compose.yml
# Original: /opt/anythingllm/storage/documents/custom-documents:/allm-storage:ro
# New:      /opt/anythingllm-data/storage/documents/custom-documents:/allm-storage:ro
sed -i 's|/opt/anythingllm/storage|/opt/anythingllm-data/storage|g' docker-compose.yml

# IMPORTANT: keep DRY_RUN=true for first runs
docker compose up -d
docker logs vcf-doc-updater -f
```

### Step 10.7 — Smoke test the full pipeline

```bash
# On any host
ALLM_KEY=<your key>

# Refusal path — should not have an answer in corpus
curl -s -X POST "http://192.168.6.154:3001/api/v1/workspace/vcf-reference/chat" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -H "Content-Type: application/json" \
    -d '{"message":"What is the capital of France?","mode":"query"}' \
    | jq -r '.textResponse'
# Expect: "Not in the provided VCF documents."

# Positive path — should return relevant content
curl -s -X POST "http://192.168.6.154:3001/api/v1/workspace/vcf-reference/chat" \
    -H "Authorization: Bearer $ALLM_KEY" \
    -H "Content-Type: application/json" \
    -d '{"message":"What prefix lengths are valid for edge uplink subnets?","mode":"query"}' \
    | jq -r '.textResponse'
# Expect: response referencing /29 or /30 with VCF-NET-014 or VCF-IP-015 citation
```

**Stop and verify before proceeding:**
- [ ] All VCF markdown files uploaded
- [ ] Embedding completed without errors
- [ ] Refusal path returns the configured refusal string
- [ ] Positive path returns relevant content with citations
- [ ] VCF auto-updater shows healthy log output

---

## Phase 11 — Final Verification

**Estimated time:** 30 minutes

Run the full smoke test suite from `local-gpu-cluster-v2.md` Appendix C. Each test should pass.

### Step 11.1 — Save the IP plan

```bash
# Save your final IP plan to /etc/hosts on each LXC for clean references
cat >> /etc/hosts <<EOF
192.168.6.151  llamacpp-amd
192.168.6.152  llamacpp-nv
192.168.6.153  llm-router
192.168.6.154  anythingllm
192.168.6.155  mcp-stack
EOF
```

Repeat on each LXC.

### Step 11.2 — Configure DHCP reservations

In your router's admin panel, create static DHCP reservations for each LXC's MAC address so they always get the same IP. Get the MAC addresses:

```bash
# From Proxmox host
for vmid in 151 152 153 154 155; do
    mac=$(pct config $vmid | grep ^net0 | grep -oP 'hwaddr=\K[0-9A-F:]+' || \
          pct exec $vmid -- ip link show eth0 | grep ether | awk '{print $2}')
    echo "VMID $vmid: $mac"
done
```

### Step 11.3 — Document the deployment

Create a deployment record:

```bash
# On the Proxmox host
mkdir -p /root/deployment-record
cat > /root/deployment-record/deployment.md <<EOF
# GPU Cluster Deployment Record

**Deployed:** $(date)
**Proxmox version:** $(pveversion | head -1)
**Kernel:** $(uname -r)
**Driver versions:**
- NVIDIA: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader)
- ROCm: $(pct exec 151 -- rocminfo 2>/dev/null | grep "Runtime Version" | head -1)

**LXCs:**
- 151 llamacpp-amd: $(pct exec 151 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')
- 152 llamacpp-nv: $(pct exec 152 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')
- 153 llm-router: $(pct exec 153 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')
- 154 anythingllm: $(pct exec 154 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')
- 155 mcp-stack: $(pct exec 155 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')

**Models loaded on V620 stack:**
$(pct exec 151 -- curl -s http://localhost:8080/v1/models | jq -r '.data[].id' 2>/dev/null)

**Models loaded on 3060 stack:**
$(pct exec 152 -- curl -s http://localhost:8082/v1/models | jq -r '.data[].id' 2>/dev/null)

EOF
cat /root/deployment-record/deployment.md
```

---

## Backup Procedures

The full system has three layers of state to back up:

1. **LXC root filesystems** (configurations, installed software) — backup via `vzdump`
2. **Persistent data** (`/tank/anythingllm`, `/tank/mcp`) — backup via `zfs send`
3. **Model files** (`/tank/models`) — large but easily redownloadable, backup optional

### Strategy 1 — Built-in vzdump (simplest, recommended)

#### Daily LXC backups via Proxmox UI

1. **Datacenter → Backup → Add**:
   - Storage: `tank-backups`
   - Day of week: Daily
   - Start time: 03:00
   - Selection mode: Include selected VMs
   - Containers: 151, 152, 153, 154, 155
   - Mode: Snapshot
   - Compression: zstd
   - Retention: keep-daily=7, keep-weekly=4, keep-monthly=3

This keeps a week of dailies, four weekly snapshots, and three monthly archives. Total disk usage typically ~30-50 GB.

#### CLI backup (manual or scripted)

```bash
# Backup all LXCs at once
vzdump 151 152 153 154 155 \
    --storage tank-backups \
    --mode snapshot \
    --compress zstd \
    --notes-template "Manual backup $(date +%Y-%m-%d)"

# Backup with retention pruning
vzdump 154 \
    --storage tank-backups \
    --mode snapshot \
    --compress zstd \
    --prune-backups keep-last=3,keep-daily=7,keep-monthly=3
```

#### Verify backups

```bash
# List backups
pvesm list tank-backups

# Verify integrity of a specific backup
pvesm path tank-backups:backup/vzdump-lxc-154-2026_05_10-03_00_01.tar.zst | xargs zstdcat | tar tf - | head
```

### Strategy 2 — ZFS send/receive for persistent data

Daily ZFS snapshots of the data datasets, keeping local snapshots for fast rollback and optionally replicating to a remote host:

```bash
cat > /usr/local/bin/zfs-snapshot-rotation.sh <<'EOF'
#!/bin/bash
# /usr/local/bin/zfs-snapshot-rotation.sh
# Daily ZFS snapshots with rotation
set -e
DATE=$(date +%Y-%m-%d)
DATASETS="tank/anythingllm tank/mcp"

for ds in $DATASETS; do
    # Create today's snapshot
    zfs snapshot "${ds}@daily-${DATE}" || true
done

# Rotate — keep 14 daily snapshots
zfs list -H -t snapshot -o name | grep "@daily-" | sort | head -n -14 | while read snap; do
    zfs destroy "$snap"
    echo "Destroyed old snapshot: $snap"
done
EOF
chmod +x /usr/local/bin/zfs-snapshot-rotation.sh

# Add to cron
cat >> /etc/cron.d/zfs-snapshots <<EOF
0 4 * * * root /usr/local/bin/zfs-snapshot-rotation.sh >> /var/log/zfs-snapshots.log 2>&1
EOF
```

### Strategy 3 — Off-host backup via Proxmox Backup Server

If you have a second machine, install Proxmox Backup Server (PBS) on it. Then add it as storage in PVE:

```bash
# In PVE web UI: Datacenter → Storage → Add → Proxmox Backup Server
# Configure with PBS server details
```

PBS gives you deduplication, encryption, and incremental backups — far better than vzdump for long-term retention. With PBS storing dedupe'd chunks, a year of daily backups for these LXCs typically uses ~80 GB total.

### Strategy 4 — External backup of `/tank/models` (optional)

Models are large (~25 GB total) but trivially redownloadable from Hugging Face. Decision criteria:
- If your internet is fast (>500 Mbps) and HF stays up: don't bother backing up
- If you fine-tuned or modified any models: definitely back up
- If your ISP has download limits: back up

```bash
# Backup to external USB drive
rsync -aP /tank/models/ /mnt/external-backup/models/
```

### Recommended baseline strategy

1. **Daily vzdump** of all 5 LXCs to `tank-backups` (retention: 7d/4w/3m)
2. **Daily ZFS snapshots** of `tank/anythingllm` and `tank/mcp` (retention: 14 days)
3. **Weekly off-host copy** of vzdump archives to PBS or external storage
4. **Skip** `/tank/models` backups (just record which models were loaded)

### Test your backups

**Quarterly restore test** — actually verify a backup works:

```bash
# Pick a test backup
TEST_BACKUP=tank-backups:backup/vzdump-lxc-153-2026_05_10-03_00_01.tar.zst

# Restore as a new VMID for testing (don't overwrite the running container)
pct restore 999 $TEST_BACKUP --storage local-zfs --rootfs local-zfs:8

# Start it and verify
pct start 999
pct enter 999
# Confirm it's a working copy of llm-router
systemctl status llm-router
exit

# Cleanup
pct stop 999
pct destroy 999
```

If the test fails, you have a backup configuration problem that you need to fix BEFORE you actually need the backups.

---

## Monitoring Procedures

### Layer 1 — Built-in Proxmox monitoring

The PVE web UI shows per-LXC CPU, memory, network, and disk usage at minute-resolution. Click a container in the tree → **Summary**. Good for quick health checks but no alerting.

### Layer 2 — systemd OnFailure email alerts (zero-cost minimum)

Every llama-server, router, and key Docker service should email you on failure:

```bash
# On Proxmox host — set up a mail relay first
apt install -y postfix mailutils
# Configure as "Internet Site" with your domain
# Or use a relay smarthost (Gmail, SendGrid, etc.)

# Per LXC, edit each critical service to add OnFailure
# Inside LXC 151, 152, 153
mkdir -p /etc/systemd/system/<service>.service.d/
cat > /etc/systemd/system/llama-server.service.d/notify.conf <<EOF
[Unit]
OnFailure=service-failure-notify@%n.service
EOF

# Create the notification template service (once per LXC)
cat > /etc/systemd/system/service-failure-notify@.service <<EOF
[Unit]
Description=Send email on service failure for %i

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'echo "Service %i failed on $(hostname) at $(date)" | mail -s "FAILURE: %i on $(hostname)" admin@example.com'
EOF

systemctl daemon-reload
```

### Layer 3 — Health-check cron on each LXC (recommended baseline)

Periodic checks that ping each upstream service and alert if down:

```bash
# Save on the Proxmox host as /usr/local/bin/cluster-health-check.sh
cat > /usr/local/bin/cluster-health-check.sh <<'EOF'
#!/bin/bash
# Cluster-wide health check
# Run from cron on the Proxmox host

set -u
ALERT_EMAIL=admin@example.com
ALERT_FILE=/var/run/cluster-health.alerts
SUBJECT="GPU Cluster Health Alert"
ALERTS=()

check() {
    local name=$1
    local url=$2
    local expected_pattern=$3
    
    if ! response=$(curl -sf --max-time 10 "$url" 2>&1); then
        ALERTS+=("DOWN: $name ($url)")
        return 1
    fi
    
    if ! echo "$response" | grep -q "$expected_pattern"; then
        ALERTS+=("BAD RESPONSE: $name ($url) — expected '$expected_pattern'")
        return 1
    fi
    
    return 0
}

# V620 stack
check "V620 llama-server" "http://192.168.6.151:8080/v1/models" "data"

# 3060 services
check "3060 embedder" "http://192.168.6.152:8082/v1/models" "data"
check "3060 reranker" "http://192.168.6.152:8083/v1/models" "data"

# Router
check "Router health" "http://192.168.6.153:8000/healthz" '"ok":true'

# AnythingLLM
check "AnythingLLM" "http://192.168.6.154:3001/api/ping" "online"

# MCPs
for port in 3002 3003 3004; do
    check "MCP port $port" "http://192.168.6.155:$port/sse" "endpoint" || true
done

# GPU temperature checks
TEMP_AMD=$(pct exec 151 -- rocm-smi --showtemp 2>/dev/null | grep -oP "(?<=Temperature \(Sensor edge\) \(C\):\s)[\d.]+" | head -1)
TEMP_NV=$(pct exec 152 -- nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits)

if [ -n "$TEMP_AMD" ] && [ "${TEMP_AMD%.*}" -gt 85 ]; then
    ALERTS+=("V620 #1 temperature $TEMP_AMD°C — investigate cooling")
fi
if [ -n "$TEMP_NV" ] && [ "$TEMP_NV" -gt 85 ]; then
    ALERTS+=("3060 temperature ${TEMP_NV}°C — investigate cooling")
fi

# Send alerts only on transitions (don't spam every 5 min)
if [ ${#ALERTS[@]} -gt 0 ]; then
    NEW_HASH=$(printf '%s\n' "${ALERTS[@]}" | sha256sum | head -c 16)
    OLD_HASH=$(cat $ALERT_FILE 2>/dev/null || echo "")
    
    if [ "$NEW_HASH" != "$OLD_HASH" ]; then
        printf '%s\n' "${ALERTS[@]}" | mail -s "$SUBJECT" "$ALERT_EMAIL"
        echo "$NEW_HASH" > $ALERT_FILE
    fi
else
    # Clear alert state on full health
    rm -f $ALERT_FILE
fi
EOF

chmod +x /usr/local/bin/cluster-health-check.sh

# Schedule every 5 minutes
cat > /etc/cron.d/cluster-health <<EOF
*/5 * * * * root /usr/local/bin/cluster-health-check.sh
EOF
```

### Layer 4 — Prometheus + Grafana (advanced, recommended for production)

For real metrics and dashboards:

#### Step 1: Provision a monitoring LXC

```bash
pct create 156 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname monitoring \
  --cores 2 \
  --memory 4096 \
  --rootfs local-zfs:32 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=1,keyctl=1 \
  --unprivileged 1 \
  --start 1
```

#### Step 2: Install Docker (same as Step 8.3) and deploy stack

```bash
mkdir -p /opt/monitoring
cd /opt/monitoring

cat > docker-compose.yml <<'EOF'
services:
  prometheus:
    image: prom/prometheus:latest
    container_name: prometheus
    restart: unless-stopped
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.retention.time=30d'

  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    restart: unless-stopped
    ports:
      - "3000:3000"
    volumes:
      - grafana-data:/var/lib/grafana
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=changeme   # CHANGE THIS

volumes:
  prometheus-data:
  grafana-data:
EOF

cat > prometheus.yml <<'EOF'
global:
  scrape_interval: 30s
  evaluation_interval: 30s

scrape_configs:
  - job_name: 'llama-v620'
    static_configs:
      - targets: ['192.168.6.151:8080']
    metrics_path: /metrics

  - job_name: 'llama-embed'
    static_configs:
      - targets: ['192.168.6.152:8082']
    metrics_path: /metrics

  - job_name: 'llama-rerank'
    static_configs:
      - targets: ['192.168.6.152:8083']
    metrics_path: /metrics
  
  # Add node-exporter on each LXC for system metrics
  - job_name: 'node-exporter'
    static_configs:
      - targets:
        - '192.168.6.151:9100'
        - '192.168.6.152:9100'
        - '192.168.6.153:9100'
        - '192.168.6.154:9100'
        - '192.168.6.155:9100'

  # Proxmox VE itself via PVE Exporter
  - job_name: 'pve'
    static_configs:
      - targets: ['192.168.6.150:9221']
EOF

docker compose up -d
```

#### Step 3: Install node-exporter on each LXC

```bash
# Run on each LXC (151, 152, 153, 154, 155)
for vmid in 151 152 153 154 155; do
    pct exec $vmid -- bash -c '
        cd /opt
        wget https://github.com/prometheus/node_exporter/releases/download/v1.9.0/node_exporter-1.9.0.linux-amd64.tar.gz
        tar xzf node_exporter-1.9.0.linux-amd64.tar.gz
        mv node_exporter-1.9.0.linux-amd64/node_exporter /usr/local/bin/
        rm -rf node_exporter-1.9.0.linux-amd64*
        
        cat > /etc/systemd/system/node-exporter.service <<EOF
[Unit]
Description=Node Exporter
After=network.target

[Service]
ExecStart=/usr/local/bin/node_exporter --web.listen-address=:9100
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable --now node-exporter
    '
done
```

#### Step 4: Access Grafana

Browse to `http://192.168.6.156:3000`. Login `admin` / `changeme`. Change password.

Add data source:
- **Configuration → Data sources → Add data source → Prometheus**
- URL: `http://prometheus:9090`
- Save & Test

Import dashboards:
- Dashboard 1860 (Node Exporter Full)
- Dashboard 18674 (llama.cpp metrics)

### Critical metrics to watch

- **llama.cpp**: `llamacpp:requests_processing`, `llamacpp:tokens_predicted_seconds`, `llamacpp:kv_cache_tokens`
- **Spec decode acceptance**: log-derived — parse from `journalctl -u llama-server | grep acceptance`
- **GPU temperature**: per-card via rocm-smi/nvidia-smi
- **GPU memory**: per-card utilization vs. total
- **System load**: 1m/5m load average per LXC
- **Disk space**: `/tank/models`, `/tank/anythingllm`, LXC root

---

## Recovery Procedures

### Scenario 1 — Single LXC crash or hung service

**Symptoms:** A service is reported down by health check, or `systemctl status` shows failed state inside an LXC.

**Recovery:**

```bash
# Quick restart
systemctl restart <service-name>

# If service won't start, check logs
journalctl -u <service-name> -n 100 --no-pager

# If LXC itself is hung
pct stop <vmid> && pct start <vmid>

# Hard restart if stop hangs
pct stop <vmid> --force
pct start <vmid>
```

### Scenario 2 — Single GPU failure

**Symptoms:** `rocm-smi` or `nvidia-smi` reports an error or doesn't see one of the GPUs; llama-server fails to start.

**For V620 failure:** The cluster can limp along on the surviving V620 with reduced capacity.

```bash
# Inside LXC 151
# Edit llama-server.service to use only the working GPU
# Change: --tensor-split 1,1
# To:     --tensor-split 1,0   (or 0,1 depending on which failed)
# Also change: HIP_VISIBLE_DEVICES=0   (or 1)

systemctl daemon-reload
systemctl restart llama-server

# Reduce context size — single V620 has only 32 GB VRAM
# Add: --ctx-size 65536 (was 131072)
```

**For 3060 failure:** Embedding and reranking are unavailable. Workarounds:

- Switch AnythingLLM to use a CPU embedder (slow but functional)
- Or temporarily run embedding on a V620 (steal capacity from chat)

### Scenario 3 — Backup restore (LXC level)

**When:** Container data is corrupted, or you need to roll back a bad change.

```bash
# List available backups
pvesm list tank-backups

# Stop the broken container
pct stop 154
pct destroy 154

# Restore from backup
pct restore 154 tank-backups:backup/vzdump-lxc-154-2026_05_10-03_00_01.tar.zst \
    --storage local-zfs

# Start it
pct start 154
```

### Scenario 4 — Full host reinstall

**When:** Boot drive failure, ZFS pool corruption on the boot drive, accidental Proxmox uninstall, etc.

**Prerequisites:** You have backups on `/tank/backups` (which lives on the secondary NVMe — survives boot drive failure if the boot drive is what failed).

**Procedure:**

1. Replace the boot drive if hardware failure
2. Reinstall Proxmox VE 9.x per Phase 3
3. Re-create the ZFS pool import:
   ```bash
   # Don't recreate /tank — import the existing pool
   zpool import tank
   zfs list
   # Verify all datasets are present
   ```
4. Re-add storage definitions:
   ```bash
   pvesm add dir tank-backups --path /tank/backups --content backup,iso,vztmpl
   ```
5. Re-do Phase 4 (host configuration: IOMMU, NVIDIA driver, etc.)
6. Restore each LXC from backup:
   ```bash
   for vmid in 151 152 153 154 155; do
       BACKUP=$(ls -t /tank/backups/dump/vzdump-lxc-${vmid}-*.tar.zst | head -1)
       pct restore $vmid $BACKUP --storage local-zfs
       pct start $vmid
   done
   ```
7. Run Phase 11 verification

Estimated full-recovery time: 2-3 hours from a failed boot drive.

### Scenario 5 — Network outage / connectivity loss

**Symptoms:** LXCs unreachable from the LAN, but the Proxmox host is up.

```bash
# Check bridge
ip a show vmbr0

# Restart networking
systemctl restart networking
# Or if that fails:
ifreload -a

# Check LXC has IP
for vmid in 151 152 153 154 155; do
    ip=$(pct exec $vmid -- ip -4 addr show eth0 | grep inet | awk '{print $2}' || echo "no IP")
    echo "VMID $vmid: $ip"
done

# If an LXC is missing IP, restart it
pct stop $vmid && pct start $vmid
```

### Scenario 6 — AnythingLLM database corruption

**Symptoms:** AnythingLLM UI shows errors, workspace queries return strange results, or the SQLite database fails to open.

```bash
# Stop AnythingLLM
pct enter 154
cd /opt/anythingllm
docker compose down

# Restore from latest ZFS snapshot
LATEST_SNAPSHOT=$(zfs list -H -t snapshot -o name | grep "tank/anythingllm@" | tail -1)
echo "Restoring from $LATEST_SNAPSHOT"
zfs rollback "$LATEST_SNAPSHOT"

# Or restore from earlier daily
zfs rollback tank/anythingllm@daily-2026-05-09

# Restart
docker compose up -d
```

### Scenario 7 — Model file corruption

**Symptoms:** llama-server fails to load model with checksum errors.

```bash
# Verify checksum
sha256sum /tank/models/qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf

# Compare against the published checksum on Hugging Face

# If mismatched, redownload
cd /tank/models
mv qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf.corrupt
wget -O qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf <URL>

# Restart V620 service
pct exec 151 -- systemctl restart llama-server
```

### Scenario 8 — Disaster recovery checklist

Worst case: building burned down, all hardware lost. What you need:

- **Off-site backup of Proxmox configuration** (`/etc/pve/` snapshot)
- **Off-site backup of LXC archives** (vzdump tar.zst files)
- **Off-site backup of `/tank/anythingllm` data**
- **Documentation** (this runbook + the v2 reference doc)
- **API keys and credentials** stored in a password manager

With all of this, full rebuild on new hardware takes 1-2 days end to end.

---

## Troubleshooting Cheatsheet

### llama-server won't start in V620 LXC

```bash
# Check if model file is accessible
pct exec 151 -- ls -la /opt/models/

# Check ROCm visibility
pct exec 151 -- rocminfo | grep gfx1030

# Run interactively to see error
pct enter 151
cd /opt/llama.cpp
./build/bin/llama-server --model /opt/models/qwen3.5-35b-a3b-q4_k_m-00001-of-00002.gguf --port 8080
```

### `Failed to initialize NVML: Driver/library version mismatch`

The host kernel module and LXC userspace versions don't match.

```bash
# On host
nvidia-smi --query-gpu=driver_version --format=csv,noheader
# Note the version

# Inside LXC 152
nvidia-smi --query-gpu=driver_version --format=csv,noheader
# Should match exactly

# If different, reinstall LXC userspace at the matching version per Step 6.4
```

### Speculative decoding acceptance rate is low (<40%)

Likely model mismatch — the draft and target models don't share enough vocabulary.

```bash
# Verify both are from the same family
pct exec 151 -- ls -la /opt/models/qwen3.5-*

# Both must be qwen3.5 — not qwen2.5 + qwen3.5 mix
# If they're mixed, redownload the correct draft model
```

### High GPU temperatures on V620 (>85°C)

```bash
# Verify shroud fans are spinning
# Visually inspect, or check that the SATA-to-fan adapter is plugged in

# Check ambient room temp — V620s in a closed case need <25°C ambient
# for sustained heavy load

# Reduce load temporarily by limiting context or batch size
```

### AnythingLLM workspace shows refusal for questions clearly in corpus

This is the v1 §5.7 dimension-mismatch issue. Embedding dimension changed but documents weren't re-embedded.

```bash
# In AnythingLLM UI: Workspace → Documents → Select All → Remove + Delete
# Then re-upload from /tmp/opt/vcf-ingest/out/ (Step 10.4)
# Then trigger embedding (Step 10.5)
```

### Router returns 504 Gateway Timeout

Upstream service is down or slow. Check upstream:

```bash
# Quick health check
curl -s http://192.168.6.153:8000/healthz | jq

# If V620 says "unreachable", it's down — see Scenario 1
```

### MCP "SSE error: Unable to connect" in OpenCode

```bash
# Verify container is running
pct exec 155 -- docker ps

# If down
pct exec 155 -- bash -c 'cd /opt/<mcp-name> && docker compose up -d'

# If running but not responding, check logs
pct exec 155 -- docker logs <container-name> --tail 50
```

---

## Appendix — Quick Reference

### Useful one-liners

```bash
# Status of all GPU stacks
for vmid in 151 152; do
    echo "=== LXC $vmid ==="
    pct exec $vmid -- systemctl --no-pager --type=service --state=running | grep llama
done

# All LXC IPs
for vmid in 151 152 153 154 155; do
    echo "VMID $vmid: $(pct exec $vmid -- hostname -I 2>/dev/null | awk '{print $1}')"
done

# Backup all LXCs now
vzdump 151 152 153 154 155 --storage tank-backups --mode snapshot --compress zstd

# Restart everything in dependency order
pct restart 151        # V620 stack first
sleep 30
pct restart 152        # 3060 stack
sleep 15
pct restart 153        # Router
sleep 5
pct restart 154        # AnythingLLM
sleep 5
pct restart 155        # MCP stack

# Check spec decode acceptance rate
pct exec 151 -- journalctl -u llama-server -n 100 --no-pager | grep -i acceptance | tail -5
```

### File / data inventory

| Path | What it is | How to recover |
|---|---|---|
| `/tank/models/*.gguf` | Model files (~25 GB) | Redownload from HF |
| `/tank/anythingllm/` | RAG data, workspaces | ZFS snapshot or vzdump |
| `/tank/mcp/` | MCP working state | ZFS snapshot |
| `/etc/pve/lxc/*.conf` | LXC configs | Backed up in vzdump |
| `/etc/pve/storage.cfg` | PVE storage definitions | Manual backup recommended |
| `/root/deployment-record/` | Your deployment notes | Off-host backup |
| LXC root filesystems | Installed software, OS config | vzdump |

### Contacts and escalation paths

When things break and you're not sure how to fix them:

1. **llama.cpp issues**: GitHub issues at `ggml-org/llama.cpp` — well-maintained, fast responses
2. **Proxmox issues**: Proxmox community forum — searchable, responsive community
3. **ROCm on V620**: AMD ROCm GitHub or Reddit r/ROCm
4. **AnythingLLM**: Mintplex Labs Discord or GitHub issues
5. **MCP-specific**: Claude / OpenCode community

---

## Known Risks and Caveats

These are issues identified during architectural verification that may affect deployment. Each is paired with a fallback or mitigation. Read this section before bringing the cluster online.

### Hardware-level risks

**1. NF-A8 PWM airflow may be marginal under sustained V620 load.**
- Spec: 32.67 CFM, 2.37 mm H₂O static pressure at full 12V (2200 RPM).
- Two NF-A8s side-by-side over a V620 give roughly 65 CFM of unfocused airflow. V620 is rated for ~225W TDP and was designed for high-static-pressure server fans (50-150 CFM each at 5+ mm H₂O).
- **Risk:** Thermal throttling under sustained inference load (target+draft both at full GPU utilization for >5 minutes).
- **Partially mitigated by 5× ARCTIC P14 Pro PST chassis fans** (Step 1.10). With 3× P14 as front intake (~72 CFM each, 2.4 mm H₂O static pressure), the V620 NF-A8s receive dense cool intake air rather than recirculated chassis air. This significantly reduces but does not eliminate the throttling risk, since the NF-A8 itself is the bottleneck pushing air *through* the GPU heatsink.
- **Mitigation A (preferred):** Monitor V620 edge/junction temperatures via `rocm-smi --showtemp` during sustained load. If junction temp exceeds 95°C, throttle GPU clocks: `rocm-smi -d 0 --setperflevel low` or set a lower power cap with `rocm-smi -d 0 --setpoweroverdrive 175` (caps at 175W instead of 225W).
- **Mitigation B:** Replace NF-A8 with a higher-airflow 80mm fan (Noctua NF-R8 redux-1800 PWM is 31.4 CFM; or a server-grade Delta AFB0812SH at ~52 CFM with notable noise increase).
- **Mitigation C:** Already implemented — 5× ARCTIC P14 Pro PST as case fans dramatically improve total chassis airflow.

**2. Marvell AQC113CS 10GbE Linux driver has reported regressions on PVE 9.x.**
- Multiple users on Proxmox forums report the AQC113 link going down after kernel upgrades (working on 6.8.4-2, broken on 6.8.4-4+, intermittent on 6.14.x).
- **Risk:** Primary uplink fails after a kernel update, leaving the node accessible only via the 2.5GbE Realtek port (or not at all if you only configured the 10GbE port).
- **Mitigation A:** Always configure both NICs in `/etc/network/interfaces` from day one — bridge the 10GbE as `vmbr0` and the 2.5GbE as `vmbr1`, with the 2.5GbE as a backup default route at higher metric. If 10GbE fails, the 2.5GbE stays online.
- **Mitigation B:** Pin the kernel version (which we already do for NVIDIA reasons — 6.14) and don't auto-upgrade.
- **Mitigation C:** If the 10GbE never works on PVE 9.x, fall back to a known-good 10GbE PCIe NIC (Intel X550-T2, X710, or Mellanox ConnectX-4) in the chipset PCIe 4.0 x4 slot.

**3. Phantom Spirit 120 EVO is overkill for a 65W Ryzen 7600.**
- Not a problem, just an observation — the 7600's TDP is well under the cooler's 280W rating.
- Fan curve in BIOS should be set to a quiet preset to keep noise minimal at this thermal load.

**4. Power Zone 2 1200W has Teapo capacitors and rifle-bearing fan.**
- Per Tom's Hardware Feb 2026 review: 80+ Platinum certified, no Cybenetics certification at time of review. The 1200W variant is HEC-built (smaller variants are FSP). Teapo caps are 105°C-rated but considered budget-tier vs Japanese Nichicon/Chemicon.
- **Risk:** Capacitor lifespan may be shorter than premium PSUs (Seasonic, Corsair AX series). Long-term reliability unknown.
- **Mitigation:** Monitor PSU under load for instability. The 10-year warranty covers replacement. Keep a spare PSU available if uptime is critical.

### Software-level risks

**5. Docker-in-LXC on Ubuntu 24.04 may hit systemd 255 issues.**
- Ubuntu 24.04 ships systemd 255, which has reported incompatibilities with certain Docker storage driver / cgroup interactions in unprivileged LXC.
- This runbook uses Ubuntu 24.04 templates throughout for consistency, but the AnythingLLM (154) and MCP (155) LXCs run Docker.
- **Risk:** Docker fails to start, or containers crash after a few hours.
- **Mitigation A:** If you encounter Docker failures on the 154/155 LXCs, recreate them with the **Debian 12 (Bookworm) template** instead — Debian 12 ships systemd 252 which is well-tested for Docker-in-LXC.
- **Mitigation B:** Run Docker in a full QEMU VM rather than LXC for these workloads. Costs ~512MB RAM overhead per VM but eliminates the LXC/Docker interaction surface entirely.

**6. ROCm + V620 + unprivileged LXC may have intermittent KFD failures.**
- Multiple guides (kextcache.com, Strix Halo Proxmox guide) recommend **privileged** containers for ROCm reliability, citing the KFD driver handshake.
- This runbook starts with unprivileged + `apparmor.profile: unconfined` for better isolation.
- **Risk:** `rocm-smi` returns errors, llama.cpp HIP backend fails to initialize, or sustained workloads cause KFD crashes.
- **Mitigation:** Switch the V620 LXC (151) to privileged mode if issues occur. Edit `/etc/pve/lxc/151.conf`, change `unprivileged: 1` to `unprivileged: 0`, and remove the `lxc.apparmor.profile: unconfined` line. Restart. The 3060 LXC (152) and Docker LXCs (154/155) can stay unprivileged.

**7. Marvell AQC113 atlantic driver may break on kernel 6.17+.**
- We pin to 6.14 for NVIDIA driver compatibility, which also keeps us on a kernel where AQC113 is more reliably supported.
- **Risk:** When NVIDIA eventually supports 6.17 and we unpin, the AQC113 may regress.
- **Mitigation:** Test 10GbE link state immediately after any kernel upgrade. Have the 2.5GbE fallback configured.

**8. Speculative decoding on a MoE target (35B-A3B) may have lower acceptance rates than expected.**
- Speculative decoding healthy acceptance rate for dense pairs is 0.57-0.70.
- 35B-A3B is MoE with 3B active params per token. The 0.8B dense draft model may match its routing decisions less reliably than for a 35B dense target.
- **Risk:** Acceptance rate falls below 0.50, at which point speculative decoding hurts rather than helps throughput.
- **Mitigation:** After deployment, measure acceptance rate via llama-server's `/health` endpoint or by inspecting `--verbose` logs. If <0.50, disable speculative decoding (`-md` flag removed from the systemd unit) and run target alone — the 35B-A3B will still be fast due to its 3B active params.

**9. Qwen3 Embedding's `<|endoftext|>` requirement is silent if not handled.**
- llama-server accepts inputs without the EOT token but produces lower-quality embeddings.
- The router injects EOT automatically, so direct calls through the router are fine.
- **Risk:** Custom code calling the embedder LXC directly (bypassing the router) gets degraded embeddings without warning.
- **Mitigation:** Document this clearly for any future RAG code: always go through the router (port 8000), not directly to the embedder LXC (port 8082).

**10. AnythingLLM `addToWorkspaces` parameter is intermittent.**
- Known bug Mintplex-Labs/anything-llm#5271.
- **Risk:** Documents upload but don't attach to workspace.
- **Mitigation:** Always use the documented two-step pattern: `POST /v1/document/upload` → `POST /v1/workspace/{slug}/update-embeddings` with `{"adds": [doc_location]}`. The auto-updater script does this.

### Deployment-time empirical checks

These cannot be verified from documentation alone — confirm during your actual deployment:

- [ ] Exact `RENDER_GID` / `VIDEO_GID` on your host (run `getent group render | cut -d: -f3`)
- [ ] Exact major device numbers for `/dev/kfd`, `/dev/dri/renderD128`, `/dev/nvidia*` (run `ls -la /dev/kfd /dev/dri/` on host)
- [ ] PCIe link width on V620 #2 in PCIE_2 with M.2_2 empty (should be 4.0 x8; check `lspci -vv -s <bdf> | grep LnkSta`)
- [ ] Flash Attention performance on V620 (gfx1030) — benchmark `-fa on` vs `-fa off`; gfx1030 has had documented FA regressions
- [ ] Speculative decoding acceptance rate with 35B-A3B + 0.8B (target 0.55-0.70; below 0.50 = disable)
- [ ] V620 thermal behavior under 5-minute sustained load with NF-A8 fans (read junction temp, watch for throttling)
- [ ] AQC113 10GbE link stability over a 24-hour period

---

## Document Maintenance

Update this runbook whenever:
- The cluster gains or loses a service
- A model file changes
- An IP address changes
- A new failure scenario is discovered (add to Recovery)
- Software versions change significantly (Proxmox major version, ROCm, llama.cpp breaking changes)

Last verified deployment: TBD (fill in when first successful end-to-end run completes).
