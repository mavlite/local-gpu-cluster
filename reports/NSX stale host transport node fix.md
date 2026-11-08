# Clear hyp02's ghost transport node, step by step

**Bottom line:** hyp02 is stuck because NSX still has a persisted record of the host transport node (HTN) `hyp02-4163234c-1477-4799-b225-61b39329d18fhost-26`, even though the list and GET APIs no longer return it. That record survived a full NSX Manager cold restart, so it is not a memory cache.

Broadcom documents this class of problem. The exact 601 string is in KB 422174 and the exact 26208 error is in KB 380641. The hub article for both, KB 319975, has a five-option ladder whose last option is a scripted database cleanup run by Broadcom support. No KB covers the leftover state you have now, where both APIs return 404 but the Transport Node Collection (TNC) still raises 601.

Your first attempt at the documented vLCM workaround stopped short. You moved hyp02 back into the cluster while NSX still showed "Removing host status 40%". KB 380641 says to wait until the host shows as unprepared before moving it back.

The recommended order is:
1. Read-only diagnosis, including the Corfu `showTable` check from KB 387942. This tells you whether a database row exists at all.
2. Three cheap documented nudges.
3. Redo the move to datacenter level and let the removal finish.
4. Remove hyp02 from vCenter and re-add it. This gives it a new moref and therefore a new HTN id. This route is inferred, not documented.
5. Edit Corfu directly, with an NSX backup and a powered-off snapshot of the NSX Manager VM.

Nothing in this plan touches hyp01 or hyp03, as long as you never use the TNC-wide `remove_nsx` action and never delete a Corfu row whose key does not contain `host-26` or `hyp02-4163234c`.

## What has already been tried, and what it tells us

These steps are already done, so the plan does not repeat them:
- A per-TN delete inside the cluster was refused with **9411**. This is expected: the API reference says "Force delete is not supported if transport node is part of a cluster on which Transport node profile is applied" ([NSX 9.1.0 API DeleteHostTransportNode](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/9.1.0/method_DeleteHostTransportNode.html)).
- Moving hyp02 to datacenter level removed the management-plane (MP) TN, but the removal stopped at "orphaned / Removing host status 40%". The host was then moved back.
- A full NSX Manager cold restart hid the stale MP TN and the Policy HTN from the list APIs. Both now return 404.
- `retry_profile_realization` ran four cycles over 24 minutes and failed with the same 601 each time.
- A direct Policy PUT was refused, first for an extra field and then with **26208**. KB 380641 attributes 26208 to a "stale entry of the Node when ESXi TN being rebuilt with same DNS and IP" ([KB 380641](https://knowledge.broadcom.com/external/article/380641)).

On the host side, nsx-proxy is not running, the certificate files are empty, and the discovered node shows `owner_nsx=NONE` with vmnic0 and vmnic1.

These results point the same way. The cold restart is the single-node version of the "rolling reboot of the NSX Manager nodes" that KBs 423818 and 387942 suggest ([KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html); [KB 387942](https://knowledge.broadcom.com/external/article/387942/identifying-and-cleaning-up-orphanstale.html)). It made the orphan invisible but did not make it deletable. Because the 601 came back after the restart, the conflict is stored in the database, not held in memory.

The closest documented match is KB 322453. In that case host preparation fails with "Credentials for the host need to be specified" and `nsxapi.log` reports a discovered node that "was recently deleted and is being cleaned up". Broadcom's stated cause is "Inconsistent records in the NSX Corfu database", and the only remedy it offers is a support case ([KB 322453](https://knowledge.broadcom.com/external/article/322453)).

The interrupted 40% removal fits that picture: a delete started, was interrupted, and was never completed. Two other things are worth knowing:
- KB 319975 says the stale-host problem was fixed in NSX 4.2.3 and 9.0.1 ([KB 319975](https://knowledge.broadcom.com/external/article/319975)). KB 444026, however, shows a stale HTN after decommission and re-add on VCF/NSX 9.1 ([KB 444026](https://knowledge.broadcom.com/external/article/444026/deleted-esx-host-from-vcenter-blocks-the.html)). The fix evidently does not cover every path.
- The host is not the problem. KB 319975 notes that reinstalling ESXi does not help, because the stale record sits on the manager.

### On ESXi 9 the "no NSX VIBs" check cannot pass

KB 380641's vLCM workaround tells you to confirm "no NSX VIBs" and an "Unprepared" UI state before you return the host to the cluster. On ESXi 9 the NSX VIBs ship in the base image, so `esxcli software vib list | grep -i nsx` will always find them.

The practical test is therefore the NSX side: the host shows as Not Configured, and the transport-node GET calls return "Object Not found". Do not run `nsxcli -c del nsx`:
- KB 319975 and KB 448232 prescribe it only when NSX components remain after the host leaves the cluster, followed by a reboot ([KB 448232](https://knowledge.broadcom.com/external/article/448232/nsx-host-preparation-process-stuck-at-23.html)).
- hyp02 has no NSX configuration. nsx-proxy is down and the certificates are empty.
- Removing base-image VIBs would make the host non-compliant with its vLCM image.

The research found no NSX 9 documentation on how `del nsx` interacts with base-image VIBs. That is an unknown, not a green light.

## Step 0: prepare, back up and diagnose without changing anything

Everything in this step is read-only or a backup. Its job is to decide which branch to follow later.

**Snapshot and backup.** Take both before anything else, because the later rungs end in a database edit:
- A powered-off VM snapshot of the single NSX Manager. With no cluster peer to re-sync from, the snapshot is your only real rollback. Shutting the manager down briefly should leave hyp01 and hyp03 forwarding traffic, but that is an inference.
- An NSX backup to SFTP.

The backup API is believed to be `POST /api/v1/cluster?action=backup_to_remote`, configured at `/api/v1/cluster/backups/config`. The research did not verify that path. Using the UI's System > Backup & Restore > Backup Now is equivalent and safer.

**Read-only API diagnosis.** In the commands below, `$NSX` is the manager FQDN and the TNC id is `720030c6-23d2-481e-86b9-fabd41a31f38`.

```bash
HTN=hyp02-4163234c-1477-4799-b225-61b39329d18fhost-26
TNC=720030c6-23d2-481e-86b9-fabd41a31f38
EP=/policy/api/v1/infra/sites/default/enforcement-points/default
curl -sku admin "https://$NSX$EP/host-transport-nodes/$HTN/state"            # KB 319975 Opt 4 (expect 404 today)
curl -sku admin "https://$NSX/api/v1/transport-nodes/state" | grep -i -B2 -A8 hyp02   # KB 392879
curl -sku admin "https://$NSX$EP/transport-node-collections/$TNC"            # inferred: read validation errors
curl -sku admin "https://$NSX/api/v1/fabric/discovered-nodes" | grep -i -A5 hyp02    # inferred path
curl -sku admin "https://$NSX/api/v1/fabric/compute-managers"                # KB 447344: duplicate/stale CM?
curl -sku admin "https://$NSX/api/v1/upgrade/upgrade-units?component_type=HOST" # KB 444026: expect 3 units
```

KB 447344 found that stale TNs persist while an old Compute Manager registration remains ([KB 447344](https://knowledge.broadcom.com/external/article/447344/nsx-upgrade-failure-due-to-stale-transpo.html)). Delete a duplicate Compute Manager only if it belongs to a vCenter that no longer exists. Never delete the live one. KB 444026 uses the upgrade-units count to detect a doubled host (in its field case, 17 units for 16 hosts). If you see four HOST units for three hosts, that confirms a ghost exists.

**Corfu read-only check.** This is the decisive test. Run it as root on the manager. Writing the dumps to `/image` follows KB 391840 ([KB 391840](https://knowledge.broadcom.com/external/article/391840)):

```bash
R=/opt/vmware/bin/corfu_tool_runner.py
$R --help                                                   # confirm operations available on 9.1
for T in HostTransportNode TransportNodeState TransportNodeStatus HostModelMsg GenericPolicyRealizedResource; do
  $R --tool corfu-editor -n nsx -o showTable -t $T > /image/$T.txt 2>&1
  echo "== $T"; grep -n -i -E 'hyp02|host-26|4163234c' /image/$T.txt | head -50
done
```

KB 387942 and KB 423818 document `showTable -t HostTransportNode | grep "host-transport-nodes"` as the way to find stale records that exist in the database but not in the UI. Both KBs then refer you to KB 319975 or a support case.

Two caveats on the other tables:
- HostModelMsg and GenericPolicyRealizedResource come from a community blog that the research could only see through search extracts ([techaspire](https://techaspire.com.au/blog-014-git-commit/)).
- The dumps can contain credentials in plain text, so delete them when you are done ([lab2prod](https://www.lab2prod.com.au/2021/11/nsx-t-edge-deletion-failed.html)).

**Branch rule.** The grep result decides the path:
- If HostTransportNode contains a row whose key is the 601 path, note its full key JSON and any `marked_for_delete`-style field. Steps 1 and 2 may still clear it. If they do not, Step 4 has a precise target.
- If no table contains a matching row, the conflict lives somewhere the public tables do not cover. Step 4 then has no target, and the moref-change route in Step 3 becomes the main option.

## Steps 1 and 2: the documented ladder, finished properly

The table ranks every remaining option from least to most invasive.

| Rung | Action | Basis | Risk to hyp02 / cluster | Stop and move on when |
|---|---|---|---|---|
| 1a | `start search resync` policy/manager/telemetry, then all | Documented (KB 319975 Opt 2) | None | 601 is unchanged ≥10 min after resync all |
| 1b | UI CONFIGURE NSX on hyp02 | Documented (KB 422174) | None | Same 601 or option absent |
| 1c | No-op TNP save | Documented (KB 432202), different trigger | Refresh touches all hosts | Same 601 |
| 2 | MM → datacenter → force delete → wait Not Configured → back | Documented (KB 380641, 319975) | Evacuate VMs; vSAN degraded | Delete stalls again, or 601 returns after re-entry |
| 3 | Remove hyp02 from vCenter, re-add (new moref) | Inferred | Same as 2, plus SDDC Mgr/VDS reconcile | New HTN id also collides (name/IP) |
| — | Broadcom support case, scripted DB cleanup | Documented (KB 319975 Opt 5) | None | No entitlement in an unsupported lab |
| 4 | Corfu `deleteRecord` with backup + snapshot | Community + inferred | Single-manager DB integrity | See Step 4 |

### Step 1: three cheap documented nudges

**1a. Search resync.** In the admin CLI, run the commands from KB 319975 Option 2 ([KB 319975](https://knowledge.broadcom.com/external/article/319975)) in this order:
1. `start search resync policy`
2. `start search resync manager`
3. `start search resync telemetry`
4. Wait at least 10 minutes.
5. `start search resync all`

KB 398710 describes resync as what keeps "corfudb and Search indexes … in sync" ([KB 398710](https://knowledge.broadcom.com/external/article/398710)). It rebuilds the search index and does not touch intent records, so expect it to help only if the 601 comes from an index lookup.

**1b. CONFIGURE NSX.** In the NSX UI, go to System > Fabric > Hosts, select hyp02 in the cluster view and click **CONFIGURE NSX**. This is KB 422174's resolution for this exact 601 string ([KB 422174](https://knowledge.broadcom.com/external/article/422174/adding-host-to-nsx-prepared-cluster-thro.html)).

**1c. No-op TNP save.** Edit the Transport Node Profile and save it with no changes. KB 432202 says this "forces a global refresh" of the profile across all associated hosts ([KB 432202](https://knowledge.broadcom.com/external/article/432202/nsx-host-validation-error-9779-transport.html)). Its documented trigger is error 9779, so using it for 601 is an extrapolation. It does touch hyp01 and hyp03, but only to re-apply an unchanged profile.

**Verify after each nudge.** Wait 10 minutes, then run:

```
POST $EP/transport-node-collections/$TNC?action=retry_profile_realization
```

Then re-read the TNC state. Success means the TNC leaves FAILED_TO_REALIZE and the hyp02 HTN appears with SUCCESS.

Skip the following, because they cannot help:
- `realized-entity?action=refresh` refreshes status and statistics and deletes nothing ([NSX 4.2 API RefreshRealizedState](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/4.2/method_RefreshRealizedState.html)).
- Detaching the TNP is impossible under vLCM.
- The TNC's `remove_nsx` action "uninstalls NSX applied to the Transport Node Collection", meaning every host in domain-c9, including hyp01 and hyp03 ([NSX 9.1.0 API RemoveNSXOnTransportNodeCollection](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/9.1.0/method_RemoveNSXOnTransportNodeCollection.html)).

### Step 2: redo the move to datacenter level, and let it finish this time

This is the documented vLCM procedure from KB 380641 and the prerequisite in KB 319975. The difference from last time is that you wait for the removal to reach a terminal state before moving the host back.

**Before you start:**
- Check that hyp01 and hyp03 have enough RAM headroom for sddc-manager, licsrv, opscollector, the VSP nodes and devvm02. Dev VMs can simply be powered off.
- Note that the VSP nodes restart when they move between hosts.
- Enter maintenance mode with **Ensure accessibility**. A three-host ESA cluster cannot do a full data evacuation, and moving the host out of the cluster leaves vSAN degraded but accessible until it rejoins. That is an inference consistent with your earlier move.

**Procedure:**
1. Move hyp02 to the datacenter object. It should now appear under Other Nodes / Standalone.
2. If an entry is visible, use REMOVE NSX with **Force Delete** (KB 420783 and KB 415331; [KB 420783](https://knowledge.broadcom.com/external/article/420783/unable-to-install-the-add-the-host-back.html), [KB 415331](https://knowledge.broadcom.com/external/article/415331/transport-node-removed-from-clusters-but.html)).
3. With the host now outside the TNP cluster, the 9411 refusal no longer applies. Issue the documented force deletes against both planes even if their GETs return 404. These are KB 319975 Options 3 and 4:

```bash
curl -sku admin -X DELETE "https://$NSX$EP/host-transport-nodes/$HTN?force=true&unprepare_host=false"
# MP UUID only if Step 0 found one in /api/v1/transport-nodes/state:
curl -sku admin -X DELETE "https://$NSX/api/v1/transport-nodes/<UUID>?force=true&unprepare_host=false"
curl -sku admin -X POST   "https://$NSX/api/v1/transport-nodes?action=clean_stale_entries"   # inferred for hosts
```

4. After each delete, wait 5 minutes and repeat the `/state` GETs until they return "Object Not found".

`clean_stale_entries` is documented only for Edge nodes (KB 345848), and KB 345813 says to prefer a Policy force DELETE on NSX 9.0 and later ([KB 345848](https://knowledge.broadcom.com/external/article/345848/cleanup-stale-edge-nodes-present-in-the.html); [KB 345813](https://knowledge.broadcom.com/external/article/345813/troubleshooting-edge-deletion-failures-i.html)). Running it against a host is harmless but unproven.

**If removal stalls again.** Known stall points are 8% with MP9538, fixed in 9.1+ ([KB 445611](https://knowledge.broadcom.com/external/article/445611/force-deleting-orphaned-nsx-transport-no.html)), and 20% with `waiting_for_host_config_reply` ([KB 423818](https://knowledge.broadcom.com/external/article/423818/how-to-remove-a-stale-host-entry.html)). If it sticks at 40% again, leave the host at datacenter level and:
1. Run a manager reboot and `start search resync all` **while the host is out of the cluster**. KB 423818 says orphans "may appear under the Standalone Node section" after a reboot, and can then be deleted. Last time the reboot happened after the host was already back in the cluster.
2. Delete the orphan if it reappears.

**Verify before moving the host back:**
- hyp02 shows as Not Configured under Other Nodes.
- Both `/state` GETs return "Object Not found".
- The upgrade-units count is two.
- The Step 0 Corfu grep, re-run, no longer shows the row.

Then return hyp02 to domain-c9. KB 319975 says a TNP-applied cluster prepares it automatically. Exit maintenance mode only after the TNC reports success and `esxcli vsan cluster get` shows hyp02 as a member again.

**Stop criterion:** the 601 returns after a clean re-entry, or the Corfu row survives a completed force delete. Go to Step 3 or Step 4.

## Step 3: give hyp02 a new moref by re-adding it to vCenter

The 601 path itself suggests the HTN id is built from the display name, the vCenter UUID and the moref. NSX's API reference confirms that the discovered-node id for vCenter-managed hosts is `<vc-uuid>:host-NN` ([HostTransportNodeListParameters](https://developer.broadcom.com/xapis/nsx-t-data-center-rest-api/4.2/types_HostTransportNodeListParameters.html)). KB 448462 confirms that removing and re-adding a host assigns a new HostSystem moref ([KB 448462](https://knowledge.broadcom.com/external/article/448462/sddc-manager-cluster-expansion-fails-wi.html)).

A re-added hyp02 would therefore try to create `hyp02-4163234c-…host-NN`, which should not collide with the ghost's key. **No source documents this as a fix.** It is also possible that NSX's name or IP uniqueness checks still trip on the ghost: KB 380641 and KB 319975 both describe stale records that block by same name and IP.

**Procedure:**
1. Put the host in maintenance mode, exactly as in Step 2.
2. Remove hyp02 from the vDS, or let vCenter do it.
3. Remove hyp02 from the vCenter inventory.
4. Re-add it at datacenter level.
5. Re-add it to the vDS with vmnic0 and vmnic1 mapped exactly as on hyp01 and hyp03. The TNP's vDS host switch requires vDS membership (KB 406955; [KB 406955](https://knowledge.broadcom.com/external/article/406955)).
6. Move it into domain-c9.
7. Reconcile SDDC Manager, which stores the moref as `host.source_id`. Use the documented 9.1 inventory sync from KB 448462. Do not edit the SDDC Manager database.

```bash
psql -h localhost -U postgres -d platform -c "SELECT hostname, source_id FROM host WHERE hostname='hyp02.<domain>';"
GET  https://<sddc-manager>/v1/domains
POST https://<sddc-manager>/v1/domains/<domainId>/synchronizations
```

**On vSAN:** the cluster UUID lives on the host, and your earlier rebuild note says "ESA pool re-mounts itself". A remove and re-add of the inventory object should therefore rejoin the host without touching its disks. This is not confirmed for ESA, and KB 436377 warns that wiping ESA disks destroys data ([KB 436377](https://knowledge.broadcom.com/external/article/436377/deploy-a-new-vsan-esa-cluster-from-an-ex.html)). Never claim or reformat the disks during the re-add.

**Why not the SDDC Manager route:** the full decommission, recommission and expand cycle is heavier, and no source says it clears NSX stale state. Its own procedure requires keeping enough vSAN hosts during removal, which a three-host cluster cannot do comfortably ([VCF 5.1 Remove a Host](https://docs.vmware.com/en/VMware-Cloud-Foundation/5.1/vcf-admin/GUID-41BE1B30-8D52-43BF-8297-0059B92D723D.html)).

**Stop criterion:** the new HTN fails with a name or IP "already exists" error. That means the ghost is still the blocker and only Step 4 removes it. Note that if the HTN id is the only collision, Step 3 sidesteps the ghost without deleting it. The record stays behind and can resurface, for example as an extra upgrade unit that blocks an NSX upgrade later (KB 444026).

## Step 4: delete the Corfu row, with a rollback ready

Broadcom never publishes a host-TN `deleteRecord`. KB 319975 Option 5 and KBs 423818 and 391840 send database deletion to support. The procedure below is a synthesis of:
- a Broadcom upgrade-guide `deleteRecord` on a system table ([NSX 4.2 upgrade guide](https://techdocs.broadcom.com/us/en/vmware-cis/nsx/vmware-nsx/4-2/upgrade-guide/upgrading-nsxt/upgrading-management-plane/upgrade-management-plane-from-nsx-3-2-1-x-and-later.html));
- an NSX 9.0.2 practitioner `deleteRecord` using a JSON `stringId` key ([vstellar](https://vstellar.com/2026/04/lessons-from-the-field-recovering-from-orphaned-avi-controllers-in-vcf-9/));
- an NSX 4.x blog that deleted the HostTransportNode, HostModelMsg and GenericPolicyRealizedResource rows for a host that would not become a TN (search extracts only, so the key strings are unverified; [techaspire](https://techaspire.com.au/blog-014-git-commit/)).

The vstellar author warns that such edits "can be catastrophic and can lead to inconsistent states and outages".

**Preconditions:**
- Step 0 found an exact row, and you have copied its key JSON verbatim from the dump.
- A fresh NSX backup has succeeded.
- A powered-off NSX Manager snapshot exists.
- No other NSX configuration changes are pending.
- hyp02 is in maintenance mode.

**Procedure:**

```bash
R=/opt/vmware/bin/corfu_tool_runner.py
$R --tool corfu-editor -n nsx -o showTable -t HostTransportNode > /image/HTN.before.txt
# delete ONLY the Policy HTN row the 601 names; paste the key exactly as printed in the dump:
$R --tool corfu-editor -n nsx -o deleteRecord -t HostTransportNode \
   --keyToDelete='{"stringId": "/infra/sites/default/enforcement-points/default/host-transport-nodes/hyp02-4163234c-1477-4799-b225-61b39329d18fhost-26"}'
# expect exactly: "1 records deleted successfully" — anything else means nothing was deleted
```

The key shown is the expected form. The dump is authoritative, so use the key exactly as it appears there. The sources disagree on whether `corfu-editor` or `corfu-browser` performs deletes, so use whichever `--help` lists for `deleteRecord`, and treat both as able to write.

**After the delete:**
1. Reboot the manager. The official example restarts corfu-server afterwards, and on a single node a reboot is the cleanest equivalent. No source says whether to stop proton or policy first.
2. Run `start search resync all`.
3. Re-dump the table to confirm the row is gone.
4. Run `retry_profile_realization` on the TNC.
5. Delete the matching TransportNodeState, HostModelMsg or GenericPolicyRealizedResource rows only if a **new** error names them. Delete one table at a time, re-testing after each.

**Health checks** (from [KB 378470](https://knowledge.broadcom.com/external/article/378470)):
- `service corfu-server status`
- `/var/log/corfu/corfu.9000.log`
- `get cluster status`
- `get filesystem-stats`, watching `/config` usage
- Your hyp01 and hyp03 TNs still report SUCCESS.

**Rollback:** power off the manager and revert to the snapshot. Reverting discards every NSX change made since the snapshot, which is why the precondition says no other changes should be pending. Keep the snapshot until hyp02 has been stable for a day, then delete it.

Restoring the NSX backup is the second line of defence, not the first. On a single node it means deploying a fresh manager and restoring into it, a much larger operation.

**Stop criterion:** the 601 is replaced by a different error that no Corfu row explains, or any health check degrades. In that case, revert the snapshot rather than deleting more rows.

## Conclusion

The cold restart is the turning point in this case. It moved the ghost from "visible but undeletable" to "invisible but still blocking", and that is exactly where Broadcom's public procedures end and support-only database work begins. The best remaining non-database option is to run the KB 380641 move-to-datacenter procedure again, this time with the manager reboot done *while hyp02 is outside the cluster*. That targets the interrupted 40% delete, which is the most plausible origin of the stuck record.

The moref change in Step 3 is attractive because it never touches NSX internals. It works around the ghost without removing it, so a leftover row may still cause trouble later. The Corfu edit is the only step that actually removes the record. On a single manager, it is only reasonable because a powered-off snapshot turns a potentially catastrophic database mistake into a revert you can undo. Whichever rung succeeds, re-run the Step 0 diagnosis afterwards and expect three HOST upgrade units, so the ghost does not resurface at the next NSX upgrade.
