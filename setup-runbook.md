# Local GPU Cluster v2 — Deployment Runbook

**Sequential, step-by-step deployment of the v2 cluster: ASUS ProArt X870E-Creator + Ryzen 7600 + 2× V620 on Proxmox VE 9.x.**

> ### ⚠️ Build-status banner (V620-only pivot, mid-flight)
> This runbook is mid-pivot from the original "2× V620 + 1× RTX 3060" architecture to "V620-only". V620-only-clean phases (runbook side): **1, 2, 3, 4, 5 (three units), 6 (cutover), 7 (router LXC + API key generation), 8.6 (AnythingLLM), 10 (data migration), 11 (all steps including 11.4 acceptance suite), Monitoring + Backup, Scenario 2 (single-V620 failure recovery), Risks section** (thermal math + kernel pin + privileged-mode mitigation now V620-only). The provisioning scripts under `scripts/` and `scripts/files/router-app.py` still carry pre-pivot code (FAST_URL, no Bearer auth, hardcoded `192.168.6.152` URLs) — tracked as todos #18–#20 in the pivot plan.
>
> **What this means for fresh builds today:** Phases 1–11 of the runbook deliver a fully working V620-only inference + router + AnythingLLM stack with Bearer auth, plus the Phase 11.4 acceptance verification suite. The runbook prose path is operable end-to-end. **The supporting scripts under `scripts/` still need updating** (todos #18–#20) — fresh operators following the runbook commands directly will succeed; those who try to run `scripts/bootstrap.sh` will hit the pre-pivot 3060 references.
>
> **What this means for cutover from existing v2 cluster:** Phase 6 (Cutover) is operational. Its BLOCKER notice now only requires the router-app.py rewrite (todo #20) before live cutover is safe — until then, run Phase 6 in dry-run mode (skip Step 6.2 cutover, only do pre-cutover audits in Step 6.1).
>
> See `C:\Users\willi\.claude\plans\we-will-pivot-to-squishy-lampson.md` for the full pivot plan and outstanding todos.

This runbook is the operational companion to [`local-gpu-cluster-v2.md`](./local-gpu-cluster-v2.md). The v2 doc explains *why*; this runbook is the exact *how*. Every command is meant to be run in order. Each phase ends with a verification step — do not proceed if it fails.

> **Architecture correction notes (vs. v2 doc rev 1):**
>
> 1. Speculative decoding requires both target and draft models in the same `llama-server` process (via `-md` / `--model-draft`). Both models live on the V620 LXC. With the V620-only pivot, embeddings and reranking moved into the same LXC as additional `llama-server` instances pinned per-card via `--main-gpu` (see `local-gpu-cluster-v2.md` §1.3 and Phase 5 below).
>
> 2. The "Qwen3.6 35B" model is technically **Qwen3.6-35B-A3B** — a Mixture-of-Experts model with 3 B active parameters per token out of 35 B total. Inference speed is closer to a 3 B model while VRAM cost matches the full 35 B weight set. Speculative decoding still works but speedup may be modest since the target model is already fast. Qwen3.6 reuses the Qwen3 tokenizer family, so Qwen3-0.6B is a vocab-compatible draft.
>
> 3. Proxmox VE 9.1 (released Nov 2025) defaults to Linux kernel 6.17. With NVIDIA removed, no kernel pinning is required — AMDGPU is in-tree and stable on 6.17.
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
- [Phase 6 — Cutover (live migration from running v2 cluster)](#phase-6--cutover-live-migration-from-running-v2-cluster)
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
- [ ] 2× AMD Radeon Pro V620 (32 GB GDDR6 each) — only GPUs in the build; see `local-gpu-cluster-v2.md` §1.3 for the V620-only pivot rationale
- [ ] Thermalright Phantom Spirit 120 EVO CPU cooler ✅
- [ ] be quiet! Power Zone 2 1200W PSU ✅ (verified: 80+ Platinum, dual 12V-2x6, HEC-built)
- [ ] **Lian Li Lancool 217 case (SKU LAN217X — Black with walnut wood accent, non-RGB)** — includes 5× pre-installed PWM fans (2× 170mm front + 2× 120mm bottom + 1× 140mm rear), 6-channel PWM fan hub, adjustable GPU support bracket, dual PSU mount positions
- [ ] 2× GF Computers V620 80mm cooling shroud kits (eBay) — **shrouds only, no fans/power included**
- [ ] **2-4× Noctua NF-A8 PWM 80mm fans** — quantity depends on shroud (1 vs 2 fans per V620 shroud); confirm before ordering. If each V620 shroud holds 2 fans (typical), you need 4 total
- [ ] **2× upHere G205 GPU brace supports** (anti-sag jack stands) — one per V620; the case includes a built-in adjustable bracket as well, providing redundant support for the top V620
- [ ] **5× ARCTIC P14 Pro PST 140mm chassis fans** — 3 deployed as top exhaust (PST-chained on a single motherboard header), 2 held as cold spares. The Lancool 217's 5 pre-installed fans cover front + bottom + rear positions
- [ ] Boot NVMe (1 TB+ recommended) for Proxmox — install in M.2_1 (CPU PCIe 5.0 x4). Will be formatted **ext4** (LVM-thin for `local-lvm`).
- [ ] 2× Data NVMe (2 TB+ each, matched capacity) for `/tank` — installed in M.2_3 and M.2_4 (chipset PCIe 4.0). Will be configured as a **ZFS mirror** (single-drive failure tolerant, self-healing via checksums).
- [ ] Quality PCIe 4.0 x16 riser (LinkUp Ultra) — **only if using vertical mount** (not needed for standard 2-GPU horizontal layout)

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

1. Unbox the Lancool 217. Remove both side panels (tool-less — pull from the rear edge). Verify all 5 pre-installed fans are present and undamaged from shipping (Lian Li uses rubber-grommet isolators that can come loose in transit).
2. Remove the top panel via the rear pull tab to expose the top fan mounts (this is where the 3× P14 PST top exhaust fans will go).
3. Decide PSU orientation:
   - **Traditional rear mount (recommended for this build):** PSU cables exit toward the front of the case. Supports up to 220mm PSU length when the right drive cage is removed. Better cable routing for our 5× GPU power cables. *Remove the right drive cage now if you choose this orientation.*
   - **Rotated 90° (alternative):** PSU cables exit toward the side panel. Limited to 180mm PSU. Uses the included L-shaped power extension cable.
4. Install the Power Zone 2 PSU in the chosen orientation, fan facing down toward the bottom dust filter. Use the included anti-vibration screws.
5. Install the motherboard standoffs in the ATX positions per the Lancool 217 manual. Confirm all 9 standoffs are present and seated.
6. Identify the location of the built-in 6-channel PWM fan hub behind the motherboard tray — typically pre-routed at the top-right of the rear chamber. Note where its master PWM input cable and SATA power cable terminate (you'll connect these in Step 1.9).

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
2. **M.2_3** (chipset PCIe 4.0 x4): First `/tank` member.
3. **M.2_4** (chipset PCIe 4.0 x4): Second `/tank` member. M.2_3 and M.2_4 will be paired as a ZFS mirror in §4.8; they're both chipset-attached and don't affect GPU lanes.
4. **Leave M.2_2 empty** — per ASUS spec, populating M.2_2 drops PCIEX16(G5)_2 from x8 to x4, hurting V620 #2 bandwidth.
5. Install M.2 heatsinks on all three drives (the board provides them; pull tabs are tool-free).

### Step 1.6 — Install GPUs (in this exact order)

1. **PCIE_1 (top slot):** V620 #1 — secure the slot retention clip
2. **PCIE_2 (middle slot):** V620 #2 — secure the slot retention clip
3. **PCIE_3 (bottom slot):** **leave empty** (see PCIE_3 sidebar in Step 1.5 for future-use options)

For each GPU, install screws to secure to the rear case bracket. **Do not power them on yet** — fan shrouds and supports come next.

> **PCIE_3 future-use sidebar.** The bottom slot is PCIe 4.0 x4 from the chipset; populating it doesn't affect the V620 lanes (which live on CPU-attached PCIE_1/PCIE_2). Prioritized options if you later expand: (1) dual-port 10 GbE NIC like Intel X710 ~$100 used for cluster federation, (2) HBA like LSI 9300-8i ~$50 if `/tank` outgrows two NVMes, (3) PCIe USB-C / audio capture for a future local-Whisper LXC's ingest, (4) OCuLink adapter for external GPU (niche; defers cooling/power to a separate rail). Skip eGPU enclosures — OCuLink + external PSU + cooling is more project than a bigger case is worth.

### Step 1.7 — Install V620 fan shrouds

1. Mount each 80 mm Noctua NF-A8 to a V620 shroud per the shroud kit instructions.
2. Attach the shroud assembly to the rear of each V620 with the included screws. The fan should pull air **through** the heatsink toward the rear of the card.
3. Route the 3-pin fan cables along the bottom of the case toward where the SATA-to-fan adapter will attach.

### Step 1.8 — Install GPU support brackets

The Lancool 217 includes a built-in adjustable GPU support bracket on the motherboard tray. Use it for the top GPU (V620 #1), and supplement with the 2× upHere G205 jack stands.

1. **Built-in bracket → V620 #1** (top, heaviest with shroud): Adjust the case's built-in support bracket vertically so its lip just touches the underside of V620 #1's heatsink.
2. **upHere G205 #1 → V620 #2** (middle): Place on the PSU shroud floor under the front edge of V620 #2. Adjust the telescopic screw so the rubber pad just touches the underside of the heatsink.
3. **upHere G205 #2 → V620 #1** (redundant support): Place alongside the case's built-in bracket. V620 + shroud is ~1.5 kg; dual support reduces long-term sag risk over 24/7 operation.
4. The G205's magnetic + rubber base holds it in place without screws into the PSU shroud. Verify each GPU sits level — neither sagging nor lifted.

### Step 1.9 — Power connections

1. **24-pin ATX** → motherboard
2. **2× 8-pin EPS** → motherboard CPU power (both required)
3. **V620 #1:** 2× 8-pin PCIe (use 2 separate PSU cables, not one daisy-chain — 300 W on a single cable is at the safety edge)
4. **V620 #2:** 2× 8-pin PCIe (one cable acceptable here; daisy-chain the second connector)
5. **Shroud fans (V620 cooling):** see Step 1.9.1 below for the recommended PWM control approach.

Total: 4× 8-pin PCIe in use, 2 free PSU PCIe connectors remain (PSU has dual EPS + 4× PCIe 8-pin native). The freed connector from removing the 3060 is the budget headroom for a future third V620 (~225 W sustained, well under the PSU's 340 W remaining headroom at 80% derate; slot count and cooling are the constraints, not power — see `local-gpu-cluster-v2.md` appendix for the math).

### Step 1.9.1 — V620 shroud fan power and control

The 4× NF-A8 PWM fans on the V620 shrouds need power AND ideally PWM-modulated speed control so they idle quietly when the GPUs aren't under load. Three approaches; pick the one that matches your wiring:

**Approach A: V620 shroud fans on dedicated motherboard fan headers (recommended for this build).**

Plug each V620 shroud fan into its own motherboard 4-pin PWM header — typically CHA_FAN4 and CHA_FAN5 on the ASUS ProArt X870E-Creator. If the shrouds carry 2 fans each, daisy-chain them or use a Y-splitter so each card's pair of fans shares one header. Case stock fans go on a separate header (or the Lancool 217's built-in hub) with their own BIOS curve.

Why this is preferable:
- **Independent control.** V620 fans ramp aggressively when GPUs are hot; case fans stay quiet on their own profile. No noise penalty for the chassis when only the GPUs need cooling.
- **Per-card fan failure visibility.** Each header reports its fan's RPM separately via lm-sensors. If one shroud fan dies, the dead RPM shows up immediately in monitoring.
- **No SATA-power dependency.** Direct PWM headers source from the motherboard's own 12 V rail and PWM signal — fewer cables, no SATA-to-fan adapter.

Hardware:
- V620 #1's NF-A8 shroud fan(s) → **CHA_FAN4** motherboard header
- V620 #2's NF-A8 shroud fan(s) → **CHA_FAN5** motherboard header
- Case stock fans → Lancool 217 built-in PWM hub → **CHA_FAN3** (or your chosen case-fan header)

BIOS configuration (Step 2.2 covers BIOS broadly):
- Q-Fan Configuration → CHA_FAN4 + CHA_FAN5 → Mode: **PWM**, Speed control: **Manual**
- Flat 75% baseline curve as fail-safe (if the host software bridge dies, fans stay at safe high speed — better to be loud than throttle)
- CHA_FAN3 (case fans) can stay on a quiet curve driven by CPU or motherboard temp, independent of GPUs

Software setup (defer until after Phase 5 — LXC 151 must be running first):
- See Step 5.13 below. The `v620-fan-bridge.service` supports a space-separated list of PWM paths in `FAN_PWM_PATH`, so it writes the V620-driven duty cycle to both CHA_FAN4 and CHA_FAN5 simultaneously.

**Approach A-alt: Lancool 217 built-in PWM fan hub + single CHA_FAN header (shared curve).**

If you wired the V620 shroud fans into the Lancool 217's 6-channel PWM hub along with the case fans (single header drives everything), set `FAN_PWM_PATH` to that one PWM file. All fans share one curve. Simpler wiring but louder chassis when GPUs are working:

Hardware:
- All 4× NF-A8 PWM fans → spare channels on the built-in hub (channels 1-4 typically occupied by stock case fans, leaving 5-6 available for NF-A8s)
- Hub master PWM input cable → one CHA_FAN motherboard header (commonly CHA_FAN3)
- Hub power → SATA connector from PSU

Power budget check (built-in hub rated 24 W / 2 A at 12 V):
- 4× NF-A8 PWM @ 0.08 A each = 0.32 A
- Case stock fans (2× 170mm + 2× 120mm + 1× 140mm) total ~0.6 A
- Grand total ~0.92 A — well within budget

Trade-off: V620 temp drives the whole chassis. Whisper-quiet at idle, but case fans ramp every time the GPUs are working — even if the case fans themselves are cool. Use Approach A (dedicated headers) if you can.

**Approach B: Aquacomputer Quadro fan controller (~$70) — independent fan groups.**

If you want different fan curves for V620 cooling vs the rest of the chassis (e.g., V620 fans aggressive while case fans stay quiet), use a dedicated controller:
- Buy 1× Aquacomputer Quadro (~$70) and 2× 10K thermistors (~$5 each)
- Stick one thermistor under each V620's shroud, near the heatsink fins
- Wire 4× NF-A8 to one Quadro PWM channel; case stock fans stay on the Lancool hub
- Thermistors to two Quadro temp inputs
- Configure curve via `liquidctl` on Linux
- Quadro is USB-connected; runs from boot independent of OS
- Case stock fans on the Lancool hub stay on the motherboard's CPU-temp curve

**Approach C (simplest, but loud): SATA constant 12V for V620 fans only.**

Bypass the Lancool hub for the V620 fans:
- Power V620 NF-A8s from a SATA-to-fan splitter at constant 12V (Noctua NA-FH1 hub, NA-SAC5+NA-SYC1, or third-party SATA splitter)
- Case stock fans stay on the Lancool hub at motherboard-controlled curve
- V620 fans always at 100% (~24 dB(A) combined) regardless of load
- Use only if Approach A's software bridge fails or you don't want the complexity

For the rest of this runbook, **Approach A is assumed**. The software bridge implementation appears in Step 5.13 below.

### Step 1.10 — Install case fans

The Lancool 217 ships with **5 pre-installed PWM fans**: 2× 170mm front (mounted on a removable bracket that can be repositioned vertically), 2× 120mm reverse-blade on the PSU shroud (aimed UP at the GPU stack), and 1× 140mm rear exhaust. These are well-engineered for compute workloads — **keep them all**.

Add the 3× ARCTIC P14 Pro PST as **top exhaust** (the only stock position not pre-populated). The remaining 2× P14 PSTs stay as cold spares.

1. **Verify front 170mm fans are in "GPU mode" (lower position)** — they should sit lower on the front fan bracket, aimed at the middle of the case where the GPU stack will be, not the upper position which aims at the CPU socket. If not, loosen the bracket screws, slide the fan mount down, retighten.
2. **Top exhaust (3× 140mm slots):** Install 3× ARCTIC P14 Pro PST. Airflow direction OUT of the case. Removes hot air rising from CPU + GPUs.
3. **PST chain for top fans:** The P14 Pro PST has a built-in Y-splitter (PWM Sharing Technology). Chain the 3 top fans together — the master plugs into a single motherboard PWM header (CHA_FAN1 or similar), and the next 2 daisy-chain off it. The motherboard sees them as one fan with shared RPM signal.
4. **Don't connect top fans to the Lancool hub.** The hub is reserved for fans on the V620 PWM bridge curve (per Step 1.9.1). The top exhaust fans should run on a different motherboard curve (e.g., CPU temp-based) since they're removing heat from above the CPU socket, not the GPU stack.
5. **Bottom fans are already installed** in the PSU shroud, aimed up at the GPU stack. Do not remove them — they're providing direct GPU undercurrent airflow which directly mitigates the V620 thermal concern. They connect to the Lancool hub at the factory.
6. **Front 170mm fans are already installed** and connect to the Lancool hub at the factory. Verify the cables are routed through the rear of the front fan bracket to reach the hub behind the motherboard tray.
7. **Rear exhaust fan is already installed** and connects to the Lancool hub.
8. **Hold the remaining 2× P14 PSTs as spares.** Possible future uses: replace a failed stock fan, populate a 5th top slot if the case ever supports it, or add 120mm rear cooling if you swap to a different rear fan.
9. Configure BIOS fan curve in Step 2.2:
   - **CHA_FAN1** (top exhaust, P14 PST chain): CPU-temp-driven curve, ramp at 50°C+
   - **CHA_FAN3** (Lancool hub master): Manual, controlled by V620 PWM bridge (per Step 1.9.1)

### Step 1.11 — Cable management

1. Route all cables behind the motherboard tray.
2. Use the case's included velcro straps generously.
3. Tuck unused PCIe and SATA connectors behind the tray, secured with velcro.
4. Confirm no cables interfere with fan blades or GPU airflow.

### Step 1.12 — Pre-flight check

Before powering on:

- [ ] All four PSU connectors firmly seated (24-pin, 2× EPS, GPU power)
- [ ] Both V620 GPUs locked in slot retention clips (PCIE_1 + PCIE_2); PCIE_3 empty
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
- PCIE_3: empty (intentionally — see Step 1.6 sidebar for future-use options)

If a slot that should have a V620 shows empty, power off and re-seat that GPU.

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

> **Kernel version note:** Proxmox VE 9.1 (Nov 2025+) ships with kernel **6.17** as default. AMDGPU has full gfx1030 (V620) support in mainline since 6.6; no kernel pinning required. Historical note: earlier revisions of this runbook pinned to 6.14 because of NVIDIA DKMS build failures against 6.17 — with NVIDIA removed, that constraint no longer applies.

### Step 3.2 — Boot the installer

1. Insert the USB into a **rear** USB-A port (front-panel USB occasionally has init-order issues during Proxmox boot)
2. Power on, press F8 to choose boot device, select the USB
3. At the Proxmox boot menu, select "Install Proxmox VE (Graphical)"

### Step 3.3 — Storage configuration

The boot drive uses **ext4 with LVM** (not ZFS). Rationale:
- Boot drive only hosts Proxmox + LXC rootfs; no need for the ZFS ARC RAM overhead on this drive.
- The two data NVMes (M.2_3 + M.2_4) will be paired as a ZFS mirror for `/tank` in §4.8 — that's where snapshots, checksumming, and self-healing actually matter (model files, RAG data, MCP state).
- With ext4, Proxmox exposes `local-lvm` (LVM-thin) for LXC rootfs and `local` (a directory on the root LV) for ISOs/templates.

**Installer steps:**

1. Accept EULA
2. Target hard disk: select your **boot NVMe** (M.2_1)
3. Click "Options"
4. Filesystem: **ext4**
5. Set the advanced options (values below assume a **1 TB boot NVMe**; scale proportionally — see notes):
   - `hdsize`: leave default (full disk)
   - `swapsize`: **8** (8 GB — emergency cushion only; you have 128 GB RAM)
   - `maxroot`: **64** (64 GB for `pve/root` — holds the OS, `/var/log`, and `/var/lib/vz` which is `local` storage. Backups land on `/tank/backups`, not here.)
   - `minfree`: **32** (32 GB LVM-thin reserve — never let the thin pool fill or it corrupts)
   - `maxvz`: leave default (auto-computed as `hdsize − maxroot − swapsize − minfree` ≈ 896 GB on a 1 TB drive; this becomes `local-lvm` for LXC rootfs)
6. OK to confirm

**Scaling for other boot-drive sizes:**
- **500 GB drive:** `swapsize=8`, `maxroot=48`, `minfree=16` → `local-lvm` ≈ 428 GB
- **2 TB drive:** same `swapsize=8`, `maxroot=64`, `minfree=32` → `local-lvm` ≈ 1.9 TB (no reason to grow root/swap; the extra space goes to LXC pool)

**Why these aren't the installer defaults:** Proxmox defaults `maxroot` to `hdsize/4` (~250 GB on 1 TB) assuming you'll store backups in `local`. We don't — `tank-backups` (on the ZFS mirror) catches all vzdump output, so a 64 GB root is plenty.

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

### Step 4.1 — Kernel version (no pinning required)

This cluster runs only AMD V620 GPUs via the in-tree `amdgpu` kernel module. AMDGPU has full gfx1030 (RDNA 2) support in mainline since kernel 6.6, including PVE 9.1's default 6.17. **No kernel pinning is required.**

Historical note: earlier revisions of this runbook pinned PVE 9.1's kernel to 6.14 because the NVIDIA RTX 3060 leg required Debian's nvidia-driver package, which had DKMS build failures against 6.17. With NVIDIA removed (see `local-gpu-cluster-v2.md` §1.3 for the pivot rationale), that constraint no longer applies.

```bash
# Verify kernel
uname -r
# Expect: 6.17.x-pve on a fresh PVE 9.1 install. 6.14.x is also fine if you came from an older PVE.
```

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
done | sort -V | grep -iE "vga|3d|display controller|radeon|navi|amd.*audio"
```

Expected: each GPU and its audio function in its own dedicated IOMMU group, with no unrelated devices co-grouped. The ProArt X870E-Creator should produce clean groupings.

If groups are bad (multiple GPUs sharing a group with chipset devices), recheck BIOS settings — particularly **ACS Enable**.

### Step 4.4 — AMD firmware + amdgpu autoload on Proxmox

**⚠️ Critical:** Do not run `apt install firmware-amd-graphics` on Proxmox VE. Proxmox ships `pve-firmware`, which already contains all AMD GPU firmware blobs (including V620 / gfx1030). The Debian `firmware-amd-graphics` package conflicts with `pve-firmware`; APT will offer to remove `pve-firmware` → which removes `proxmox-default-kernel` and `proxmox-ve` (the meta-package) → which can brick the install. The `pve-apt-hook` is supposed to catch this and abort; if you see that warning, **say no / cancel**.

```bash
# Verify pve-firmware is installed (it ships with PVE by default)
dpkg -l pve-firmware
# Expect: ii  pve-firmware ...
```

**Add amdgpu to module autoload list.** On a fresh PVE install — especially on the newer 7.0.x kernel branch — `amdgpu` may not autoload via udev for class-0380 "Display controller" devices (V620). Explicitly add it to `/etc/modules` so it loads at boot:

```bash
# Check what's currently in /etc/modules
cat /etc/modules

# Add amdgpu if missing (idempotent)
grep -q "^amdgpu" /etc/modules || echo amdgpu >> /etc/modules

# Rebuild initramfs so the change applies at boot
update-initramfs -u

# Load it now for the current session (don't wait until reboot)
modprobe amdgpu

# Verify
lsmod | grep amdgpu
# Expect: amdgpu     N      M
# (where N is the bound device count — should include both V620s)
```

**Important — three GPUs share amdgpu on this build.** The Ryzen 7600's integrated Raphael iGPU (`7e:00.0`) is also an amdgpu device. After amdgpu loads, `/dev/dri/` contains render nodes for all three GPUs (V620 #1, V620 #2, Raphael iGPU) — typically `renderD128`, `renderD129`, `renderD130`. The mapping is **not** guaranteed to be "V620s first, iGPU last" — it depends on PCIe enumeration order. Map them explicitly:

```bash
for r in /dev/dri/renderD*; do
  pci=$(readlink "/sys/class/drm/$(basename $r)/device" 2>/dev/null | awk -F/ '{print $NF}')
  device=$(lspci -nn -s "$pci" 2>/dev/null | grep -oE '\[[0-9a-f]{4}:[0-9a-f]{4}\]')
  echo "$r → $pci $device"
done
# Expect entries like:
#   /dev/dri/renderD128 → 0000:03:00.0 [1002:73a1]    V620 #1
#   /dev/dri/renderD129 → 0000:07:00.0 [1002:73a1]    V620 #2
#   /dev/dri/renderD130 → 0000:7e:00.0 [1002:164e]    Raphael iGPU (NOT to be passed to LXC 151)
```

When Phase 5 sets up GPU passthrough to LXC 151, it passes only the V620 render nodes (PCI device ID `1002:73a1`) — the script `scripts/51-lxc-amd.sh` does this filtering automatically; if you set up the LXC manually, exclude `renderD130` (or whichever node maps to the iGPU `7e:00.0`).

```bash
# If amdgpu still complains about missing firmware in dmesg, run:
#   apt install --reinstall pve-firmware
# DO NOT install firmware-amd-graphics from Debian non-free.
```

### Step 4.5 — Verify host sees all GPUs

```bash
# V620 server cards report PCI class 0380 ("Display controller") — NOT VGA (0300) or
# 3D controller (0302) — because they're headless inference cards with no display
# output. The naive `grep "vga|3d"` filter misses them entirely. Filter by AMD/ATI
# Navi 21 device ID (1002:73a1) or by name instead:
lspci -nn | grep -iE "navi 21|radeon pro v620|\[1002:73a1\]"
# Expect: two entries (one per V620). They appear behind Navi 21's internal PCIe
# switch — addresses typically 03:00.0 (V620 #1 behind 01:00.0/02:00.0) and 07:00.0
# (V620 #2 behind 05:00.0/06:00.0).

# Sanity: full AMD/ATI listing should also show the Raphael iGPU + switch fabric:
lspci -nn | grep -iE "amd|ati"

# Note: V620 has NO HDMI audio function (headless server card). The only AMD audio
# you'll see is the Raphael iGPU at 7e:00.1 + 7e:00.6. This is normal.
```

```bash
# AMD GPUs should have render nodes after the next reboot
# After amdgpu loads, check:
ls -l /dev/dri/
# Expect: card0, card1, renderD128, renderD129 (one pair per V620). No renderD130.
```

If the AMD GPUs aren't showing render nodes, reboot once more — the kernel module load order on a fresh install sometimes needs a reboot to settle.

### Step 4.6 — (Removed) NVIDIA driver install

This step previously installed the NVIDIA host driver for RTX 3060 passthrough. With the 3060 removed (see `local-gpu-cluster-v2.md` §1.3 for pivot rationale), no NVIDIA driver is required. The renumbering is intentional — Step 4.7 follows directly.

### Step 4.7 — Identify device major numbers

```bash
# AMD render and KFD nodes (note the major numbers on each line)
ls -l /dev/dri/render* /dev/kfd

# Example output:
# crw-rw---- 1 root render 226, 128 ... /dev/dri/renderD128   (V620 #1)
# crw-rw---- 1 root render 226, 129 ... /dev/dri/renderD129   (V620 #2)
# crw-rw-rw- 1 root render 234,   0 ... /dev/kfd
```

**Record the major numbers from your system** — they vary by kernel build:
- AMD render: typically `226`
- AMD KFD: typically `234`

You'll need these in Phase 5 when configuring LXC 151's GPU passthrough.

### Step 4.8 — Set up ZFS mirror for shared models

The two data NVMes in M.2_3 and M.2_4 are paired as a **ZFS mirror** (RAID-1 equivalent). Properties of this layout:
- **Redundancy:** survives single-drive failure. The remaining drive serves reads with no downtime; replace and `zpool replace` to rebuild.
- **Self-healing:** ZFS checksums every block. On a mirror, mismatched checksums between the two copies are auto-repaired from the good copy. Plain mdadm RAID-1 can detect mismatches but can't tell which copy is correct — ZFS can.
- **Snapshots:** instant, atomic, copy-on-write. Phase 9's backup strategy depends on `zfs snapshot` / `zfs send`.
- **Compression:** `lz4` saves ~30-50% on model files and config data with negligible CPU cost.
- **Usable capacity:** equals one drive (mirroring, not striping). Two 2 TB drives → 2 TB usable.

```bash
# Identify your two data NVMes (NOT the boot drive on nvme0n1)
lsblk -d -o NAME,SIZE,MODEL
# You should see three nvme devices — boot (smaller or matched) plus the two data drives.

# Confirm device IDs by-id (preferred over /dev/nvmeXnY which can re-enumerate)
ls -l /dev/disk/by-id/ | grep -i nvme | grep -v part
# Use the nvme-eui... or nvme-Model_Serial... entries below.

# Create the mirrored pool — ADJUST device paths to match your hardware
DATA_NVME_A=/dev/disk/by-id/nvme-XXXXXXXX   # ADJUST: first data drive (M.2_3)
DATA_NVME_B=/dev/disk/by-id/nvme-YYYYYYYY   # ADJUST: second data drive (M.2_4)

zpool create -o ashift=12 \
    -O compression=lz4 \
    -O atime=off \
    -O xattr=sa \
    tank mirror "$DATA_NVME_A" "$DATA_NVME_B"

# Create datasets
zfs create tank/models
zfs create tank/anythingllm
zfs create tank/mcp
zfs create tank/backups

# Per-dataset tuning — recordsize=1M is optimal for large sequential reads (GGUF models)
zfs set recordsize=1M tank/models

# Verify
zpool status tank
# Expect: pool: tank, state: ONLINE, config shows "mirror-0" with both drives ONLINE

zfs list
# Expect: tank, tank/models, tank/anythingllm, tank/mcp, tank/backups
```

**ZFS ARC tuning (important — boot drive is ext4, but `/tank` is ZFS):**

ZFS will use up to 50% of host RAM for ARC by default. On this 128 GB system that's 64 GB — more than you want, given LXCs need RAM for model inference. Cap ARC at a sensible value:

```bash
# Cap ARC at 16 GB (adjust based on your RAM and workload)
echo "options zfs zfs_arc_max=17179869184" > /etc/modprobe.d/zfs.conf
update-initramfs -u
# Takes effect on next reboot, or live with:
echo 17179869184 > /sys/module/zfs/parameters/zfs_arc_max
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
- [ ] No `nvidia-smi` on host (no NVIDIA hardware): `which nvidia-smi || echo OK`
- [ ] `ls /dev/dri/` shows renderD128, renderD129 (NOT renderD130 — 3060 absent)
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
  --rootfs local-lvm:64 \
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

# Build with HIP backend, targeting V620's gfx1030 architecture.
# Extra flags for V620-only build:
#   -DGGML_HIP_GRAPHS=ON  kernel-graph capture, ~1-3% throughput gain on gfx1030, no downside.
#   -DGGML_OPENMP=ON      better CPU-thread scaling on Ryzen 7600 prompt-eval paths.
#   -DLLAMA_BUILD_SERVER=ON  explicit (default in recent llama.cpp, but pin it).
HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
cmake -S . -B build \
    -DGGML_HIP=ON \
    -DGPU_TARGETS=gfx1030 \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLAMA_CURL=ON \
    -DLLAMA_BUILD_SERVER=ON \
    -DGGML_HIP_GRAPHS=ON \
    -DGGML_OPENMP=ON

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

The V620-only LXC hosts three llama-server processes (chat + embed + rerank). All four model files live in `/tank/models`:

| File | Repo | Approx size | Used by |
| --- | --- | --- | --- |
| Target: Qwen3.6-35B-A3B UD-Q4_K_M | `unsloth/Qwen3.6-35B-A3B-GGUF` | ~22 GB | `llamacpp-chat.service` (port 8080) |
| Draft: Qwen3-0.6B Q4_K_M (tokenizer-compatible with Qwen3.6) | `unsloth/Qwen3-0.6B-GGUF` | ~400 MB | `llamacpp-chat.service` (`--model-draft`) |
| Embedder: Qwen3-Embedding-0.6B Q8_0 | `Qwen/Qwen3-Embedding-0.6B-GGUF` | ~1.0 GB | `llamacpp-embed.service` (port 8082) |
| Reranker: BGE Reranker v2-m3 (GGUF) | see note below | ~1.5 GB | `llamacpp-rerank.service` (port 8083) |

**Reranker GGUF availability.** `BAAI/bge-reranker-v2-m3` doesn't always ship a pre-built GGUF on HuggingFace. Two paths:
- **(a)** convert via `convert_hf_to_gguf.py` from the llama.cpp source tree, OR
- **(b)** fall back to `Qwen/Qwen3-Reranker-0.6B-GGUF` which ships GGUF directly and works with `--reranking --pooling rank` identically.
Smoke-test the chosen reranker on V620 before the cutover (Phase 6 Step 6.1.5).

```bash
cd /tank/models

# Use llama-server's built-in HF downloader (recommended — handles multi-shard automatically)
# Or wget directly:

# Target: Qwen3.6 35B-A3B (MoE) UD-Q4_K_M (Unsloth Dynamic — single file, ~22 GB).
# Verify current file list at https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/tree/main
wget -O qwen3.6-35b-a3b-ud-q4_k_m.gguf \
  "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"

# Draft: Qwen 3.5 0.8B Q4_K_M
wget -O qwen3-0.6b-q4_k_m.gguf \
  "https://huggingface.co/unsloth/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q4_K_M.gguf"

# Embedder: Qwen3-Embedding-0.6B Q8_0 (uses --pooling last; do NOT use cls — see Step 5.11.2)
wget -O qwen3-embedding-0.6b-q8_0.gguf \
  "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf"

# Reranker: BGE v2-m3 (or Qwen3-Reranker-0.6B if BGE GGUF not available)
# Verify URL before running; both options shown:
# Option A — BGE Reranker v2-m3 (if a community GGUF exists):
# wget -O bge-reranker-v2-m3.gguf "<community-mirror-URL>"
# Option B — Qwen3-Reranker-0.6B (ships GGUF officially):
wget -O qwen3-reranker-0.6b-q8_0.gguf \
  "https://huggingface.co/Qwen/Qwen3-Reranker-0.6B-GGUF/resolve/main/Qwen3-Reranker-0.6B-Q8_0.gguf"

# Verify checksums against published values on the model card
sha256sum *.gguf

# Pin the embedder SHA — Phase 6 cutover compares against this to detect re-quantization
# that would invalidate AnythingLLM's existing vector DB.
sha256sum qwen3-embedding-0.6b-q8_0.gguf | awk '{print $1}' > /tank/models/.embedder-sha
cat /tank/models/.embedder-sha

chmod 644 *.gguf
```

**Alternative: use llama-server's built-in HF downloader.** Modern llama.cpp can download directly:

```bash
# Inside the LXC, the systemd unit can reference a HF repo:
# --hf-repo unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M --hf-repo-draft unsloth/Qwen3-0.6B-GGUF:Q4_K_M
# This auto-downloads to LLAMA_CACHE on first start.
```

**URL stability caveat:** Hugging Face URLs and exact quant filenames change as new quantization algorithms (e.g. Unsloth Dynamic 2.0) are released. Verify the current file list at `https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/tree/main` before running `wget`. The `UD-Q4_K_M` (Unsloth Dynamic) quant is recommended as a quality/size sweet spot — slightly better than vanilla Q4_K_M at the same ~22 GB size.

**About Qwen3.6 35B-A3B specifically:**
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
    --model /opt/models/qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf \
    --model-draft /opt/models/qwen3-0.6b-q4_k_m.gguf \
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
    --model /opt/models/qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf \
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
    --model /opt/models/qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf \
    -ngl all \
    -fa 0

# Benchmark with flash attention
./build/bin/llama-bench \
    --model /opt/models/qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf \
    -ngl all \
    -fa 1
```

Compare the `tg128` row's t/s value. Use whichever is higher in the production systemd unit. (As of llama.cpp main branch in early 2026, FA is often slower on V620 — but this evolves with each release, so verify on your specific build.)

### Step 5.11 — Generate API key + create three production systemd units

The V620 LXC hosts three llama-server processes:
- `llamacpp-chat.service` (port 8080, tensor-split across both V620s)
- `llamacpp-embed.service` (port 8082, pinned to V620 #1)
- `llamacpp-rerank.service` (port 8083, pinned to V620 #2)

All three share a single `LLAMACPP_API_KEY` (Bearer auth) and the same llama.cpp binary built in §5.6. Inside the LXC:

#### Step 5.11.1 — Generate LLAMACPP_API_KEY

```bash
# Idempotent — re-running does not duplicate the key
if ! grep -q "^LLAMACPP_API_KEY=" /etc/llamacpp.env 2>/dev/null; then
    echo "LLAMACPP_API_KEY=$(openssl rand -hex 32)" >> /etc/llamacpp.env
fi
chmod 600 /etc/llamacpp.env
chown root:root /etc/llamacpp.env

# Record the key for use in /etc/router.env (Phase 7.1.5) and AnythingLLM (Phase 8.6)
awk -F= '/^LLAMACPP_API_KEY=/{print "LLAMACPP_API_KEY=" $2}' /etc/llamacpp.env
```

#### Step 5.11.2 — Warm-up script (used by ExecStartPost on chat unit)

```bash
cat > /usr/local/bin/warm-chat.sh <<'EOF'
#!/bin/bash
# Fire a tiny completion after llamacpp-chat starts so the first user request
# doesn't pay the cold-start latency (model load from /tank takes ~10-30s on
# first run). Reads the API key from /etc/llamacpp.env.
sleep 5
. /etc/llamacpp.env
curl -s -m 30 http://localhost:8080/v1/chat/completions \
    -H "Authorization: Bearer ${LLAMACPP_API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"model":"x","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
    > /dev/null
EOF
chmod +x /usr/local/bin/warm-chat.sh
```

#### Step 5.11.3 — `llamacpp-chat.service` (port 8080, tensor-split)

Decide based on your benchmark (§5.10) whether to set `--flash-attn` to `on`, `off`, or leave at the default `auto`.

```bash
cat > /etc/systemd/system/llamacpp-chat.service <<'EOF'
[Unit]
Description=llama.cpp chat (V620 ROCm — Qwen3.6-35B-A3B UD-Q4_K_M + Qwen3-0.6B draft, tensor-split 1,1)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
EnvironmentFile=/etc/llamacpp.env
Environment="HIP_VISIBLE_DEVICES=0,1"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="HIP_FORCE_DEV_KERNARG=1"
Environment="GPU_MAX_HW_QUEUES=2"
Environment="GGML_HIP_UMA=0"
ExecStart=/opt/llama.cpp/build/bin/llama-server \
    --model /opt/models/qwen3.6-35b-a3b-ud-q4_k_m.gguf \
    --model-draft /opt/models/qwen3-0.6b-q4_k_m.gguf \
    --alias rag-qwen3.6 \
    --host 0.0.0.0 --port 8080 \
    --api-key ${LLAMACPP_API_KEY} \
    --ctx-size 131072 \
    --n-gpu-layers all \
    --n-gpu-layers-draft all \
    --tensor-split 1,1 \
    --threads 8 \
    --batch-size 512 --ubatch-size 512 \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --cont-batching \
    --parallel 4 \
    --cache-reuse 1024 \
    --mlock \
    --spec-draft-n-max 16 --spec-draft-n-min 0 \
    --flash-attn auto \
    --log-prefix \
    --metrics
ExecStartPost=/usr/local/bin/warm-chat.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Key flag rationale (V620-only adjustments):

- `--api-key ${LLAMACPP_API_KEY}` enforces Bearer auth on llama-server. The key is loaded via `EnvironmentFile=/etc/llamacpp.env` — never inline in the unit file (would leak via `systemctl show` + vzdump).
- `--parallel 4` enables 4 concurrent KV slots (vs. the old `--parallel 2`). Each slot at 128K ctx q8_0 ≈ 3-4 GB, so total ~16 GB of slot KV on top of the 22 GB weights. Fits comfortably in the 64 GB pool with ~25 GB headroom for embed + rerank co-tenants.
- `--cache-reuse 1024` (was 256) — system-prompt prefix reuse window of 1024 tokens. RAG system prompts are typically 1-4K tokens; the old 256-token window was sub-optimal. Adds ~1 slot's worth of KV but we have headroom.
- `--mlock` pins the model in VRAM, prevents demand paging under bulk-embed contention.
- `--log-prefix` writes prompt prefix to journald — required for spec-decode acceptance-rate scraping. Has privacy implications: see journald retention config below (Step 5.11.6).
- `HIP_FORCE_DEV_KERNARG=1` shaves ~2-5% off small-kernel paths on gfx1030.
- `GPU_MAX_HW_QUEUES=2` caps ROCm queue context-switching when 4 chat slots compete on each card.
- `--alias rag-qwen3.6` triggers the router's strip-thinking heuristic (`^rag-|-rag$`).
- **Optional:** `--reasoning-format deepseek` moves `<think>...</think>` to `message.reasoning_content`, simplifying the router. Default `auto` leaves them in `message.content`.

#### Step 5.11.4 — `llamacpp-embed.service` (port 8082, V620 #1)

```bash
cat > /etc/systemd/system/llamacpp-embed.service <<'EOF'
[Unit]
Description=llama.cpp embedder (V620 #1 — Qwen3-Embedding-0.6B Q8_0, --pooling last)
After=network-online.target llamacpp-chat.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
EnvironmentFile=/etc/llamacpp.env
Environment="HIP_VISIBLE_DEVICES=0"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="HIP_FORCE_DEV_KERNARG=1"
ExecStart=/opt/llama.cpp/build/bin/llama-server \
    --model /opt/models/qwen3-embedding-0.6b-q8_0.gguf \
    --alias qwen3-embed \
    --host 0.0.0.0 --port 8082 \
    --api-key ${LLAMACPP_API_KEY} \
    --main-gpu 0 \
    --n-gpu-layers all \
    --embeddings \
    --pooling last \
    --ctx-size 8192 \
    --cont-batching \
    --parallel 8 \
    --batch-size 2048 --ubatch-size 512 \
    --flash-attn off \
    --mlock \
    --metrics
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Critical correctness note: `--pooling last`, NOT `cls`. Qwen3-Embedding-0.6B uses the final `<|endoftext|>` token for pooling. Using `cls` produces semantically wrong embeddings and silently invalidates AnythingLLM's vector DB. Embedding dim = 1024.

#### Step 5.11.5 — `llamacpp-rerank.service` (port 8083, V620 #2)

**Important — match the model path to your chosen reranker GGUF.** If you picked Option A (BGE Reranker v2-m3) in Step 5.7, change `--model /opt/models/qwen3-reranker-0.6b-q8_0.gguf` below to `--model /opt/models/bge-reranker-v2-m3.gguf` (or whatever filename you actually downloaded). The default shown is Option B (Qwen3-Reranker-0.6B), which ships GGUF officially and is the fallback in the runbook.

```bash
cat > /etc/systemd/system/llamacpp-rerank.service <<'EOF'
[Unit]
Description=llama.cpp reranker (V620 #2 — Qwen3-Reranker-0.6B by default; BGE if downloaded)
After=network-online.target llamacpp-chat.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
EnvironmentFile=/etc/llamacpp.env
Environment="HIP_VISIBLE_DEVICES=1"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="HIP_FORCE_DEV_KERNARG=1"
ExecStart=/opt/llama.cpp/build/bin/llama-server \
    --model /opt/models/qwen3-reranker-0.6b-q8_0.gguf \
    --alias bge-rerank \
    --host 0.0.0.0 --port 8083 \
    --api-key ${LLAMACPP_API_KEY} \
    --main-gpu 1 \
    --n-gpu-layers all \
    --embeddings --pooling rank \
    --reranking \
    --ctx-size 8192 \
    --cont-batching \
    --parallel 4 \
    --flash-attn off \
    --mlock \
    --metrics
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Correctness note: `--reranking` opens the `/v1/rerank` endpoint, but the underlying mechanism requires `--embeddings --pooling rank` together (per llama.cpp upstream best practice). If you went with BGE Reranker GGUF instead of Qwen3-Reranker, update the `--model` path accordingly.

#### Step 5.11.6 — journald retention + SSH hardening

The chat unit's `--log-prefix` writes prompt content to journald. Bound the retention to avoid both disk fill and long-tail prompt exposure via `pct exec ... journalctl`:

```bash
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/retention.conf <<'EOF'
[Journal]
SystemMaxUse=500M
MaxRetentionSec=7day
EOF
systemctl restart systemd-journald

# SSH hardening — disable password auth + root password login.
# Push the Proxmox host's SSH public key into this LXC's authorized_keys before running these,
# or you'll lock yourself out. Use `pct exec 151 -- bash` to keep host-side access.
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh 2>/dev/null || systemctl restart sshd
```

#### Step 5.11.7 — Enable and start all three units

```bash
systemctl daemon-reload
systemctl enable --now llamacpp-chat llamacpp-embed llamacpp-rerank

# Cold-start load is dominated by reading the 22 GB chat model from /tank over PCIe.
# Wait up to 120s for all three to report `active`. Bail out with a journalctl pointer
# if any unit hasn't become active within that window.
for attempt in $(seq 1 24); do
  states=$(systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank 2>/dev/null | tr '\n' ' ')
  echo "[${attempt}/24] $states"
  echo "$states" | grep -qv "active" || break
  sleep 5
done
systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank
# Expect: three lines of "active"

# If any unit shows "failed" or "activating" after 120s, inspect:
journalctl -u llamacpp-chat --since "5 min ago" --no-pager | tail -40
```

The warm-up script (`/usr/local/bin/warm-chat.sh`, Step 5.11.2) sleeps 5s then fires a small completion. That's fine when systemd reaches `active` (which only happens after the model is loaded), because `ExecStartPost` runs after the model load. The 5s sleep is just slack for socket bind.

Watch logs to confirm clean startup:

```bash
journalctl -u llamacpp-chat -f
# Wait for: "main: server is listening on http://0.0.0.0:8080"
# Then Ctrl-C and check the other two:
journalctl -u llamacpp-embed --since "2 min ago" | tail -20
journalctl -u llamacpp-rerank --since "2 min ago" | tail -20
```

### Step 5.12 — Verify from outside the LXC

Find the LXC's IP:

```bash
pct exec 151 -- ip -4 addr show eth0 | grep inet | awk '{print $2}'
# Note this IP — likely 192.168.6.151 or DHCP-assigned
```

From the Proxmox host (or any LAN host that has the LLAMACPP_API_KEY):

```bash
LLAMACPP_AMD_IP=192.168.6.151   # adjust to your actual IP
LLAMACPP_KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)

# Chat (port 8080)
curl -sf -H "Authorization: Bearer $LLAMACPP_KEY" http://$LLAMACPP_AMD_IP:8080/v1/models | jq .

# Embedder (port 8082) — verify dim=1024 (--pooling last correctness)
curl -sf -X POST -H "Authorization: Bearer $LLAMACPP_KEY" -H "Content-Type: application/json" \
    -d '{"input":"hello"}' http://$LLAMACPP_AMD_IP:8082/v1/embeddings | jq '.data[0].embedding | length'
# Expect: 1024 — if you see a different dim, the embedder is using wrong pooling.

# Reranker (port 8083) — verify Paris ranks first for "capital of France"
curl -sf -X POST -H "Authorization: Bearer $LLAMACPP_KEY" -H "Content-Type: application/json" \
    -d '{"query":"capital of France","documents":["Paris is in France","Berlin is in Germany"]}' \
    http://$LLAMACPP_AMD_IP:8083/v1/rerank | jq '.results[0].document'
# Expect: "Paris is in France"

# Auth-gate test: request without Bearer should 403
curl -s -o /dev/null -w "%{http_code}\n" http://$LLAMACPP_AMD_IP:8080/v1/models
# Expect: 401 (llama-server returns 401 for missing auth, not 403)
```

**Stop and verify before proceeding:**
- [ ] `rocminfo` shows two `gfx1030` agents
- [ ] All three systemd units active: `pct exec 151 -- systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank` prints "active" three times
- [ ] Chat (8080), embed (8082), rerank (8083) all respond with valid `Authorization: Bearer` headers
- [ ] Unauthed request to any of the three returns 401 (auth gate works)
- [ ] Embedding dim returned is 1024 (confirms `--pooling last` is correct)
- [ ] Reranker scores "Paris is in France" higher than "Berlin is in Germany"
- [ ] Both GPUs split work during chat generation (`rocm-smi --showuse` shows activity on both)
- [ ] Speculative decoding acceptance rate >0.6 in `journalctl -u llamacpp-chat | grep acceptance`

### Step 5.13 — V620 fan control software bridge (Approach A from Step 1.9.1)

This sets up the systemd services that read V620 temperatures from inside LXC 151 and translate them into motherboard PWM duty cycle, giving you V620-temp-driven fan speed control. The motherboard PWM signal then feeds the **Lancool 217's built-in 6-channel PWM fan hub**, which mirrors the duty cycle to all connected fans (4× NF-A8 on V620 shrouds + the case's stock fans wired into the hub).

**Architecture:**
- Inside LXC 151: a small writer service polls `rocm-smi` every 5s and writes the max V620 edge temp to a shared file
- On the Proxmox host: a reader service polls that file and writes PWM duty cycle to the motherboard's hwmon endpoint (CHA_FAN3, where the Lancool hub master input is connected per Step 1.9.1)
- Bind mount: a host directory `/var/lib/v620-temps/` is mounted into the LXC at the same path
- Fan-out: the Lancool hub broadcasts the PWM signal to all connected fans (V620 NF-A8s + case stock fans, except top exhaust which is on a separate motherboard header per Step 1.10)

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
After=network-online.target llamacpp-chat.service
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

# After detection, find the right pwm endpoint(s) for the V620 shroud fans.
# Per Step 1.9.1 Approach A, the V620 shrouds plug into CHA_FAN4 + CHA_FAN5 (independent
# motherboard headers, NOT through the Lancool 217 hub). You need to identify which
# /sys/class/hwmon/hwmonX/pwmN file corresponds to each of those two headers.
sensors  # lists all hwmon devices + per-fan RPM (look for fan4_input, fan5_input)
ls /sys/class/hwmon/

# Typically on ASUS X870E you'll see hwmon entries like:
#   nct6798-isa-0290 (the SuperI/O fan/sensor chip)
# Probe each pwmN by switching to manual mode and listening:
echo 1 > /sys/class/hwmon/hwmon3/pwm5_enable   # adjust hwmon3/pwm5 — try CHA_FAN4 first
echo 64 > /sys/class/hwmon/hwmon3/pwm5         # 25% — listen for THAT shroud fan slowing
echo 255 > /sys/class/hwmon/hwmon3/pwm5        # 100% — listen for it ramping up
# Repeat for the other header (pwm6 if CHA_FAN5 lives there, etc.)
```

When you've identified both pwm files that drive the V620 shroud fans, note both full paths. Below we assume `/sys/class/hwmon/hwmon3/pwm5` and `/sys/class/hwmon/hwmon3/pwm6` — substitute yours.

**Step 5.13.4 — Install the host fan control bridge (writes to BOTH headers in lockstep).**

```bash
cat > /usr/local/bin/v620-fan-bridge.sh <<'EOF'
#!/bin/bash
# Read V620 max edge temp from LXC 151 (via shared bind mount) and write the
# same PWM duty cycle to every V620-shroud motherboard header.

TEMP_FILE="/var/lib/v620-temps/current-temp"
# List every PWM endpoint the V620 shroud fans live on. Both ramp in lockstep.
# If your build has only one PWM (Lancool 217 hub model), just list one.
PWMS=(
    /sys/class/hwmon/hwmon3/pwm5    # ADJUST: CHA_FAN4 (V620 #1 shroud fan)
    /sys/class/hwmon/hwmon3/pwm6    # ADJUST: CHA_FAN5 (V620 #2 shroud fan)
)

# Switch every PWM to manual mode
for p in "${PWMS[@]}"; do
    echo 1 > "${p}_enable" 2>/dev/null || true
done

while true; do
    if [ -r "$TEMP_FILE" ]; then
        TEMP=$(cat "$TEMP_FILE" 2>/dev/null)
        TEMP=${TEMP:-65}  # safe default if read fails

        # Map temp → PWM duty (0-255). Same curve applies to all V620 PWMs.
        if   [ "$TEMP" -lt 50 ]; then PWM_VAL=64    # ~25% — quiet idle
        elif [ "$TEMP" -lt 60 ]; then PWM_VAL=102   # ~40%
        elif [ "$TEMP" -lt 70 ]; then PWM_VAL=153   # ~60%
        elif [ "$TEMP" -lt 80 ]; then PWM_VAL=204   # ~80%
        else                          PWM_VAL=255   # 100% — full cooling
        fi
    else
        # Temp file missing (LXC down) → safe fail-over to 75%
        PWM_VAL=192
    fi

    for p in "${PWMS[@]}"; do
        echo "$PWM_VAL" > "$p"
    done
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
# Read every PWM the bridge is driving (per Step 5.13.4 you have a PWMS=(...) array):
cat /sys/class/hwmon/hwmon3/pwm5       # current PWM duty on CHA_FAN4 (0-255)
cat /sys/class/hwmon/hwmon3/pwm6       # current PWM duty on CHA_FAN5 (0-255)
# Confirm both fans are responding (RPM should track PWM):
sensors | grep -E "fan4|fan5"          # nct67xx labels for CHA_FAN4 / CHA_FAN5

# Stress test: trigger heavy V620 load and watch fans ramp
pct exec 151 -- bash -c 'cd /opt/llama.cpp && ./build/bin/llama-bench -m /opt/models/qwen3.6-35b-a3b-ud-q4_k_m.gguf -ngl 999 -t 4 -n 128 -p 512'
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

## Phase 6 — Cutover (live migration from running v2 cluster)

> ### ⚠️ BLOCKER — One remaining upstream dependency before Phase 6 cutover is safe
>
> ~~1. Phase 5 expansion.~~ ✅ **DONE** (Phase 5 now defines `llamacpp-chat.service`, `llamacpp-embed.service`, `llamacpp-rerank.service` with `EnvironmentFile=/etc/llamacpp.env` and `--api-key`).
>
> 2. **Phase 7 router-app.py rewrite — still required.** Phase 7's Step 7.3 currently ships `router-app.py` with `FAST_URL` / `FAST_ALIASES` (referenced the deleted 3060 fast-chat service) and no auth middleware. Phase 6's cutover assumes the rewritten version: drop `FAST_URL`/`FAST_ALIASES`, add `Authorization: Bearer` middleware (validates `ROUTER_API_KEY`), add `httpx` headers `Authorization: Bearer ${LLAMACPP_API_KEY}` on upstream calls, add `asyncio.Semaphore` admission control (chat=1, embed=4), add `prometheus-fastapi-instrumentator`, add `slowapi` per-IP rate limit, add fail-open SSE on upstream 5xx, restrict `/metrics` to allowlist IPs. Until this is done, Phase 6 Step 6.2.E (router restart picks up new code) is a no-op — the router will keep routing as if the 3060 LXC still exists. Tracked as todos #18 (scripts/53-lxc-router.sh) and #20 (scripts/files/router-app.py) in the pivot plan.
>
> ~~3. Phase 11 verification suite.~~ ✅ **PARTIAL** — Step 6.3 below includes inline fallback verification tests (auth gates, embed dim, rerank correctness, concurrent VRAM, rate limit). Full Phase 11 expansion is a separate todo (#13) but Phase 6 is no longer blocked on it.
>
> See `C:\Users\willi\.claude\plans\we-will-pivot-to-squishy-lampson.md` for full unit contents, router code requirements, and verification checklists.

**When to run this phase:** Only when migrating an EXISTING running cluster from "2× V620 + 1× 3060" to "2× V620 only". For fresh builds, all required services (chat + embed + rerank) are deployed during Phase 5 — skip directly from Phase 5 to Phase 7.

**Estimated time:** 1 hour, with ~30 seconds of chat-client SSE reconnect during the cutover window. Add ~1 hour for the optional bulk re-embed (Step 6.6) if the embedder GGUF changed.

The cluster previously ran a separate "3060 LXC" (`llamacpp-nv`, VMID 152) hosting the embedder + reranker + a fast-chat service. With the pivot to V620-only, those workloads moved into LXC 151 as additional `llama-server` processes pinned per-card via `--main-gpu`. This phase walks through a non-destructive cutover, with rollback path if anything goes wrong.

### Step 6.1 — Pre-cutover preparation (no service impact)

```bash
# 1. Snapshot the existing AnythingLLM vector DB
zfs snapshot tank/anythingllm@pre-pivot-$(date +%Y%m%d-%H%M)

# 2. Final backup of LXC 152 (for rollback). Verify the archive is accessible.
PRE_CUTOVER_BACKUP=$(vzdump 152 --storage tank-backups --mode snapshot --compress zstd 2>&1 | \
    awk -F"'" '/creating vzdump archive/{print $2}')
echo "Pre-cutover backup: $PRE_CUTOVER_BACKUP"
ls -lh "$PRE_CUTOVER_BACKUP" || { echo "Backup archive missing — abort"; exit 1; }

# 3. Verify embedder GGUF SHA stability (vector DB depends on this being byte-identical).
#    The result is written to /tank/.phase6-embed-trigger (on ZFS — survives reboot) so
#    Step 6.6 can read it later, possibly hours/days after Step 6.1.
EMBEDDER_FILE=/tank/models/qwen3-embedding-0.6b-q8_0.gguf
EXISTING_SHA=$(sha256sum "$EMBEDDER_FILE" 2>/dev/null | awk '{print $1}')
echo "Existing embedder SHA: ${EXISTING_SHA:-<absent>}"
# Compare against the canonical SHA recorded in /tank/models/.embedder-sha (set on first download).
CANONICAL_SHA=$(cat /tank/models/.embedder-sha 2>/dev/null)
if [ -z "$EXISTING_SHA" ] || [ -z "$CANONICAL_SHA" ] || [ "$EXISTING_SHA" != "$CANONICAL_SHA" ]; then
    echo "EMBED_GGUF_CHANGED=true" > /tank/.phase6-embed-trigger
    echo "Embedder SHA differs from canonical. Step 6.6 (re-embed) is REQUIRED."
else
    echo "EMBED_GGUF_CHANGED=false" > /tank/.phase6-embed-trigger
    echo "Embedder unchanged. Step 6.6 (re-embed) is SKIPPABLE."
fi

# 4. Generate API keys (idempotent — re-running does not corrupt existing keys).
pct exec 151 -- bash -c '
  mkdir -p /etc
  if ! grep -q "^LLAMACPP_API_KEY=" /etc/llamacpp.env 2>/dev/null; then
    echo "LLAMACPP_API_KEY=$(openssl rand -hex 32)" >> /etc/llamacpp.env
  fi
  chmod 600 /etc/llamacpp.env
  chown root:root /etc/llamacpp.env
'
pct exec 153 -- bash -c '
  mkdir -p /etc
  if ! grep -q "^ROUTER_API_KEY=" /etc/router.env 2>/dev/null; then
    echo "ROUTER_API_KEY=$(openssl rand -hex 32)" >> /etc/router.env
  fi
  chmod 600 /etc/router.env
  chown root:root /etc/router.env
'
# Copy LLAMACPP_API_KEY to LXC 153 (router needs it to authenticate upstream calls).
# Idempotent: replace existing line if present, otherwise append.
LLAMACPP_KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)
pct exec 153 -- bash -c "
  if grep -q '^LLAMACPP_API_KEY=' /etc/router.env 2>/dev/null; then
    sed -i 's|^LLAMACPP_API_KEY=.*|LLAMACPP_API_KEY=$LLAMACPP_KEY|' /etc/router.env
  else
    echo 'LLAMACPP_API_KEY=$LLAMACPP_KEY' >> /etc/router.env
  fi
"

# 5. Smoke-test reranker endpoint manually before relying on it for cutover.
#    Requires llamacpp-rerank.service to exist in LXC 151 (Phase 5 expansion delivers this).
RKEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)
pct exec 151 -- curl -sf -X POST -H "Authorization: Bearer $RKEY" \
    -H "Content-Type: application/json" \
    -d '{"query":"capital of France","documents":["Paris is in France","Berlin is in Germany"]}' \
    http://localhost:8083/v1/rerank | head -c 200
# Expect: JSON with "results" array; "Paris is in France" should score higher.

# 6. MCP audit: detect AND remediate hardcoded 192.168.6.152 / port 8081 references.
#    All commands run INSIDE LXC 155 (the file list lives in its /tmp, not the host's).
if pct exec 155 -- grep -rqE "192\.168\.6\.152|llamacpp-nv|:8081" /opt/ /etc/ 2>/dev/null; then
    echo "MCP has stale references — applying rewrites:"
    pct exec 155 -- bash -c '
      grep -rlE "192\.168\.6\.152|llamacpp-nv|:8081" /opt/ /etc/ 2>/dev/null > /tmp/mcp-files-to-fix
      cat /tmp/mcp-files-to-fix
      while read -r f; do
        [ -n "$f" ] && sed -i "s|192\.168\.6\.152|192.168.6.151|g; s|llamacpp-nv|llamacpp-amd|g; s|:8081|:8082|g" "$f"
      done < /tmp/mcp-files-to-fix
      cd /opt/mcp-stack && docker compose restart || true
    '
else
    echo "MCP audit clean — no stale references."
fi

# 7. Record AnythingLLM state — does it talk to router or direct to 152?
#    Use the AnythingLLM API rather than reading the sqlite file (avoids file-lock risk).
ALLM_KEY=$(pct exec 154 -- awk -F= '/^ANYTHINGLLM_API_KEY=/{sub(/^ANYTHINGLLM_API_KEY=/,""); gsub(/^"|"$/,""); print}' /opt/anythingllm/.env 2>/dev/null)
pct exec 154 -- curl -sf -H "Authorization: Bearer $ALLM_KEY" \
    http://localhost:3001/api/v1/system | python3 -c '
import sys, json
data = json.load(sys.stdin)
for k, v in data.items():
    if isinstance(v, str) and ("192.168.6." in v or "http" in v):
        print(f"{k}: {v}")
' 2>/dev/null || echo "Could not read AnythingLLM state — verify manually via UI Settings → AI Providers"
```

### Step 6.2 — Cutover (chat clients see brief SSE reconnect)

```bash
# A. LXC 151: install the new embed + rerank systemd units (requires Phase 5 expansion — see BLOCKER above).
pct exec 151 -- systemctl daemon-reload

# B. LXC 153: update router env file with new URLs (idempotent).
pct exec 153 -- bash -c '
  cat > /tmp/router-env-new <<EOF
V620_URL=http://192.168.6.151:8080
EMBED_URL=http://192.168.6.151:8082
RERANK_URL=http://192.168.6.151:8083
CHAT_CONCURRENCY=1
EMBED_CONCURRENCY=4
MAX_CHAT_INPUT_TOKENS=100000
MAX_EMBED_INPUT_TOKENS=8192
RATE_LIMIT_CHAT=60/minute
RATE_LIMIT_EMBED=200/minute
METRICS_ALLOWED_IPS=127.0.0.1,192.168.6.150
EOF
  # Preserve existing API key lines
  grep -E "^(ROUTER_API_KEY|LLAMACPP_API_KEY)=" /etc/router.env >> /tmp/router-env-new
  install -m 600 -o root -g root /tmp/router-env-new /etc/router.env
'
# Do not restart router yet — wait until embed/rerank services on 151 are up (Step D).

# C. LXC 153: install new router-app.py with auth middleware + semaphores + Prometheus.
#    This is deployed by re-running scripts/53-lxc-router.sh from the host (Phase 7 provisioning).
#    The script is idempotent — running it overwrites router-app.py and restarts the service.
#    REQUIRES: scripts/files/router-app.py has been rewritten with the new auth/admission code
#    AND scripts/53-lxc-router.sh deploys it. Both are listed in the BLOCKER notice above.
SCRIPTS_DIR="${SCRIPTS_DIR:-/root/local-gpu-cluster/scripts}"   # adjust to your clone location
[ -f "$SCRIPTS_DIR/53-lxc-router.sh" ] || { echo "scripts dir not found: $SCRIPTS_DIR"; exit 1; }
bash "$SCRIPTS_DIR/53-lxc-router.sh" 153

# D. Start the new embed + rerank services on 151.
pct exec 151 -- systemctl start llamacpp-embed llamacpp-rerank
sleep 10
pct exec 151 -- systemctl is-active llamacpp-embed llamacpp-rerank
# Re-derive LLAMACPP_KEY in case Step 6.1 and Step 6.2 are running in separate shell sessions
LLAMACPP_KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)
# Confirm /health endpoints respond before continuing
for port in 8082 8083; do
  pct exec 151 -- curl -sf -H "Authorization: Bearer $LLAMACPP_KEY" \
      "http://localhost:${port}/health" || { echo "Port $port not healthy — abort cutover"; exit 1; }
done

# E. Restart router — routes now hit 151. Chat unit is still using old flags (no API key);
#    new router would 401 on upstream calls without the LLAMACPP_API_KEY upgrade in step F.
#    To avoid a window of broken auth, run E and F as a tight pair below.
pct exec 153 -- systemctl restart llm-router

# F. Restart chat on 151 IMMEDIATELY after the router restart. This picks up
#    --api-key, --parallel 4, --cache-reuse 1024, --mlock, etc. Chat clients see
#    a ~30s SSE disconnect during F; router's degraded-mode event is emitted.
pct exec 151 -- systemctl restart llamacpp-chat

# G. AnythingLLM (LXC 154): fix the ALLM_LLM_TOKEN_LIMIT mismatch
#    Previously 262144 (256K) which exceeds llama-server's --ctx-size 131072 (128K).
#    Result: silent chat failure mid-conversation at overflow.
pct exec 154 -- sed -i 's/^ALLM_LLM_TOKEN_LIMIT=.*/ALLM_LLM_TOKEN_LIMIT=131072/' /opt/anythingllm/.env
# Update embedder URL if previously direct-to-152 (Step 6.1.7 informed which).
pct exec 154 -- sed -i 's|http://192\.168\.6\.152:8082|http://192.168.6.151:8082|g' /opt/anythingllm/.env
pct exec 154 -- bash -c 'cd /opt/anythingllm && docker compose restart'

# H. MCP (LXC 155): if Step 6.1.6 applied rewrites, the restart already happened there.
#    Otherwise this is a no-op.
```

### Step 6.3 — Run Phase 11 verification (auth gates, RAG smoke test, concurrent VRAM)

If Phase 11 passes, proceed to Step 6.5 (decommission) after a 24-hour soak. If it fails, proceed to Step 6.4 (rollback) before anything is destroyed.

> **If Phase 11 hasn't been expanded yet** (it's listed in the BLOCKER above), here is the minimum manual checklist to run before considering the cutover stable:
>
> ```bash
> # 1. Auth gate: router refuses requests without Bearer key
> curl -s -o /dev/null -w "%{http_code}\n" http://192.168.6.153:8000/v1/chat/completions
> # Expect: 403
>
> # 2. Auth gate: router accepts requests with key
> ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)
> curl -sf -X POST -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
>   -d '{"model":"qwen3-35b","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
>   http://192.168.6.153:8000/v1/chat/completions
>
> # 3. Embedding dim correctness (1024 = correct --pooling last on Qwen3-Embedding-0.6B)
> curl -sf -X POST -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
>   -d '{"input":"hello"}' http://192.168.6.153:8000/v1/embeddings | jq '.data[0].embedding | length'
> # Expect: 1024
>
> # 4. Rerank correctness
> curl -sf -X POST -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
>   -d '{"query":"capital of France","documents":["Paris is in France","Berlin is in Germany"]}' \
>   http://192.168.6.153:8000/v1/rerank | jq '.results[0].document'
> # Expect: "Paris is in France"
>
> # 5. Concurrent-load VRAM (chat stream + 100 embed batch overlap)
> KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)
> pct exec 151 -- bash -c "
>   curl -s -H 'Authorization: Bearer $KEY' http://localhost:8080/v1/chat/completions -X POST \
>     -H 'Content-Type: application/json' \
>     -d '{\"messages\":[{\"role\":\"user\",\"content\":\"essay\"}],\"stream\":true,\"max_tokens\":200}' &
>   sleep 2
>   for i in \$(seq 1 100); do
>     curl -s -H 'Authorization: Bearer $KEY' http://localhost:8082/v1/embeddings -X POST \
>       -H 'Content-Type: application/json' -d \"{\\\"input\\\":\\\"d \$i\\\"}\" > /dev/null &
>   done
>   wait
>   rocm-smi --showmeminfo vram --showuse
> "
> # Expect both V620s at 18-20 GB, > 80% util during overlap; no OOM in journalctl
>
> # 6. No errors in last 30 min
> pct exec 151 -- journalctl -u llamacpp-chat -u llamacpp-embed -u llamacpp-rerank --since "30 min ago" \
>   | grep -iE "oom|hip error|out of memory" || echo "OK — clean"
> ```

### Step 6.4 — Rollback (if verification fails)

```bash
# 1. Revert router env to point at 192.168.6.152.
pct exec 153 -- sed -i '
  s|^V620_URL=.*|V620_URL=http://192.168.6.151:8080|;
  s|^EMBED_URL=.*|EMBED_URL=http://192.168.6.152:8082|;
  s|^RERANK_URL=.*|RERANK_URL=http://192.168.6.152:8083|
' /etc/router.env
pct exec 153 -- systemctl restart llm-router

# 2. Stop new services on 151 (LXC 152 still owns embed + rerank duties)
pct exec 151 -- systemctl stop llamacpp-embed llamacpp-rerank

# 3. Revert chat unit flags — the additive flags (--parallel, --cache-reuse, --mlock,
#    --log-prefix) are safe even pre-cutover, but --api-key requires clients to send it.
#    If your pre-cutover clients didn't use API keys, restart chat without --api-key:
pct exec 151 -- bash -c 'systemctl revert llamacpp-chat 2>/dev/null; systemctl daemon-reload'
pct exec 151 -- systemctl restart llamacpp-chat

# 4. AnythingLLM token limit can stay at 131072 (it's a bug fix either way).
#    Embedder URL: if reverting, also revert to 192.168.6.152:8082.

# LXC 152 was not stopped during cutover, so the cluster returns to pre-cutover state.
echo "Rollback complete. Run Phase 11 verification again to confirm."
```

### Step 6.5 — Decommission LXC 152 (only after 24h green metrics)

```bash
# Soak check before stopping
pct exec 153 -- journalctl -u llm-router --since "24 hours ago" | grep -iE "error|fail|5[0-9]{2}" | wc -l
# Expect: very low count (< 10 over 24h)

# Stop LXC 152
pct stop 152
# Verify the cluster still works (chat / embed / rerank all healthy via router) for 1 hour
sleep 3600   # or wait manually

# FINAL archive before destroy — verify it's readable
FINAL_BACKUP=$(vzdump 152 --storage tank-backups --mode snapshot --compress zstd 2>&1 | \
    awk -F"'" '/creating vzdump archive/{print $2}')
echo "Final archive: $FINAL_BACKUP"
ls -lh "$FINAL_BACKUP" || { echo "Final archive missing — DO NOT destroy 152"; exit 1; }

# Last chance to abort
read -r -p "Destroy LXC 152 permanently? Type EXACTLY 'destroy 152' to confirm: " CONFIRM
[ "$CONFIRM" = "destroy 152" ] || { echo "Aborted."; exit 0; }
pct destroy 152
pct list | awk '$1 ~ /^(151|153|154|155)$/'   # expect 4 entries, no 152

# Remove 152 from any UI-configured backup jobs:
#   Datacenter → Backup → edit any job with VMID list containing 152
# Remove 152 from any DNS / /etc/hosts entries on management workstations:
#   sed -i '/llamacpp-nv/d' /etc/hosts   (on each client)
```

### Step 6.6 — Bulk re-embed (only if Step 6.1.3 detected an embedder GGUF change)

```bash
# Read the trigger flag set in Step 6.1.3
source /tank/.phase6-embed-trigger 2>/dev/null
if [ "$EMBED_GGUF_CHANGED" != "true" ]; then
    echo "Embedder unchanged — skipping re-embed."
else
    echo "Embedder changed — re-embed required. Procedure:"
    # Pre-snapshot already taken in Step 6.1.1
    # 1. In AnythingLLM UI: Workspaces → each workspace → delete all documents
    # 2. Re-ingest from source via Phase 10.4 procedure
    # 3. Verify a known query returns expected citation
    # Expected time: ~15-30 min for 5K docs on V620 with --parallel 8
    # Note: chat history citations from before the re-embed may become unstable

    # Update the canonical SHA pin so future cutovers don't re-trigger
    sha256sum /tank/models/qwen3-embedding-0.6b-q8_0.gguf | awk '{print $1}' > /tank/models/.embedder-sha
fi
```

### Step 6.7 — Hardware decommission (next planned power-down window)

```bash
# Power off the host. Disconnect the 8-pin PCIe power cable that fed PCIE_3 (3060).
# If your previous build had a third G205 brace supporting the 3060, remove it now
# (V620-only builds use 2× G205 + 1× built-in case bracket — see Step 1.8).
# Remove the RTX 3060 from PCIE_3.
# Power on.

# Verify
lspci | grep -i vga
# Expect: only 2x AMD V620 entries. No NVIDIA device.
```

PCIE_3 (PCIe 4.0 x4 from chipset) is now empty. Prioritized future-use options, by likelihood:
1. **Dual-port 10 GbE NIC** (Intel X710, ~$100 used) — for cluster federation if a second host is added.
2. **HBA** (LSI 9300-8i, ~$50) — only if `/tank` outgrows its two NVMes.
3. **PCIe USB-C / audio capture** — for a future local-Whisper LXC's audio ingest.
4. **OCuLink adapter for external GPU** — niche; defers cooling/power to a separate rail.

Populating PCIE_3 does not affect the V620 PCIe lanes (they're on CPU-attached PCIE_1 + PCIE_2).

---

## Phase 7 — Deploy the Router LXC (153 — `llm-router`)

**Estimated time:** 30 minutes

### Step 7.1 — Create the LXC

```bash
pct create 153 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst \
  --hostname llm-router \
  --cores 2 \
  --memory 4096 \
  --rootfs local-lvm:8 \
  --net0 name=eth0,bridge=vmbr0,firewall=0,ip=dhcp,type=veth \
  --features nesting=0 \
  --unprivileged 1 \
  --ostype ubuntu \
  --start 1
```

### Step 7.1.5 — Generate API keys (fresh builds only — skip if you ran Phase 6)

Phase 6's cutover generates `/etc/llamacpp.env` (on LXC 151) and `/etc/router.env` (on LXC 153) as part of pre-cutover prep. For **fresh builds that never touch Phase 6**, the same keys must be generated here so the router has an upstream-auth key (LLAMACPP_API_KEY) and clients have a router-auth key (ROUTER_API_KEY). Run from the Proxmox host:

```bash
# 1. LLAMACPP_API_KEY lives on LXC 151 (used by llama-server units in Phase 5 + by router for upstream)
pct exec 151 -- bash -c '
  mkdir -p /etc
  if ! grep -q "^LLAMACPP_API_KEY=" /etc/llamacpp.env 2>/dev/null; then
    echo "LLAMACPP_API_KEY=$(openssl rand -hex 32)" >> /etc/llamacpp.env
  fi
  chmod 600 /etc/llamacpp.env
  chown root:root /etc/llamacpp.env
'

# 2. ROUTER_API_KEY lives on LXC 153 (used by AnythingLLM + any other clients)
pct exec 153 -- bash -c '
  mkdir -p /etc
  if ! grep -q "^ROUTER_API_KEY=" /etc/router.env 2>/dev/null; then
    echo "ROUTER_API_KEY=$(openssl rand -hex 32)" >> /etc/router.env
  fi
  chmod 600 /etc/router.env
  chown root:root /etc/router.env
'

# 3. Copy LLAMACPP_API_KEY to LXC 153 (router authenticates upstream calls with it).
#    Idempotent: replace if present, else append.
#    Quoting note: the outer bash -c "..." is double-quoted, so the host shell
#    expands $LLAMACPP_KEY BEFORE passing the string to pct exec. The inner single
#    quotes around sed/echo arguments are processed by the LXC's bash AFTER the
#    host expansion already replaced $LLAMACPP_KEY with its value. Both echo and
#    sed see the literal key value, not the variable name.
LLAMACPP_KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)
pct exec 153 -- bash -c "
  if grep -q '^LLAMACPP_API_KEY=' /etc/router.env 2>/dev/null; then
    sed -i 's|^LLAMACPP_API_KEY=.*|LLAMACPP_API_KEY=$LLAMACPP_KEY|' /etc/router.env
  else
    echo 'LLAMACPP_API_KEY=$LLAMACPP_KEY' >> /etc/router.env
  fi
"
```

**Record these keys somewhere safe** — you'll paste `ROUTER_API_KEY` into AnythingLLM's UI in Phase 8.6.

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
  /opt/llm-router/venv/bin/pip install \
    fastapi "uvicorn[standard]" httpx \
    slowapi prometheus-fastapi-instrumentator
'
```

### Step 7.3 — Deploy the router application

Create `/opt/llm-router/app.py` with the corrected logic (note: removed cross-LXC speculative decoding, since spec decode is now fully local on the V620 LXC):

```bash
cat > /opt/llm-router/app.py <<'PYEOF'
"""
LLM cluster router (V620-only).
- /v1/chat/completions       -> V620 chat on LXC 151 (port 8080) with in-process speculative decoding
- /v1/embeddings             -> V620 embedder on LXC 151 (port 8082, --main-gpu 0, --pooling last)
- /v1/rerank                 -> V620 reranker on LXC 151 (port 8083, --main-gpu 1)
- SSE keepalive on streaming responses
- Per-request <think> block decision (header > body > system prompt > model name > default)
- NOTE: This is the pre-pivot router code. The full V620-only rewrite (Bearer auth,
  asyncio.Semaphore admission control, prometheus-fastapi-instrumentator, slowapi
  rate limit, fail-open SSE) is delivered by scripts/files/router-app.py — that
  version overwrites this file at deploy time. The skeleton below is illustrative.
"""

import asyncio
import os
import re
import time

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import StreamingResponse, JSONResponse

V620_URL    = os.environ.get("V620_URL",   "http://192.168.6.151:8080")
EMBED_URL   = os.environ.get("EMBED_URL",  "http://192.168.6.151:8082")
RERANK_URL  = os.environ.get("RERANK_URL", "http://192.168.6.151:8083")
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
# Defaults below are overridden by EnvironmentFile values when /etc/router.env exists.
# Phase 7.1.5 creates it for fresh builds; Phase 6.1.4 creates it for cutover migrations.
# The full env contract is in /etc/router.env:
#   V620_URL, EMBED_URL, RERANK_URL, KEEPALIVE_INTERVAL,
#   ROUTER_API_KEY, LLAMACPP_API_KEY  (auth — router uses LLAMACPP_API_KEY for upstream calls),
#   CHAT_CONCURRENCY, EMBED_CONCURRENCY, MAX_CHAT_INPUT_TOKENS, MAX_EMBED_INPUT_TOKENS,
#   RATE_LIMIT_CHAT, RATE_LIMIT_EMBED, METRICS_ALLOWED_IPS  (admission + rate limit + metrics auth)
Environment="V620_URL=http://192.168.6.151:8080"
Environment="EMBED_URL=http://192.168.6.151:8082"
Environment="RERANK_URL=http://192.168.6.151:8083"
Environment="KEEPALIVE_INTERVAL=12"
EnvironmentFile=-/etc/router.env
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
  --rootfs local-lvm:32 \
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

> **Prerequisites for this step:**
> - Phase 7 router LXC (153) is deployed and `curl -sf http://192.168.6.153:8000/healthz` returns 200.
> - Phase 5 V620 LXC (151) has all three services running: `pct exec 151 -- systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank` should print "active" three times.
> - `ROUTER_API_KEY` is set in `/etc/router.env` on LXC 153 (generated in Phase 7.1.5 for fresh builds, Phase 6.1.4 for cutover migrations).
> - `LLAMACPP_API_KEY` is set in `/etc/llamacpp.env` on LXC 151 (same generation steps).

In AnythingLLM UI: **Settings → AI Providers → LLM Preference**

- Provider: **Generic OpenAI**
- Base URL: `http://192.168.6.153:8000/v1` (the router on LXC 153)
- API Key: the value of `ROUTER_API_KEY` from `/etc/router.env` on LXC 153 — required (the rewritten V620-only router enforces Bearer auth; a placeholder like `sk-anything` returns 403). AnythingLLM's Generic OpenAI provider accepts the raw hex key as-is; no `sk-` prefix needed.
- Chat Model Name: query the actual model id. Two equivalent ways:
   - Via router (matches AnythingLLM's traffic path): `curl -H "Authorization: Bearer $ROUTER_API_KEY" http://192.168.6.153:8000/v1/models | jq -r .data[0].id`
   - Direct to llama-server (verification only — bypasses router): `curl -H "Authorization: Bearer $LLAMACPP_API_KEY" http://192.168.6.151:8080/v1/models | jq -r .data[0].id` using the LLAMACPP_API_KEY from `/etc/llamacpp.env` inside LXC 151
- Token context window: `131072` — **must match llama.cpp's `--ctx-size 131072`** (the V620 chat unit's context window). Values higher cause silent chat failure mid-conversation when the request exceeds llama.cpp's actual context.
- Max Tokens: `8192`

> ⚠️ **Important — script-vs-config mismatch warning:** The provisioning script `scripts/54-lxc-anythingllm.sh` currently sets `ALLM_LLM_TOKEN_LIMIT=262144` in `/opt/anythingllm/.env` (a leftover from when llama.cpp was configured with a 256K context). When you set "131072" in the UI above, AnythingLLM persists the UI value in its database and the UI wins at runtime. However, if the AnythingLLM container is recreated (e.g., `docker compose down && up`), it re-bootstraps from `.env` and reverts to 262144. **Either** (a) fix the script first by editing line 36 to `ALLM_LLM_TOKEN_LIMIT=131072` and re-running it, **or** (b) edit `/opt/anythingllm/.env` directly post-deploy. The runbook's Phase 6.2.G cutover step (Step G in Step 6.2) automates option (b) with a one-shot `sed` command for existing v2 clusters.

Click **Save Changes**.

**Settings → AI Providers → Embedder Preference**:

- Provider: **Generic OpenAI**
- Base URL: `http://192.168.6.153:8000/v1` (router forwards `/v1/embeddings` to V620 LXC 151 port 8082). The router authenticates its upstream call using `LLAMACPP_API_KEY` (loaded from `/etc/router.env` — Phase 7.1.5 for fresh builds, Phase 6.1.4 for cutover migrations copies that key into the router's env). Clients (including AnythingLLM) authenticate to the router using `ROUTER_API_KEY`.
- API Key: the value of `ROUTER_API_KEY` from `/etc/router.env` on LXC 153 (generated in Phase 7.1.5 for fresh builds or Phase 6.1.4 for cutover migrations)
- Embedding Model Name: query the actual model id. Two equivalent ways:
   - Via router: `curl -H "Authorization: Bearer $ROUTER_API_KEY" http://192.168.6.153:8000/v1/models | jq -r '.data[] | select(.id | ascii_downcase | contains("embed")) | .id'` (filter for embedder among multi-service router responses; portable across jq versions)
   - Direct to embedder (verification): `curl -H "Authorization: Bearer $LLAMACPP_API_KEY" http://192.168.6.151:8082/v1/models | jq -r .data[0].id`
- Embedding dimension: **1024** (Qwen3-Embedding-0.6B with `--pooling last` → confirms correct pooling — if you see a different dim, the embedder unit is misconfigured)
- Max embed chunk length: `2500`

Click **Save Changes**.

**Settings → AI Providers → Reranker** (if AnythingLLM exposes this option in your version):

- Provider: **Generic OpenAI**
- Base URL: `http://192.168.6.153:8000/v1` (router forwards `/v1/rerank` to V620 LXC 151 port 8083)
- API Key: same `ROUTER_API_KEY` as LLM Preference
- Reranker Model Name: `bge-reranker-v2-m3` (or whatever model id `curl -H "Authorization: Bearer $LLAMACPP_API_KEY" http://192.168.6.151:8083/v1/models | jq -r .data[0].id` returns)

> **Note:** Not all AnythingLLM versions expose a dedicated Reranker provider in the UI. If yours doesn't, the `/v1/rerank` endpoint is still available to direct callers (router, MCP tools) — it's just not auto-wired into AnythingLLM's RAG pipeline. The reranker improves retrieval quality but is not strictly required; the embedder alone produces functional RAG.

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
  --rootfs local-lvm:16 \
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

The `/v1/workspace/$ws/update-embeddings` calls in §10.4 trigger immediate embedding via the router → V620 LXC 151 (port 8082). Embedding runs synchronously per request, so the curl returns once embeddings are persisted. For a 5,000-document corpus, expect ~15–30 minutes total wall-clock time on a V620 with `--parallel 8` (vs the prior 30–60 min budget when the embedder ran on the smaller 3060).

While embedding runs, monitor the V620 embedder in another shell:

```bash
# In another shell on the Proxmox host
pct exec 151 -- watch -n 1 'rocm-smi --showmeminfo vram --showuse | grep -E "VRAM|GPU\["'
# Expect: V620 #1 utilization spikes during embedding (--main-gpu 0 pinning); modest VRAM growth on top of chat's allocation
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

**Estimated time:** 30-45 minutes for the full suite.

This phase covers (1) IP plan + DHCP records, (2) deployment record, (3) the **acceptance verification suite** (Step 11.4) which exercises auth gates, embedder correctness, reranker correctness, concurrent VRAM load, rate limiting, and an end-to-end RAG smoke test. Step 11.4 is the same suite that Phase 6 Step 6.3 falls back to inline — running it here for fresh builds is the canonical post-deploy gate.

### Step 11.1 — Save the IP plan

```bash
# Save your final IP plan to /etc/hosts on each LXC for clean references
cat >> /etc/hosts <<EOF
192.168.6.151  llamacpp-amd
192.168.6.153  llm-router
192.168.6.154  anythingllm
192.168.6.155  mcp-stack
EOF
# Note: 192.168.6.152 (llamacpp-nv, the old 3060 LXC) is intentionally omitted.
```

Repeat on each LXC.

### Step 11.2 — Configure DHCP reservations

In your router's admin panel, create static DHCP reservations for each LXC's MAC address so they always get the same IP. Get the MAC addresses:

```bash
# From Proxmox host
for vmid in 151 153 154 155; do
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
**GPU stack:** 2× AMD Radeon Pro V620 (ROCm only; no NVIDIA hardware)
**Driver versions:**
- AMDGPU kernel module: $(modinfo amdgpu 2>/dev/null | awk '/^version:/{print $2}' | head -1)
- ROCm: $(pct exec 151 -- rocminfo 2>/dev/null | grep "Runtime Version" | head -1)

**LXCs:**
- 151 llamacpp-amd: $(pct exec 151 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')
- 153 llm-router: $(pct exec 153 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')
- 154 anythingllm: $(pct exec 154 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')
- 155 mcp-stack: $(pct exec 155 -- ip -4 addr show eth0 | grep inet | awk '{print $2}')

**Services on V620 LXC 151:**
- Chat (port 8080): $(pct exec 151 -- curl -sf -H "Authorization: Bearer \$(awk -F= '/^LLAMACPP_API_KEY=/{print \$2}' /etc/llamacpp.env)" http://localhost:8080/v1/models 2>/dev/null | jq -r '.data[].id' 2>/dev/null)
- Embed (port 8082): $(pct exec 151 -- curl -sf -H "Authorization: Bearer \$(awk -F= '/^LLAMACPP_API_KEY=/{print \$2}' /etc/llamacpp.env)" http://localhost:8082/v1/models 2>/dev/null | jq -r '.data[].id' 2>/dev/null)
- Rerank (port 8083): $(pct exec 151 -- curl -sf -H "Authorization: Bearer \$(awk -F= '/^LLAMACPP_API_KEY=/{print \$2}' /etc/llamacpp.env)" http://localhost:8083/v1/models 2>/dev/null | jq -r '.data[].id' 2>/dev/null)

EOF
cat /root/deployment-record/deployment.md
```

### Step 11.4 — Acceptance verification suite

Run from the Proxmox host. All commands assume `LLAMACPP_API_KEY` and `ROUTER_API_KEY` are set in their respective env files (Phase 5.11.1 + Phase 7.1.5). The suite is 14 checks split into 5 groups.

```bash
# Helper variables — re-derive at runtime so it's safe to copy any test in isolation
LLAMACPP_KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env)
ROUTER_KEY=$(pct exec 153 -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env)

# Pre-flight: wait for all three services on LXC 151 to actually serve requests.
# Even after `systemctl is-active` returns "active", llama-server may still be
# loading model weights for ~30-60s on cold start. Don't run the suite until all
# three /v1/models endpoints return 200 with the auth header.
echo "Pre-flight: waiting for chat/embed/rerank to accept requests..."
for port in 8080 8082 8083; do
  for attempt in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $LLAMACPP_KEY" \
            --max-time 5 "http://192.168.6.151:${port}/v1/models")
    [ "$code" = "200" ] && { echo "  151:${port} ready"; break; }
    sleep 2
  done
done
```

**Group 1 — Hardware + host (no NVIDIA, two V620s):**

```bash
# 1.1 Host has no NVIDIA artifacts
which nvidia-smi && echo "FAIL: nvidia-smi still present" || echo "OK — no NVIDIA on host"

# 1.2 Default kernel (no manual pin required for V620-only)
uname -r   # Expect: 6.17.x-pve on PVE 9.1; 6.14.x is also fine on older PVE

# 1.3 AMDGPU exposes both V620 render nodes
ls /dev/dri/   # Expect: card0, card1, renderD128, renderD129 (no renderD130)
ls /dev/kfd

# 1.4 Four LXCs present, no 152
pct list | awk '$1 ~ /^(151|153|154|155)$/'
pct list | awk '$1 == "152"' | wc -l   # Expect: 0
```

**Group 2 — Service health + auth gates:**

```bash
# 2.1 All three llama-server units active on LXC 151
pct exec 151 -- systemctl is-active llamacpp-chat llamacpp-embed llamacpp-rerank
# Expect: active, active, active (three lines)

# 2.2 Direct endpoint health (Bearer required)
for port in 8080 8082 8083; do
  http_code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $LLAMACPP_KEY" \
               http://192.168.6.151:${port}/health)
  echo "151:${port} -> ${http_code}"   # Expect: 200
done

# 2.3 Auth gate works (unauthed request returns 401)
for port in 8080 8082 8083; do
  http_code=$(curl -s -o /dev/null -w "%{http_code}" http://192.168.6.151:${port}/health)
  echo "151:${port} unauthed -> ${http_code}"   # Expect: 401
done

# 2.4 Router refuses unauthed requests
http_code=$(curl -s -o /dev/null -w "%{http_code}" \
             -X POST -H "Content-Type: application/json" \
             -d '{"messages":[{"role":"user","content":"ping"}]}' \
             http://192.168.6.153:8000/v1/chat/completions)
echo "router unauthed -> ${http_code}"   # Expect: 403

# 2.5 Router accepts authed request
http_code=$(curl -s -o /dev/null -w "%{http_code}" \
             -X POST -H "Authorization: Bearer $ROUTER_KEY" \
             -H "Content-Type: application/json" \
             -d '{"model":"qwen3-35b","messages":[{"role":"user","content":"ping"}],"max_tokens":5}' \
             http://192.168.6.153:8000/v1/chat/completions)
echo "router authed -> ${http_code}"   # Expect: 200
```

**Group 3 — Embedder + reranker correctness:**

```bash
# 3.1 Embedding dim = 1024 (verifies --pooling last correctness for Qwen3-Embedding-0.6B)
dim=$(curl -sf -X POST -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
       -d '{"input":"hello"}' http://192.168.6.153:8000/v1/embeddings | jq '.data[0].embedding | length')
echo "embed dim: ${dim}"   # Expect: 1024 — anything else means wrong pooling

# 3.2 Two embeddings of identical input are bit-identical (determinism check)
e1=$(curl -sf -X POST -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
      -d '{"input":"hello"}' http://192.168.6.153:8000/v1/embeddings | jq -c '.data[0].embedding')
e2=$(curl -sf -X POST -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
      -d '{"input":"hello"}' http://192.168.6.153:8000/v1/embeddings | jq -c '.data[0].embedding')
[ "$e1" = "$e2" ] && echo "OK — embeddings deterministic" || echo "FAIL — embedder is non-deterministic"

# 3.3 Reranker scores semantically — "Paris is in France" must rank above "Berlin is in Germany"
top=$(curl -sf -X POST -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" \
       -d '{"query":"capital of France","documents":["Paris is in France","Berlin is in Germany"]}' \
       http://192.168.6.153:8000/v1/rerank | jq -r '.results[0].document')
echo "rerank top: ${top}"   # Expect: "Paris is in France"
```

**Group 4 — Concurrent load (VRAM + slot contention):**

```bash
# 4.1 Chat streaming + 100 parallel embed requests overlap — verify no OOM, both cards work
pct exec 151 -- bash -c "
  KEY=\$(awk -F= '/^LLAMACPP_API_KEY=/{print \$2}' /etc/llamacpp.env)
  # Launch a long-ish chat completion in background
  curl -s -H \"Authorization: Bearer \$KEY\" http://localhost:8080/v1/chat/completions \\
    -X POST -H 'Content-Type: application/json' \\
    -d '{\"messages\":[{\"role\":\"user\",\"content\":\"Write a 200-word essay about V620 GPUs.\"}],\"stream\":true,\"max_tokens\":200}' > /tmp/chat.out &
  CHAT_PID=\$!
  sleep 2
  # Fire 100 small embed requests in parallel
  for i in \$(seq 1 100); do
    curl -s -H \"Authorization: Bearer \$KEY\" http://localhost:8082/v1/embeddings \\
      -X POST -H 'Content-Type: application/json' -d \"{\\\"input\\\":\\\"doc \$i\\\"}\" > /dev/null &
  done
  wait \$CHAT_PID
  wait   # wait for all embed jobs
  # Snapshot VRAM after overlap
  rocm-smi --showmeminfo vram --showuse
"
# Expect: both V620s 18-22 GB used; > 60% util during overlap window; no OOM in journal

# 4.2 No OOM / HIP errors in journal over the last 30 min
pct exec 151 -- journalctl -u llamacpp-chat -u llamacpp-embed -u llamacpp-rerank --since '30 min ago' \
  | grep -iE "oom|hip error|out of memory|cuda error" \
  && echo "FAIL — see errors above" || echo "OK — clean journals"

# 4.3 Spec-decode acceptance rate >= 0.6 over recent requests
pct exec 151 -- journalctl -u llamacpp-chat --since '10 min ago' \
  | grep -oP 'accept_rate=\K[0-9.]+' | tail -5
# Expect: values >= 0.6; lower means the draft model isn't matching the workload
```

**Group 5 — Router admission control + rate limit + RAG E2E:**

```bash
# 5.1 Router rate limit kicks in (slowapi 60/min default for chat)
echo "Firing 80 requests rapidly to test rate limit..."
codes=$(for i in $(seq 1 80); do
  curl -s -o /dev/null -w "%{http_code} " -H "Authorization: Bearer $ROUTER_KEY" \
    -X POST -H "Content-Type: application/json" \
    -d '{"model":"x","messages":[{"role":"user","content":"q"}],"max_tokens":1}' \
    http://192.168.6.153:8000/v1/chat/completions
done)
echo "$codes" | tr ' ' '\n' | sort | uniq -c
# Expect: some 429s after ~60 (rate limit), plus 200s — confirms rate limiter is active

# 5.2 Router /metrics endpoint is gated (only METRICS_ALLOWED_IPS pass)
http_code=$(curl -s -o /dev/null -w "%{http_code}" http://192.168.6.153:8000/metrics)
echo "/metrics from non-allowed IP -> ${http_code}"   # Expect: 403 (or 404 if hidden)

# 5.3 E2E RAG smoke test
# Workspace name and AnythingLLM API key
WS_SLUG=verify-rag
ALLM_KEY=$(pct exec 154 -- awk -F= '/^ANYTHINGLLM_API_KEY=/{sub(/^ANYTHINGLLM_API_KEY=/,""); gsub(/^"|"$/,""); print}' /opt/anythingllm/.env 2>/dev/null)
TEST_DOC=$'The squishy lampson is a fictional creature with three eyes, native to the
forests of Drelnar. It feeds primarily on phosphorescent moss.'

# Create a temporary workspace via API
curl -sf -X POST "http://192.168.6.154:3001/api/v1/workspace/new" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"$WS_SLUG\"}" > /dev/null

# Upload the doc (note: API shape varies by AnythingLLM version; this is one common path)
echo "$TEST_DOC" > /tmp/squishy.txt
curl -sf -X POST "http://192.168.6.154:3001/api/v1/document/upload" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -F "file=@/tmp/squishy.txt" > /dev/null

# Attach + embed
curl -sf -X POST "http://192.168.6.154:3001/api/v1/workspace/$WS_SLUG/update-embeddings" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"adds":["squishy.txt"]}' > /dev/null

# Wait for embedding to complete (poll workspace until docCount > 0 or 30s timeout).
# This is more reliable than a fixed sleep because the embedder model warm-up time
# varies (cold = ~5-15s, warm = ~1s).
for attempt in $(seq 1 15); do
  docCount=$(curl -sf "http://192.168.6.154:3001/api/v1/workspace/$WS_SLUG" \
    -H "Authorization: Bearer $ALLM_KEY" 2>/dev/null \
    | jq -r '.workspace.documents | length // 0')
  [ "${docCount:-0}" -gt 0 ] && { echo "embedding done"; break; }
  sleep 2
done

# Query and check the citation pulls the squishy passage
response=$(curl -sf -X POST "http://192.168.6.154:3001/api/v1/workspace/$WS_SLUG/chat" \
  -H "Authorization: Bearer $ALLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"How many eyes does a squishy lampson have?","mode":"query"}')
echo "$response" | jq -r '.textResponse'
# Expect: response mentions "three" and "eyes" — confirms full RAG path works

# Clean up
curl -sf -X DELETE "http://192.168.6.154:3001/api/v1/workspace/$WS_SLUG" \
  -H "Authorization: Bearer $ALLM_KEY" > /dev/null
rm -f /tmp/squishy.txt
```

**Acceptance gate — proceed only if all 14 checks above pass.** If any FAIL:
- Group 1 failures → re-check Phase 1 (hardware) and Phase 4 (host config).
- Group 2 failures → re-check Phase 5 (units active + Bearer auth) and Phase 7 (router auth middleware).
- Group 3 failures → most commonly `--pooling` is wrong; fix in Phase 5 unit file.
- Group 4 failures → reduce `--parallel`, drop `--ctx-size`, or check VRAM allocation math.
- Group 5 failures → router code (`scripts/files/router-app.py`) is missing rate limit or IP allowlist features; see todo #20 in the pivot plan.

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
   - Containers: 151, 153, 154, 155 (V620-only; LXC 152 was destroyed in the Phase 6 cutover. Existing UI backup jobs created pre-pivot must be hand-edited to remove 152, otherwise the job logs "no such container 152" each run.)
   - Mode: Snapshot
   - Compression: zstd
   - Retention: keep-daily=7, keep-weekly=4, keep-monthly=3

This keeps a week of dailies, four weekly snapshots, and three monthly archives. Total disk usage typically ~30-50 GB.

#### CLI backup (manual or scripted)

```bash
# Backup all LXCs at once
vzdump 151 153 154 155 \
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

1. **Daily vzdump** of all 4 LXCs (`151 153 154 155`) to `tank-backups` (retention: 7d/4w/3m)
2. **Daily ZFS snapshots** of `tank/anythingllm` and `tank/mcp` (retention: 14 days)
3. **Weekly off-host copy** of vzdump archives to PBS or external storage
4. **Skip** `/tank/models` backups (just record which models were loaded)

### Test your backups

**Quarterly restore test** — actually verify a backup works:

```bash
# Pick a test backup
TEST_BACKUP=tank-backups:backup/vzdump-lxc-153-2026_05_10-03_00_01.tar.zst

# Restore as a new VMID for testing (don't overwrite the running container)
pct restore 999 $TEST_BACKUP --storage local-lvm --rootfs local-lvm:8

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
# Inside LXC 151 and 153 (152 was destroyed in Phase 6 cutover)
# For LXC 151: post-Phase-5-expansion you have three units (llamacpp-chat, llamacpp-embed, llamacpp-rerank).
# Drop the notify.conf into each. Pre-expansion the single unit is named llama-server.service.
mkdir -p /etc/systemd/system/<service>.service.d/
for unit in llamacpp-chat llamacpp-embed llamacpp-rerank; do
  [ -f "/etc/systemd/system/${unit}.service" ] || continue
  cat > "/etc/systemd/system/${unit}.service.d/notify.conf" <<EOF
[Unit]
OnFailure=service-failure-notify@%n.service
EOF
done
# Fallback for pre-Phase-5-expansion (single llama-server unit):
[ -f /etc/systemd/system/llama-server.service ] && cat > /etc/systemd/system/llama-server.service.d/notify.conf <<EOF
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

# V620 LXC 151 — chat + embed + rerank services (auth required)
LLAMACPP_KEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env 2>/dev/null)
check_authed() {
    local name=$1 url=$2 pattern=$3
    if ! response=$(curl -sf --max-time 10 -H "Authorization: Bearer $LLAMACPP_KEY" "$url" 2>&1); then
        ALERTS+=("DOWN: $name ($url)")
        return 1
    fi
    echo "$response" | grep -q "$pattern" || ALERTS+=("BAD RESPONSE: $name ($url)")
}
check_authed "V620 chat (151:8080)"   "http://192.168.6.151:8080/v1/models" "data"
check_authed "V620 embedder (151:8082)" "http://192.168.6.151:8082/v1/models" "data"
check_authed "V620 reranker (151:8083)" "http://192.168.6.151:8083/v1/models" "data"

# Router
check "Router health" "http://192.168.6.153:8000/healthz" '"ok":true'

# AnythingLLM
check "AnythingLLM" "http://192.168.6.154:3001/api/ping" "online"

# MCPs
for port in 3002 3003 3004; do
    check "MCP port $port" "http://192.168.6.155:$port/sse" "endpoint" || true
done

# GPU temperature checks — both V620 cards via rocm-smi (no NVIDIA in V620-only build)
TEMP_AMD_0=$(pct exec 151 -- rocm-smi -d 0 --showtemp 2>/dev/null | grep -oP "(?<=Temperature \(Sensor edge\) \(C\):\s)[\d.]+" | head -1)
TEMP_AMD_1=$(pct exec 151 -- rocm-smi -d 1 --showtemp 2>/dev/null | grep -oP "(?<=Temperature \(Sensor edge\) \(C\):\s)[\d.]+" | head -1)

if [ -n "$TEMP_AMD_0" ] && [ "${TEMP_AMD_0%.*}" -gt 85 ]; then
    ALERTS+=("V620 #1 temperature $TEMP_AMD_0°C — investigate cooling")
fi
if [ -n "$TEMP_AMD_1" ] && [ "${TEMP_AMD_1%.*}" -gt 85 ]; then
    ALERTS+=("V620 #2 temperature $TEMP_AMD_1°C — investigate cooling")
fi

# ZFS pool health
zpool status tank 2>/dev/null | grep -q "state: ONLINE" || ALERTS+=("tank ZFS pool NOT ONLINE")

# Backup-storage free space (alert if <10% free)
BACKUP_FREE_PCT=$(df /tank/backups 2>/dev/null | awk 'NR==2 {print 100-int($5)}')
if [ -n "$BACKUP_FREE_PCT" ] && [ "$BACKUP_FREE_PCT" -lt 10 ]; then
    ALERTS+=("/tank/backups <10% free (${BACKUP_FREE_PCT}%)")
fi

# Per-card VRAM headroom (alert if either V620 hits >90% used — risk of OOM under bulk embed)
for gpu in 0 1; do
    VRAM_LINE=$(pct exec 151 -- rocm-smi -d $gpu --showmeminfo vram 2>/dev/null | grep -i "used")
    [ -z "$VRAM_LINE" ] && continue
    USED_BYTES=$(echo "$VRAM_LINE" | grep -oE '[0-9]+' | tail -1)
    TOTAL_BYTES=34359738368  # 32 GiB per V620 (approximate; rocm-smi reports in bytes)
    [ -n "$USED_BYTES" ] && PCT=$(( USED_BYTES * 100 / TOTAL_BYTES ))
    [ -n "$PCT" ] && [ "$PCT" -gt 90 ] && ALERTS+=("V620 #$((gpu+1)) VRAM ${PCT}% used (>90% — OOM risk)")
done

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
  --rootfs local-lvm:32 \
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
  # llama-server's --metrics endpoint requires Bearer auth in the V620-only build.
  # Credentials live in a separate file (mode 600) so the key never lands in this YAML
  # (which would otherwise leak via vzdump backups). After running this Phase, mount or
  # copy the key inside the monitoring LXC:
  #   echo "$LLAMACPP_API_KEY" > /etc/prometheus/llamacpp-api-key
  #   chmod 600 /etc/prometheus/llamacpp-api-key
  # Exclude /etc/prometheus/llamacpp-api-key from any backup that travels off-host
  # (or encrypt the off-host copy with age).
  - job_name: 'llama-chat'
    static_configs:
      - targets: ['192.168.6.151:8080']
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/llamacpp-api-key

  - job_name: 'llama-embed'
    static_configs:
      - targets: ['192.168.6.151:8082']
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/llamacpp-api-key

  - job_name: 'llama-rerank'
    static_configs:
      - targets: ['192.168.6.151:8083']
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/llamacpp-api-key

  # Router's /metrics is gated by METRICS_ALLOWED_IPS in /etc/router.env on LXC 153.
  # Add the monitoring LXC's IP (192.168.6.156) to that allowlist so this scrape works.
  - job_name: 'llm-router'
    static_configs:
      - targets: ['192.168.6.153:8000']
    metrics_path: /metrics

  # Add node-exporter on each LXC for system metrics (152 omitted — destroyed in pivot)
  - job_name: 'node-exporter'
    static_configs:
      - targets:
        - '192.168.6.151:9100'
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
# Run on each LXC (V620-only: 151, 153, 154, 155 — 152 was destroyed in the pivot)
for vmid in 151 153 154 155; do
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

- **llama.cpp**: `llamacpp:requests_processing`, `llamacpp:tokens_predicted_seconds`, `llamacpp:kv_cache_tokens` (per-service: chat / embed / rerank)
- **Spec decode acceptance**: log-derived — parse from `journalctl -u llamacpp-chat | grep acceptance`
- **GPU temperature**: per-card via `rocm-smi -d 0 --showtemp` and `rocm-smi -d 1 --showtemp` (V620 only — no `nvidia-smi` in V620-only build)
- **GPU memory + utilization**: `rocm-smi --showmeminfo vram --showuse` per card
- **Router**: per-route latency P50/P99, in-flight gauge, rate-limit 429 count, upstream 5xx count (via `prometheus-fastapi-instrumentator` in router-app.py)
- **System load**: 1m/5m load average per LXC (via node-exporter)
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

### Scenario 2 — Single V620 GPU failure

**Symptoms:** `rocm-smi` reports an error or doesn't see one of the V620 cards; `pct exec 151 -- systemctl status llamacpp-chat` shows failed-to-start; logs mention HIP/HSA initialization errors.

**Impact in V620-only architecture:** With both V620s allocated across chat (tensor-split), embed (V620 #1), and rerank (V620 #2), a single-card failure degrades all three services — not just chat. Specifically:
- If **V620 #0** fails: embedder (pinned to `--main-gpu 0`) becomes unavailable; chat loses half its weights/KV (tensor-split breaks).
- If **V620 #1** fails: reranker (pinned to `--main-gpu 1`) becomes unavailable; chat loses the other half of its weights/KV.

Recovery limps the cluster along on the surviving card with reduced capacity (single 32 GB pool instead of 64 GB).

**Step 1: Identify which V620 failed.**

```bash
pct exec 151 -- rocm-smi --showid
# If only one GPU is listed, the missing index is the failed one.
# Note: --main-gpu indexes (0, 1) align with what rocm-smi lists; if rocm-smi shows only
# one card, treat the missing slot as failed.
```

**Step 2: Reconfigure chat to use only the surviving V620.**

```bash
# Inside LXC 151 — edit /etc/systemd/system/llamacpp-chat.service
# Suppose V620 #1 failed (only V620 #0 survives).
# Change:
#   Environment="HIP_VISIBLE_DEVICES=0,1"
#   --tensor-split 1,1
#   --ctx-size 131072
#   --parallel 4
# To:
#   Environment="HIP_VISIBLE_DEVICES=0"
#   --tensor-split 1,0     (or remove the flag — single GPU defaults to all-on-one)
#   --ctx-size 32768       (single V620 has only 32 GB VRAM — weights 22 GB + KV ~5 GB)
#   --parallel 2           (cut concurrent slots to fit 32 GB)

pct exec 151 -- systemctl daemon-reload
pct exec 151 -- systemctl restart llamacpp-chat
```

**Step 3: Stop the service that pinned to the failed card.**

If V620 #1 failed (reranker uses `--main-gpu 1`), stop the reranker — it can't run on the surviving card without also reconfiguring (which would cause it to compete with embed for the same card):

```bash
pct exec 151 -- systemctl stop llamacpp-rerank
# The router (LXC 153) will start returning upstream 502s on /v1/rerank.
```

> ⚠️ **Dependency on router rewrite (todo #20):** The "fail-open SSE" graceful-degradation behavior — where the router emits a `service degraded` event instead of propagating raw 502s to AnythingLLM — is delivered by the rewritten `scripts/files/router-app.py` (tracked as todo #20 in the pivot plan). Until that lands, AnythingLLM will see raw 502s on `/v1/rerank` and may fail entire RAG queries instead of falling back to plain top-K retrieval. Workaround for current pre-rewrite clusters: in AnythingLLM Settings → AI Providers → Reranker, set the provider to "Disabled" until the failed V620 is replaced.

If V620 #0 failed (embedder uses `--main-gpu 0`):

```bash
pct exec 151 -- systemctl stop llamacpp-embed
# AnythingLLM's RAG ingest is now broken until either (a) the card is replaced and
# the service restarted, or (b) the embedder is reconfigured to the surviving card
# (see Step 4 below).
```

**Step 4 (optional): Move the small service to the surviving card if you can spare ~1.5 GB.**

The reranker is ~1.5 GB or the embedder is ~1.2 GB; either fits on top of a degraded-mode chat (22 GB weights + 5 GB KV = ~27 GB used on a 32 GB card; ~5 GB headroom). To re-home the orphaned small service to the surviving card:

```bash
# Edit the orphaned service's unit:
#   Environment="HIP_VISIBLE_DEVICES=<surviving_card>"
#   --main-gpu <surviving_card>
# Reduce chat's --parallel to 1 if VRAM is tight.

pct exec 151 -- systemctl daemon-reload
pct exec 151 -- systemctl restart llamacpp-chat llamacpp-embed llamacpp-rerank
```

**Step 5: Plan replacement.**

V620s are passively-cooled server cards; failure modes include the active 80 mm NF-A8 shroud fan dying (overheat → throttle → eventual silicon damage). Before replacing the card, replace the NF-A8 on the surviving card (it likely shares the same wear pattern). Source replacement V620s from server-pull sellers; verify gfx1030 identification on the new card before deploying.

**Note:** This runbook no longer covers a separate "RTX 3060 failure" path — the 3060 was removed in the V620-only pivot (see `local-gpu-cluster-v2.md` §1.3). If you are running a pre-pivot cluster with an active 3060 LXC 152, the old failure-recovery path (CPU embedder fallback or borrow V620 capacity) still applies, but you should be on the pivot path instead.

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
    --storage local-lvm

# Start it
pct start 154
```

### Scenario 4 — Full host reinstall (boot drive failure)

**When:** Boot drive (M.2_1) failure, LVM-thin pool corruption, accidental Proxmox uninstall, etc.

**Prerequisites:** Backups live on `/tank/backups`, which is on the **ZFS mirror across M.2_3 + M.2_4**. The mirror is independent of the boot drive, so a boot-drive failure leaves all backups intact. (For a `/tank` drive failure, see Scenario 4b below.)

**Procedure:**

1. Replace the boot drive if hardware failure
2. Reinstall Proxmox VE 9.x per Phase 3 (ext4, same installer options)
3. Import the existing ZFS mirror — do NOT recreate it:
   ```bash
   # The /tank pool survives on M.2_3 + M.2_4; just import it
   zpool import tank
   zpool status tank   # confirm both mirror members ONLINE
   zfs list            # confirm all datasets present
   ```
4. Re-add storage definitions:
   ```bash
   pvesm add dir tank-backups --path /tank/backups --content backup,iso,vztmpl
   ```
5. Re-do Phase 4 (host configuration: IOMMU, NVIDIA driver, ZFS ARC cap, etc.)
6. Restore each LXC from backup:
   ```bash
   for vmid in 151 153 154 155; do
       BACKUP=$(ls -t /tank/backups/dump/vzdump-lxc-${vmid}-*.tar.zst | head -1)
       pct restore $vmid $BACKUP --storage local-lvm
       pct start $vmid
   done
   ```
7. Run Phase 11 verification

Estimated full-recovery time: 2-3 hours from a failed boot drive.

### Scenario 4b — Single `/tank` mirror member failure

**When:** One of the two data NVMes (M.2_3 or M.2_4) fails. The pool remains ONLINE in **DEGRADED** state — no downtime, no data loss.

**Procedure:**

1. Identify the failed device:
   ```bash
   zpool status tank
   # Look for FAULTED, UNAVAIL, or DEGRADED next to one mirror member
   ```
2. Physically replace the failed NVMe with a same-or-larger drive (power off if not hot-swappable).
3. Identify the new device's by-id path:
   ```bash
   ls -l /dev/disk/by-id/ | grep nvme | grep -v part
   ```
4. Replace within the pool — ZFS resilvers (rebuilds) automatically:
   ```bash
   zpool replace tank /dev/disk/by-id/nvme-OLD_FAILED_ID /dev/disk/by-id/nvme-NEW_DRIVE_ID
   zpool status tank   # watch resilver progress; expect a few minutes to hours depending on data size
   ```
5. When `zpool status` shows both members ONLINE and no scrub/resilver in progress, you're back to fully-redundant.

**Estimated recovery time:** 15 min hands-on + resilver time (~10-30 min per TB used). Zero downtime for running LXCs throughout.

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
for vmid in 151 153 154 155; do
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
sha256sum /tank/models/qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf

# Compare against the published checksum on Hugging Face

# If mismatched, redownload
cd /tank/models
mv qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf.corrupt
wget -O qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf <URL>

# Restart V620 chat service (post-Phase-5-expansion unit name is llamacpp-chat;
# pre-expansion clusters use the older llama-server name)
pct exec 151 -- systemctl restart llamacpp-chat 2>/dev/null \
  || pct exec 151 -- systemctl restart llama-server
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
./build/bin/llama-server --model /opt/models/qwen3.6-35b-a3b-ud-q4_k_m-00001-of-00002.gguf --port 8080
```

### (Removed) `Failed to initialize NVML` — was an NVIDIA-only failure mode

This troubleshooting entry covered NVML version mismatches between the host's NVIDIA kernel module and the LXC's userspace driver. The V620-only build has no NVIDIA hardware, no `nvidia-driver`, and no `nvidia-smi` — this failure mode no longer applies. If you see this error, check whether you accidentally installed nvidia-driver and remove it: `apt purge -y nvidia-driver firmware-nvidia-graphics`.

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
# Status of GPU stack (LXC 151 only — V620-only build; LXC 152 was destroyed in Phase 6)
echo "=== LXC 151 (V620 chat + embed + rerank) ==="
pct exec 151 -- systemctl --no-pager --type=service --state=running | grep llama

# All LXC IPs
for vmid in 151 153 154 155; do
    echo "VMID $vmid: $(pct exec $vmid -- hostname -I 2>/dev/null | awk '{print $1}')"
done

# Backup all LXCs now
vzdump 151 153 154 155 --storage tank-backups --mode snapshot --compress zstd

# Restart everything in dependency order (simple — no health gates)
pct restart 151        # V620 LXC: chat + embed + rerank
sleep 30
pct restart 153        # Router (depends on 151 being healthy)
sleep 15
pct restart 154        # AnythingLLM (depends on 153 router endpoint)
sleep 5
pct restart 155        # MCP stack

# OR — health-gated restart (waits for each service to become healthy before continuing)
# This catches the case where the next sleep is too short for a slow cold start.
restart_with_gate() {
    local vmid=$1 check_cmd=$2 timeout=${3:-90}
    pct restart "$vmid"
    local start=$SECONDS
    until eval "$check_cmd" >/dev/null 2>&1; do
        [ $((SECONDS - start)) -ge "$timeout" ] && { echo "LXC $vmid did not become healthy in ${timeout}s"; return 1; }
        sleep 5
    done
    echo "LXC $vmid healthy"
}
LKEY=$(pct exec 151 -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env 2>/dev/null)
restart_with_gate 151 "pct exec 151 -- curl -sf -H 'Authorization: Bearer $LKEY' http://localhost:8080/v1/models | grep -q data" 180 || exit 1
restart_with_gate 153 "pct exec 153 -- curl -sf http://localhost:8000/healthz | grep -q ok" 60 || exit 1
restart_with_gate 154 "pct exec 154 -- curl -sf http://localhost:3001/api/ping | grep -q online" 60 || exit 1
pct restart 155

# Check spec decode acceptance rate
pct exec 151 -- journalctl -u llamacpp-chat -n 100 --no-pager | grep -i acceptance | tail -5
# (unit name is llamacpp-chat after the Phase 5 expansion; pre-expansion it was llama-server)
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

**1. NF-A8 PWM airflow may be marginal under sustained V620 load** *(severity downgraded after Lancool 217 case selection)*.
- Spec: 32.67 CFM, 2.37 mm H₂O static pressure at full 12V (2200 RPM) for each NF-A8.
- The NF-A8s push air *through* the V620 heatsink, but they need cool air *supplied* to them. With the Lancool 217's combination of high-volume intake (2× 170mm front at 142 CFM each in "GPU mode" lower position) and direct undercurrent (2× 120mm reverse-blade on the PSU shroud aimed up at the GPU stack), the NF-A8s receive abundant cool supply air.
- **Risk:** Reduced but not eliminated. Under prolonged maximum-power inference (both V620s at full 225W sustained ≈ 450W of GPU heat for 30+ minutes; spec TDP is 300W per card but measured draw on this generation is closer to 225W under llama.cpp-style workloads), the NF-A8 may still be the limiting factor pushing air through the V620 heatsink fin density.
- **Mitigation A (preferred):** Monitor V620 edge/junction temperatures via `rocm-smi --showtemp` during sustained load. If junction temp exceeds 95°C, throttle GPU clocks: `rocm-smi -d 0 --setperflevel low` or set a lower power cap with `rocm-smi -d 0 --setpoweroverdrive 175` (caps at 175W instead of 225W).
- **Mitigation B:** Replace NF-A8 with a higher-airflow 80mm fan (Noctua NF-R8 redux-1800 PWM is 31.4 CFM; or a server-grade Delta AFB0812SH at ~52 CFM with notable noise increase).
- **Mitigation C (built-in):** The Lancool 217's case airflow design (front 170mm intake + bottom-shroud 120mm reverse-blade aimed up at GPU stack) directly addresses the upstream supply of cool air to the V620 NF-A8s. This was not available with the originally specified Define 7 XL.

**2. Marvell AQC113CS 10GbE Linux driver has reported regressions on PVE 9.x.**
- Multiple users on Proxmox forums report the AQC113 link going down after kernel upgrades (working on 6.8.4-2, broken on 6.8.4-4+, intermittent on 6.14.x).
- **Risk:** Primary uplink fails after a kernel update, leaving the node accessible only via the 2.5GbE Realtek port (or not at all if you only configured the 10GbE port).
- **Mitigation A:** Always configure both NICs in `/etc/network/interfaces` from day one — bridge the 10GbE as `vmbr0` and the 2.5GbE as `vmbr1`, with the 2.5GbE as a backup default route at higher metric. If 10GbE fails, the 2.5GbE stays online.
- **Mitigation B:** Pin the kernel version to a known-good release and disable auto-upgrade for `proxmox-kernel-*` packages. (Note: kernel 6.14 pinning was previously documented here for NVIDIA DKMS reasons; with NVIDIA removed in the V620-only pivot, pinning is now an OPTIONAL defensive measure against AQC113 regressions, not a required NVIDIA workaround.)
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
- **Mitigation:** Switch the V620 LXC (151) to privileged mode if issues occur. Edit `/etc/pve/lxc/151.conf`, change `unprivileged: 1` to `unprivileged: 0`, and remove the `lxc.apparmor.profile: unconfined` line. Restart. The Docker LXCs (154 AnythingLLM, 155 MCP) can stay unprivileged — only the GPU-passthrough LXC has the KFD-handshake risk.

**7. Marvell AQC113 atlantic driver may break on kernel 6.17+.**
- With the V620-only pivot, kernel pinning is no longer mandatory (no NVIDIA DKMS dependency). The default PVE 9.1 kernel is 6.17. AQC113 may exhibit driver regressions on 6.17+.
- **Risk:** 10 GbE link drops after a kernel upgrade.
- **Mitigation A:** Configure both NICs (10 GbE primary + 2.5 GbE backup) from day one so a link failure doesn't lock you out.
- **Mitigation B:** Optionally pin to a known-good kernel (e.g., 6.14) as a defensive measure — see Risk #2 Mitigation B above. This is a tradeoff: pinned kernels accumulate security debt over time. Default recommendation is to stay on the latest PVE kernel and test 10 GbE link state immediately after any upgrade; roll back via `proxmox-boot-tool kernel pin` if regressed.

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
