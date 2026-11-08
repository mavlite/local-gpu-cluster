# Hypervisor-level and operational RAM/CPU reduction for a VCF 9.1.1 lab, plus optional components

Lab context (from the assignment): 3x Ryzen 16C/32T, 64 GB DRAM + NVMe memory tiering (~187 GB reported per host), vSAN ESA, DRS PartiallyAutomated, HA on. Management VMs: vcsa 4/21, sddc-manager 4/16, nsxa 6/24, ops 2/8, opscollector 4/16, licsrv 2/4, VSP (VCF Services Runtime / VCFMS) 3x control 4/10 + 3x worker 10/16. No reservations. Known hazards: consumer tier NVMe hangs; VMs with pages on a dead tier crash on vMotion; License Server 180-day disconnected license fails closed.

## Q1 — ESXi 9.x memory mechanisms (TPS/salting, balloon, compression, tiering ratio, per-VM tier exclusion) and which VMs to keep in DRAM

### Takeaway
Memory tiering is the only 9.x mechanism that meaningfully stretches DRAM, and it only engages once host DRAM is ~80% consumed; it moves cold pages only. Per-VM opt-out is `sched.mem.enableTiering = FALSE`, and Broadcom says a tier-disabled VM must also be fully reserved or it is "risky". TPS is intra-VM only by default (salting = 2) and gives little across appliances unless salting is relaxed, which is a security trade-off. No 9.x-specific Broadcom guidance on management appliances on tiered memory was found.

### Cited Findings
**Tiering activation and behaviour**
- Tiering kicks in only when host DRAM (Tier 0) consumption reaches about 80%. Below that, no pages go to NVMe Tier 1. Right after crossing 80% the scan/migration rate is very conservative and rises with pressure — [Broadcom KB 449016](https://knowledge.broadcom.com/external/article/449016/understanding-nvme-memory-tiering-activa.html)
- Only "cold" pages (by recency/frequency) move to Tier 1. The hot working set stays in DRAM — [KB 449016](https://knowledge.broadcom.com/external/article/449016/understanding-nvme-memory-tiering-activa.html)
- Per-VM Tier 1 view on the host: `memstats -r vmtier-stats -u mb -s name:memSize:active:tier1Target:tier1Consumed`. vSphere Client: Monitor > Performance > Advanced > Memory, with the counters "Memory type read/write bandwidth" and "Memory type read/write latency" — [KB 449016](https://knowledge.broadcom.com/external/article/449016/understanding-nvme-memory-tiering-activa.html)

**Ratio parameter**
- `Mem.TierNVMePct` default is 100, which means DRAM:NVMe = 1:1. Examples are 200 (1:2) and 50 (2:1). Before raising it, confirm all active memory fits in host DRAM. The blog recommends keeping the default for most workloads — [VMware VCF blog, memory tiering advanced config](https://blogs.vmware.com/cloud-foundation/?p=28715)
- Lam's older vSphere 8.0U3 post (tech preview, OLDER) gives the default as 25% — [William Lam 2024](https://williamlam.com/2024/10/useful-nvme-tiering-reporting-using-vsphere-8-0-update-3-apis.html). This conflicts with the 9.x value of 100. Treat 25% as 8.0U3-only and check the live host.
- In VCF 9.1 the older esxcli commands "will no longer properly enable NVMe Tiering". 9.1 uses a new `esxcli memtier` namespace — [William Lam, June 2026](https://williamlam.com/2026/06/vcf-9-1-are-you-using-the-correct-esxcli-command-to-enable-nvme-tiering.html); [VMware blog: configuring memory tiering in VCF 9.1](https://blogs.vmware.com/cloud-foundation/2026/05/19/more-memory-less-effort-configuring-memory-tiering-in-vcf-9-1/)
- No documented maximum for the ratio was found (see Gaps).

**Per-VM exclusion and reservations**
- Per-VM opt-out: VM advanced parameter `sched.mem.enableTiering = FALSE`, which keeps the VM's pages in DRAM only. `sched.mem.EncryptTierNVMe = TRUE` encrypts one VM's tiered pages. Host-wide, `Mem.EncryptTierNvme = 1` encrypts with AES-XTS — [VMware VCF blog](https://blogs.vmware.com/cloud-foundation/?p=28715); [Yellow-bricks memory-tiering tag](https://www.yellow-bricks.com/tag/memory-tiering/)
- To revert, set the parameter to TRUE or remove it to inherit the host default (vSphere 9.0) — [Broadcom KB 448305](https://knowledge.broadcom.com/external/article/448305/rhel-8-virtual-machines-do-not-utilize-m.html)
- Tiering is disabled automatically (implicitly) for VMs with a passthrough device, Latency Sensitivity = High, or Fault Tolerance. These and other pinned cases (UPT, SEV) take memory from DRAM only — [VMware blog, Aug 2026: memory tiering and VM memory reservation](https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/)
- Broadcom says to fully reserve every tier-disabled VM ("All locked" or a reservation equal to the full memory size). It calls an unreserved tier-disabled VM "risky" — [Aug 2026 blog](https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/)
- A reservation, including "Reserve all guest memory (All locked)", does not pin memory to DRAM. Reserved pages can still sit on NVMe. "All locked" only blocks swap, balloon and compression reclaim, and makes the swap file 0 MB. With a partial reservation, the swap file is configured minus reserved — [Aug 2026 blog](https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/)
- Every tier-disabled VM moves the effective ratio for the rest. In the blog's example, fully reserving one 256 GB tier-disabled VM on a 512+512 host leaves the other VMs at 1:2 — [Aug 2026 blog](https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/)
- Best practice is to keep active memory below 50% of total (DRAM+NVMe) physical memory — [Aug 2026 blog](https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/)
- Workloads that should not be tiered: low-latency VMs, FT VMs, large-memory-page VMs, SAP HANA and other in-memory databases, HFT, and security VMs. The list is not exhaustive — [VMware VCF blog](https://blogs.vmware.com/cloud-foundation/?p=28715)

**NVMe failure risk**
- After a tier NVMe failure the host stays up. Impact depends on whether affected VMs touch their tiered pages: none if no cold pages are on the device, otherwise "a handful of VMs may eventually HA-fail over". RAID-1/5/10 (HW RAID, tri-mode, VROC) is the supported mitigation. vSAN ESA does not support RAID controllers — [VMware blog, NVMe tiering design part 2, Nov 2025](https://blogs.vmware.com/cloud-foundation/2025/11/18/nvme-memory-tiering-design-and-sizing-on-vmware-cloud-foundation-9-part-2/)

**TPS / salting (all OLDER, ESXi 5.x–7.x)**
- Since the 2014–2015 patches, `Mem.ShareForceSalting` defaults to 2. Each VM is salted by `sched.mem.pshare.salt`, falling back to `vc.uuid`, so TPS is intra-VM only. 0 = inter-VM sharing for all VMs. 1 = inter-VM by default but per-VM salts are honoured. Giving several VMs the same `sched.mem.pshare.salt` lets them share with each other — [VMware KB 2097593](https://kb.vmware.com/kb/2097593); [VMware KB 2091682](https://kb.vmware.com/s/article/2091682); [rakhesh.com](https://rakhesh.com/virtualization/til-transparent-page-sharing-tps-between-vms-is-disabled-by-default/)
- CIS benchmarks for ESXi 6.7/7.0 recommend keeping it at 2 (the L2 control) — [Tenable CIS ESXi 7.0 audit item](https://www.tenable.com/audits/items/CIS_VMware_ESXi_7.0_v1.5.0_L2_VMware.audit:ab4c0cc96d439aa9cd8ac699421b2bfb)
- I found no ESXi 9.x source confirming the default or describing how TPS interacts with tiering.

**Balloon / swap signals**
- Any non-zero balloon (esxtop MCTLSZ/MCTLTGT) means the host has reclaimed under pressure. Any non-zero host swap (SWCUR, SWR/s, SWW/s) means contention that affects VMs. The target is sustained zero — [Broadcom KB 438023 (ESXi 8.0 rightsizing)](https://knowledge.broadcom.com/external/article/438023/rightsizing-virtual-machines-on-esxi-80.html)

### Inferences
- With 64 GB DRAM per host and ~120 GB of configured management memory across 3 hosts (~40 GB per host before any other VMs), DRAM can sit below the 80% trigger. In that case tiering does nothing, and the ~187 GB "reported" capacity is headroom that has not been exercised. Measure `tier1Consumed` before attributing any savings to tiering.
- The tier-hang/vMotion-crash history points to keeping control-plane VMs whose loss breaks the lab in DRAM: vcsa, nsxa, licsrv, and possibly the VSP control nodes. The cost is a DRAM-backed full reservation for each one, per Broadcom. That shrinks the tierable DRAM for everything else, so pick a short list. (This matches the earlier project memory: enableTiering=FALSE plus full reservation for critical VMs.)
- Relaxing `Mem.ShareForceSalting` to 0, or setting a shared salt on identical Photon appliances (the six VSP nodes), could reclaim some duplicate pages. Two caveats: ESXi backs guests with 2 MB large pages and generally only breaks them for sharing under memory pressure (general ESXi behaviour, not confirmed by a 9.x source here), and it reopens the cross-VM side-channel that salting closed. In a lab this is a low-value, low-risk experiment. Measure the "shared" and "shared common" counters before and after.
- Do not raise `Mem.TierNVMePct` above 100 on consumer drives. A larger tier puts more cold pages on the device that is already known to hang.

### Gaps
- No Broadcom or Lam guidance specifically on running vCenter, NSX, SDDC Manager or VSP appliances on tiered memory. The supported-workload lists don't name them.
- No documented maximum for `Mem.TierNVMePct` was found. Check on a host with `esxcli system settings advanced list -o /Mem/TierNvmePct`.
- Confirmation that `sched.mem.enableTiering` behaves unchanged under the 9.1 `esxcli memtier` model was not found. Yellow-bricks and the VMware blog name the key, but not explicitly for 9.1.
- No ESXi 9.x sources on memory compression defaults (`Mem.MemZipEnable`, `Mem.MemZipMaxPct`) or ballooning behaviour changes.

## Q2 — CPU: vCPU right-sizing, co-stop/ready, oversized vCPU (10-vCPU workers on 16-core hosts), latency sensitivity, shares

### Takeaway
On 16C/32T hosts, a 10-vCPU VM is not "wide" by core count but uses most of a host's cores. Broadcom's thresholds are ready under ~5% per vCPU (benign), 5–10% (investigate), over 10% (impact), and co-stop under ~3% per vCPU (normal). VCF 9.1.1 itself right-sized VCFMS workers and, for Simple deployments, removed one 12 vCPU/24 GB worker. Latency Sensitivity = High is the wrong lever here: it pins memory out of tiering and needs reservations.

### Cited Findings
- Thresholds: CPU ready (%RDY) below about 5% per vCPU is benign, 5–10% means investigate, above 10% means noticeable impact. Sustained ready usually means host-level contention, not VM undersizing. Co-stop (%CSTP) below about 3% per vCPU is normal, and sustained higher values mean too many vCPUs, so reduce the count. Aim for guest CPU at or below 80% — [Broadcom KB 438023](https://knowledge.broadcom.com/external/article/438023/rightsizing-virtual-machines-on-esxi-80.html)
- For wide VMs, keep vCPUs at or below the physical core count and split them evenly across the fewest NUMA nodes. Leave Cores per Socket at "Assigned at power on" (NumCoresPerSocket = 0) on HW20 and later. NUMA scheduling is enabled on hosts with 4 or more cores and at least 2 cores per node — [KB 438023](https://knowledge.broadcom.com/external/article/438023/rightsizing-virtual-machines-on-esxi-80.html)
- KB 438023 explicitly does not cover shares, reservations, limits or latency-sensitivity tuning — [KB 438023](https://knowledge.broadcom.com/external/article/438023/rightsizing-virtual-machines-on-esxi-80.html)
- Relaxed co-scheduling means modern ESXi co-stop is much lower than under strict co-scheduling. The vendor guide treats over 1% sustained as worth investigating and over 3% as strong evidence of oversizing. This is a third-party rule of thumb, stricter than Broadcom's — [Netdata guide: co-stop](https://www.netdata.cloud/guides/vmware-vsphere/vmware-vsphere-cpu-co-stop-high/); [Netdata: vCPU oversizing](https://www.netdata.cloud/guides/vmware-vsphere/vmware-vsphere-vcpu-oversizing/)
- Latency Sensitivity = High disables tiering for the VM, so its memory is pinned in DRAM — [Aug 2026 VMware blog](https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/)
- VCF 9.1.1 changed VCFMS sizing from "largest optional Day-N service" to Day-0-optimized, and right-sized individual components. A new Simple (non-HA) 9.1.1 deployment has one fewer worker (12 vCPU / 24 GB) than 9.1.0. Upgraded deployments don't get this automatically — [William Lam, Sep 2026](https://williamlam.com/2026/09/vcf-9-1-1-adopting-the-reduced-vcf-management-services-vcfms-footprint-after-an-upgrade.html)
- For brownfield 9.1 deployments patched to 9.1.1, Broadcom KB 455842 supplies `rightsize-day0-workers.sh`, which reapplies the Day-0 worker machine type and minimum replica count. Prerequisites: Day-0-only (aborts if Log Management or Real-time Metrics is installed), aborts if `cluster.type` = consumption, not for VVF, run as vmware-system-user then root on a control-plane node, needs a maintenance window for the worker rollout. Monitor with `kubectl get pd vmsp-platform -n vmsp-platform` — [Broadcom KB 455842](https://knowledge.broadcom.com/external/article/455842); [Lam](https://williamlam.com/2026/09/vcf-9-1-1-adopting-the-reduced-vcf-management-services-vcfms-footprint-after-an-upgrade.html)
- In VVF, VCFMS deploys 1 control plane + 2 workers, expanding to 3 workers if Log Management is enabled (from a reader comment) — [Lam 9.1 lab workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)
- Lam has a separate post on enabling HA for a small VCFMS deployment, which suggests Simple (1 control plane) is a supported starting shape — [Lam, June 2026](https://williamlam.com/2026/06/vcf-9-1-enabling-high-availability-for-a-small-vcf-management-services-vcfms-deployment.html)
- 9.1 lab workarounds (SDDC Manager properties) exist for single/dual-host domains (`feature.vcf.vgl-29121.single.host.domain=true`, `bringup.mgmt.cluster.minimum.size=1`) and single-node AVI (`feature.vcf.vgl-41078.alb.single.node.cluster=true`). The page has no VM-size or reservation workarounds for VCFMS, collector or License Server — [Lam 9.1 lab workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)

### Inferences
- The lab's VSP is 3 control + 3 workers (the HA shape). Lam's Simple example is 1 control + 3 workers, reduced by one. Whether KB 455842 also shrinks HA deployments is not stated. If it does, or if a redeploy as Simple were acceptable, that would be the largest single supported reduction: 2 control nodes (8 vCPU/20 GB) plus potentially a worker. Verify `vcf services runtime` sizing on the live lab before relying on this.
- Six VSP nodes total 42 vCPU (3x4 + 3x10) on 3 hosts of 16 cores, which is about 14 vCPU per host from VSP alone before vcsa/nsx/ops. Hand-shrinking VSP worker vCPU is unsupported, because the platform reconciles machine types (KB 455842's whole point is that it re-applies sizes). Hand edits will likely be reverted or break rollouts. Prefer the supported script.
- For non-VSP appliances (ops, opscollector, sddc-manager), cutting vCPU is a lab-only change with upgrade-precheck risk. Measure %RDY and %CSTP first. If co-stop is under 3% per vCPU and ready is under 5%, the CPU "cost" is mostly idle vCPUs, which the relaxed co-scheduler handles cheaply. The real cost of oversizing on small hosts is ready-time inflation during simultaneous bursts (boot storms, upgrades).
- CPU shares only matter under contention. That matches the project memory that Custom 1000 shares on the agent tier had no measured effect without entitlement contention.

### Gaps
- No ESXi 9-specific guidance on SMT/hyperthread accounting for small hosts, and no current Denneman post on it.
- Published VCFMS node sizes (before and after 9.1.1) per profile were not available in the text of KB 455842 or Lam's post. They appear only in screenshots or the script.

## Q3 — How to measure the safe floor (counters and observation window)

### Takeaway
Use the active working set against host DRAM, with zero balloon/swap/tier-1 activity as the guard rail. Over-commit only what stays cold through a full cycle: at least 24 hours (Broadcom's minimum), and in practice a window that includes the nightly jobs, a backup, and an LCM/upgrade precheck, which are the peaks.

### Cited Findings
- Collect over a representative period, "typically 24 hours". A consistently low active-to-allocated ratio means the VM is oversized. Signals that you went too far: non-zero balloon (MCTLSZ/MCTLTGT), host swap (SWCUR, SWR/s, SWW/s), and in-guest paging — [Broadcom KB 438023](https://knowledge.broadcom.com/external/article/438023/rightsizing-virtual-machines-on-esxi-80.html)
- CPU: %RDY under 5% per vCPU and %CSTP under 3% per vCPU — [KB 438023](https://knowledge.broadcom.com/external/article/438023/rightsizing-virtual-machines-on-esxi-80.html)
- Tiering: per-VM `active`, `tier1Target` and `tier1Consumed` via `memstats -r vmtier-stats`. Memory type read/write bandwidth and latency charts in the vSphere Client — [KB 449016](https://knowledge.broadcom.com/external/article/449016/understanding-nvme-memory-tiering-activa.html)
- Before changing the tier ratio, confirm all active memory fits in DRAM. Keep active below 50% of total tiered memory — [VMware blog](https://blogs.vmware.com/cloud-foundation/?p=28715); [Aug 2026 blog](https://blogs.vmware.com/cloud-foundation/2026/08/06/memory-tiering-and-vm-memory-reservation-what-you-actually-need-to-know/)

### Inferences
- Active memory just after boot (NSX 13.9/24, SDDC Manager 2.1/16) understates the steady state. Java appliances (SDDC Manager, Ops, NSX) grow heaps over days, and LCM prechecks or bundle downloads in SDDC Manager spike. Proposed minimum: 7 days covering one backup cycle, one certificate/password-expiry check, and one LCM precheck or bundle download. Use the per-VM max of active plus a margin, not the average.
- Counter set per VM to record: Memory Active, Consumed, Granted, Balloon, Swapped/Swap-in rate, Compressed, the guest-reported in-use figure (VMware Tools guest stats), and Tier1Consumed. CPU: Ready %, Co-stop %, Usage. Per host: DRAM consumed against the 80% tier trigger.
- Safe floor test: the sum over the three hosts of (max active per VM + overhead) should fit in DRAM with the 80% trigger never crossed. That keeps the dead-tier vMotion crash risk at zero. Anything above it is an explicit tiering bet.

### Gaps
- No Broadcom source gives a recommended observation window specific to VCF management appliances or their periodic jobs. 24 hours is the only published number.

## Q4 — Which VCF 9 components can be powered off part-time, what breaks, and licensing cadence

### Takeaway
The License Server must stay on, and Broadcom says so explicitly. VCF Operations is the licensing authority and must not be removed. If it is down, licensed vCenters keep their entitlements, but assignments, capacity changes and syncs fail. Usage must be reported, and licenses refreshed, at least every 180 days. Connected mode submits usage daily. VCF Automation and the whole VCF Services Runtime cluster have supported graceful power-off procedures. SDDC Manager can be shut down per its documentation, but Ops then loses its SDDC data feed.

### Cited Findings
**Licensing chain**
- Disconnected VCF Operations must have license usage manually updated at least every 180 days. If not, licenses are treated as expired and hosts disconnect from vCenter, blocking workload operations. In connected mode, usage is submitted automatically daily, but a manual license update in the UI is still required every 180 days. A renewal extends validity by another 180 days, capped at the contract end date — [Broadcom KB 438771](https://knowledge.broadcom.com/external/article/438771/getting-error-usage-report-overdue-today.html); [KB 438868](https://knowledge.broadcom.com/external/article/438868/vcf-9-broadcom-services-console-license.html); [KB 443726](https://knowledge.broadcom.com/external/article/443726/bsc-portal-reports-zero-license-usage-de.html) (these details are from search-result summaries of these KBs; the full text was not fetched)
- Running disconnected for more than 180 days can make a later switch to connected mode fail and require a new activation code — [KB 428209](https://knowledge.broadcom.com/external/article/428209)
- The VCF Operations appliance and its embedded license services cannot be decommissioned after initial licensing, because Ops is the ongoing licensing authority. Without it the environment eventually enters a restricted management state (no timing given). In full VCF stacks, deploying the License Server is mandatory during the SDDC Manager 9.1 deployment or upgrade. It is IPv4-only — [Broadcom KB 446056](https://knowledge.broadcom.com/external/article/446056/licensing-vcf-operations-91-in-disconnec.html)
- In 9.1, licensing through vCenter is impossible until the License Server is deployed within VCF Management Services — [Broadcom KB 440471](https://knowledge.broadcom.com/external/article/440471/one-or-more-vcenter-instances-are-not-co.html)
- If Ops or the License Server is down, fully licensed vCenters keep their existing entitlements and workloads keep running. New license assignments and capacity changes (such as adding cores or hosts) are blocked. Evaluation or renewal environments fail to sync, and if the 90-day evaluation lapses, hosts show Disconnected and VMs cannot power on. Alarms: the vSphere banner "not connected to a license server", and in Ops "Licenses could not be synchronized with the vCenter systems". Broadcom recommends keeping the License Server VM powered on at all times, in both connected and disconnected modes. No sync interval or maximum tolerable downtime is given — [Broadcom KB 443240](https://knowledge.broadcom.com/external/article/443240)
- Time skew between vCenter and Ops expires SAML tokens immediately and blocks licensing. Use the same NTP source — [Broadcom KB 440471](https://knowledge.broadcom.com/external/article/440471/one-or-more-vcenter-instances-are-not-co.html)

**Powering components off**
- VCF Automation in 9.1: power off and on via VCF Operations > Build > Lifecycle > VCF Management Components > Action > Power off/on. The UI path changed from 9.0 — [Broadcom KB 444635](https://knowledge.broadcom.com/external/article/444635/how-to-power-off-or-power-on-vcf-automat.html)
- The VCF Services Runtime (VSP) cluster: `vcf_services_runtime_shutdown.sh --node-ip <cp-ip> [--dry-run | --poweroff-vms]`, run from Linux with curl and jq (govc for auto power-off) using the vmware-system-user breakglass password. It stops "all VCF management components running on the platform", for example Log Management, Real-time Metrics, and VCF Operations Lifecycle and Configuration Management. It runs prechecks including snapshot detection and sets flags for automatic recovery on boot. Do not force power-off mid-shutdown. After a forced shutdown, use KB 440862 for power-on. The KB does not say whether the License Server stops — [Broadcom KB 440874](https://knowledge.broadcom.com/external/article/440874/)
- SDDC Manager (9.0.2 KB): snapshot SDDC Manager and Fleet Manager, then shut down per documentation. While SDDC Manager is off, VCF Operations loses its data feed from the SDDC — summarised from [Broadcom KB 440732](https://knowledge.broadcom.com/external/article/440732/vcf-instance-inventory-or-objects-such-a.html) and [KB 428433](https://knowledge.broadcom.com/external/article/428433/fleet-management-lifecycle-vcf-manage.html) via search results (OLDER, 9.0.x)
- A known 9.1 issue: the VCF Operations component shows Powered Off with a missing Actions menu — [Broadcom KB 449937](https://knowledge.broadcom.com/external/article/449937/vcf-operations-component-displays-as-pow.html)
- vSAN cluster shutdown requires all VMs except the managing vCenter to be off. PowerCLI: `Get-VsanClusterPowerState`, `Stop-VsanCluster`, `Start-VsanCluster` (Lam, VCF 9.0) — per search result pointing to [williamlam.com vSAN category](https://williamlam.com/category/vsan)

### Inferences
- On the License Server in this lab: it appears as its own VM (licsrv 2/4), but KB 440471 says it is "deployed within VCF Management Services". It may depend on VSP services, so stopping VSP could break licensing even with the licsrv VM still on. KB 440874 doesn't say, so check before using the VSP shutdown script as a nightly saver. Broadcom's "keep the License Server VM on at all times" applies either way.
- Because fully licensed vCenters keep their entitlements while Ops and the License Server are down, short outages (hours to days) of Ops are tolerable for a licensed lab, provided no license assignment or host/core change happens in that window. The hard deadline is the 180-day usage/renewal window. Real data on this lab: the 180-day license fails closed. Treat "Ops plus License Server off" as safe only for short windows, and never near the "Next Usage Report Due" date.
- Candidate power-off schedule, by increasing risk: (1) the Ops collector, if the cloud-proxy requirement allows, though see Q5; (2) VCF Automation, if installed, via the supported path; (3) SDDC Manager when no LCM, cert or password work is planned (it owns password/cert rotation and LCM, so scheduled rotations and expiry remediation will not run); (4) Ops itself for short periods. Keep vcsa, NSX Manager (the dataplane survives, but the management/control plane and DFW changes stop) and the License Server up.
- Re-verify after every power-on (SDDC Manager feed, Ops, VSP pods). Lab memory already records that a cold boot can wedge vpxa and that VSP restarts nodes on shutdown.

### Gaps
- No documented interval for how often the License Server or Ops pushes license state to vCenter, or how long vCenter tolerates a disconnected License Server before restricting a licensed (non-eval) environment.
- No Broadcom statement on what NSX Manager tolerates being powered off in VCF 9 (beyond general knowledge that the dataplane continues), or on SDDC Manager behaviour for scheduled password rotation while it is off.
- No 9.1 KB fetched in full for SDDC Manager graceful shutdown or power-on order of the whole stack.

## Q5 — Can VCF 9.1 run without the Operations collector, or with it merged into Operations?

### Takeaway
Probably not in a supported way. VCF 9.1 requires a cloud proxy in the VCF Instance that hosts VCF Operations for the Ops–SDDC Manager–VCFMS integration, and this is a hard upgrade gate. A local collector (one running inside the Ops node) fails registration validation. The collector VM appears to be a required component, though it may tolerate part-time downtime.

### Cited Findings
- A cloud proxy in the first VCF Instance (the one hosting VCF Operations) is required for the integration between VCF Operations, SDDC Manager and VCF Management Services. It is a hard gate: without it the 9.1 upgrade cannot complete — [WWT: Before you upgrade to VCF 9.1](https://www.wwt.com/blog/before-you-upgrade-to-vcf-91-what-you-need-to-have-ready)
- In 9.1 the VCF instance (vCenter) adapter must point to a healthy Cloud Proxy or a Cloud Proxy collector group, not a local collector, or registration validation fails during upgrade — [Broadcom KB 443106](https://knowledge.broadcom.com/external/article/443106/vcf-instance-cloud-proxy-registration-va.html)

### Inferences
- "Merging" the collector into Ops (using the Ops node's local collector) is exactly the configuration KB 443106 says fails validation. So the opscollector VM (4 vCPU/16 GB) should stay. The supported reductions are right-sizing it, if a smaller cloud-proxy size is offered, and powering it off between upgrades and collection needs. While it is off, Ops loses vCenter/SDDC metrics and the integration health checks, and any upgrade precheck will fail.
- Whether licensing data flows through the cloud proxy (making the collector part of the licensing chain) was not established. If it does, the collector inherits the "keep on" status for license sync.

### Gaps
- No Broadcom doc found stating the minimum supported cloud-proxy size in 9.1, or whether the collector carries License Server ↔ vCenter traffic.
- No source on how long Ops tolerates an offline cloud proxy before alarms or integration failures affect SDDC Manager or VCFMS.
