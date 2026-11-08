# NSX stale/ghost Host Transport Node after ESXi reinstall: community and field cases

Scope note: the searches turned up almost no Reddit, VMTN or YouTube threads for this symptom. They did turn up a dense cluster of Broadcom KB articles, many of which are effectively write-ups of field/GSS cases, plus two lab2prod.com.au field blogs. Every fix below is labelled by how confirmed it is. KB "resolutions" are vendor-stated, not reporter-confirmed.

## Q1: What fixes have worked for 601 "already present in the system" / ghost HTN / TNP failing on one re-added host?

### Takeaway
Error 601 on TNC realization is a documented Broadcom case (KB 422174, NSX 4.2.x + SDDC Manager). Its cause is a host removed from vCenter before NSX was removed. The standard fix ladder is:
1. Click "Configure NSX" on the host.
2. Force "Remove NSX" on the host after moving it out of the cluster. This works in vLCM clusters, where the TNP can't be detached.
3. Force DELETE through the MP API or the Policy API.
4. Run `start search resync` and do a rolling restart of the NSX Managers.
5. If none of that works, GSS does a scripted Corfu cleanup.

The user's exact pair of errors, 26208 "Credentials for host need to be specified" on the Policy HTN endpoint plus a stale HTN, matches KB 380641 (NSX 4.x). That KB's fix for non-vLCM clusters is to detach the TNP, force-delete NSX on the orphaned host, then re-attach the TNP. When the stale object returns 404 on every API while the UI still shows a failure, Broadcom's documented end state is a GSS Corfu cleanup (KB 319975 Option 5, KB 322453).

### Cited Findings
- **Exact symptom match (601).** KB 422174 shows "Adding host to NSX prepared cluster through SDDC Manager gets stuck at 'Prepare Transport Node Collection'". The host shows a validation error: "601: The object HostTransportNode/infra/sites/default/enforcement-points/default/host-transport-nodes/... is already present in the system". Affected versions: NSX 4.2.x with SDDC Manager/VCF Installer. Cause: the host was removed from vCenter without first being removed from NSX. Resolution: System > Fabric > Hosts, select the host, CONFIGURE NSX. No fixed version is given. It points onward to KBs 319975, 313788, 418946 and 373328. Vendor-stated, not reporter-confirmed. — [KB 422174](https://knowledge.broadcom.com/external/article/422174/adding-host-to-nsx-prepared-cluster-thro.html)
- **Same validation error ("node ID already exists"/"is already present in the system"), seen as a vMotion block.**
  - Fix: put the host in maintenance mode and remove it from the cluster. Then look under Fabric > Hosts > Other hosts, and if the host is listed, run Delete NSX with Force Delete. Confirm the host shows Not Configured, then re-add it.
  - No versions or fixed version are given. — [KB 420783](https://knowledge.broadcom.com/external/article/420783/unable-to-install-the-add-the-host-back.html)
- **Exact 26208 match.** KB 380641 is "Reconnecting a failed host returns 'Credentials for host need to be specified. (Error code: 26208)'". `nsxapi.log` shows HTTP 400 on `/policy/api/v1/infra/sites/default/enforcement-points/default/host-transport-nodes/...` (module service-installation-fabric).
  - Explicit cause: "ESXi TN being rebuilt with same DNS and IP" without first being removed from NSX, which leaves a stale node entry. Version: NSX 4.x; no fixed version.
  - Non-vLCM fix:
    1. Detach the TNP from the cluster.
    2. Force delete NSX from the orphaned host.
    3. Re-attach the TNP.
    4. Re-add the host if it isn't in the cluster.
  - vLCM fix (the TNP can't be detached):
    1. Put the host in maintenance mode.
    2. Move it to the Datacenter level.
    3. Confirm there are no NSX VIBs and that the UI shows "Unprepared".
    4. Move it back into the cluster.
  - Vendor-stated. — [KB 380641](https://knowledge.broadcom.com/external/article/380641)
- **Related 26208-type case with no self-service fix.** "Credentials for the host need to be specified" during host prep. `nsxapi.log` shows "Record validation error in TNC for discovered node ... was recently deleted and is being cleaned up. Please retry registering after some time." Cause: "Inconsistent records in the NSX Corfu database". The only workaround is to open a Broadcom support case. — [KB 322453](https://knowledge.broadcom.com/external/article/322453)
- **Master KB for stale hosts.** KB 319975 says a host removed from vCenter before NSX leaves a "Stale Host" that "will prevent adding back a refreshed or new host using the same name". Reinstalling ESXi does not help. It lists NSX-T 3.x, NSX 4.x and NSX 9.0.0 as affected, and says the issue is fixed in NSX 4.2.3 and 9.0.1. Workarounds:
  - **Prerequisite:** put the host in maintenance mode and move it to the datacenter level so it shows under Other Nodes.
  - **Option 1:** REMOVE NSX with Force Delete in the UI.
  - **Option 2:** if the host isn't visible, run `start search resync policy`, then `manager`, then `telemetry` on all managers. Wait at least 10 minutes. Then run `start search resync all` if needed and retry Option 1.
  - **Option 3 (MP API):** `GET /api/v1/transport-nodes/<uuid>/state`, then `DELETE /api/v1/transport-nodes/<uuid>?force=true&unprepare_host=false`. Repeat the GET until it returns "Object Not found", then move the host back into the cluster.
  - **Option 4 (Policy API):** `GET`/`DELETE /policy/api/v1/infra/sites/default/enforcement-points/default/host-transport-nodes/<node_name>?force=true&unprepare_host=false`.
  - **Option 5:** a scripted DB cleanup through a Broadcom support case.
  - For vLCM, security-only or Service Insertion clusters, the TNP can't be detached. Move the host to the DC level instead, and if VIBs remain, run `nsxcli -c del nsx` and reboot.
  - Vendor-stated. — [KB 319975](https://knowledge.broadcom.com/external/article/319975/installing-or-upgrading-nsx-on-an-esxi-h.html)
- **Re-imaged/renamed hosts that keep old HTN records; force delete stuck at 20%, then 404.** KB 423818 shows a force DELETE that returns 200 OK while the task hangs at 20% ("deployment.progress.tn.delete.waiting_for_host_config_reply"). A GET on the status then returns 404. This "200 then 404 but still present" pattern closely resembles the user's 404 ghost.
  - Diagnose by counting DB rows: `/opt/vmware/bin/corfu_tool_runner.py --tool corfu-editor -n nsx -o showTable -t HostTransportNode | grep "host-transport-nodes"`, compared against the UI host count.
  - Fix: Remove NSX on the old entry in Other Nodes, then re-add the host to the prepared cluster. If it persists, do a **rolling reboot of the NSX Manager nodes**, after which orphans "may appear under the Standalone Node section" and can be deleted. Otherwise, a GSS case.
  - Vendor-stated. — [KB 423818](https://knowledge.broadcom.com/external/article/423818/stale-host-entry-removal.html)
- **Same Corfu-count method and rolling-reboot advice.** KB 387942 gives the same Corfu-count diagnosis for a stale HTN that is in the DB but not in the UI, and the same rolling-reboot advice. — [KB 387942](https://knowledge.broadcom.com/external/article/387942/identifying-and-cleaning-up-orphanstale.html)
- **Stale HTN after removing and re-adding a host to the cluster, error 26164.** Fix:
  1. Put the host in maintenance mode and remove it from the cluster.
  2. `GET /api/v1/transport-nodes/<UUID>/status`.
  3. `DELETE ...?force=true`.
  4. Re-add the host to the cluster.

  — [KB 406955](https://knowledge.broadcom.com/external/article/406955)
- **Stale logical ports can block deletion or prep.** The fix is to remove the stale logical ports, then force-delete the TN. — [KB 424917](https://knowledge.broadcom.com/external/article/424917/nsx-preparation-failed-on-esxi-host-due.html)
- **Force delete stalls at 8% ("Disabling host status in controller") with MP9538 "Could not find the HostSwitch ... of type VDS"** when the VDS was already removed. Fixed in NSX 9.1 and later. The workaround is the stale-TN removal script in KB 406768. — [KB 445611](https://knowledge.broadcom.com/external/article/445611/force-deleting-orphaned-nsx-transport-no.html); [KB 406768](https://knowledge.broadcom.com/external/article/406768/script-for-removing-large-number-of-stal.html)
- **A stale Compute Manager registration can block the final TN deletion.** — [KB 447344](https://knowledge.broadcom.com/external/article/447344/nsx-upgrade-failure-due-to-stale-transpo.html) (search snippet only, not fetched)
- **VCF 9.1 / NSX 9.1 field case.** An HTN left stale after a host was "decommissioned and re-added, or renamed" made one host appear twice in `GET /api/v1/upgrade/upgrade-units?component_type=HOST` (17 units for 16 hosts). This blocked Edge/NSX finalize with LCM_NSXT_HOST_NOT_UPGRADED_IN_SHARED_DOMAIN. Fix: Remove NSX with Force on the Other Nodes entry. — [KB 444026](https://knowledge.broadcom.com/external/article/444026)
- **Field blog, VCF (NSX-T era, 2022), reporter says it "works for me".**
  1. Removed the failed host from the vCenter inventory. It then appeared in NSX-T as a standalone disconnected host, which the author removed.
  2. Removed the host from the SDDC Manager DB (unsupported).
  3. Re-commissioned the host and expanded the cluster through the API.

  Final completion is not shown. — [lab2prod: Rebuild a Failed VCF Managed Host](https://www.lab2prod.com.au/2022/03/rebuild-failed-vcf-managed-host.html)
- **Field blog, 2026.** Hosts were removed in vCenter rather than through SDDC Manager, then reimaged, which orphaned their NSX registrations. The author advises against editing NSX DB tables and finished the domain deletion with Force. Partly confirmed: the domain deleted, but not every step was verified. — [lab2prod: VCF workload domain stuck deleting after reimaging hosts](https://www.lab2prod.com.au/2026/08/vcf-workload-domain-stuck-deleting-after-reimaging-hosts.html)

### Inferences
- The user's case (Policy HTN 404 on GET, but the TNC realization claims the object exists, plus 26208 on Policy PUT) fits the "record exists in a Corfu table or intent index that the API GET path doesn't read" pattern of KBs 322453, 423818 and 319975 Option 2. In order, the community and KB-sanctioned non-destructive levers are:
  1. `start search resync policy`, then `manager`, then `all`.
  2. Detach the TNP from the cluster, force Remove NSX on the host, then re-attach (KB 380641). This is only possible on a non-vLCM cluster. VCF 9 clusters are usually vLCM-image clusters, where the documented equivalent is moving the host to the DC level.
  3. Restart the NSX Manager. With a single manager this is a full reboot.
  4. Run the Corfu `showTable HostTransportNode` count to prove the stale row exists, then open a GSS case for a scripted cleanup.
- The KB 319975 fix claim (4.2.3/9.0.1) covers the common stale-host path. The user's 9.1.x hit suggests a residual variant. I found no 9.x release note naming it.
- No source reported fixing it by renaming the host, deleting and recreating the TNC as a standalone step, or restoring an NSX backup. Restoring a backup from before the reinstall would roll back all NSX config and is not mentioned anywhere as a fix.

### Gaps
- No Reddit, VMTN or YouTube thread with this exact 601/404 combination and a reporter-confirmed fix was found.
- No source documents deleting and recreating the TNC (rather than detaching the TNP) as a fix.
- No public source documents Corfu edits beyond "open a GSS case for scripted cleanup".

## Q2: Is the Policy HTN id derived from host name + vCenter UUID + moref, so that a new moref gives a fresh id?

### Takeaway
No public doc states the derivation. The user's own error path (`hyp02-<vc-uuid>host-26`) is the strongest evidence that the auto-created TNC HTN id embeds the display name, the compute-manager UUID and the moref. Broadcom's API docs confirm that the discovered-node id for vCenter-managed hosts is `<vc-uuid>:host-NN`. A new moref would therefore very likely produce a different HTN id, but this is inferred, not documented.

### Cited Findings
- The `discovered_node_id` list filter "applies only to vCenter Managed hosts". Its example value is `6ab2278f-951d-471b-8d0f-510c825945f1:host-14`, which is the vCenter UUID plus the moref. — [NSX 4.2 API: HostTransportNodeListParameters](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/4.2/types_HostTransportNodeListParameters.html)
- Policy HTN has a `discovered_node_id_for_create` field, which is "required during Host Transport Node create from vCenter server managing the ESXi type HostNode". — [NSX 4.0 API: HostTransportNode schema](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/4.0.0/schemas_HostTransportNode.html)
- KB 319975's Policy API workaround keys the HTN path by `<node_name>`, which is not a UUID. — [KB 319975](https://knowledge.broadcom.com/external/article/319975/installing-or-upgrading-nsx-on-an-esxi-h.html)
- KB 448462 says vCenter assigns a new HostSystem MoRef when a host is removed and re-added. — [KB 448462](https://knowledge.broadcom.com/external/article/448462/sddc-manager-cluster-expansion-fails-wi.html)

### Inferences
- If the host is removed from the vCenter inventory and re-added, it gets a new moref (host-NN). The TNC would then try to create `hyp02-<vc-uuid>host-NN`, which avoids an id collision with the ghost `...host-26`.
- However, KB 319975 and KB 380641 say stale entries also block by **same name/IP** ("Node ... with same ip ... already exists"). So the new id could still trip an IP or name uniqueness check against the ghost. This is plausible but untested in any source.

### Gaps
- No Broadcom doc or community post explicitly describes the Policy HTN id format or confirms that a new moref sidesteps a stale record.

## Q3: In VCF, does removing a host from vCenter and re-adding it break SDDC Manager inventory? How do people reconcile it? What about vSAN ESA and VDS?

### Takeaway
Yes. SDDC Manager stores each host's vCenter MoRef as `host.source_id`. Removing and re-adding the host in vCenter makes later cluster operations fail with "Detected out-of-band changes". In VCF 9.1, Broadcom documents a supported reconcile: run an inventory sync with `POST /v1/domains/{id}/synchronizations`, which updates `source_id`. Older field fixes edited the SDDC Manager DB directly, or decommissioned and recommissioned the host. vSAN rejoin after a reimage is a documented procedure. The VDS needs the host re-added to the switch.

### Cited Findings
- **KB 448462 (SDDC Manager 9.1).** Cluster expansion fails at "Validate Platform Infrastructure State for Cluster Expansion" with "Detected out-of-band changes" (CLUSTER_EXPANSION_VALIDATION_FAILED). Cause: SDDC Manager identifies hosts by HostSystem MoRef, and a remove/re-add assigns a new one.
  - Check: `psql -h localhost -U postgres -d platform`, then `SELECT hostname, source_id FROM host WHERE hostname='<fqdn>';`. Compare the result with the moid in the vCenter MOB.
  - Fix: `GET /v1/domains`, then `POST /v1/domains/{domainId}/synchronizations`. After the sync, `source_id` matches vCenter, and the Add Host retry works.
  - Vendor-stated. — [KB 448462](https://knowledge.broadcom.com/external/article/448462/sddc-manager-cluster-expansion-fails-wi.html)
- **KB 445846 (VCF 9.x).** An out-of-band host removal makes Inventory Version Synchronization fail, and the resolution requires DB changes to purge the stale host. — [KB 445846](https://knowledge.broadcom.com/external/article/445846/inventory-version-synchronization-fails.html) (search snippet only)
- **Out-of-band VDS rename breaks NSX sync in VCF.** The fix is to rename it back. — [KB 449922](https://knowledge.broadcom.com/external/article/449922/vsphere-distributed-switch-name-mismatch.html) (search snippet only)
- **Reimaging regenerates SSH host keys.** SDDC Manager needs `fixHostKeys.py` (KB 316028). Local service accounts are wiped and must be recreated (KB 314643). — [lab2prod 2026](https://www.lab2prod.com.au/2026/08/vcf-workload-domain-stuck-deleting-after-reimaging-hosts.html); [KB 316028](https://knowledge.broadcom.com/external/article/316028/how-to-update-the-ssh-host-keys-on-the-s.html)
- **vSAN rejoin after a rebuild (KB 327032).**
  1. Remove the old host entry from vCenter.
  2. Reinstall the same ESXi build, preserving the vSAN partitions.
  3. Re-add the host and configure the vSAN vmk.
  4. Drag the host into the vSAN cluster, which enables vSAN and pushes the unicast agent list.

  — [KB 327032](https://knowledge.broadcom.com/external/article/327032) (search snippet only)
- **No ESA-specific rejoin guidance was found.** The ESA rebuild KB warns that removing ESA disks destroys data. — [KB 436377](https://knowledge.broadcom.com/external/article/436377/deploy-a-new-vsan-esa-cluster-from-an-ex.html)

### Inferences
- For hyp02, if the host is removed from vCenter and re-added to get a new moref, follow up with the KB 448462 inventory sync rather than a DB edit. Also re-add hyp02 to the VDS with the same uplink mapping, since the TNP's VDS host-switch requires the host to be a VDS member (KB 406955 and its reference to KB 383559). Then confirm vSAN ESA cluster membership (`esxcli vsan cluster get`).
- The vSAN cluster UUID is held on the host, so a remove/re-add of the inventory object should not drop the host from the vSAN cluster membership. Its storage-pool disks should re-mount, which matches the user's earlier hyp02 note that "ESA pool re-mounts itself". This is inferred and not confirmed for ESA by any source.

### Gaps
- No source covers the full VCF 9.1 sequence: remove from vCenter, re-add with a new moref, inventory sync, then NSX TNC realization.
- No confirmed report covers ESA plus a moref change.

## Q4: Are there NSX 4.2/9.x bugs or release notes about a stale HTN after a host reinstall, and fixed versions?

### Takeaway
KB 319975 claims the stale-host-blocks-re-add issue is fixed in NSX 4.2.3 and 9.0.1. KB 445611 says the force delete aborting with MP9538 on a missing VDS is fixed in 9.1+. No 9.1.x release-note entry was found for the 601/404 variant the user hit on 9.1.1, so it appears to be an unfixed residual or a different code path (MP-to-Policy or index divergence).

### Cited Findings
- KB 319975: "This issue has been resolved in NSX 4.2.3 and 9.0.1" (stale host preventing re-add). — [KB 319975](https://knowledge.broadcom.com/external/article/319975/installing-or-upgrading-nsx-on-an-esxi-h.html)
- KB 445611: the MP9538 VDS-lookup abort during force delete is fixed in NSX 9.1 and later. — [KB 445611](https://knowledge.broadcom.com/external/article/445611/force-deleting-orphaned-nsx-transport-no.html)
- KB 444026 shows a stale HTN after decommission/re-add still occurring on VCF/NSX 9.1, after the claimed 9.0.1 fix. — [KB 444026](https://knowledge.broadcom.com/external/article/444026)

### Inferences
- KB 444026 on 9.1 indicates the 9.0.1 fix does not cover every path that leaves a stale HTN behind.

### Gaps
- The NSX 9.1.x release-notes resolved-issues sections were not found or fetched. A search returned only unrelated vendors' release notes.
- No information was found about MP-to-Policy promotion leftovers causing this specific symptom.
