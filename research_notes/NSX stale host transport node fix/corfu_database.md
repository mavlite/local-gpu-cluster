# NSX Manager Corfu database: inspecting and removing a stale HostTransportNode record

Context: VCF 9.1.1 / NSX 9.1.x, single NSX Manager node, lab. TNC realization fails with
`601: The object HostTransportNode//infra/sites/default/enforcement-points/default/host-transport-nodes/hyp02-4163234c-1477-4799-b225-61b39329d18fhost-26 is already present in the system.`
Both the Policy GET and the MP transport-node GET return 404.

No Broadcom KB found in this research documents NSX **9.1** specifically. The commands below are labelled with the version each source showed. Everything under "Inferences" is reasoning, not documentation.

## Corfu tooling on NSX Manager: what exists and the exact syntax

### Takeaway
On NSX 4.x and 9.0.x, the documented entry point is `/opt/vmware/bin/corfu_tool_runner.py` (run as root). Its flags are `--tool corfu-browser|corfu-editor`, `-n <namespace>`, `-t <table>`, `-o showTable|deleteRecord`, and `--keyToDelete='<JSON key>'`. Keys are JSON, usually `{"stringId": "<id>"}`. Older NSX-T 3.x used a Java `CorfuEditorMain` class from `/opt/vmware/corfu-tools` with `printTable` and `removeEntries` instead.

### Cited Findings
- **Read-only dump of the Policy HostTransportNode table (Broadcom KB, no version stated, NSX 4.x era):** `/opt/vmware/bin/corfu_tool_runner.py --tool corfu-editor -n nsx -o showTable -t HostTransportNode | grep "host-transport-nodes"`. Run it from the NSX Manager root CLI and compare the host count with the UI to find DB-only stale entries. — [KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html); same command in [KB 387942](https://knowledge.broadcom.com/external/article/387942/identifying-and-cleaning-up-orphanstale.html)
- **Dump to file for support (Broadcom KB; links reference NSX-T 3.1 and NSX 4.2):**
  - `/opt/vmware/bin/corfu_tool_runner.py --tool corfu-editor -n nsx -o showTable -t HostTransportNode > /image/HostTransportNode.txt`
  - `/opt/vmware/bin/corfu_tool_runner.py --tool corfu-editor -n nsx -o showTable -t TransportNodeState > /image/TransportNodeState.txt`
  - Source: [KB 391840](https://knowledge.broadcom.com/external/article/391840)
- **corfu-browser showTable plus corfu-editor deleteRecord with a JSON `stringId` key (NSX 9.0.2.0, practitioner blog):**
  - `/opt/vmware/bin/corfu_tool_runner.py -n nsx -o showTable --tool corfu-browser -t RoleBinding`
  - `/opt/vmware/bin/corfu_tool_runner.py -n nsx -o deleteRecord --tool corfu-editor -t RoleBinding --keyToDelete='{"stringId": "de22d9f5-7241-439a-9885-074fc068e244"}'`
  - The tool printed "1 records deleted successfully".
  - The blog says NSX 9.0 uses this Python wrapper in place of the older Java JAR tools.
  - Source: [vstellar.com, Apr 2026](https://vstellar.com/2026/04/lessons-from-the-field-recovering-from-orphaned-avi-controllers-in-vcf-9/)
- **Official Broadcom doc using deleteRecord on a system table, with a composite JSON key, port, compaction and restart (NSX 4.2 upgrade guide; same content in the VCF 9.0 upgrade docs):**
  - `/opt/vmware/bin/corfu_tool_runner.py -t RegistryTable -n CorfuSystem --port 9000 -o deleteRecord --keyToDelete='{"namespace": "CorfuSystem","tableName": "CompactionControlsTable"}'`
  - Then run `/opt/vmware/bin/corfu_compactor_upgrade_runner.py --runs 3 --lock false`.
  - Then stop corfu-server on every manager node and start it on all of them.
  - Sources: [NSX 4.2 upgrade guide](https://techdocs.broadcom.com/us/en/vmware-cis/nsx/vmware-nsx/4-2/upgrade-guide/upgrading-nsxt/upgrading-management-plane/upgrade-management-plane-from-nsx-3-2-1-x-and-later.html); [VCF 9.0 copy](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/deployment/upgrading-cloud-foundation/upgrade-the-management-domain-to-vmware-cloud-foundation-5-2/upgrading-nsx--to-version-9/manual-upgrade-to-nsx-9-by-using-upgrade-coordinator/upgrade-management-plane-from-nsx-3-2-1-x-and-later.html)
- **Deleting HostTransportNode and HostModelMsg records for a host that failed to add as a TN (NSX 4.x-era blog, roughly 2.5 years old):**
  - The blog's `showTable` uses `--tool corfu-editor` against HostModelMsg, GenericPolicyRealizedResource and HostTransportNode.
  - The deletion uses `corfu_tool_runner.py --tool corfu-browser -o deleteRecord -n nsx -t HostTransportNode --keyToDelete` with a JSON `stringId` key that holds the host-transport-node path.
  - A separate deleteRecord with `--tool corfu-editor` removes the ESX host key from `HostModelMsg`.
  - The blog also clears the host's record from `GenericPolicyRealizedResource`.
  - Success is "1 records deleted successfully"; any other output means nothing was deleted.
  - I could not open the page itself because its TLS certificate has expired. These details come from search-engine extracts, so the exact key strings are **unverified**. — [techaspire.com.au blog 014](https://techaspire.com.au/blog-014-git-commit/), [techaspire blog index](https://techaspire.com.au/295-2/blog/)
- **Legacy NSX-T 3.x tool (2021):** corfu-browser runs from `/opt/vmware/corfu-tools` as root via the `CorfuEditorMain` class (not `-jar`). Its arguments are `-hostname <mgr IP> -port 9000 printTable -role nsx-manager -objectType EdgeNode`, then `removeEntries -role nsx-manager -objectType EdgeNode -uuid <id>`. It prompts before it deletes. The author says this should "really be done with support". — [lab2prod.com.au](https://www.lab2prod.com.au/2021/11/nsx-t-edge-deletion-failed.html)
- **Engineering mode on a Global Manager:** one KB dumps tables there by entering `st en` and the root password. — via search summary of [KB 399906](https://knowledge.broadcom.com/external/article/399906/search-index-is-out-of-sync-error-when-s.html) (not fetched directly)

### Inferences
- The tool list is not documented. Run `/opt/vmware/bin/corfu_tool_runner.py --help` on the 9.1 manager to see which operations it actually supports (for example `listTables` or `editTable`). Do not assume either exists.
- `corfu-browser` is probably the read-oriented tool and `corfu-editor` the write-capable one, but the sources use them inconsistently (corfu-editor for showTable, corfu-browser for deleteRecord). Treat both as able to write.

### Gaps
- I found no source that confirms a `listTables` operation. A search summary explicitly said none was found.
- I found no source that confirms `editTable` or `--newRecord` syntax.
- I found no 9.1-specific documentation of the tool.

## Which Corfu tables would hold the ghost record

### Takeaway
Broadcom KBs name these tables: **HostTransportNode** (Policy intent, keyed by the `host-transport-nodes` path), **TransportNodeState**, and **TransportNodeStatus**. A blog adds **HostModelMsg** and **GenericPolicyRealizedResource (GPRR)** for a host that failed to become a TN. I found no source that names a DiscoveredNode, TNC-realization or uniqueness-index table.

### Cited Findings
- HostTransportNode, TransportNodeState and TransportNodeStatus are the tables support asks you to dump for duplicate or orphan TN issues. — [KB 391840](https://knowledge.broadcom.com/external/article/391840); search summary of [KB 408798](https://knowledge.broadcom.com/external/article/408798/transport-node-display-name-and-manageme.html)
- `grep "host-transport-nodes"` on the HostTransportNode dump shows that its records carry the Policy path. — [KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html)
- HostModelMsg, GenericPolicyRealizedResource and HostTransportNode were the three tables cleaned for a host that would not add as a TN. — [techaspire blog](https://techaspire.com.au/blog-014-git-commit/) (search extract only)
- GenericPolicyRealizedResource (GPRR) is a real Corfu table. A large GPRR table is linked to a slow UI. — search summary of [KB 322456](https://knowledge.broadcom.com/external/article/322456/error-code-101-when-accessing-nsx-ui-or.html)
- `DiscoveredNode` appears only as an API model class in the NSX Java SDK. I found no evidence of a Corfu table with that name. — [NSX Java SDK model index](https://vmware.github.io/vsphere-automation-sdk-java/nsx/nsx/com/vmware/nsx/model/package-summary.html)
- The NSX 4.2.0 release notes list Fixed Issue 3321459: after a force delete of a TN from the UI or through the DELETE API, "the host is not removed from the corfu database and NSX Manager UI" (the host is stuck in uninstalling). This confirms that DB-only remnants are a known product defect. — [NSX 4.2.0 release notes](https://techdocs.broadcom.com/us/en/vmware-cis/nsx/vmware-nsx/4-2/release-notes/vmware-nsx-420-release-notes.html)
- Force delete "can still fail in some cases, particularly if the TN has already been marked for deletion". — [KB 391840](https://knowledge.broadcom.com/external/article/391840)

### Inferences (unverified)
- The 601 path ID is `hyp02-<discovered-node-id with the colon stripped>`, which looks like an ID derived from the display name and the discovered node. The TNC realizer probably tries to create a Policy HostTransportNode with exactly that ID and collides with an existing key in the HostTransportNode table.
- The API probably returns 404 because the record is filtered: it may be marked for delete, or hidden because of a stale `compute_collection_id`. It could also be that the search index is out of sync. Note that KB 415331 says an uninstall removes the `compute_collection_id`, which moves the node to Other/Standalone nodes.
- Inspection order (all read-only):
  1. `showTable -t HostTransportNode | grep -i 'hyp02\|host-26\|4163234c'`
  2. `TransportNodeState`
  3. `TransportNodeStatus`
  4. `HostModelMsg`
  5. `GenericPolicyRealizedResource`, grepping for the same strings
- Table names such as `TransportNodeCollection`, `ComputeCollectionTransportNodeTemplate`, `TNCRealizationState`, `PolicyIntent` and `DiscoveredNode` are **guesses and have not been verified**. Check whether they exist with the tool's help or list output before relying on them.

### Gaps
- I found no source that documents the Policy-to-MP ID mapping table, a unique name or ID index table, or the tombstone (`marked_for_delete`) field layout in Corfu.
- KB 319975 "Option 5" is a scripted database cleanup that requires a Broadcom support case. Its script and tables are not public.

## How Broadcom KBs handle stale host/edge TNs (escalation ladder)

### Takeaway
Broadcom's published path is UI or API first, then search resync, then a rolling reboot, then a support case for DB cleanup. The public KBs show Corfu tools only for **read-only dumps**. They never publish a host-TN `deleteRecord`; DB deletion is support-only (KB 319975 Option 5).

### Cited Findings
- **KB 422174 (NSX 4.2.x):** this is the exact 601 "HostTransportNode ... is already present in the system" error, seen when adding a host through SDDC Manager, which stalls at Prepare Transport Node Collection. Cause: the host was removed from vCenter without first removing NSX. Fix: System > Fabric > Hosts, select the host, **CONFIGURE NSX**. If that fails, the KB points to KB 319975, 313788, 418946 and 373328. — [KB 422174](https://knowledge.broadcom.com/external/article/422174)
- **KB 319975** (affects NSX-T 3.x, NSX 4.x and 9.0.0; fixed in 4.2.3 and 9.0.1):
  - Option 2: `start search resync policy`, then `start search resync manager`, then `start search resync telemetry`. Wait at least 10 minutes, then `start search resync all`.
  - Option 3 (MP API): `GET /api/v1/transport-nodes/<uuid>/state`, then `DELETE /api/v1/transport-nodes/<uuid>?force=true&unprepare_host=false`.
  - Option 4 (Policy API): `GET .../infra/sites/default/enforcement-points/default/host-transport-nodes/<node_name>/state`, then `DELETE` the same path with `?force=true&unprepare_host=false`.
  - After each DELETE, wait 5 minutes and repeat the GET until it returns "Object Not found".
  - On the host: `nsxcli -c del nsx`, then reboot (vLCM case).
  - Option 5 is the scripted DB cleanup, which needs a support case.
  - Source: [KB 319975](https://knowledge.broadcom.com/external/article/319975)
- **KB 423818 / 387942:** for DB-only stale entries, use the showTable grep above, then apply the KB 319975 resolution. If it persists, open a support case to delete the DB entries, or do a rolling reboot of the managers, after which orphans may appear under Standalone Nodes where they can be deleted. — [KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html), [KB 387942](https://knowledge.broadcom.com/external/article/387942/identifying-and-cleaning-up-orphanstale.html)
- **KB 447344 (NSX 4.x):** stale TNs stay in Corfu while a decommissioned vCenter is still registered as a Compute Manager. Delete that Compute Manager, then Force Delete, then `start search resync all` on every manager. — [KB 447344](https://knowledge.broadcom.com/external/article/447344/nsx-upgrade-failure-due-to-stale-transpo.html)
- **KB 447383:** stale TN alarms for decommissioned hosts. Run `start search resync all`, then Force Delete, then resync again. — search summary of [KB 447383](https://knowledge.broadcom.com/external/article/447383/stale-transport-node-alarms-for-decommis.html)
- **KB 415331:** the TN is gone from Clusters but still in NSX. Look in Other Nodes / Standalone and use REMOVE NSX > Force Delete. — [KB 415331](https://knowledge.broadcom.com/external/article/415331/transport-node-removed-from-clusters-but.html)
- **Stale edges, KB 345848 (NSX-T 3.2.1+):** the API `POST https://<nsx-mgr>/api/v1/transport-nodes?action=clean_stale_entries`, or `curl -u admin -k -X POST https://127.0.0.1/api/v1/transport-nodes?action=clean_stale_entries` run as root on the manager holding the VIP. — [KB 345848](https://knowledge.broadcom.com/external/article/345848/cleanup-stale-edge-nodes-present-in-the.html)
- **KB 398710:** `service proton stop` on all managers, then `start search resync policy` and `start search resync manager` "so that corfudb and Search indexes are in sync". This comes from a migration context. — search summary of [KB 398710](https://knowledge.broadcom.com/external/article/398710)

### Inferences
- Before any Corfu edit, run `clean_stale_entries`. It is documented for edges, but it acts on the generic transport-nodes collection, so it may also sweep host remnants (unverified). Also run `start search resync all` and a manager reboot. On a single node, a "rolling reboot" is just one reboot.
- For this case, also check for a stale/duplicate Compute Manager registration (KB 447344) and for a "CONFIGURE NSX" option on the host in the UI (KB 422174).

### Gaps
- No public KB gives the exact `deleteRecord` keys for a host TN.

## Prerequisites, risks, safe procedure

### Takeaway
Every source treats direct Corfu edits as support-only and potentially catastrophic. No source says you must stop proton or policy before a `deleteRecord`. The official deleteRecord example (RegistryTable) instead restarts corfu-server on all nodes afterwards. Take a backup first and stay read-only until the exact key is identified.

### Cited Findings
- Practitioner disclaimer: "Don't attempt the following steps in a prod environment and without Broadcom support". Modifying the database "can be catastrophic and can lead to inconsistent states and outages". The blog's procedure stopped no services and took no backup. — [vstellar.com](https://vstellar.com/2026/04/lessons-from-the-field-recovering-from-orphaned-avi-controllers-in-vcf-9/)
- KBs 423818, 391840 and 319975 all route DB deletion to Broadcom Support. — [KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html), [KB 391840](https://knowledge.broadcom.com/external/article/391840), [KB 319975](https://knowledge.broadcom.com/external/article/319975)
- Corfu health and compaction checks:
  - `service corfu-server status`
  - `/var/log/corfu/corfu.9000.log`
  - `/var/log/corfu/corfu-compactor.log`
  - `grep "completed checkpoint" corfu-compactor-audit.*`
  - `get filesystem-stats` (watch `/config` utilisation)
  - `get runbook CorfuServer help`
  - Source: [KB 378470](https://knowledge.broadcom.com/external/article/378470)
- After a deleteRecord, the official sequence was to run compaction (`corfu_compactor_upgrade_runner.py --runs 3 --lock false`) and then restart corfu-server on all nodes. — [NSX 4.2 upgrade guide](https://techdocs.broadcom.com/us/en/vmware-cis/nsx/vmware-nsx/4-2/upgrade-guide/upgrading-nsxt/upgrading-management-plane/upgrade-management-plane-from-nsx-3-2-1-x-and-later.html)
- When an API DELETE returned 200 OK, the node still showed in the UI. Verify through the UI and API, not just HTTP codes. — [lab2prod.com.au](https://www.lab2prod.com.au/2021/11/nsx-t-edge-deletion-failed.html); KB 423818 also notes the task can stay stuck at 20% after a 200 OK.
- Tool output can include credentials in plain text, so handle dumps carefully. — [lab2prod.com.au](https://www.lab2prod.com.au/2021/11/nsx-t-edge-deletion-failed.html)

### Inferences: suggested lab procedure (synthesis, not a Broadcom procedure)
1. **Back up.**
   - Take a successful NSX backup to SFTP. The manual backup API is believed to be `POST /api/v1/cluster?action=backup_to_remote`, with the config at `/api/v1/cluster/backups/config`. This is from prior knowledge and was **not verified in this research**.
   - Also take a VM-level snapshot of the single NSX Manager, powered off or quiesced. On a single node there is no peer to re-sync from, so the snapshot is the real rollback.
2. **Exhaust the supported steps.**
   - In the UI: Other/Standalone nodes > Force Delete.
   - Policy and MP `GET .../state` and `DELETE ?force=true&unprepare_host=false` against `hyp02-4163234c-1477-4799-b225-61b39329d18fhost-26`.
   - `POST /api/v1/transport-nodes?action=clean_stale_entries`.
   - `start search resync all`, waiting at least 10 minutes.
   - Reboot the manager.
3. **Read-only inspection.**
   - Dump HostTransportNode, TransportNodeState, TransportNodeStatus, HostModelMsg and GenericPolicyRealizedResource to `/image/*.txt`.
   - Grep each dump for `hyp02`, `host-26` and `4163234c`.
   - For each hit, record the exact key JSON (for example `{"stringId": "/infra/sites/default/enforcement-points/default/host-transport-nodes/hyp02-..."}`) and fields such as `marked_for_delete`.
4. **Delete.** Use `deleteRecord` one table at a time, starting with the Policy `HostTransportNode` record that the 601 names, with the exact key copied from the dump. Expect "1 records deleted successfully". Re-run the TNC, then delete the related HostModelMsg and GPRR rows only if the 601 or a new error points to them.
5. **Restart and verify.**
   - Restart corfu-server, or reboot the manager.
   - `start search resync all`.
   - Re-dump the tables.
   - Re-apply the TNC, which should reach SUCCESS.
   - Check that `get cluster status` and the `/config` usage look normal.
   - Whether `proton` or `policy` should be stopped around the edit is **not documented**. One option is to stop them before the edit and start them after, so the in-memory caches reload, but this is unverified.

### Gaps
- I found no source on stopping proton or policy before a deleteRecord, or on the restart order for a single node.
- I did not find Broadcom's general statement on the supportability of customer-run corfu edits, beyond "open a support case".
- I did not verify the exact backup API path.
