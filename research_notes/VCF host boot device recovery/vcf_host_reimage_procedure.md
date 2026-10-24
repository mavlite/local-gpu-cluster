# VCF 9.x: recovering a managed ESXi host after boot device / OSData failure

Scope note: ~17 tool calls. techdocs.broadcom.com pages were NOT retrievable via search, so the official 9.x "Decommission/Commission/Remove host" docs were not read directly. The 9.x procedure is inferred from 9.x-dated KBs that point at it. Items marked (5.x) or (3.x/4.x) are older material.

## 1. What does Broadcom document: decommission + reimage + commission, vs in-place reinstall and reconnect?

### Takeaway
The only Broadcom-documented path is: remove host from cluster, decommission in SDDC Manager, reimage, commission, add back to the cluster. I found no Broadcom document that supports reinstalling ESXi in place under the same FQDN and reconnecting it. Several KBs say the in-place route (rebuild and re-add to vCenter without decommission/commission) leaves the SDDC Manager records and the svc account out of sync.

### Cited Findings
- KB 411583 "Rebuild a failed ESX host in VCF": decommission the failed host, reinstall the matching ESX build and configure it, commission it back. Its links point to VCF 5.2 docs; no detailed steps and it does not define "matching build". Written for a TPM/motherboard-replacement case. It also says to back up the TPM recovery key (KB 323401) before hardware changes. (5.2-era docs) — [KB 411583](https://knowledge.broadcom.com/external/article/411583/rebuild-a-failed-esx-host-in-vcf.html)
- KB 374333 "Migrating ESXi Boot Disk in VCF SDDC/vSAN Environment" is the closest match to this scenario (boot media replaced, vSAN host). Steps: in SDDC Manager remove the host (this auto-evacuates vSAN data and removes it from the vSAN cluster); decommission; optionally remove the vSAN disk group, or leave the vSAN disks untouched (called the conservative choice); rebuild with a VCF-compatible ESXi; commission; add the host back to its original cluster. States: "The vSAN disk groups will be automatically recreated during the SDDC host add automation." Verify in vSphere Client, vSAN, and cluster health. Links to VCF 5.1/5.2 guides; no ESXi build named (5.x-era) — [KB 374333](https://knowledge.broadcom.com/external/article/374333)
- KB 414650 (applies 5.x and 9.x): a host cannot be moved between domains outside the workflow; it must be decommissioned, re-imaged, then commissioned. SDDC Manager UI: Inventory > Hosts > Unassigned Hosts > decommission; then Inventory > Hosts > Commission Hosts (accept checklist, add host, verify server fingerprint, validate, commission). Rationale: NSX, vSAN and vSphere profiles are tied to the host's cluster context. Says nothing about vSAN disk cleanup — [KB 414650](https://knowledge.broadcom.com/external/article/414650)
- KB 432436 (VCF 5.x): if a host's SSL certs changed after commissioning, SDDC Manager holds a stale thumbprint and "Add host to Datacenter" fails with SSLVerifyFault "Authenticity of the host's SSL certificate is not verified" (domainmanager.log). Resolution is the full cycle: remove from cluster, decommission, re-image, commission, add. No in-place thumbprint refresh offered — [KB 432436](https://knowledge.broadcom.com/external/article/432436/add-esxi-host-to-cluster-fails-authent.html)
- KB 427267 (VCF 9.1.x): hosts added directly to vCenter (outside the commission workflow) can be missing from SDDC Manager LCM pages and VCF Operations. Fix tries inventory sync first (VCF Operations: Infrastructure Operations > Configurations > Inventory Management > manual sync), then as the disruptive last resort: remove from vCenter, reinstall ESXi, commission via the VCF workflow — [KB 427267](https://knowledge.broadcom.com/external/article/427267/esxi-hosts-show-in-vcenter-inventory-but.html)
- KB 314643 (4.x, 5.x, 9.x): cause is stated as a host "rebuilt or reconfigured and re-added to vCenter without a decommission/commission from SDDC Manager". Symptoms are the svc-vcf account missing on the host, disconnected in SDDC Manager, failed upgrade prechecks, and config drift upgrade failing at vSAN LSOM update stage. This is Broadcom explicitly describing the in-place rebuild as a source of drift — [KB 314643](https://knowledge.broadcom.com/external/article/314643)
- William Lam (Jun 2026): for native VCF domains, Host Decommission/Commission workflows exist. For imported domains, adding or removing hosts in an existing cluster is NOT supported via VCF workflows and is done in vCenter. The lab management domain was deployed by the installer, so it is presumably native (unverified for this lab) — [Lam](https://williamlam.com/2026/06/what-host-lifecycle-operations-are-available-after-importing-vcenter-into-vcf-9-x-fleet.html)
- KB 438359 / 444026 warn that deleting a dead host from vCenter directly (outside VCF workflows) causes later trouble, including a stale NSX host entry that blocked an NSX upgrade in VCF 9.1 (fix: delete the host from NSX Manager) — [KB 438359](https://knowledge.broadcom.com/external/article/438359/inventory-sync-fails-when-a-decommission.html), [KB 444026](https://knowledge.broadcom.com/external/article/444026/deleted-esx-host-from-vcenter-blocks-the.html). I only saw search snippets of these two.
- Community thread (old, unanswered): a 4-node management domain where a host with a corrupt OS could not be decommissioned through SDDC Manager — [thread](https://communities.vmware.com/thread/611802). Only seen as a search result; contents not read.

### Inferences
- For this lab the nearest documented route is KB 374333 plus KB 411583. A dead-OSData host may fail the SDDC Manager remove step because it cannot be contacted (see Gaps).
- An in-place reinstall is unsupported rather than forbidden. It is workable only if every record listed in section 3 is reconciled by hand.

### Gaps
- Official VCF 9.0/9.1 TechDocs pages ("Decommission ESX Hosts", "Commission Hosts", "Remove a host from a cluster") were not fetched. Exact 9.x UI labels are unverified.
- No Broadcom doc found for decommissioning a host whose OS is unbootable. The API force-decommission path was not found.
- Nothing found on vSAN ESA disks that still hold the cluster's data when the host is reinstalled (William Lam's 2014 post, 5.5-era, says reinstalling generally leaves vSAN partitions alone and `esxcli vsan cluster join -u <uuid>` rejoins; not authoritative for ESA/VCF).

## 2. Minimum host counts and alternatives when removal is blocked

### Takeaway
VCF's design rule requires 4 hosts in a management domain, so the lab's 3-host domain is already below the documented minimum. Older SDDC Manager docs offer "Force Remove" when removal drops below the minimum. I could not confirm that this exists in 9.x.

### Cited Findings
- Broadcom KB 450622: minimum 4 ESXi hosts for the management domain in a single AZ; 8 for stretched (4 per site) — [KB 450622](https://knowledge.broadcom.com/external/article/450622/multisite-esxi-host-requirements.html) (snippet only)
- defaultreasoning.com (community blog, Jan 2025): the 4-host minimum comes from design requirement VCF-VSAN-REQD-CFG-002; 3 hosts is a VCF rule, not a vSAN limit; the single-host bring-up trick is lab-only and unsupported for production — [post](https://defaultreasoning.com/2025/01/23/why-management-domain-needs-4-hosts-vcf/)
- VCF 3.10/3.11/4.5 admin guide (3.x/4.x): if removal leaves fewer hosts than the minimum, click Force Remove. Warns that too few remaining hosts for the vSAN availability setting can make the datastore read-only or cause data loss. Also: cannot remove a host that hosts an NSX Edge node; after decommission, the host must be re-imaged and commissioned before reuse — [4.5 doc](https://docs.vmware.com/en/VMware-Cloud-Foundation/4.5/vcf-admin/GUID-41BE1B30-8D52-43BF-8297-0059B92D723D.html)
- VCF 5.2 admin guide, as summarised by search (not fetched): at least four hosts in a management domain, three in a VI domain; a failed host is "forcibly removed" if assigned to a workload domain — [5.2 doc](https://docs.vmware.com/en/VMware-Cloud-Foundation/5.2/vcf-admin/GUID-B355149E-BDCF-4794-9F09-3449712375EE.html)

### Inferences
- With 3 hosts and vSAN ESA, removing hyp02 leaves 2. FTT=1 RAID-5 (needs 3+ hosts) would not have a compliant layout, and a 2-host cluster cannot rebuild a failed host's data. The removal step is risky. The 3.x-era warning about read-only datastores applies.
- Alternatives (all unverified against Broadcom 9.x docs): (a) in-place reinstall and manually reconcile SDDC Manager/vCenter; (b) add a temporary 4th host, then remove/decommission hyp02; (c) vCenter-only host removal, which Broadcom discourages (KB 438359, 444026).

### Gaps
- Whether the VCF 9.x SDDC Manager UI or API still provides Force Remove for an under-minimum cluster is unverified.
- Whether a policy of FTT=0 or "force provisioning" is needed for a 2-host ESA cluster was not researched.

## 3. SDDC Manager records that go stale after an in-place reinstall (same FQDN/IP)

### Takeaway
Stale items found in Broadcom sources: host SSL thumbprint/certificate, the svc-vcf service account on the host (wiped by a reinstall), and the root and service credentials in Password Manager. I found nothing documenting an SSH host-key fingerprint record or a dedicated thumbprint-refresh API; that part is unverified.

### Cited Findings
- Thumbprint: SDDC Manager keeps the host's SSL thumbprint from commissioning. A changed cert (a reinstall regenerates `/etc/vmware/ssl/rui.crt`) breaks add-host with SSLVerifyFault (VCF 5.x) — [KB 432436](https://knowledge.broadcom.com/external/article/432436/add-esxi-host-to-cluster-fails-authent.html)
- Commissioning requires the cert subject alternative name to match the FQDN. Fix: `/sbin/generate-certificates` then reboot — [KB 327187](https://knowledge.broadcom.com/external/article/327187/unable-to-commission-hosts-due-to-certif.html). Custom host certificates can also block commissioning (regenerate a self-signed one from the FQDN) — [KB 438351](https://knowledge.broadcom.com/external/article/438351/failure-commissioning-host-with-a-custom.html). Snippets only.
- A thumbprint mismatch at VCF 9.x host validation can come from DNS (port 53) being blocked, so the FQDN fails to resolve. This is for the installer, but worth checking after reinstall — [KB 441245](https://knowledge.broadcom.com/external/article/441245/vcf-9x-host-validation-fails-with-ssl-th.html)
- Service account svc-vcf-<shortname>: a fresh install does not have it. SDDC Manager shows it as disconnected. Recreate: `esxcli system account add --id svc-vcf-<short> --password '<pw>' ...` then `esxcli system permission set --id svc-vcf-<short> --role Admin`; use the password retrieved from SDDC Manager Password Manager (KB 327195); then Administration > Password Manager > remediate the svc account with the same password; then check the host root account and remediate it if disconnected — [KB 314643](https://knowledge.broadcom.com/external/article/314643)
- Root password: the reinstall root password must equal what SDDC Manager stores, or be updated via Password Manager (remediate = set SDDC Manager to a password already set on the host). KB 449686 covers a root password changed out of band; the fix involved editing the SDDC Manager database and clearing `opasswd` on the host, so involve Broadcom support (VCF 9.x; only seen in a snippet) — [KB 449686](https://knowledge.broadcom.com/external/article/449686/esxi-password-remediation-fails-in-vcf-o.html). KB 401305 covers svc-vcf-esxi account realignment — [KB 401305](https://knowledge.broadcom.com/external/article/401305/rootservice-account-password-remediate-o.html). Lockdown mode on the host blocks password validation ("Password validation cannot be performed when host ... is in lockdown mode"): disable lockdown — [KB 426417](https://knowledge.broadcom.com/external/article/426417/synchronize-domain-inventory-failed-on.html)
- Password terminology in VCF 9 Fleet Management: Update = sync a password changed outside VCF; Remediate = fix credential mismatch/communication failure. KB describes management components, not ESX hosts directly — [KB 430609](https://knowledge.broadcom.com/external/article/430609)
- Stale svc accounts after an incomplete decommission break the next commission at "Create New Local ESX Host User" (VCF 9.x). Fix: `esxcli system account remove -i svc-####`, rerun — [KB 451550](https://knowledge.broadcom.com/external/article/451550/host-commissioning-fails-due-to-stale-es.html). Relevant if the full-cycle route is chosen without wiping the OS.
- vSphere Configuration Profiles (VCP) enabled clusters in VCF 9.0.x delete the svc-vcf account on the reboot that applies the profile. Resolved in 9.1; earlier releases use KB 314643 — [KB 446151](https://knowledge.broadcom.com/external/article/446151/esx-host-accounts-show-disconnected-afte.html)
- Inventory: after changes outside the workflow, run SDDC Manager inventory sync (built into SDDC Manager since 9.0; also VCF Operations > domain Actions > Sync Inventory) — [Lam](https://williamlam.com/2026/06/what-host-lifecycle-operations-are-available-after-importing-vcenter-into-vcf-9-x-fleet.html), [KB 438359](https://knowledge.broadcom.com/external/article/438359/inventory-sync-fails-when-a-decommission.html)
- Certificates issued by VCF to the host (VCF Operations/SDDC Manager certificate management): not found in a source. After a reinstall, the host has a fresh self-signed cert. If the lab had replaced host certs via VCF, they must be reissued (unverified).

### Inferences
- Likely stale set after a same-FQDN reinstall: SDDC Manager thumbprint, svc-vcf account (missing on host), root password (if different), vCenter's stored host thumbprint, NSX transport node state, vSAN membership metadata, and any VCF-issued host certificate.
- The SSH host-key fingerprint matters mainly for the admin workstation's known_hosts and for any tool that pins SSH keys. I did not find SDDC Manager storing one; unverified.

### Gaps
- Specific SDDC Manager 9.x API for refreshing a host's stored thumbprint (`/v1/hosts`, credentials `PATCH /v1/credentials` for remediate/update) not verified. The VCF API reference was not fetched.
- KBs 327187, 438351, 441245, 449686, 401305, 426417, 430609 were read via search summary only.

## 4. vCenter, VDS, NSX, vLCM after reinstall

### Takeaway
Little 9.x-specific guidance was found. The one directly relevant item is vCenter 9.0 thumbprint desynchronisation and its workaround.

### Cited Findings
- KB 405750 (vCenter 9.0 / VCF 9.x): vpxd crashes and hosts fail to reconnect, log message "sslThumbprint and sslCertificate parameters are both set, but the thumbprint does not match the certificate". Broadcom has no code fix yet (watch KB 275360). Workaround: stop vpxd, back up vpxd.cfg, set the mobility health timer interval to -1, start vpxd, disconnect and reconnect all hosts so certs refresh (KB 321930), fix individual Not Responding hosts in the VCDB by MoRef via psql, restore vpxd.cfg, restart vpxd — [KB 405750](https://knowledge.broadcom.com/external/article/405750/error-the-sslthumbprint-and-sslcertifica.html)
- Stale NSX host entry after deleting a host from vCenter blocked a VCF 9.1 NSX upgrade; delete it from NSX Manager and retry — [KB 444026](https://knowledge.broadcom.com/external/article/444026/deleted-esx-host-from-vcenter-blocks-the.html) (snippet only)
- KB 436452: SDDC Manager NSX upgrade precheck fails with "hosts in this NSX that are not known to SDDC Manager" — [KB 436452](https://knowledge.broadcom.com/external/article/436452/sddc-manager-nsx-upgrade-precheck-fails.html) (snippet only; plausible in this scenario since NSX is deployed)
- KB 427267 (9.1.x): after repair, a missing host in VCF Operations may need licence check and inventory sync — [KB 427267](https://knowledge.broadcom.com/external/article/427267/esxi-hosts-show-in-vcenter-inventory-but.html)
- KB 314643 symptom: config-drift upgrade fails at vSAN LSOM update stage on a rebuilt host with a missing svc account.

### Inferences
- A reinstalled host reconnecting to vCenter under the same name will require either "Reconnect host" with a new thumbprint accepted or remove-and-re-add. The VDS host membership and the VMkernel/uplink configuration live in the OSData that was lost, so the host must be reattached to the VDS and its vmks recreated or restored through host profile/VCP.
- NSX is deployed but unused. The host's transport-node entry may remain stale; check NSX Manager.
- vLCM image compliance should be rechecked after rebuild. The host must run the cluster image's ESX build (9.1.1 build 25714478) and vendor components, or it shows non-compliant. Not sourced.

### Gaps
- No source found for VDS rejoin after OSData loss, NSX transport-node recovery, or vLCM remediation for a rebuilt host in 9.x. This is inferred from general vSphere behaviour.
- Whether the vCenter "Reconnect host" dialog accepts a new thumbprint was not checked against 9.x documentation.

## 5. Preserving the local VMFS datastore on the boot NVMe, and community write-ups

### Takeaway
William Lam's August 2026 post describes how to wipe partitions for reinstall but not how to preserve VMFS. No source on this lab's preserve-VMFS question was found. The installer's preserve-VMFS option is not confirmed here from a source.

### Cited Findings
- William Lam "quick tip: the fastest way to clear partitions for ESX reinstallation" (Aug 2026): wipe NVMe tiering partition (9.1: `esxcli memtier config delete`; 9.0: `esxcli system tierdevice`), vSAN ESA disk (`vdq -q`, `esxcli vsan storagepool remove -d`), and the boot device partitions 1, 5, 6, 8 via `partedutil delete` as the minimum to reinstall. A commenter reports `clearpart --overwritevmfs --drives=<dev>` in kickstart works on 9.1. Does not cover preserving VMFS — [post](https://williamlam.com/2026/08/quick-tip-the-fastest-way-to-clear-partitions-for-esx-reinstallation.html)
- William Lam 2014 (5.5-era background only): reinstalling ESXi generally leaves vSAN partitions in place and the host can rejoin with `esxcli vsan cluster join -u <UUID>`; vSAN waits ~30 min before rebuilding data after a host returns — [post](https://williamlam.com/2014/07/does-reinstalling-esxi-with-an-existing-vsan-datastore-wipe-your-data.html)
- Lam VCF 9.x identity-broker/vCenter reset posts exist but are not about host reimaging: [Jan 2026](https://williamlam.com/2026/01/quick-tip-reset-vcenter-server-from-previously-managed-vcf-operations-for-vcf-single-sign-on-sso.html).
- KB 374333 says to leave vSAN disks untouched is the conservative option and disk groups auto-recreate on host add. This was for an iSCSI boot LUN, not a boot NVMe with a shared VMFS.

### Inferences
- The memory-tiering device and the vSAN ESA disk are on separate devices from the boot NVMe on the lab hosts (the boot NVMe carries OS plus VMFS, the 4 TB NVMe is vSAN). So the installer should not need to touch the vSAN disk. Whether the lab's tiering partition lives on the boot NVMe is unverified.
- Avoid the Lam `partedutil` partitions 1, 5, 6, 8 only on the boot device; any VMFS partition (typically partition 3 or later) is not in his list, which suggests it survives, but this is unverified.

### Gaps
- Installer "preserve VMFS" behaviour in ESX 9.1.1 and whether it works when OSData sits on the same disk as VMFS: not verified from any source; test or read the ESX 9.1 installation guide.
- No Broadcom or Lam article found specifically on in-place reimaging of a VCF host.
