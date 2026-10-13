# VCF 9.1 lab: cluster-level settings that prevent or slow memory bursts onto the tier NVMe

Research date: 2026-10-12. Companion to `vcf-host-tiering-nvme-settings.md` (host/VM/device levers). This file
covers **cluster-level** levers: vMotion, HA, DRS, vSAN, VSP and operating procedure.

Labels:
- **VERIFIED-in-docs**: Broadcom TechDocs, KB, or API reference text read during this session.
- **VENDOR-BLOG**: VMware/Broadcom staff blog (performance team, Epping, Morera, Lam).
- **COMMUNITY**: forum or third-party.
- **UNVERIFIED**: my inference, or something no source covered.

Many KBs show a blank "Updated On" field. Where that happens I give the access date (2026-10-12).

---

## 0. Headline findings

1. **The burst happens at both ends of a vMotion, and a NIOC limit is the only documented way to set its rate.**
   - The source must read tiered pages back from NVMe. Epping (2026-07-16): "the vMotion process will be slower for VMs
     which have tiered memory pages", and pages on NVMe are read back into DRAM before transfer. VENDOR-BLOG.
   - The destination receives everything into DRAM. Morera (2025-11-18): pages are "decrypted first, then sent over on
     the encrypted vMotion channel" and land in DRAM. Once destination DRAM consumption passes about 80%, the
     destination starts demoting cold pages ("When Memory Consumption is roughly 80%, the algorythm will start
     considering to move cold memory pages from DRAM to NVMe", Epping). VENDOR-BLOG.
   - So an incoming vMotion causes a burst of **writes** (demotions) on the destination's tier device, on top of the
     **read** burst at the source.
   - A NIOC v3 vMotion **Limit** caps bytes per second "on a single physical adapter" (TechDocs 9.0). VERIFIED-in-docs.
   - That the cap also slows the source's NVMe read rate and the destination's demotion rate (through backpressure)
     is UNVERIFIED. It is the expected behaviour of a streaming pipeline, but no source states it.
2. **HA restart concurrency has no documented knob in 9.x.**
   - `das.perHostConcurrentFailoversLimit` (default 32) appears in KB 313261 only for 5.0–6.5. It is absent from the
     vSphere 9.0 "vSphere HA Advanced Options" table. UNVERIFIED that it still works.
   - What is documented in 9.0: restart priority tiers, a "VM Restart Priority Condition", and an additional delay.
     These serialize restarts **between** tiers, not within a tier.
   - **The strongest documented cascade-breaker is a smaller restart scope.** Set HA restart to Disabled per VM, and
     use `das.reregisterrestartdisabledvms` so those VMs are registered on another host without being powered on.
3. **VM Monitoring can be disabled per VM or cluster-wide** (KB 337335 workaround: "disable Virtual Machine
   Monitoring"). This removes the "guest OS crash" resets of sddc-manager and the Ops collector during an incident.
   VERIFIED-in-docs.
4. **DRS PartiallyAutomated does not evacuate a host entering maintenance mode by itself.** KB 451053: the task
   "stalls between 29% and 32%" and vCenter "waits for the administrator to manually approve or initiate the
   migration". VERIFIED-in-docs.
   - This works in your favour: every evacuation is already manual and can be done one VM at a time.
   - The cost: VCF workflows that do an EMM dry run (upgrade prechecks) need it to pass. VCF Installer and brownfield
     import validation require DRS **Fully Automated** (KB 441786, KB 444167). VERIFIED-in-docs.
5. **No source says SDDC Manager re-applies HA or DRS settings.** I searched VCF 9.x docs and KBs.
   - Documented interactions: upgrade prechecks fail the EMM dry run when admission control has no room
     (KB 439134).
   - Broadcom's documented resolution in one 9.1 case is to **disable admission control for the upgrade and re-enable
     it afterwards** (KB 421536).
   - vLCM can deactivate HA admission control during remediation itself (vSphere 8.0 vLCM docs).
   - **Host** advanced settings are different: vSphere Configuration Profiles flags them as drift (automatic compliance
     check every 8 h) and remediation pushes the desired value back. Set host advanced options in the cluster's
     configuration profile, not with `esxcli`.
6. **In a 3-host vSAN cluster the Object Repair Timer barely matters.** vSAN "cannot rebuild data on another host" in
   2- and 3-host clusters (vSphere 8 vSAN planning). VERIFIED-in-docs. The real ESA-on-10G lever is
   `/VSAN/DOMNetworkSchedulerThrottleComponent=1` (KB 410662). VERIFIED-in-docs.
7. **The ~10 s same-host restart of a VSP VM is not HA.**
   - HA does not restart a guest-initiated shutdown, and VM Monitoring needs a 30–120 s heartbeat timeout.
   - VSP is a Kubernetes platform that self-remediates nodes (KB 389510). Its supported off switch is the platform
     shutdown script (KB 440874) or Fleet Management power-off, not a vSphere setting.
   - Which controller powers the VM back on is UNVERIFIED. Find out from the vCenter task "Power On virtual machine":
     its "Initiated by" user names the service account.
8. **VCF 9.1 runs DRS vMotions in parallel.** The VVF 9.1 announcement (2026-05-05) says "Parallel Processing of DRS
   vMotion … allowing multiple migrations to occur simultaneously". VENDOR-BLOG. This makes a concurrency cap more
   relevant, but the only cap (vCenter `config.vpxd.ResourceManager.costPerVmotionESX6x`) is "not officially
   supported".

---

## 1. Throttling vMotion

### 1.1 NIOC v3: vMotion system traffic (vDS)
- Semantics, quoted from TechDocs 9.0 "Bandwidth Allocation Parameters for System Traffic". VERIFIED-in-docs.
  - Shares: "Shares, from 1 to 100, reflect the relative priority of a system traffic type … against the other
    system traffic types that are active on the same physical adapter."
  - Reservation: "The minimum bandwidth, in Mbps, that must be guaranteed on a single physical adapter."
  - Limit: "The maximum bandwidth, in Mbps or Gbps, that a system traffic type can consume on a single physical
    adapter."
  - "The total bandwidth reserved among all system traffic types cannot exceed 75 percent" of the slowest adapter.
- vCenter propagates the allocation from the vDS "to each physical adapter" on connected hosts. VERIFIED-in-docs.
- **The limit is per uplink.** If the vMotion port group has two active uplinks, or multi-NIC vMotion, the effective
  cap doubles. Give the vMotion port group exactly one Active uplink (the other Standby) so the limit is the real
  cap. Per-uplink behaviour was VERIFIED-in-docs; the advice to use one Active uplink is mine.
- **Shares do nothing on an idle 10G link.** Only Limit throttles when there is no contention. Frank Denneman/VMware:
  limits "impose hard limits … even when network bandwidth is available". VENDOR-BLOG.
- Floor: Broadcom KB 312562 says 250 Mbps "dedicated vMotion network bandwidth per concurrent vMotion operation".
  If a VM dirties memory faster than the link carries it, vMotion cancels with "amount of changing memory … greater
  than the available network bandwidth". VERIFIED-in-docs (KB text as surfaced by search).
- Choosing a value is UNVERIFIED and must be measured.
  - Start at **3 Gbps (≈375 MB/s)** on the vMotion uplink. That is well under a 10G link and probably under the
    PM981's post-cache sustained write rate.
  - Lower it (2 Gbps) if a tier device still shows latency spikes during a test migration.
  - Raise it if busy VCF VMs (vCenter, NSX) fail to converge.
- Concurrency is unaffected by the limit. vCenter derives vMotion slots from the NIC's **reported link speed**, so a
  NIOC limit does not reduce the number of simultaneous vMotions (VMware/Denneman, vSphere 5 era). VENDOR-BLOG, old.
  With two vMotions in flight they share the capped bandwidth, so the total burst stays capped.
- UI path: Networking > vDS > Configure > Resource Allocation > System traffic > vMotion Traffic > Edit > Limit.
  NIOC must be enabled on the vDS. VCF-built vDSes normally have it enabled; check under vDS > Configure >
  Properties.

### 1.2 vMotion host advanced settings
- `Migrate.VMotionStreamHelpers`: default 0 = "dynamically allocates one stream per IP" (VMware blog 2019). VENDOR-BLOG.
  It raises throughput and cannot throttle below one stream. **Leave it at 0.**
- Encrypted vMotion (per VM: VM Options > Encryption > Encrypted vMotion = Disabled / Opportunistic / Required).
  Tiered pages are decrypted at the source before they enter the vMotion channel (Morera). VENDOR-BLOG.
  Turning encrypted vMotion off saves source CPU but does not change the memory or NVMe burst. **Not a burst lever.**
- No documented "precopy rate" or "memory read rate" Migrate.* option exists in any source found. UNVERIFIED that one
  exists.

### 1.3 Concurrent migrations per host
- Default per-host limit: "eight" concurrent vMotions. Each vMotion charges the host "2 units". VMware performance
  blog 2021-02-26. VENDOR-BLOG.
- vCenter advanced setting `config.vpxd.ResourceManager.costPerVmotionESX6x`:
  - Default 2 gives 8 concurrent. The blog set 4, which gave 4 concurrent. By extrapolation, **16 gives 1 concurrent
    per host** (UNVERIFIED on 9.x; the key may have been renamed).
  - The blog warns: "changing the limits is not officially supported." Denneman says support will ask you to remove
    such settings before troubleshooting.
  - Set it in vCenter > Configure > Settings > Advanced Settings.
- Recommendation: in this lab, migrating by hand one VM at a time (section 3) gives the same result without an
  unsupported key. Use the key only if a VCF workflow keeps launching parallel evacuations.

### 1.4 Making vMotion of specific VMs impossible or manual-only
| Method | Effect | VCF impact | Label |
|---|---|---|---|
| DRS VM override **Disabled** | "vCenter does not migrate that virtual machine or provide migration recommendations for it" (TechDocs 9.0). A manual vMotion is still possible. | EMM of that VM's host waits for you; harmless under PartiallyAutomated. | VERIFIED-in-docs |
| DRS VM override **Manual** | Recommendations only. | Same. | VERIFIED-in-docs |
| VM-Host **must** rule (VM group → host group of one host) | DRS never moves the VM off the group. HA "won't fail over a VM if doing so would break" a required rule (TechDocs 9.0, HA+DRS). | Blocks EMM of that host. A host failure means **no HA restart** of that VM. Never use it for vCenter. Suits worker VMs. | VERIFIED-in-docs |
| VM-Host **should** rule | Soft preference. HA respects it by default (`das.respectvmhostsoftaffinityrules`, "Default is true in vSphere 6.5 and later"). | Safe for VCF VMs. Use it to keep vCenter and NSX on chosen hosts. | VERIFIED-in-docs |
| `DisabledMethod` (internal API, `RelocateVM_Task`/`MigrateVM_Task`) | Blocks all migrations of the VM, including Storage vMotion since 6.5. | "Internal APIs are not officially supported." Would break SDDC Manager / vLCM workflows that move the VM. **Do not use.** | COMMUNITY (William Lam) |

---

## 2. HA settings that stop cascades

### 2.1 What actually cascades
A tier-device hang causes "the VM … [to be] restarted (HA event) when a page is being accessed on the failed device"
(Epping FAQ). Each restart lands on another host and boots there. The guest touches memory during boot, and if that
host is near 80% DRAM it demotes pages to **its** tier device: the next burst. VENDOR-BLOG plus inference. The levers
below cut the **number** of restarts, their **ordering**, and their **timing**.

### 2.2 Settings (vSphere 9.0 TechDocs "vSphere HA Advanced Options" table unless noted)
- **Per-VM HA restart priority = Disabled** (VM Overrides).
  - Workers already have it. Extend it to everything that isn't needed to recover: VCF Ops collector, fleet
    management, and VSP nodes (see §5 for why VSP is a special case).
  - Keep restart enabled for vCenter, NSX Manager and SDDC Manager.
  - API: `ClusterDasVmSettings.restartPriority` = `disabled`: "vSphere HA is disabled for this virtual machine".
    VERIFIED-in-docs.
- **`das.reregisterrestartdisabledvms = true`.** For VMs with HA disabled, registers them on another host after a host
  failure, without powering them on. Workers and Ops VMs stay manageable after a host loss without a boot burst.
  VERIFIED-in-docs (9.0 table).
- **Restart priority tiers plus a condition and delay** (Cluster > Configure > vSphere Availability > Edit > Failures
  and Responses > Host Failure Response).
  - "A specific condition must be selected as well as a delay after that condition has been met" before HA moves to
    the next priority (TechDocs 9.0). VERIFIED-in-docs.
  - API: `ClusterVmReadiness.readyCondition` (default `none`) and `postReadyDelay` ("Additional delay in seconds
    after ready condition is met"). VERIFIED-in-docs.
  - Timeout: `restartPriorityTimeout` ("Timeout specified in seconds"). The UI default of 600 s comes from Epping
    (2018). VENDOR-BLOG.
  - Suggested values (UNVERIFIED):

    | VM | Restart priority |
    |---|---|
    | vCenter | High |
    | NSX Manager | Medium |
    | SDDC Manager | Low |
    | Everything else | Disabled |

    Set condition = "Guest Heartbeats detected", additional delay = 120–300 s, timeout = 600 s. This puts at least
    one boot plus a settle period between each management VM's start.
  - Keep "Highest" unused (older VCF design: "reserves highest for future needs"). VERIFIED-in-docs, VCF 4.x/5.x
    design KBs.
  - Note on NSX Edges: deploying or redeploying an Edge from NSX Manager sets its restart priority to **Highest**
    automatically (NSX 4.1 docs). Re-check it after any Edge redeploy. VERIFIED-in-docs, NSX 4.1.
- **`das.perHostConcurrentFailoversLimit = 1`.** Documented only for 5.0–6.5 in KB 313261 ("The number of concurrent
  failovers a given FDM will have in progress at one time … default 32"). Not in the 9.0 table. UNVERIFIED on 9.1:
  set it, then test with a controlled host isolation or power-off before relying on it.
- **`das.maxvmrestartcount`**: retry cap. KB 313261 lists 5 attempts (5.x) and 6 (6.x) if unset.
  - Lowering it to 1–2 stops HA from repeatedly re-placing a VM onto hosts with hung devices.
  - Documented only to 6.5. UNVERIFIED on 9.x.
- **Admission control.** With no N-1 capacity, any reserved-capacity policy blocks power-ons and EMM.
  - KB 421536 (VCF 9.1) resolution: Disable "Define host failover capacity by", run prechecks, re-enable after the
    upgrade. VERIFIED-in-docs.
  - **Recommendation: Admission control = Disabled.** Accept that HA restarts can overcommit. Rely on the small
    restart scope above to keep overcommit (and tiering pressure) bounded.
  - Percentage-based admission control would only make sense if workers were off during upgrades.
- **VM Monitoring** (Failures and Responses > VM Monitoring).
  - KB 337335 workaround: "disable Virtual Machine Monitoring". Event text: "This virtual machine reset by HA.
    Reason: VMware Tools heartbeat failure." VERIFIED-in-docs.
  - **Recommendation:** set cluster VM Monitoring to Disabled. If you want it, keep "VM Monitoring Only" at
    **Low** sensitivity and override sddc-manager and Ops VMs to Disabled.
  - Older VMware blog presets: Low = 120 s failure interval, 480 s minimum uptime, 3 resets per 7 days;
    Medium = 60 s, 240 s, 3 per 24 h; High = 30 s, 120 s, 3 per 1 h. VENDOR-BLOG (older).
  - `das.iostatsinterval` (default 120 s, "Setting 0 disables the check", "Values below 50 are not recommended"):
    leave it at 120 or raise it to match the failure interval. VERIFIED-in-docs.
- **Host isolation.** With vSAN on, "HA interagent traffic flows over this storage network". A host is isolated when
  "Isolation addresses not pingable and vSAN storage network inaccessible". vSAN datastores cannot be heartbeat
  datastores (TechDocs 9.0 "Using vSphere HA with vSAN"). VERIFIED-in-docs.
  - Set `das.isolationaddress0` = an always-up IP **on the vSAN subnet** and `das.usedefaultisolationaddress = false`.
    Changing either requires deactivating and re-activating HA ("you must deactivate and then re-activate vSphere HA
    before your changes take effect"). VERIFIED-in-docs.
  - A tier-device hang does not isolate the host, so the isolation response doesn't matter for this failure. Keep
    the usual vSAN choice, "Power off and restart VMs". UNVERIFIED in 9.x docs: VMware's long-standing vSAN guidance,
    not re-read in this session.
  - `das.config.fdm.isolationPolicyDelaySec` (minimum 30) can delay acting on a brief isolation. VERIFIED-in-docs.
- **Does SDDC Manager need HA on these VMs?** VCF 5.1 design: "vSphere HA is the only supported method to protect
  vCenter Server availability in VMware Cloud Foundation". VERIFIED-in-docs (5.1). No 9.x doc found requiring HA
  restart for SDDC Manager, Ops or VSP. In a lab, disabling restart per VM is a supportability deviation only in the
  sense that it departs from design guidance. No precheck found that checks it. UNVERIFIED.

---

## 3. DRS settings

- **Automation: keep PartiallyAutomated** (user decision).
  - "If the cluster or any of the virtual machines involved are manual or partially automated", vCenter "does not
    take automatic actions to balance resources" (TechDocs 9.0). VERIFIED-in-docs.
  - Initial placement at power-on is still automatic.
- **Migration threshold: 1 (Conservative).** "The Conservative setting generates only priority-one recommendations
  (mandatory recommendations)" (TechDocs 9.0). VERIFIED-in-docs. Under PartiallyAutomated this only reduces
  recommendation noise, but it keeps a mistaken "Apply all" from triggering balancing vMotions.
- **Memory Metric for Load Balancing**: "no longer supported and will not be displayed in vCenter 7.0" (TechDocs 7.0).
  VERIFIED-in-docs. Not available in 9.x.
- **VM Distribution: off.** It is "secondary to DRS load balancing" and adds moves. VERIFIED-in-docs (7.0 text).
- **Predictive DRS: off** (Cluster > vSphere DRS > Edit > Automation > Predictive DRS). Nothing found on its 9.x
  behaviour. It only acts with VCF Operations integration and adds pre-emptive moves. Off is the safe choice.
  UNVERIFIED.
- **DRS and tiering.** TechDocs 9.0: "Memory Tiering integrates seamlessly with DRS" and DRS "places VMs to
  load-balance memory usage across the cluster". VERIFIED-in-docs. No doc says DRS avoids placing onto a host that
  will then demote. Assume it does not. UNVERIFIED.
- **Maintenance mode under PartiallyAutomated** (KB 451053). The EMM task waits; you migrate each VM yourself. Make
  evacuations gentle and sequential:
  1. Pre-check (section 6) on the **destination** host before each VM.
  2. Migrate the largest-tier1 VMs first (most NVMe read on the source) with nothing else in flight.
  3. Wait for destination DRAM and tier write activity to settle before starting the next one.
  4. vSAN EMM option: "Ensure accessibility". In a 3-host cluster "Full data migration" is impossible anyway.
  5. Do **not** temporarily flip DRS to Fully Automated to finish an EMM. That is exactly the 9.1 parallel-vMotion
     path.
- **VCF lifecycle exception.** SDDC Manager / vLCM upgrades perform their own EMM.
  - Before an upgrade: power off workers, disable admission control (KB 421536), and confirm tier health on all
    hosts.
  - Expect the upgrade's evacuations to be automatic and parallel: vLCM requires DRS for placement and migration
    (vLCM pre-check "DRS is deactivated on the cluster. Enable DRS"). VERIFIED-in-docs (8.0 vLCM).
  - Whether a 9.1 upgrade precheck rejects PartiallyAutomated: not found. UNVERIFIED.

---

## 4. vSAN ESA (consumer drives, 10G)

- **Object repair timer** (Cluster > Configure > vSAN > Services > Advanced Options > Object Repair Timer;
  host key `/VSAN/ClomRepairDelay`).
  - "By default, the repair delay value is set to 60 minutes." After a UI change, "wait for at least 180 seconds
    before proceeding with any Maintenance/Disk Group/Disk/reboot-related activity" (KB 327031). VERIFIED-in-docs.
  - A failed **device** rebuilds immediately; a failed or absent **host** waits for the timer (KB 327031).
    VERIFIED-in-docs.
  - In 2- and 3-host clusters "vSAN cannot rebuild data on another host" (vSphere 8 vSAN planning). VERIFIED-in-docs.
    The timer therefore rarely matters for a host blip.
  - When the host returns, the changed-block resync starts at once regardless of the timer.
  - Raising the timer (e.g. 120–240 min) is harmless and reduces surprise rebuild attempts during a long host outage.
- **Storage policy shape.** ESA auto-policy for 3 hosts without Host Rebuild Reserve = "RAID-1 (Mirroring)"; with HRR
  "HRR is not supported" (KB 444255, ESA 8.x). VERIFIED-in-docs. Check that VCF VMs use the RAID-1 FTT=1 policy.
- **Resync throttle.**
  - The manual "resync throttle" is "deprecated with Version 7.0, due to the introduction of Adaptive Resync", and a
    leftover limit should be cleared (`/VSAN/DomCompResyncThrottle = 0`) (KB 326963). VERIFIED-in-docs.
  - **On ESA over 10G, set `/VSAN/DOMNetworkSchedulerThrottleComponent = 1` on every host** (KB 410662): "affects
    all versions of ESA when configured on 10G networks"; "can be set on a running host"; "effect will be immediate
    and persistent". Command: `esxcfg-advcfg -s 1 /VSAN/DOMNetworkSchedulerThrottleComponent`. VERIFIED-in-docs.
  - On VCF 9.1, put it in the cluster configuration profile so remediation doesn't revert it as drift.
- **When a host hangs on a dead tier drive.**
  - Morera: "the ESX host will keep on running", VMs with pages on the device "may enter a HA situation". VENDOR-BLOG.
  - TechDocs 9.1 (per companion file): "Placing a host in maintenance mode after an NVMe failure is a manual
    operation in vSphere 9.1". VERIFIED-in-docs (companion research).
  - vSAN itself is affected only if the host stops servicing vSAN I/O. The tier device is separate from vSAN, and
    TechDocs 9.0 says devices "already part of a datastore, such as vSAN, cannot be used for Memory Tiering".
  - If the whole host wedges, its vSAN components go Absent and objects keep running on the other two hosts at
    reduced redundancy. UNVERIFIED for the hung-PCIe case.
  - Recommended action after a tier hang: on the affected host, migrate or shut down the surviving VMs manually and
    one at a time. Then enter MM with "Ensure accessibility" and power-cycle the host (physical AC cycle per your
    notes). Do not let HA or DRS move everything at once.

---

## 5. VSP (VCF Services Platform / VCF Management Services Runtime) restart behaviour

- VSP self-heals at the Kubernetes layer (KB 389510, VSP 9.0).
  - Unplanned node replacement (Node Problem Detector) "will cause the node to be removed first and then a new node
    to be provisioned".
  - "if a VM was deleted from vCenter, the cluster will automatically attempt to provision a new node".
  - Node rotation also triggers "at 90% disk usage for over a 5 minute period".
  - Replacement **clones a new VM** (needs the VirtualMachineTemplate), which is its own memory and vSAN burst.
    VERIFIED-in-docs.
- In vSphere Kubernetes Service clusters, the MachineHealthCheck controller treats a powered-off node as NotReady and
  "automatically triggers a remediation task (Power On or Recreate)". The documented stop is
  `kubectl patch virtualmachine <vm> --type=merge -p '{"spec":{"powerState":"PoweredOff"}}'` (KB 446072).
  VERIFIED-in-docs for VKS. Whether VSP uses the same VM Operator/MHC path is UNVERIFIED.
- `vmoperator.vmware.com/paused` annotation halts VM Operator reconciliation (KB 454022, VCF Automation context).
  Emergency or diagnostic use only. VERIFIED-in-docs for that context.
- **Supported ways to stop VSP from powering nodes back on:**
  - Platform shutdown, KB 440874 (Services Runtime 9.1.0.x):
    `./vcf_services_runtime_shutdown.sh --node-ip <CP_IP> [--dry-run|--poweroff-vms]`. It "provisions flags to ensure
    automatic recovery upon boot". "Do not force power off VMs in vCenter while a graceful shutdown is in progress."
    VERIFIED-in-docs.
  - VCF Operations > Fleet Management > Lifecycle > VCF Management > Components > Manage > Power off (KB 452820
    path). VERIFIED-in-docs.
- **Power-on** (KB 440862): control-plane VMs first, "wait for 5 minutes", then workers, then "Allow 15-20 minutes
  for the automatic recovery process to complete". If nodes stay `Ready,SchedulingDisabled`, use the KB's
  `cluster-manual-recovery.sh`. VERIFIED-in-docs.
- **HA for VSP VMs.** VSP replaces nodes itself, and three control-plane nodes are needed for HA (KB 389510). HA
  restart of a VSP node is partly redundant with VSP's own remediation. Disabling HA restart for VSP nodes removes
  one source of simultaneous boots, but VSP may then clone a replacement instead. Test one node before committing.
  UNVERIFIED.
- **Snapshots break node rotation:** "PersistentVolumes cannot detach whilst there are snapshots" (KB 389510). Never
  leave snapshots on VSP VMs. VERIFIED-in-docs.

---

## 6. Operational guardrails

### 6.1 Pre-checks before ANY vMotion, EMM, or power-on of a large VM
1. Tier device health on **source and destination**: `esxcli system health report get -r vmw.memTierHealth` (JSON).
   - Also check the vCenter alarm "NVMe Memory Tiering device is not healthy", and Host > Monitor > Memory Tiering >
     Health Overview.
   - Replace the device at Available Spare ≤10% (Lam, 2026-09-22).
   - Look for any NVMe abort/reset lines in `vmkernel.log` since the last boot. A device that logged aborts is not a
     safe destination.
   - VENDOR-BLOG (Lam) plus your own notes.
2. Size of the burst: `memstats -r vmtier-stats -u mb -s name:me` (KB 448305). A VM with a large tier1Consumed means a
   large NVMe read on the source. VERIFIED-in-docs.
3. Destination headroom: destination DRAM consumed + incoming VM's configured memory must stay **below ~80% of DRAM**.
   That keeps the destination below the threshold where demotion starts (KB 448305 / Epping: "approximately 80%").
   The 80% figure is VERIFIED; the rule is mine (UNVERIFIED as a guarantee).
4. Only one migration in flight cluster-wide. Confirm in Recent Tasks before starting the next.
5. Host-level active memory under 50% of DRAM (TechDocs 9.0 capacity planning: "active memory usage on the ESX host
   has not exceeded 50% of the DRAM capacity"). VERIFIED-in-docs.

### 6.2 Power-on order
Assembled from VCF 4.x/5.x order plus 9.1 KBs. No VCF 9.1 whole-stack shutdown page was found, so the order is
UNVERIFIED as a whole.
1. Hosts → vSAN health green (no resync in progress).
2. vCenter → NSX Manager(s) → NSX Edges (if any) → SDDC Manager.
3. VCF Operations: primary first, then replica/data nodes (KB 341964 reverse-of-shutdown) → Ops collector → fleet
   management.
4. VSP: control-plane nodes → wait 5 min → workers → wait 15–20 min (KB 440862).
5. Re-run the 6.1 checks. Then power on the LLM worker VMs **one at a time, on their assigned host**, waiting for
   memory to settle between each.

### 6.3 Power-off order
Reverse of the power-on order:
1. Worker VMs.
2. VSP via the KB 440874 script.
3. Fleet management, then Ops collector, then Ops data/replica nodes, then the Ops primary last (KB 341964).
4. SDDC Manager, then NSX Edges, then NSX Manager.
5. vCenter last. Save host credentials first: older VCF docs get them from SDDC Manager `lookup_passwords`. In 9.x,
   password management lives in VCF Operations.

---

## 7. Prioritised settings list

| # | Setting (exact / UI path) | Expected effect | Risk / side effect | Verify | Label |
|---|---|---|---|---|---|
| 1 | VM Overrides → vSphere HA restart priority **Disabled** for Ops collector, fleet mgmt, (test) VSP nodes; keep vCenter High / NSX Medium / SDDC Mgr Low | Fewer simultaneous boots after a tier hang; breaks the cascade | Those VMs stay down after a host loss until you start them; departs from VCF design guidance | Cluster > Configure > VM Overrides; after a test, check events for "vSphere HA restarted" | VERIFIED-in-docs (setting) |
| 2 | HA advanced option `das.reregisterrestartdisabledvms = true` | HA-disabled VMs are re-registered on a live host, not powered on | None known | vSphere Availability > Advanced Options list; dead-host test shows VMs registered and off | VERIFIED-in-docs |
| 3 | Failures and Responses > VM Monitoring = **Disabled** (or Low + per-VM Disabled for sddc-manager/Ops) | Stops "VMware Tools heartbeat failure" resets during incidents | Genuinely hung guests are not auto-reset | Events: no "reset by HA" during the next incident | VERIFIED-in-docs |
| 4 | Host Failure Response: VM Restart Priority Condition = **Guest heartbeats detected**, additional delay **120–300 s**, timeout **600 s** | Management VMs boot in tiers with a settle gap | Slower full recovery (minutes) | HA config in UI; API `ClusterVmReadiness` | VERIFIED-in-docs (option); values UNVERIFIED |
| 5 | vDS NIOC → System traffic → vMotion **Limit = 3000 Mbps** (tune 2000–4000); vMotion port group with **one Active uplink** | Caps vMotion bytes/s and therefore (expected) source NVMe reads and destination demotion rate | Slower EMM; busy VMs may fail to converge below ~250 Mbps per vMotion | `esxtop` network view during a test vMotion; vMotion speed in task details; tier device latency in Host > Monitor > Memory Tiering | VERIFIED-in-docs (semantics); throttling-of-NVMe UNVERIFIED |
| 6 | Admission control = **Disabled** (and before VCF upgrades per KB 421536) | Lab can run both stacks; EMM dry runs pass | HA restarts may overcommit. Keep item 1 so scope stays small | Precheck in SDDC Manager passes | VERIFIED-in-docs |
| 7 | DRS migration threshold **1 (Conservative)**, VM Distribution **off**, Predictive DRS **off**, automation PartiallyAutomated | No balancing vMotions; only mandatory recommendations | None for the lab | Cluster > vSphere DRS settings; DRS Recommendations list mostly empty | VERIFIED-in-docs (threshold); predictive UNVERIFIED |
| 8 | DRS VM override **Disabled** + VM-Host **must** rule for worker VMs; VM-Host **should** rules for vCenter/NSX | Workers never move; HA won't restart them elsewhere; mgmt VMs prefer chosen hosts | Must rules block EMM of the worker host until workers are off | Cluster > VM/Host Rules; EMM dry run lists the workers as blockers | VERIFIED-in-docs |
| 9 | Each host: `/VSAN/DOMNetworkSchedulerThrottleComponent = 1` (via configuration profile on 9.1) | Resync after a host blip doesn't flood 10G and spike VM latency | Slower resync (fine on 10G) | `esxcli system settings advanced list -o /VSAN/DOMNetworkSchedulerThrottleComponent`; VCP compliance green | VERIFIED-in-docs |
| 10 | Object Repair Timer **120–240 min**; confirm `/VSAN/DomCompResyncThrottle = 0`; VCF VMs on RAID-1 FTT=1 | No surprise rebuild attempts during long outages; no leftover manual throttle | Longer exposure window (no rebuild target exists anyway in 3 hosts) | vSAN Services > Advanced Options; Skyline "Resync Operations Throttling" green | VERIFIED-in-docs |
| 11 | Isolation: `das.isolationaddress0 = <vSAN-subnet IP>`, `das.usedefaultisolationaddress = false` (re-enable HA after) | No false isolation → no false restarts | Wrong IP = false isolation; pick an always-up address | HA config issues pane clear; `fdm.log` | VERIFIED-in-docs |
| 12 | Procedure: 6.1 pre-checks, one migration at a time, destination < 80% DRAM after migration | Avoids demotion bursts on the destination | Manual effort | Checklist | Rule = UNVERIFIED; inputs VERIFIED |
| 13 | VSP: power off/on only through KB 440874 script or Fleet Management; never guest-shutdown individual nodes | No ~10 s restart loop; no node replacement clones | None | vCenter tasks show no unexpected "Power On" after shutdown | VERIFIED-in-docs |
| 14 | (Optional, unsupported) vCenter `config.vpxd.ResourceManager.costPerVmotionESX6x = 16` | ≤1 concurrent vMotion per host, even for vLCM / 9.1 parallel DRS | "Not officially supported"; may slow VCF upgrades; key may differ in 9.x | Start 2 vMotions to one host; second should queue | VENDOR-BLOG; value 16 UNVERIFIED |
| 15 | (Optional, untested on 9.x) HA `das.perHostConcurrentFailoversLimit = 1`, `das.maxvmrestartcount = 2` | One restart at a time per host; fewer retry hops | Documented only to 6.5; may be ignored | Controlled host-down test; FDM log | UNVERIFIED |

Not cluster-level, from the companion file. These are the only levers that change the failure mode itself:
- 9.1 software mirroring of the tier device (needs a second NVMe).
- Per-VM `sched.mem.enableTiering = FALSE`. TechDocs 9.1 requires a full reservation with it, which conflicts with
  "nsxa reservation stays removed".
- Broadcom's tier device requirement is "Class D >= 7300 TBW" endurance and "Class F … or Class G" performance
  (TechDocs 9.0). Consumer PM981-class drives are far outside this. VERIFIED-in-docs.

---

## Sources (accessed 2026-10-12; publication date where shown)
- TechDocs vSphere 9.0, NIOC parameters: https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-0/vsphere-networking/network-io-control/bandwidth-allocation-for-system-traffic/bandwidth-allocation-parameters-for-system-traffic.html
- TechDocs vSphere 9.0, HA Advanced Options: https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-0/vsphere-availability/creating-and-using-vsphere-ha-clusters/configuring-cluster-settings/set-advanced-options/vsphere-ha-advanced-options.html
- TechDocs vSphere 9.0, Respond to Host Failure: https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-0/vsphere-availability/creating-and-using-vsphere-ha-clusters/configuring-cluster-settings/configuring-responses-to-failures/respond-to-host-failure.html
- TechDocs vSphere 9.0, HA + DRS: https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-0/vsphere-availability/creating-and-using-vsphere-ha-clusters/vsphere-ha-interoperability/using-vmware-ha-and-drs-together.html
- TechDocs vSphere 9.0, HA + vSAN: https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-0/vsphere-availability/creating-and-using-vsphere-ha-clusters/vsphere-ha-interoperability/using-vsphere-ha-with-vsan.html
- TechDocs vSphere 9.0, Create a DRS Cluster (threshold, overrides): https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-0/vsphere-resource-management/creating-a-drs-cluster.html
- TechDocs vSphere 9.0, Memory Tiering over NVMe / Configuration / Capacity Planning: https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-0/vsphere-resource-management/memory-tiering-over-nvme.html
- API 9.0 ClusterVmReadiness / ClusterDasVmSettings: https://developer.broadcom.com/xapis/virtual-infrastructure-json-api/9.0/data-structures/ClusterVmReadiness/ , https://developer.broadcom.com/xapis/virtual-infrastructure-json-api/9.0/data-structures/ClusterDasVmSettings/
- KB 313261 HA advanced options (5.x–8.x): https://knowledge.broadcom.com/external/article/313261
- KB 337335 (legacy 1027734) VM Monitoring reset: https://knowledge.broadcom.com/external/article?legacyId=1027734
- KB 451053 EMM stalls with Manual/Partially Automated DRS: https://knowledge.broadcom.com/external/article/451053
- KB 441786 VCF Installer requires DRS Fully Automated: https://knowledge.broadcom.com/external/article/441786
- KB 444167 VCF brownfield import guardrail (DRS Fully Automated): https://knowledge.broadcom.com/external/article/444167
- KB 439134 VCF upgrade precheck EMM dry run / admission control: https://knowledge.broadcom.com/external/article/439134
- KB 421536 (VCF 9.1) disable admission control for upgrade: https://knowledge.broadcom.com/external/article/421536
- KB 422655 VCP drift on advanced options: https://knowledge.broadcom.com/external/article/422655
- KB 327031 vSAN repair delay: https://knowledge.broadcom.com/external/article/327031
- KB 326963 resync throttling deprecated: https://knowledge.broadcom.com/external/article/326963
- KB 410662 ESA 10G resync congestion workaround: https://knowledge.broadcom.com/external/article/410662
- KB 444255 ESA 3-node optimal policy: https://knowledge.broadcom.com/external/article/444255
- KB 389510 VSP cluster health checks / node replacement: https://knowledge.broadcom.com/external/article/389510
- KB 440874 Services Runtime safe shutdown: https://knowledge.broadcom.com/external/article/440874
- KB 440862 Services Runtime / Automation recovery after power-on: https://knowledge.broadcom.com/external/article/440862
- KB 446072 VKS MachineHealthCheck auto power-on: https://knowledge.broadcom.com/external/article/446072
- KB 454022 vmoperator paused annotation: https://knowledge.broadcom.com/external/article/454022
- KB 452820 Fleet Management power-off path: https://knowledge.broadcom.com/external/article/452820
- KB 341964 VCF Operations shutdown/startup sequence: https://knowledge.broadcom.com/external/article/341964
- KB 448305 RHEL 8 tiering, 80% threshold, memstats: https://knowledge.broadcom.com/external/article/448305
- VMware perf blog, vCenter limits for concurrent vMotion (2021-02-26): https://blogs.vmware.com/cloud-foundation/2021/02/26/vcenter-limits-for-concurrent-vmotion/
- VMware blog, tune vMotion stream helpers (2019-09): https://blogs.vmware.com/vsphere/2019/09/how-to-tune-vmotion-for-lower-migration-times.html
- Morera, NVMe memory tiering design part 2 (2025-11-18): https://blogs.vmware.com/cloud-foundation/2025/11/18/nvme-memory-tiering-design-and-sizing-on-vmware-cloud-foundation-9-part-2/
- Morera, Advanced memory tiering enhancements 9.1 (2026-05-07): https://blogs.vmware.com/cloud-foundation/2026/05/07/advanced-memory-tiering-enhancements-in-vmware-cloud-foundation-9-1/
- Configuring memory tiering in VCF 9.1 (2026-05-19): https://blogs.vmware.com/cloud-foundation/2026/05/19/more-memory-less-effort-configuring-memory-tiering-in-vcf-9-1/
- VVF 9.1 announcement, parallel DRS vMotion (2026-05-05): https://blogs.vmware.com/cloud-foundation/2026/05/05/announcing-vmware-vsphere-foundation-9-1/
- Epping, Memory Tiering FAQ (2026-07-16): https://www.yellow-bricks.com/2026/07/16/memory-tiering-considerations-and-frequently-asked-questions/
- Lam, NVMe tiering health monitoring 9.1 (2026-09-22): https://williamlam.com/2026/09/quick-tip-improved-nvme-tiering-device-health-monitoring-in-vcf-9-1.html
- Lam, disable vSphere API methods (DisabledMethod, internal API): https://williamlam.com/tag/svmotion
- Epping, VM dependency restart condition timeout (2018): https://www.yellow-bricks.com/tag/restart/
