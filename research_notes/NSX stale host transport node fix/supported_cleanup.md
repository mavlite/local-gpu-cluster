# Supported cleanup of a stale/ghost NSX host transport node blocking TNC preparation (NSX 4.x / 9.x, VCF 9.1)

Context: VCF 9.1.1, NSX 9.1.x single manager, vLCM cluster domain-c9 with TNP applied via TNC. hyp02 (host-26) was reinstalled. The MP TN and Policy HostTransportNode now return 404, but the TNC stays FAILED_TO_REALIZE with `601: The object HostTransportNode/.../hyp02-4163234c-...host-26 is already present in the system.` A Policy PUT was refused with 26208 "Credentials for host need to be specified".

## Q1. Broadcom KBs for error 601 "is already present in the system", stale TN after reinstall, orphaned TNs, TNC FAILED_TO_REALIZE

### Takeaway
Broadcom has KBs that match this exact error string (601 ... is already present in the system) and the exact 26208 error. All of them blame the same cause: the host was removed from vCenter, or rebuilt, without first removing NSX. They all point to one hub article, KB 319975. The fix is always the same in outline: put the host in maintenance mode, move it out of the cluster, force-delete it (UI, then MP API, then Policy API), run a search resync, then put the host back so the TNP prepares it. No KB found covers the residual state seen here, where the TN is already 404 on both MP and Policy but the TNC still carries a 601 validation error.

### Cited Findings
- **KB 422174 (NSX 4.2.x + SDDC Manager).** This is the exact 601 string: "601: The object HostTransportNode … is already present in the system". It appears as a host "validation error" while SDDC Manager add-host is stuck at "Prepare Transport Node Collection".
  - Cause: the host was removed from vCenter without first being removed from NSX.
  - Resolution: NSX UI > System > Fabric > Hosts > select host > **CONFIGURE NSX**.
  - If the issue persists, the KB points to KBs 319975, 313788, 418946 and 373328. — [KB 422174](https://knowledge.broadcom.com/external/article/422174/adding-host-to-nsx-prepared-cluster-thro.html)
- **KB 420783.** Same "is already present in the system" validation error on the HostTransportNode path.
  - Resolution: maintenance mode, remove the host from the cluster, then check System > Fabric > Hosts > Other hosts. If the host is listed there, run Delete NSX with force delete, confirm "not configured", then re-add the host.
  - No API calls are given. — [KB 420783](https://knowledge.broadcom.com/external/article/420783/unable-to-install-the-add-the-host-back.html)
- **KB 380641 (NSX 4.x).** Exact 26208 match: "Credentials for host need to be specified. (Error code: 26208)" when a PUT goes to `/policy/api/v1/infra/sites/default/enforcement-points/default/host-transport-nodes/<host TN>`. Cause: "stale entry of the Node when ESXi TN being rebuilt with same DNS and IP".
  - **Without vLCM:** detach the TNP from the cluster, force-delete NSX from the orphaned host, reapply the TNP, and add the host back if it is not in the cluster.
  - **With vLCM:** "TransportNodeProfile cannot be removed". Workaround:
    1. Put the host in maintenance mode.
    2. Move it to the Datacenter level.
    3. Confirm NSX is fully removed: no NSX VIBs, and the NSX UI shows "Unprepared".
    4. Return the host to the cluster so NSX prepares it per the TNP.
  - The KB refers to KB 375111 "For a Single Host". — [KB 380641](https://knowledge.broadcom.com/external/article/380641)
- **KB 319975 (NSX 9.0.0, NSX 4.x, NSX-T 3.x; fixed in 4.2.3 and 9.0.1).** This is the hub article.
  - Prerequisite: maintenance mode, then move the host to datacenter level so it appears under Other Nodes.
  - Options to try in order:
    1. UI REMOVE NSX with Force Delete.
    2. Search resync (see Q3).
    3. MP API: `GET /api/v1/transport-nodes/<UUID>/state`, then `DELETE /api/v1/transport-nodes/<UUID>?force=true&unprepare_host=false`. Repeat the GET until it returns "Object Not found".
    4. Policy API: `GET /policy/api/v1/infra/sites/default/enforcement-points/default/host-transport-nodes/<node_name>/state`, then `DELETE …/host-transport-nodes/<node_name>?force=true&unprepare_host=false`.
    5. Scripted database cleanup through a Broadcom support case.
  - When all options are done, move the host back into the cluster and a TNP-applied cluster prepares it automatically.
  - For security-only, vLCM or Service Insertion clusters, the TNP cannot be detached. Move the single host to datacenter level instead, and remove leftover VIBs with `nsxcli -c del nsx` plus a reboot. — [KB 319975](https://knowledge.broadcom.com/external/article/319975)
- **KB 392879.** ESXi reinstalled after a faulty disk, never unprepared in NSX, and TN config fails with "General error has occurred. (Error code:100)".
  - Fix: make the host standalone, then `GET /api/v1/transport-nodes/state` to find the Failed UUID, then `DELETE /api/v1/transport-nodes/<UUID>?force=true&unprepare_host=false`. Poll `/state` until "Object Not found", then move the host back into the cluster. — [KB 392879](https://knowledge.broadcom.com/external/article/392879)
- **KB 406955.** Re-added host fails with 26164 even though no NSX VIBs are on the host.
  - Fix: maintenance mode, remove from cluster, `GET /api/v1/transport-nodes/<UUID>/status`, `DELETE /api/v1/transport-nodes/<UUID>?force=true`, re-add the host. — [KB 406955](https://knowledge.broadcom.com/external/article/406955)
- **KB 423818 and KB 387942.** These cover orphaned/stale entries in two forms:
  - Visible in the UI: Remove NSX, or `DELETE /api/v1/transport-nodes/<UUID>?force=true`.
  - In the DB but not the UI: from root, run `/opt/vmware/bin/corfu_tool_runner.py --tool corfu-editor -n nsx -o showTable -t HostTransportNode | grep "host-transport-nodes"`, then follow KB 319975 or open a support case.
  - "A rolling reboot of the NSX Manager nodes may be required". After it, orphans may show under "Standalone Node".
  - Re-imaged or renamed hosts keep their old TN records. Remove the old entry under Other Nodes, then add the host back to the prepared cluster. — [KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html); [KB 387942](https://knowledge.broadcom.com/external/article/387942/identifying-and-cleaning-up-orphanstale.html)
- **KB 438950 (SDDC Mgr 5.2.1 / NSX 4.2).** The TNC state stayed FAILED_TO_REALIZE even after every TN reached success. The fix was to retry the "Add new host(s) to cluster" operation, which re-runs the TN State API and recomputes the TNC state. No API endpoint is given. — [KB 438950](https://knowledge.broadcom.com/external/article/438950/transport-node-collection-remains-in-uns.html)
- **KB 432202.** If a host hits a validation error when it is re-added to a cluster, do a "No-Operation" update on the TNP: edit it and save with no changes. This "forces a global refresh" of the profile across all associated hosts. The KB's own trigger is error 9779 (missing TZ profile). — [KB 432202](https://knowledge.broadcom.com/external/article/432202/nsx-host-validation-error-9779-transport.html)
- **KB 448232 (vLCM, prep stuck at 23%, UUID mismatch).** Steps in order:
  1. Maintenance mode.
  2. Delete stale segment ports.
  3. On all managers, `start search resync all` and `start search resync telemetry`.
  4. Move the host to datacenter level.
  5. Force-delete the TN if it is still orphaned.
  6. `esxcli software vib list | grep -i nsx`.
  7. If VIBs remain, `nsxcli -c del nsx`.
  8. Reboot (required for vLCM prep).
  9. Re-add the host to the cluster.
  - It also suggests comparing `get node-uuid` on the host (nsxcli) with the UUID shown in the UI. — [KB 448232](https://knowledge.broadcom.com/external/article/448232/nsx-host-preparation-process-stuck-at-23.html)
- **KB 445611 and KB 406768.** Force-deleting an orphaned TN can loop at 8% with MP9538 "Could not find the HostSwitch" (fixed in NSX 9.1+). Force delete can also hang at 20% with "waiting_for_host_config_reply" (NSX 4.x).
  - Bulk cleanup scripts exist but are available only through a Broadcom support case. — [KB 445611](https://knowledge.broadcom.com/external/article/445611/force-deleting-orphaned-nsx-transport-no.html); [KB 406768](https://knowledge.broadcom.com/external/article/406768)
- **KB 444026 (VCF 9.1 / NSX 9.1).** A stale host TN record left over from decommission and recommission blocks NSX upgrade finalize.
  - Fix: Other Nodes > Remove NSX (Force Delete), then confirm `GET /api/v1/upgrade/upgrade-units?component_type=HOST` returns one unit per physical host. — [KB 444026](https://knowledge.broadcom.com/external/article/444026/deleted-esx-host-from-vcenter-blocks-the.html)
- **KB 403287 (prevention).** Correct order: maintenance mode, remove the host from the cluster, Remove NSX, verify `esxcli software vib list | grep -E 'nsx|vsipfwlib'`, wait for Not Prepared, then remove from vCenter. — [KB 403287](https://knowledge.broadcom.com/external/article/403287)

### Inferences
- The 601, 26208, 9411 and "reinstalled after disk failure" symptoms together match KBs 422174, 380641, 319975 and 392879. This is a documented class of problem.
- This case has already gone past the documented end state: MP and Policy both return 404 for the TN. The remaining defect is the TNC's cached validation error, or a leftover Corfu HostTransportNode intent row, keyed on the discovered-node ID. The documented next steps are:
  - KB 422174 "CONFIGURE NSX" on the discovered host.
  - The KB 380641 vLCM loop (datacenter, then confirm Unprepared, then back into the cluster).
  - The KB 432202 no-op TNP save.
  - The KB 438950 retry of add-host, which recomputes the TNC.
  - KB 387942 corfu showTable (read-only) to check for a residual HostTransportNode row. If one exists, the only documented fix is a support case (KB 319975 Option 5 / KB 406768).
- In this case the host was moved back into the cluster while a stale intent still existed. Per KB 380641's vLCM path, the documented loop is to move the host to datacenter level again, wait for "Unprepared/Not Configured" under Other Nodes, and only then return it.

### Gaps
- No KB found addresses the TNC keeping a 601 validation error after the HTN returns 404 on both APIs.
- KB 375111 ("Troubleshooting NSX Uninstall Process") and KB 405458 were not fetched.
- The 601 KBs give no NSX 9.x-specific steps.

## Q2. Policy-side cleanup APIs (force delete, unprepare_host, clean_stale_entries, realized-state refresh, TNC actions, detach/reapply TNP)

### Takeaway
These are the documented APIs:
- Policy and MP DELETE with `force=true&unprepare_host=false`. Force is explicitly not supported while the TN is in a TNP-applied cluster, which explains error 9411.
- `POST /policy/api/v1/infra/realized-state/realized-entity?action=refresh&intent_path=…`.
- TNC `?action=retry_profile_realization` (vLCM only) and `?action=remove_nsx`.

`clean_stale_entries` is documented only as an Edge cleanup in the KBs, plus one TechDocs mention for hosts after a forced removal (not verified, see Gaps).

### Cited Findings
- **Policy HTN DELETE (NSX 9.1.0 API).** `DELETE /policy/api/v1/infra/sites/{site-id}/enforcement-points/{ep-id}/host-transport-nodes/{id}`. Query params:
  - `force`: default False.
  - `unprepare_host`: default True. "If unprepare_host option is set to false, then host will be deleted without uninstalling the NSX components".
  - The doc says "Force delete is not supported if transport node is part of a cluster on which Transport node profile is applied".
  - With force=true and a failed uninstall, the TransportNodeState object is deleted. Without force it is retained.
  - [NSX 9.1.0 API DeleteHostTransportNode](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/9.1.0/method_DeleteHostTransportNode.html)
- **MP route deprecated.** The 9.0.2 reference marks the old `/api/v1/transport-nodes` route as deprecated in favour of the Policy host-transport-nodes route. KBs still use it for cleanup. — [NSX 9.0.2 API DeleteTransportNodeWithDeploymentInfo](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/9.0.2/method_DeleteTransportNodeWithDeploymentInfo.html)
- **TNC remove_nsx.** `POST /policy/api/v1/infra/sites/{site-id}/enforcement-points/{ep-id}/transport-node-collections/{tnc-id}?action=remove_nsx` "uninstalls NSX applied to the Transport Node Collection". No body. Returns 201. Errors include 409 and 412. — [NSX 9.1.0 API RemoveNSXOnTransportNodeCollection](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/9.1.0/method_RemoveNSXOnTransportNodeCollection.html)
- **TNC retry_profile_realization.** `?action=retry_profile_realization` on the TNC retries the TNP on a vLCM-enabled compute collection and has no effect without vLCM. NSX docs also describe the UI "Resolve" button for the same thing. Reported in search summaries of the NSX API guide and NSX 4.1 docs; the full page was not fetched (vdc-download returned 401). — [NSX API TNC section](https://vdc-download.vmware.com/vmwb-repository/dcr-public/201a09cb-5610-49e6-9d31-7db0920e3a65/65f199d6-8516-4092-9b19-906559f972b5/api_includes/system_administration_configuration_fabric_nodes_transport_node_collections.html); [NSX 4.1 doc](https://docs.vmware.com/en/VMware-NSX/4.1/installation/GUID-0938D3D6-26B4-4FEB-B4BC-E6B6F5F81BC7.html)
- **Realized-state refresh.** `POST /policy/api/v1/infra/realized-state/realized-entity?action=refresh&intent_path=<%2F-escaped path>[&enforcement_point_path=…]` "refreshes the status and statistics of the realized entities" for an intent. It is synchronous and returns 204. Documented in 3.2.x, 4.1 and 4.2, and the IntentPathRequestParameter type exists in 9.0. Note that it refreshes status and does not delete intents. — [NSX 4.2 API RefreshRealizedState](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/4.2/method_RefreshRealizedState.html); [9.0.0 IntentPathRequestParameter](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/9.0.0/types_IntentPathRequestParameter.html)
- **clean_stale_entries.** `POST /api/v1/transport-nodes?action=clean_stale_entries` (NSX-T 3.2.1+) is documented for stale **Edge** entries. KB 345813 gives a per-node variant, and for NSX 9.0+ says to use a Policy DELETE with force=true instead. — [KB 345848](https://knowledge.broadcom.com/external/article/345848/cleanup-stale-edge-nodes-present-in-the.html); [KB 345813](https://knowledge.broadcom.com/external/article/345813/troubleshooting-edge-deletion-failures-i.html)
- **Detach and reapply TNP.** This is supported only when vLCM does not manage the cluster. "With vLCM controlling host images … TransportNodeProfile cannot be removed". — [KB 380641](https://knowledge.broadcom.com/external/article/380641); [KB 319975](https://knowledge.broadcom.com/external/article/319975)

### Inferences
- Error 9411 is the documented behaviour that force delete is refused while the TN is in a TNP cluster. That is why every KB moves the host to datacenter level first.
- `remove_nsx` on the TNC would uninstall NSX from every host in domain-c9, which is cluster-wide disruption. Neither KB 319975 nor KB 380641 recommends it for a single stale host.
- `retry_profile_realization` was already tried and is documented only for vLCM errors. It does not clear an intent conflict.

### Gaps
- No documented API deletes an orphaned Policy intent that no longer appears in GET/list.
- No documented `?action=` on an HTN besides DELETE.
- Whether `clean_stale_entries` applies to hosts on NSX 9.x is unverified. A search summary cited NSX-T 4.1 TechDocs (GUID 1B9155BA) for it after a host forced removal, but the page redirected and was not read.

## Q3. NSX Manager CLI (search resync, corfu) and host-side `del nsx` with vLCM / ESXi 9

### Takeaway
- KB 319975 Option 2 documents `start search resync policy|manager|telemetry`, then `all`, to fix the UI/search index. It does not fix the Corfu intent.
- `nsxcli -c del nsx` is a host-side last resort, used only when NSX VIBs remain after the host has been moved out of the cluster. It needs maintenance mode, and vLCM hosts need a reboot afterwards.

In this case the host has no running nsx-proxy and empty certificates. The documented check is still `esxcli software vib list | grep -i nsx`.

### Cited Findings
- **Search resync.** On each manager, as admin and one at a time: `start search resync policy`, `start search resync manager`, `start search resync telemetry`. Wait at least 10 minutes. If the host is still listed, run `start search resync all`, wait, then retry the force delete. — [KB 319975](https://knowledge.broadcom.com/external/article/319975); also [KB 378658](https://knowledge.broadcom.com/external/article/378658)
- **Corfu check.** Read-only check for a DB-only stale row: `/opt/vmware/bin/corfu_tool_runner.py --tool corfu-editor -n nsx -o showTable -t HostTransportNode | grep "host-transport-nodes"`. Deleting the row requires Broadcom support. — [KB 387942](https://knowledge.broadcom.com/external/article/387942/identifying-and-cleaning-up-orphanstale.html)
- **del nsx on vLCM hosts.** `nsxcli -c del nsx` is for vLCM clusters where VIBs remain after the host moves to datacenter level, followed by a reboot. — [KB 319975](https://knowledge.broadcom.com/external/article/319975); [KB 448232](https://knowledge.broadcom.com/external/article/448232/nsx-host-preparation-process-stuck-at-23.html)
- **del nsx preconditions (NSX 4.2 docs).**
  - The host must be in maintenance mode, with NSX portgroup resources migrated off.
  - "VLCM enabled hosts require a reboot to successfully remove the VIBs after executing del nsx".
  - It will not run while the host has an active connection to the manager.
  - Force Delete in the UI is the alternative.
  - Reported via search summary of [NSX 4.2 install guide](https://docs.vmware.com/en/VMware-NSX/4.2/installation/GUID-52FA28CD-CB47-46C6-9D7B-F006916E3C87.html) and [Broadcom KB on decommission/force delete](https://knowledge.broadcom.com/external/article/445222/nsx-host-preparation-fails-with-failed-t.html).
- **Busy errors.** When `del nsx` fails with "Busy" (bad0004) on vdl2, the documented steps are `net-dvs` property removal, then removal of the vmk10/11/50 VTEP interfaces, then a retry. The KB cautions against `esxcli software vib remove` on NSX VIBs. — [KB 451192](https://knowledge.broadcom.com/external/article/451192/del-nsx-not-working-consolidated-issues.html)

### Inferences
- A freshly reinstalled host with no NSX VIBs and no nsx-proxy needs no `del nsx`. The stale state is entirely on the manager side.
- `del nsx` would only become relevant if vLCM pushed NSX VIBs during a failed prep attempt. The VIB check decides.

### Gaps
- Found no NSX 9.x / ESXi 9 doc on how `del nsx` interacts with NSX components embedded in the vLCM image (ESXi 9 base image).
- Did not determine whether search resync or a manager restart can clear a TNC validation_errors cache.

## Q4. Policy-to-MP sync for HostTransportNode and whether a stale intent survives deletion

### Takeaway
No Broadcom documentation describes the internal Policy-to-MP intent mapping for HostTransportNode or how it is cleaned. The KBs only acknowledge DB-only stale records, which need Corfu inspection plus a support case, and orphans that reappear after a manager reboot.

### Cited Findings
- **DB-only entries.** Stale entries can live "in the database but not in the UI". KB 387942 gives the Corfu showTable HostTransportNode check and sends deletion to support. — [KB 387942](https://knowledge.broadcom.com/external/article/387942/identifying-and-cleaning-up-orphanstale.html)
- **Reappearing orphans.** After a rolling manager reboot, orphans may reappear under "Standalone Node" and then become deletable. — [KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html)
- **TNC state recompute.** The TNC state can remain FAILED_TO_REALIZE after the underlying TNs are fixed. A retry of add-host recomputes it. — [KB 438950](https://knowledge.broadcom.com/external/article/438950/transport-node-collection-remains-in-uns.html)
- **Corfu tables lag the UI.** A related precedent for Edges: stale Corfu entries remain after the UI shows the node gone. — [KB 407533](https://knowledge.broadcom.com/external/article/407533)

### Inferences
- The 601 error comes from the Policy layer: the HostTransportNode intent path is "already present". The TNC realizer probably holds either a Corfu HostTransportNode intent row that list/GET filters out, or a stale validation result.
- Running the KB 387942 corfu showTable grep for `hyp02-4163234c` is a read-only way to tell these two apart.

### Gaps
- No documentation found on Policy intent tombstones for HTN or on the TNC validation_errors lifecycle.

## Q5. VCF 9.x SDDC Manager / VCF Operations remove and re-add host as the supported route

### Takeaway
In VCF the documented route is to remove the host from the cluster/domain, decommission it, re-image it, commission it, then add it back. But no VCF 9 doc says this clears NSX stale state. KBs say SDDC Manager's NSX cleanup is best-effort and that stale TNs must still be removed in NSX first. A failed SDDC "add host" can be retried, and the retry recomputes the TNC state (KB 438950).

### Cited Findings
- **VCF 4.x/5.x docs.** Remove the host from the workload domain through SDDC Manager (keep enough vSAN hosts, move NSX Edges off). Decommission it under Unassigned Hosts. After decommission it must be re-imaged and commissioned before reuse. — [VCF 5.1 Remove a Host](https://docs.vmware.com/en/VMware-Cloud-Foundation/5.1/vcf-admin/GUID-41BE1B30-8D52-43BF-8297-0059B92D723D.html); [KB 414650](https://knowledge.broadcom.com/external/article/414650)
- **Cluster deletion.** The SDDC Manager cluster-deletion workflow removes the TNP binding, and NSX may lose the parent reference before it unprepares the hosts. This leaves orphans, and cleanup is described as "best-effort". — [KB 409724](https://knowledge.broadcom.com/external/article/409724/stale-transport-nodes-in-nsx-after-sddc.html)
- **SDDC Manager "Prepare Transport Node Collection" failure with 601.** The fix is in the NSX UI ("CONFIGURE NSX"), not in SDDC Manager. — [KB 422174](https://knowledge.broadcom.com/external/article/422174/adding-host-to-nsx-prepared-cluster-thro.html)
- **VCF 9.1.** A stale TN left over from decommission/recommission blocks NSX upgrade finalize. The fix is Remove NSX (Force) in NSX Manager. — [KB 444026](https://knowledge.broadcom.com/external/article/444026/deleted-esx-host-from-vcenter-blocks-the.html)
- **Imported (brownfield) clusters.** SDDC Manager refuses host add/remove on imported clusters, and the operation must be done in vCenter. — [KB 405095](https://knowledge.broadcom.com/external/article/405095/unable-to-add-the-esxi-to-a-cluster-on-a.htm); [William Lam 2026-06](https://williamlam.com/2026/06/what-host-lifecycle-operations-are-available-after-importing-vcenter-into-vcf-9-x-fleet.html)

### Inferences
- A decommission plus commission cycle through VCF is the supported way to rebuild hyp02's VCF inventory record. It would not, by itself, clear an NSX-side 601 intent conflict.
- The supported order is: clear the NSX stale state (KB 319975 options, or a support case), then commission and add the host. Or, if the host is already in the cluster, retry "Add host" / TNC realization.
- Whether domain-c9 is an imported cluster decides whether SDDC Manager host operations are even allowed.

### Gaps
- No VCF 9.0/9.1 TechDocs page on "Remove a Host from a vSphere Cluster" or decommission was fetched. The cited procedure is from VCF 5.1, so the VCF Operations UI paths in 9.x are unverified.
- No source states that a VCF remove/decommission/commission cycle clears a stale NSX HostTransportNode intent.

### Documented vs community
All findings above come from Broadcom KBs or the Broadcom API reference, except one third-party source: the William Lam blog post, which is community material. Items marked "reported via search summary" were not read in full.
