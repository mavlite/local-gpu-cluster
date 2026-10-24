# vSAN ESA host reinstall onto boot device only: disk reuse, rejoin, and fallback

Research limits: ~14 tool calls. Broadcom KB pages were read through a summarising fetcher (not raw HTML), so quotes are the fetcher's paraphrase unless marked. I found NO Broadcom/VMware source that explicitly documents what ESA does with a surviving storage-pool disk after a fresh install beyond "preserve the partitions and rejoin". Items marked UNVERIFIED are inference.

## 1. vSAN node UUID: what a fresh install does, and does the disk metadata bind to it?

### Takeaway
A rebuilt host gets a NEW Local Node UUID; the Sub-Cluster UUID is what you reuse to rejoin. No source says the surviving ESA disk metadata is rejected because of the node-UUID change, and Broadcom's rebuild KBs assume the preserved disks are reused. Whether the node UUID is derived from the ESXi system UUID is not confirmed by any source I found.

### Cited Findings
- After a rebuild, the host "now has its own Local Node UUID, which differs from the UUID it had before the rebuild"; Sub-Cluster UUID stays the same; member count +1 — [KB 419382](https://knowledge.broadcom.com/external/article/419382/esxi-host-rebooted-and-unable-to-find-t.html)
- Join uses the Sub-Cluster UUID (from `esxcli vsan cluster get` on a surviving host), not the Local Node UUID — [KB 327032](https://knowledge.broadcom.com/external/article/327032)
- KB 327032 and KB 419382 both say "Applies to vSAN all versions", cover OSA and ESA, and state the reinstall must keep the vSAN disk partitions intact. They do not discuss on-disk node-UUID binding at all.
- Hardware/BIOS UUID vs ESXi system UUID: a Broadcom community reply (forum, not authoritative) says the UUID checked by config restore is the hardware/BIOS UUID, and that after a restore the ESXi system UUID reverts to the backup's value — [community.broadcom.com thread](https://community.broadcom.com/vmware-cloud-foundation/discussion/recover-vsan-after-esxi-and-vcenter-reinstall) (search-snippet level; cites KB 2042141)
- Stale local vSAN state (sub-cluster UUID, unicast list) lives in the host's `esx.conf` and survives repurposing only if the config survives; KB 447194 describes repurposed ESA hosts retaining it — [KB 447194](https://knowledge.broadcom.com/external/article/447194/repurposed-vsan-esa-hosts-cause-subclust.html) (not fetched; snippet only)
- William Lam (2014, ESXi 5.5 / OSA): after reinstall "if ESXi finds partitions containing VSAN data, it automatically claims those devices"; recovery = `esxcli vsan storage list`, enable vsan traffic on a vmk, `esxcli vsan cluster join -u <UUID>`. A 2023 reader comment says it still applies to ESA, but the article does not cover ESA — [williamlam.com](https://williamlam.com/2014/07/does-reinstalling-esxi-with-an-existing-vsan-datastore-wipe-your-data.html)

### Inferences
- The surviving disk's metadata (disk/pool UUIDs, component data) is cluster-level; the old node's component entries in the CMMDS are keyed to the old node UUID, so after rejoin the old components will show as Absent until the disk is re-published under the new node UUID, or be rebuilt. UNVERIFIED mechanism; the KBs only imply it works.
- Because Broadcom's procedure says "preserve partitions" yet also says the host gets a new node UUID, node UUID is evidently not required to match for disk reuse (or the resync just treats the host as new). UNVERIFIED.

### Gaps
- No source states whether vSAN node UUID == ESXi system UUID (`esxcli system uuid get`) or is independently generated at `cluster join`. Verify by running `esxcli vsan cluster get` and `esxcli system uuid get` on hyp01/hyp03 and comparing.
- No source on ESA on-disk metadata containing the owning node UUID.

## 2. After reinstall + reconnect: auto-import, foreign/ineligible, or manual?

### Takeaway
Documented path: install ESXi (same build) without touching the vSAN NVMe, add host to Datacenter, configure vmk for vSAN, drag into the cluster (or `esxcli vsan cluster join -x -u <SubClusterUUID>`). Broadcom documents no separate "mount storage pool" step. However the same disk is described elsewhere as "logically claimed by a vSAN Storage Pool" and the installer will not overwrite it, so it will appear as in-use/ineligible for NEW claiming, which is the expected state for reuse. Whether the pool auto-reattaches with data was not documented explicitly for ESA; test and observe.

### Cited Findings
- Primary procedure (KB 327032 / 419382): remove old host entry from vCenter (disconnect, then remove from inventory); install ESXi at the exact same build keeping vSAN partitions; add host to Datacenter; configure mgmt + vSAN vmk and `vmkping` both ways; drag host into the vSAN cluster, which "enables vSAN clustering on the host and triggers a cluster update pushing the correct unicastagent list to all hosts"; check Skyline Health — [KB 327032](https://knowledge.broadcom.com/external/article/327032), [KB 419382](https://knowledge.broadcom.com/external/article/419382/esxi-host-rebooted-and-unable-to-find-t.html)
- CLI fallback: on a surviving host `esxcli vsan cluster get` (note Sub-Cluster UUID, not Local Node UUID). On rebuilt host: OSA `esxcli vsan cluster join -u <UUID>`; ESA `esxcli vsan cluster join -x -u <UUID>` ("-x" ESA flag). Verify with `esxcli vsan cluster get`: matching Sub-Cluster UUID, member count +1, Local Node Health State HEALTHY, "vSAN ESA Enabled: true" — [KB 327032](https://knowledge.broadcom.com/external/article/327032)
- KB 327032 states: "choosing a vSAN disk for the ESXi install can corrupt data" - verify the install target. If encryption is on, KMS must be online to decrypt after reinstall.
- ESA managed-disk auto-claim only triggers "when a host is added into the cluster" with the Managed Disk toggle on, and only claims ESA-eligible (i.e. partition-free) disks; manual claim otherwise; CLI `esxcli vsan storagepool add -d <device>` requires "an empty device with no partitions" — [KB 326509](https://knowledge.broadcom.com/external/article/326509/vsan-health-service-cluster-health-vsa.html). Implication: a disk with surviving vSAN partitions is not an auto-claim candidate; it is a reuse candidate.
- ESA NVMe devices "are still logically claimed by a vSAN Storage Pool"; the installer "will not automatically overwrite these partitions" (so manual cleanup is needed if you WANT them wiped) — [KB 399567](https://knowledge.broadcom.com/external/article/399567/unable-to-add-the-host-to-sddc-manager-f.html) (context: VCF 9.0 SDDC Manager commissioning; no versions stated)
- Failure modes after rebuild: join error "Failed to join the host in vSAN cluster: Unable to complete Sysinfo operation" from stale vSAN state; fix `esxcli vsan cluster leave`, then erase partitions only if unneeded — [KB 454125](https://knowledge.broadcom.com/external/article/454125/unable-to-add-a-rebuilt-esxi-host-to-a.html) (ESA/OSA not stated)
- Host stuck `Maintenance Mode State: UNKNOWN` after reinstall, "VSAN Clustering is not enabled"; fix: maintenance mode + reboot, check `esxcli vsan debug object health summary get` before recreating any disk group; contact support if inaccessible objects — [KB 436164](https://knowledge.broadcom.com/external/article/436164/troubleshooting-unhealthy-disk-alerts-on.html) (vSAN 8.x, sample shows ESA=false, i.e. OSA-leaning)

### Inferences
- Expected sequence for hyp02: reinstall, rejoin, then in Disk Management the NVMe should show as already part of the storage pool (data intact) or as "unhealthy/absent" until the pool is reattached. If it shows ineligible and the pool is not reattached, the fallback is section 4 (wipe + reclaim).
- Broadcom's vSAN-all-version KBs are generic; ESA 9.1.1 specifics (e.g. whether ESA pool auto-remounts) are UNVERIFIED.

### Gaps
- No documented ESA-specific "reimported storage pool" screenshot/CLI output. `esxcli vsan storagepool list` after rejoin is the check; not found in any source for this scenario.
- OSA vs ESA: the KBs use identical flow except the `-x` flag; no difference in disk reuse is documented.

## 3. Restoring prior config (restore_config / state.tgz) to keep identity

### Takeaway
Not a viable route for a corrupted boot device unless you already have a recent config bundle from hyp02, and no source ties it to vSAN. Requirements are strict (same build, same hardware UUID, TPM caveats). Treat as optional; the Broadcom vSAN rebuild KBs do not use it.

### Cited Findings
- Destination must be the same build as the backup; check `vmware -vl` — [KB 313510 (2042141)](https://knowledge.broadcom.com/external/article/313510)
- `vim-cmd hostsvc/firmware/restore_config 0` default; `1` forces override of a UUID mismatch; host auto-reboots; maintenance mode and a reboot first are "required" — same KB
- From vSphere 7.0 U2, the override "will not work if the host has TPM enabled"; restore needs the same TPM used at backup — same KB
- Backups exclude VM info and bootbank data; not supported for DPU hosts or Auto Deploy hosts — same KB
- The KB does not mention vSAN and does not state ESXi 8/9 explicitly (its 8.0 mention is only an Auto Deploy link).
- Community: after restore the system UUID returns to the backup's value — [community.broadcom.com](https://community.broadcom.com/vmware-cloud-foundation/discussion/recover-vsan-after-esxi-and-vcenter-reinstall) (forum, snippet)
- William Lam recommends `vim-cmd hostsvc/firmware/sync_config` before taking a backup, and says a bundle from a different host can't be restored — [williamlam.com/tag/backup](https://williamlam.com/tag/backup) (snippet)

### Inferences
- A restore would bring back the old esx.conf (including the vSAN node/cluster membership and system UUID), so the host would come back as the same node. This is plausible but UNVERIFIED for ESXi 9.1.1/ESA, and a bundle is only useful if one was taken before the failure. If the boot device was a TPM-attested host the restore may be refused.
- "Works across a fresh install of the same build": the KB says yes in principle (same build), no 9.x-specific confirmation.

### Gaps
- No source on restoring a vSAN node identity via restore_config; no ESXi 9 documentation of it found. Whether the Minisforum hosts have TPM enabled/encrypted-config not known.

## 4. If the disk must be wiped and reclaimed; resync risk with FTT=1, 3 nodes, one host absent

### Takeaway
Wiping hyp02's disk discards its replica data, so all objects stay reduced-availability (one copy/parity member missing) until the reclaimed pool is resynced, and during that window the cluster tolerates no further failure. A 3-node cluster has no spare host, so the absent-component timer cannot rebuild anywhere else; recovery happens only when hyp02 is back with a pool.

### Cited Findings
- Default: vSAN starts rebuilding absent components after 60 minutes (object repair timer delay, configurable; "maximum 4294967295 minutes"); degraded (permanent-failure) components are rebuilt immediately — [KB 446168](https://knowledge.broadcom.com/external/article/446168/data-resync-taking-a-longer-time-while-p.html); [blogs.vmware.com understanding vsan rebuilds](https://blogs.vmware.com/cloud-foundation/2017/11/09/understanding-vsan-rebuilds/) (search snippet)
- KB 446168 (all versions, ESA/OSA unspecified): during host reinstall objects stay in reduced availability for the timer length or until host exits MM; resync after rejoin applies delta changes; "additional failures on other hosts during this window can cause data unavailability or data loss"; recommends raising the repair timer above the reinstall time and using "Ensure accessibility"; run Data migration precheck and back up critical VMs.
- Without a spare host, objects are available but degraded: "If there is not an available host to rebuild the data to, the object will be available, but will be in a degraded state" — Broadcom guidance quoted in search result (community/KB snippet, https://community.broadcom.com/vmware-cloud-foundation/discussion/vsan-esa-adaptive-raid-failures-effect-on-usable-capacity)
- ESA adaptive RAID-5: 2+1 on clusters up to 5 hosts, 4+1 on 6+ (same community thread, snippet). So on 3 nodes, default ESA policy objects are RAID-5 2+1 (needs all 3 hosts) or RAID-1 FTT=1 (3 copies' worth: 2 data + witness); both need all 3 hosts for full compliance.
- Wipe procedure for ESA disk (KB 399567; destructive; host must have no active objects): `esxcli vsan storagepool list`; `esxcli vsan storagepool remove -u <DEVICE_UUID>`; `esxcli vsan cluster leave`; `partedUtil getptbl /vmfs/devices/disks/<NAA>`; `partedUtil delete /vmfs/devices/disks/<NAA> <PART#>` (vSAN partition "usually 1 or 2" - confirm); verify with `vdq -i` / `vdq -qH` (Eligible).
- GUI alternative: Host > Configure > Storage Devices > Erase Partitions — [KB 416971](https://knowledge.broadcom.com/external/article/416971/unable-to-claim-a-vsan-disk.html) (vSAN 8.x/9.x, ESA/OSA not distinguished; "operation cannot be undone"; if erase fails reboot first per KB 454125)
- After wipe, rejoin then claim the disk: auto if Managed Disk toggle on and host added to cluster, else Disk Management claim or `esxcli vsan storagepool add -d <device>` — [KB 326509](https://knowledge.broadcom.com/external/article/326509/vsan-health-service-cluster-health-vsa.html)
- Pre-checks before removing/recreating: Skyline Health, Monitor > Virtual Objects, `esxcli vsan debug object health summary get`; open a support case if inaccessible objects — [KB 436164](https://knowledge.broadcom.com/external/article/436164/troubleshooting-unhealthy-disk-alerts-on.html)
- Same health command is recommended to confirm after resync — [KB 431247](https://knowledge.broadcom.com/external/article/431247/vsan-objects-in-reduced-availability-wit.html) (that KB is about RAID-6 on 5 hosts, relevant only as the general "need N hosts to rebuild" principle).

### Inferences
- With hyp02 down for longer than 60 min, hyp01/hyp03 objects are already non-compliant (RAID-5 2+1 objects lose a member; RAID-1 loses a replica). They are readable only while both survivors stay up; any loss/corruption on either survivor means data loss. Do NOT reboot/maintenance-mode hyp01 or hyp03 (Ensure-accessibility on a survivor would make objects inaccessible in a 2+1 layout). UNVERIFIED but follows from the policy math.
- If the disk is wiped, the full hyp02 share of data (roughly 1/3 of used capacity for 2+1, or one replica for RAID-1) is rebuilt over the network from survivors. Duration scales with used capacity and 4 TB NVMe/NIC speed; no source gives a number for this lab. The surviving hosts must have free capacity equal to what hyp02 will hold once rejoined (fine, since hyp02 supplies its own).
- If the old disk IS reused, only delta/stale components resync (KB 446168: "resync expected ... to apply delta changes"), far less traffic; but if stale component data is detected as outdated it is discarded and rebuilt anyway. UNVERIFIED for ESA.
- Pre-flight: back up critical VMs first; confirm `esxcli vsan debug object health summary get` shows no inaccessible objects on hyp01/hyp03.

### Gaps
- No source on ESA resync throughput or on adaptive-RAID behaviour specific to a host returning after >60 min with a wiped pool.
- Whether vSAN reuses an existing-but-stale component on a re-claimed pool: not documented.
- No source tells which policy (RAID-1 vs RAID-5) is default here; check `Monitor > vSAN > Virtual Objects` or storage policy on the cluster.

## 5. OSA vs ESA differences

### Takeaway
Procedure is the same except the join flag (`-x` for ESA) and storage commands (`esxcli vsan storagepool ...` for ESA vs `esxcli vsan storage ...` for OSA disk groups). ESA has no cache tier, so no cache-disk-dependent disk group loss (inference). Disk-reuse-after-reinstall behaviour is not documented as differing.

### Cited Findings
- Join: OSA `esxcli vsan cluster join -u <UUID>`; ESA `esxcli vsan cluster join -x -u <UUID>`; check "vSAN ESA Enabled" — [KB 327032](https://knowledge.broadcom.com/external/article/327032)
- Cleanup: OSA historically `esxcli vsan storage remove` (Cormac Hogan 2014, William Lam 2014 — older, OSA); ESA `esxcli vsan storagepool remove -u <uuid>` — [KB 399567](https://knowledge.broadcom.com/external/article/399567/unable-to-add-the-host-to-sddc-manager-f.html)
- Omitting `-x` on an ESA cluster yields "vSAN can not be enabled when storage pool disks exist" (search-engine summary of KB 327032; the fetched copy only says the wrong variant is a likely failure point).
- KB 436164's recreate-disk-group steps are OSA-style (disk groups); do not apply to ESA.
- KB 447194 shows repurposed ESA hosts can bring stale Sub-Cluster UUID into a new OSA cluster — [KB 447194](https://knowledge.broadcom.com/external/article/447194/repurposed-vsan-esa-hosts-cause-subclust.html) (snippet only).

### Inferences
- In OSA a surviving disk group is auto-claimed by ESXi after reinstall (Lam 2014); ESA behavior is assumed analogous (KB 399567: pool claim persists on the NVMe). UNVERIFIED.

### Gaps
- No single doc comparing OSA and ESA reinstall behaviour. techdocs.broadcom.com vSAN 8/9 admin pages and yellow-bricks/core.vmware.com ESA posts did not surface in searches; not checked directly.

## Suggested safe procedure (assembled from the above; the order is my synthesis)
1. On hyp01/hyp03: `esxcli vsan debug object health summary get`, Skyline Health, confirm no inaccessible objects; note Sub-Cluster UUID (`esxcli vsan cluster get`); consider raising the object repair timer; do not disturb the survivors; back up critical VMs.
2. If any ESXi config bundle for hyp02 exists, note it (KB 313510 conditions) but the standard path does not need it.
3. Remove hyp02 from vCenter inventory (disconnect first). Install the identical ESXi build (25714478) to the boot NVMe ONLY (verify installer target; vSAN NVMe shows as claimed).
4. Re-add to Datacenter, restore management network + vSAN vmk + vmkping, then drag into the cluster; fallback `esxcli vsan cluster join -x -u <SubClusterUUID>`.
5. Inspect `esxcli vsan storagepool list`, `vdq -qH`, Disk Management, `esxcli vsan debug object health summary get`; wait for resync to finish.
6. Only if the pool does not reattach and the disk is shown ineligible/unhealthy: wipe per section 4 (storagepool remove, partedUtil delete or Erase Partitions), reboot if erase fails, reclaim (Managed Disk or `storagepool add -d`), then let full resync complete.
