# VCF 9.1 lab: host and VM settings to cut crash risk with Memory Tiering on consumer NVMe

Research date: 2026-10-12. Labels: **VERIFIED-in-docs** = Broadcom TechDocs, KB, or ESXCLI reference;
**VENDOR-BLOG** = VMware/Broadcom staff blog (Morera, Epping, Lam): authoritative in practice but not product docs;
**COMMUNITY** = forum/third-party; **UNVERIFIED** = my inference or not found in any source. Verify every
command on one host before rolling it out (per "settle VCF questions by running").

## 0. Headline findings (read these first)

1. **A reservation does NOT keep pages in DRAM.** "Memory reservations (not pinned memory) can work with Memory
   Tiering" and reservations "can be split between the DRAM and NVMe tiers" (TechDocs 9.1 best practices). Morera:
   "those reserved pages can still be satisfied from either DRAM or Non-Volatile Memory Express (NVMe)"; the "All locked"
   checkbox "does not pin memory to DRAM. Memory Tiering is still in play." Only tier-disable (explicit or implicit)
   or true pinning keeps a VM DRAM-only. VERIFIED-in-docs + VENDOR-BLOG.
2. **Tier-disabling a VM requires a full reservation (official).** "When you deactivate memory tiering for a VM, you
   must also configure a full memory reservation for that VM." (TechDocs 9.1, NVMe Failure Scenarios). Without it the
   VM's pages can go to the host swap file. VERIFIED-in-docs.
3. **Official failure semantics match what you saw.** "If a page fault is raised against a page on the failed device and
   cannot be serviced, the virtual machine powers off." DRAM-only VMs are unaffected; HA may restart the powered-off VMs.
   "Placing a host in maintenance mode after an NVMe failure is a manual operation in vSphere 9.1." VERIFIED-in-docs.
4. **There are no documented promotion/demotion, rate, or cold-threshold tunables in 9.1.** The whole `esxcli memtier`
   surface is devices, encryption, and `--tier-size-pct` (1-400). VERIFIED-in-docs (ESXCLI 9.1.0 reference).
5. **9.1 software mirroring (RAID-1) is the only documented control that lets VMs survive a tier device hang.** It needs a
   second NVMe device of equivalent capacity per host. VERIFIED-in-docs + VENDOR-BLOG. On the BD795i SE it does not fit
   unless you give up a slot (your lab notes say all lanes are in use).
6. **Your drive is a PM981, not a PM981a.** `MZVLB512HAJQ` = PM981; `MZVLB512HBJQ` = PM981a (retailer/OEM part listings).
   COMMUNITY. I found no public firmware newer than 5L2QEXA7 for it. Firmware for this OEM part ships only through
   HP/Lenovo/Dell system packages, and forum reports say cross-flashing fails. COMMUNITY.
7. **The documented failure mode is "controller stops answering I/O and aborts; reset fails; device marked offline; power
   cycle needed."** That is KB 413903 (vSAN, 8.x/9.x), the same failure you hit. No ESXi-side timeout/reset setting fixes
   it. Mitigations are (a) less exposure (fewer and less critical pages on the device, fewer write bursts), (b) removing
   PCIe/NVMe power-state transitions as a trigger, and (c) better hardware.

---

## 1. Memory tiering tunables

### 1.1 Host-level (9.1)
| Item | Value / quote | Label | Source |
|---|---|---|---|
| `esxcli memtier enable -d <dev> [-c] [-r <1-400>]` | `--tier-size-pct | -r`: "Set maximum tiering size with respect to DRAM (Value must be between 1 and 400)." | VERIFIED-in-docs | https://developer.broadcom.com/xapis/esxcli-command-reference/9.1.0/namespace/esxcli_memtier.html (read 2026-10-12) |
| `esxcli memtier config set/get/delete`, `device list -a/-c`, `status get`, `disable [-d]` | The complete namespace. No rate, threshold, or failure-policy options | VERIFIED-in-docs | same |
| Maintenance mode | "Make sure that the ESX host is placed in maintenance mode before you activate, deactivate, or reconfigure Memory Tiering." No reboot needed in 9.1 | VERIFIED-in-docs | https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-1/vsphere-resource-management/memory-tiering-over-nvme.html |
| Effective tier size | "The amount of NVMe used is the smaller of the two values - the DRAM:NVMe ratio or the NVMe Device Partition size." Doc table shows 1:1 (100%), 2:1 (50%), 4:1 (25%) as valid configs | VERIFIED-in-docs | .../9-1/.../capacity-planning-considerations/host-memory-size.html |
| Quick Boot | "Quick Boot is not supported for ESX hosts with activated Memory Tiering." (relevant for hyp03 warm-reboot behaviour) | VERIFIED-in-docs | 9.1 memory-tiering-over-nvme page |
| Tier activation threshold | "Automated tiering usually triggers only when host DRAM consumption exceeds approximately 80%." "This threshold is significant during initial power-on and load-up scenarios." | VERIFIED-in-docs | KB 448305 https://knowledge.broadcom.com/external/article/448305 ; TechDocs 9.1 best practices |
| Overcommit | "Do not use memory overcommit when Memory Tiering is activated." | VERIFIED-in-docs | .../9-1/.../memory-tiering-considerations-and-best-practices.html |
| Active memory | "Ensure that active memory usage on the ESX host has not exceeded 50% of the DRAM capacity at any point." | VERIFIED-in-docs | .../9-1/.../capacity-planning-considerations.html |
| vMotion cost | vMotion "will take longer (1.5X to 2X) to complete, since pages must be fetched from the NVMe tier first." A destination host "starts tiering a migrated VM's pages immediately" (no heat transfer in 9.1) | VERIFIED-in-docs | best-practices page; NVMe Failure Scenarios page |
| Device class | "must be of a drive similar to vSAN cache (Mixed Use with 3 DWPD)"; dedicate the device | VERIFIED-in-docs | 9.1 memory-tiering-over-nvme page |
| Legacy 9.0 keys | `Mem.TierNvmePct`, `Mem.EncryptTierNvme`, `VMkernel.Boot.memoryTiering`. Lam: legacy commands "will no longer properly enable NVMe Tiering in VCF 9.1" | VENDOR-BLOG | https://williamlam.com/2026/06/vcf-9-1-are-you-using-the-correct-esxcli-command-to-enable-nvme-tiering.html ; https://blogs.vmware.com/cloud-foundation/?p=28715 (2026-01-27) |
| Promotion/demotion/rate/cold-threshold tunables | None found in TechDocs, ESXCLI reference, Morera's 2026 series, Epping, Lam, or Lenovo LP2288. Do not set undocumented `Mem.Tier*` keys | VERIFIED absence (in the sources read) | all above |

### 1.2 Per-VM keys
| Key | Effect | Label | Source |
|---|---|---|---|
| `sched.mem.enableTiering = "FALSE"` | Excludes the VM from tiering (DRAM only). Set with the VM powered off. Pair with a full reservation | VERIFIED-in-docs (requirement) / VENDOR-BLOG (key name) | TechDocs 9.1 failure scenarios; Epping FAQ 2026-07-16 https://www.yellow-bricks.com/2026/07/16/memory-tiering-considerations-and-frequently-asked-questions/ ; Morera Part 7 2026-01-27 |
| `sched.mem.min = <memsize MB>` (or "Reserve all guest memory") | A full reservation. On a tier-enabled VM it can still be backed by NVMe; on a tier-disabled VM it protects against swap | VERIFIED-in-docs + VENDOR-BLOG | Morera 2026-08-06 https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/ |
| Latency Sensitivity = High | Implicitly tier-disables the VM ("If set to High, the VM is ineligible for tiering"), but also needs a full memory reservation and effectively exclusive pCPUs. Too costly on a 16C host. Use `enableTiering=FALSE` instead | VERIFIED-in-docs (KB 448305) | KB 448305 |
| `sched.mem.lpage.enable1GPage = "TRUE"` | 1 GB guest pages. Requires a full reservation ("Otherwise, the VM will not be able to power on"). "Large memory pages VMs" are listed as not supported for tiering, so combine only with `enableTiering=FALSE`. Allocation is best-effort and fragments over time; power on early after a host boot | VERIFIED-in-docs (vSphere 8 resource mgmt) / VENDOR-BLOG (tiering exclusion) | https://docs.vmware.com/en/VMware-vSphere/8.0/vsphere-resource-management/GUID-F0E284A5-A6DD-477E-B80B-8EFDF814EE01.html |
| `sched.mem.EncryptTierNVMe` | Per-VM tier encryption. Not relevant to stability | VENDOR-BLOG | Morera Part 7 |
| Side effect of exclusion | Removing VMs from tiering shrinks the DRAM left for tiered VMs, which pushes their effective ratio up (doc example: 1:1 becomes ~0.6:1) | VERIFIED-in-docs | failure-scenarios page |

### 1.3 Device failure behaviour and redundancy
- No RAID: "If a page fault is raised against a page on the failed device and cannot be serviced, the virtual machine powers off." HA can restart it. Hot-plug/prepare-to-remove of a tier device is not supported; to replace the device you must deactivate tiering. VERIFIED-in-docs.
- Epping: without RAID-1 "it is like a DRAM DIMM failure"; with RAID-1 (hardware, or software from 9.1) "the VM will remain running", and reads go round-robin across both devices. VENDOR-BLOG (2026-07-16).
- Software mirroring setup: Configuration Profile `memtier > nvme`, second device per host, "Each host needs two NVMe devices of equivalent capacity." VENDOR-BLOG (Morera 2026-05-19, https://blogs.vmware.com/cloud-foundation/2026/05/19/more-memory-less-effort-configuring-memory-tiering-in-vcf-9-1/). The ESXCLI 9.1.0 reference shows only "First Device" on `enable -d`, so set up mirroring through the profile, not the CLI. UNVERIFIED whether `config set -d` takes two devices.
- Not found anywhere: what happens to hostd/ENOMEM, or whether the host keeps advertising tier capacity after the device dies. Assume HA can restart VMs back onto the sick host until you put it in maintenance mode. UNVERIFIED (inference).

### 1.4 Tier ratio for consumer devices
- No Broadcom figure exists for unsupported devices. The docs only say 2:1 (50%) and 4:1 (25%) are valid configs. Virtualization Review (2026-07-29) calls 50% "conservative" for latency-sensitive workloads. COMMUNITY.
- My reasoning (UNVERIFIED): the tier size does not change how many pages demote under a given DRAM pressure, so lowering it will not cut steady-state tier I/O much. What it does is lower the host's advertised capacity. That stops DRS/HA from packing more onto a host, which is how your two burst-triggered hangs happened. 1:1 advertises 192 GB per host while you need about 17 GB of tier.

---

## 2. Consumer NVMe controller-hang mitigations on ESXi

### 2.1 Power states / APST (ESXi 9.1 commands exist; ESXi default behaviour UNVERIFIED)
ESXCLI 9.1.0 (VERIFIED-in-docs, https://developer.broadcom.com/xapis/esxcli-command-reference/9.1.0/namespace/esxcli_nvme.html):
- `esxcli nvme device feature apst get -A vmhbaN [-S current|default|saved]`: read only. There is **no** `apst set`.
- `esxcli nvme device feature pm get|set -A vmhbaN -v <PS> [-w 0] [-S]`: Power Management (FID 0x02). Pins the current power state.
- `esxcli nvme device feature set -A vmhbaN -f <FID> -c <cdw11> [-S]`: generic Set Features. APST is FID 0x0C; CDW11 bit 0 = APSTE. So `-f 0x0c -c 0` disables APST (NVMe spec mapping; ESXi worked example UNVERIFIED).
- `esxcli nvme device feature cap -A vmhbaN`: shows which features the device supports and whether it can save them.
- Whether ESXi's `nvme_pcie` driver enables APST on client drives: no source found (UNVERIFIED). A Proxmox thread on drives dropping under Linux APST notes "these drives worked perfectly under ESXi" (COMMUNITY, https://forum.proxmox.com/goto/post?id=694691, 2024-10-27). That suggests ESXi does not drive deep power states, but it proves nothing for the PM981. **Read the value first.**
- Linux equivalents for context: `nvme_core.default_ps_max_latency_us=0` (APST off) plus `pcie_aspm=off`. Thread reports run both ways: one fixed, one "100% stable with ASPM enabled", one "no solution… replaced the drives". COMMUNITY. Linux has **no** unconditional APST quirk for PM981 (PCI 144d:a808). Its only quirk is a Lenovo-board simple-suspend one. Related Samsung parts do have one: SM951 "drop off the PCIe bus randomly when APST is on and uses the deepest sleep state", and 960 EVO on a Ryzen board has NO_APST (linux drivers/nvme/host/pci.c, master, read 2026-10-12). VERIFIED (source code).
- Red Hat 6589401 shows PM981/SM981 (144d:a808) on RHEL 8: "I/O 423 QID 29 timeout, reset controller" then "controller is down; will reset: CSTS=0x3" then "Device not ready; aborting reset". CSTS=0x3 = ready + Controller Fatal Status, a firmware fault, not a link drop. The resolution is paywalled. COMMUNITY/VERIFIED (symptom only). This PM981 timeout-then-failed-reset pattern matches yours.

### 2.2 PCIe ASPM
- No ESXi `VMkernel.Boot` ASPM option found. UNVERIFIED that one exists. The documented lever is BIOS. A FreeNAS-under-ESXi 6.7 user fixed an ASPM-related hang by changing BIOS ASPM "Auto" to "Disabled" (COMMUNITY, ixsystems thread 77002).
- BD795i SE: Brandon Lee suggests "disabling ASPM or any PCIe power saving options in the BIOS" (for a GPU link-speed problem, 2025-11-17, https://www.virtualizationhowto.com/community/home-lab-forum/minisforum-bd795i-se-home-server-build-looking-good-so-far/). COMMUNITY. A Proxmox user fixed NVMe drops by disabling "PCIe Clock Gating" in BIOS (COMMUNITY, same Proxmox thread, 2024-09-11).
- BD795i/BD785i cluster report of "regular timeout on the NVMe drive's" on Intel U.2 and Crucial M.2. SR-IOV/IOMMU off did nothing; BIOS 1.07/1.09 vs 1.15 was suspected; no resolution (COMMUNITY, https://forum.level1techs.com/t/timeouts-on-nvme-with-minisforum-bd795i-boards/251976, 2026-06-29). This is the same board family on Linux, so a platform contribution is plausible.

### 2.3 Driver parameters, timeouts, queue depth
- `esxcli nvme device timeout get|set -A vmhbaN -t <sec>`: "IO timeout checker value", maximum 40, 0 deactivates. VERIFIED-in-docs. Default not documented; read it. Lowering it only makes ESXi declare the controller dead sooner. It cannot un-hang firmware.
- `esxcli nvme device feature kat set` (keep-alive) applies to fabrics, not PCIe. Ignore it.
- `vmknvme_io_queue_num` (module `vmknvme`, suggested 4/8/16, "REBOOT or RELOAD REQUIRED") is a performance knob (KB 313504). ESXi 8+ `nvme_pcie` uses polling by default. No documented interrupt-mode switch, no per-device queue-depth cap for the tier device, and no evidence that a smaller queue depth prevents a hang. VERIFIED (KB) / UNVERIFIED (any stability benefit). List the real names with `esxcli system module parameters list -m nvme_pcie` and `-m vmknvme` before touching anything.
- KB 413903 (vSAN 8.x/9.x): "The stuck I/O events on the NVMe is caused due to the underlying device not responding to I/Os or aborts." Log chain: "Controller 256 in state 8 or in recovery mode, bail out." then "Failed to enable controller 256, status: Device is permanently unavailable". Resolution: evacuate, maintenance mode, **power-cycle**, vendor firmware analysis. VERIFIED-in-docs. https://knowledge.broadcom.com/external/article/413903 . KB 448948 describes the related "State 9" CONTROLLER_STATE_FAILED. VERIFIED-in-docs.
- Same symptom on consumer drives in ESXi 7 (980 PRO "Critical warning 0x2 detected, failing controller 256", "Controller 256 in state: 9", only a reboot fixed it). COMMUNITY, https://community.broadcom.com/discussion/esxi-70-m2-nvme-ssds-failing (2021-2023).

### 2.4 Firmware
- No public PM981 (`MZVLB512HAJQ`) firmware newer than 5L2QEXA7 found. OEM firmware is per-branch (`-000L7` Lenovo, `-000H1/H2` HP, Dell). A WinRAID poster saw a `-000L7` flash fail with 0x1, "no cross-flash was reported as successful", and "the storage controller itself refused to flash". COMMUNITY, https://winraid.level1techs.com/t/firmware-updates-for-samsung-ssds-where-to-get-how-to-do/31710?page=8 . ESXi can flash with `esxcli nvme device firmware download/activate` (VERIFIED-in-docs), but without a signed image for your branch it is not worth the risk.

### 2.5 Hardware alternatives (relevant because every software lever above is weak)
- Intel Optane P1600X 118 GB M.2 2280: about 6 DWPD / 1,292 TBW, Gen3 x4. 118 GB covers a 96 GB tier at 1:1. 3D XPoint has no NAND garbage collection and is long discontinued (buy used). COMMUNITY (spec listings). Not on the Broadcom Compatibility Guide for tiering. UNVERIFIED.
- Micron 7450 PRO M.2 2280 960 GB (MTFDKBA960TFR): enterprise firmware with PLP, but about 1 DWPD (1,700 TBW), short of the 3 DWPD rule. COMMUNITY (reseller specs).
- Either replaces a client controller with a datacenter/XPoint one, which targets the actual failure mode (controller hang, not wear). This is the highest-impact change, but it means buying hardware.

---

## 3. Monitoring and early warning (ESXi 9.1)
| Tool | What it gives | Label | Source |
|---|---|---|---|
| vCenter alarm "NVMe Memory Tiering device is not healthy" | Built-in in 9.1. Lam saw it fire but "did not provide any additional details" | VENDOR-BLOG | https://williamlam.com/2026/09/quick-tip-improved-nvme-tiering-device-health-monitoring-in-vcf-9-1.html (2026-09-22) |
| `esxcli system health report get -r vmw.memTierHealth` | JSON: `model`, `device_state`, `capacity_in_GiBs`, `tier_size_in_GiBs`, `tier_used_in_GiBs`, `device_smart_stats.available_spare`, `device_smart_stats.subsystem_reliability_degraded`. UI: Host > Monitor > Memory Tiering | VENDOR-BLOG | same |
| `esxcli nvme device log smart get -A vmhbaN` / `log error get -A vmhbaN -e <n>` / `log persistentevent get` | SMART and NVMe error log. Poll it, but your hangs showed MediaErrors 0, so SMART will probably not warn before a controller hang | VERIFIED-in-docs (commands) | ESXCLI 9.1 nvme ref |
| NVMe SMART VOB alarms | `vob.vsan.lsom.{temperature,reliability,readonly,backupfailed}nvmediskhealthcriticalwarning`. These are vSAN-scoped. Unknown whether they fire for a tier device (UNVERIFIED) | VERIFIED-in-docs (KB 434866) | https://knowledge.broadcom.com/external/article/434866 |
| Syslog pattern alerting | Forward vmkernel/vobd to an LXC collector and alert on `NVMEIO`, `NVMEDEV`, `Critical warning`, `in state 8|9`, `recovery mode`, `I/O timeout`, `Failed to read tiered page`. This is the only plausible pre-failure signal: rising timeouts/aborts before the controller goes fatal | UNVERIFIED (pattern list from KBs 413903/448948 and the community thread) | — |
| `memstats -r vmtier-stats -u mb -s name:memSize:active:tier1Target:tier1Consumed` | Which VMs have pages on the tier, i.e. who dies if it hangs | VERIFIED-in-docs | KB 448305 |
| Automatic response | 9.1 maintenance mode after an NVMe failure is manual. Automation exists only via vendor hardware plug-ins (OpenManage, OneView). Proactive HA needs a provider; Lam's fake-provider PowerCLI module (2017, "not officially supported") could be driven by the syslog watcher to quarantine a host. UNVERIFIED on 9.1. vCenter alarm "run script" actions still exist but run unprivileged since vCenter 8 (KB 313285) | VERIFIED-in-docs (manual) / COMMUNITY (Lam module) | failure-scenarios page; https://williamlam.com/2017/03/powercli-module-for-proactive-ha-including-simulation.html |
| Limit of any automatic evacuation | vMotion of a tiered VM must read its NVMe pages first. If the controller is already hung, evacuation will fail and those VMs will power off anyway. Automation only helps on the *degrading* signal (timeouts) or to stop HA landing more VMs on the sick host | UNVERIFIED (follows from documented vMotion behaviour) | — |

---

## 4. LLM worker VM placement

**Arithmetic (from your numbers, UNVERIFIED-by-measurement):** stack active is 13-23% of 96 GB, about 12.5-22 GB.
Adding a 24 GB fully active worker puts per-host active at **about 38-48% of DRAM**: inside the 50% ceiling but with
little headroom. In an N-1 failure, with the worker restarted onto a survivor, active is about 57-72% of the two
surviving hosts' DRAM: **outside guidance.** Under the "do not overcommit" rule, two workers on one host break the model.

Recommendations:
1. **Workers: `sched.mem.enableTiering=FALSE` + full reservation (`sched.mem.min` = memsize).** This is the official pairing.
   Weights stay in DRAM, so no per-token NVMe reads and no NVMe re-faulting. Workers never crash from a tier hang, and the
   ~17 GB/host that must tier comes from the coldest VCF pages, the profile tiering is designed for. Cost: 24 GB/host
   becomes non-reclaimable DRAM, so the tiered stack's effective ratio rises (docs warn about this). VERIFIED-in-docs
   (pairing) / UNVERIFIED (net effect on your stack).
2. **Optional: `sched.mem.lpage.enable1GPage=TRUE` on workers** (needs 1 and a full reservation). Fewer TLB misses for weight
   scans. Best-effort; power on soon after a host boot. VERIFIED-in-docs (requirements) / UNVERIFIED (speed-up for llama.cpp in your VMs).
3. **VM-VM anti-affinity (must/should) across the 3 workers, plus HA VM override "VM restart priority = Disabled" for
   workers.** `das.respectVmVmAntiAffinityRules` defaults true in 7/8, so HA will not stack two workers ("insufficient
   resources" event instead). VERIFIED-in-docs (8.0 HA advanced options,
   https://docs.vmware.com/en/VMware-vSphere/8.0/vsphere-availability/GUID-E0161CB5-BD3F-425F-A7E0-BF83B005FECA.html).
   Disabled restart guarantees a host loss never triggers a 24 GB power-on into a full host (your PSOD path).
4. **Power-on ordering.** Tiering only engages around 80% DRAM, and power-on allocation is what hurt you. After a host
   boot, power **workers first** (reservation admitted against free DRAM, 1 GB pages most likely), then the VCF stack
   (cold pages demote gradually). If the stack is already running, power on one worker at a time per host and wait until
   `memstats ... tier1Consumed` stops growing before the next. Never power on a worker on a host whose DRAM consumed is above about 75%.
   UNVERIFIED (inference from documented 80% trigger and power-on note). Whether reservation admission for a
   tier-disabled VM counts only DRAM: UNVERIFIED. Test with one worker.
5. **NUMA/vCPU on the 7945HX.** One socket, one NUMA node, so vNUMA is not a factor. Use 1 socket x N cores. Two CCDs,
   each with 8 cores and its own 32 MB L3: keep each worker at **8 vCPU or fewer** so it can live in one CCD/LLC
   (ESXi's scheduler is CCX/LLC-aware since 7.0U2; 9.1 adds "topology-aware scheduling"). CPU inference is DRAM-bandwidth
   bound on 2-channel DDR5, so more vCPUs past the bandwidth knee do not help. Do not set Latency Sensitivity High.
   VERIFIED (CCX-aware scheduler: https://core.vmware.com/blog/optimized-esxi-cpu-scheduler-amd-epyc-processors) /
   UNVERIFIED (8-vCPU optimum for your model; your existing worker design notes win if they differ).

### Per-VM tiering exclusion for vcsa / nsxa / sddc-manager (option only; user previously declined)
- Effect: those three never power off from a tier hang. A tier hang then only takes down the remaining tiered VMs
  (Ops, fleet, edges, …), which HA restarts.
- Cost: needs full reservations (vcsa + nsxa + sddc-manager are tens of GB). That conflicts with "nsxa's reservation stays
  removed" and shrinks the DRAM left for tiering on whichever host runs them. Combined with recommendation 1 it may push
  the remaining tiered VMs' active ratio over 50%. **Measure active memory before considering it.** The cheaper half-step:
  exclude only vcsa (vCenter is what you need to recover everything else).

---

## 5. Prioritised settings list
MM = maintenance mode. "Verify" = how to prove it took effect.

| # | Setting (exact) | Expected effect | Risk | MM / reboot | Verify |
|---|---|---|---|---|---|
| 1 | Workers: power off; add `sched.mem.enableTiering = "FALSE"`; set reservation = full memory (`sched.mem.min` = memsize) | Weights never on NVMe; workers immune to tier hang; tier traffic limited to cold VCF pages | 24 GB/host DRAM locked; higher effective ratio for the stack | VM power-off only | `memstats -r vmtier-stats -u mb -s name:memSize:active:tier1Target:tier1Consumed` shows tier1Consumed = 0 for workers; vmx shows both keys |
| 2 | Workers: VM-VM anti-affinity rule (must) + HA VM override restart priority = Disabled | No HA power-on storm of 24 GB fully active VMs into a surviving host | Lost worker stays down until you act | none | Cluster > Configure > VM/Host Rules and VM Overrides; simulate with a host in MM: DRS must not co-locate |
| 3 | Power-on order: workers first after host boot; one worker at a time; DRAM consumed < ~75% before each | Avoids power-on allocation spikes and demotion bursts | Operational discipline only | none | `memstats` tier1Consumed growth flat before the next power-on; host Memory tile |
| 4 | Read APST/power state on each tier device: `esxcli nvme device list` (find vmhba) then `esxcli nvme device feature apst get -A vmhbaN -S current`, `esxcli nvme device feature pm get -A vmhbaN -S current`, `esxcli nvme device feature cap -A vmhbaN` | Tells you whether power-state transitions are even a candidate trigger | None (read-only) | none | Output recorded per host |
| 5 | If APSTE=1: `esxcli nvme device feature set -A vmhbaN -f 0x0c -c 0` (add `-S` only if `cap` says save is supported); and/or pin PS0 with `esxcli nvme device feature pm set -A vmhbaN -v 0`. Re-apply at boot from `/etc/rc.local.d/local.sh` since unsaved features reset on a controller reset or power cycle | Removes APST sleep/wake as a hang trigger (known Samsung failure class) | Slightly higher idle power/temperature; syntax UNVERIFIED on ESXi; no-op if APST was already off | none (live) | Re-run `apst get` / `pm get`; re-check after reboot |
| 6 | BIOS on all 3 hosts: PCIe ASPM = Disabled (and any "PCIe power saving / clock gating" = Disabled) for the M.2 root ports; record BIOS version, consider Minisforum's latest after reading release notes | Removes link power-state transitions (L0s/L1) as a trigger | Idle power +a few W; BIOS flash risk (a forum poster bricked a Minisforum board) | Host down (MM + reboot) | BIOS screen; cannot be read from ESXi (UNVERIFIED that `lspci -vvv`-style output exists on ESXi 9) |
| 7 | Lower tier size: in MM, `esxcli memtier config set -r 50` (or via the Configuration Profile `memtier > nvme` tier_size_pct if the cluster uses profiles; edit there to avoid drift) | Caps advertised capacity at 144 GB/host so DRS/HA cannot over-pack; still about 2.8x the ~17 GB needed | N-1 capacity drops (2 hosts x 144 = 288 GB vs 219 GB stack: still fits without workers) | MM, no reboot (9.1) | `esxcli memtier config get -r`, `esxcli memtier status get`, host memory capacity in vCenter |
| 8 | Monitoring: vCenter alarm on "NVMe Memory Tiering device is not healthy" with email; cron/LXC poll of `esxcli system health report get -r vmw.memTierHealth` and `esxcli nvme device log error get -A vmhbaN -e 64`; syslog alert on `NVMEIO|NVMEDEV|in state 8|in state 9|recovery mode|I/O timeout|tiered page` | Earliest available warning; lets you put the host into MM and stop HA landing VMs on it | Alert noise; may give no lead time on a sudden hang | none | Trigger a test syslog line; confirm the alert fires |
| 9 | Runbook on tier hang: immediately put the host into MM / or at least "Disconnect" from DRS placement; do not let HA restart VMs back onto it; AC power-cycle (hyp03 always). After recovery, `esxcli memtier status get` and the health report before exiting MM | Stops crash-restart loops onto the sick host | Manual | MM | Health report `device_state` healthy |
| 10 | Read before changing: `esxcli nvme device timeout get -A vmhbaN`; `esxcli system module parameters list -m nvme_pcie`; `-m vmknvme`. Do **not** change queue counts/timeouts without a specific KB | Baseline only | Changing them is unproven | — | — |
| 11 | Option (structural): replace PM981 tier drives with Optane P1600X 118 GB (or another datacenter-firmware M.2), or add a second device and enable 9.1 software mirroring | Targets the controller-hang root cause / survives one device hang | Cost; slot availability (mirroring likely impossible on the BD795i SE without losing vSAN or boot) | MM to swap (deactivate tiering, replace, re-enable) | Health report model; for mirroring, both devices listed in `esxcli memtier device list -c` |
| 12 | Option (user decision): `sched.mem.enableTiering=FALSE` + full reservation on vcsa (minimum) or vcsa/nsxa/sddc-manager | Management plane survives a tier hang | DRAM cost; conflicts with nsxa reservation decision; may push the rest over 50% active | VM power-off (VCF mgmt VMs: plan the outage) | `memstats` tier1Consumed = 0 for those VMs |

Not recommended: Latency Sensitivity High (exclusive pCPUs on a 16C host); "All locked"/reservation alone to keep
weights in DRAM (does not work with tiering); undocumented `Mem.Tier*` keys; lowering `nvme device timeout` as a fix;
cross-flashing OEM firmware; `config.vpxd.ResourceManager.maxCostPerHost` to throttle vMotion (unsupported, also
throttles clones, per community thread 571382 and Denneman's cautions).

## Sources (accessed 2026-10-12)
- TechDocs vSphere 9.1 Memory Tiering over NVMe + children (failure scenarios, best practices, capacity planning, host memory size, VM profiles): https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-1/vsphere-resource-management/memory-tiering-over-nvme.html
- ESXCLI 9.1.0 memtier: https://developer.broadcom.com/xapis/esxcli-command-reference/9.1.0/namespace/esxcli_memtier.html ; nvme: .../esxcli_nvme.html
- KB 448305 (tiering eligibility, 80% trigger, memstats); KB 413903 (NVMe stuck I/O, power-cycle); KB 448948 (State 9); KB 434866 (NVMe SMART VOB alarms); KB 313504 (vmknvme queues); KB 313285 (vCenter 8 alarm scripts)
- Morera (VCF blog): Tips for Success 2026-04-07; 9.1 Enhancements 2026-05-07; Configuring in 9.1 2026-05-19; Reservations 2026-08-06; Part 7 Advanced Config 2026-01-27; esxcli memtier namespace 2026-09-25
- Epping, Memory Tiering Considerations and FAQ, 2026-07-16; Lam, tiering health monitoring 2026-09-22; Lam, 9.1 esxcli 2026-06
- vSphere 9.1 FAQ (May 2026): "introduces RAID 1 mirroring for memory tiering … better protection against NVMe failure"
- Linux nvme pci.c quirk table (torvalds/master); Red Hat 6589401 (PM981 CSTS=0x3); Proxmox D3cold thread; Level1Techs BD795i timeouts (2026-06-29); Broadcom community ESXi 7 M.2 failing thread; WinRAID Samsung OEM firmware thread
