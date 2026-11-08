# VCF 9.x Management Appliance Downsizing (excluding VCF Services Platform)

Scope: VCF 9.0-9.1.x. Lab baseline being evaluated: vcsa 4/21, sddc-manager 4/16, nsxa 6/24 (single node), ops 2/8, opscollector 4/16, licsrv 2/4 (vCPU / GB). Research ran 2026-10-25, with about 20 searches and fetches. Labels used below:
- **[PROVEN]** someone reports running it.
- **[DOC]** an official Broadcom size or procedure.
- **[THEORY]** inferred, not reported.
- **[5.x/8.x]** only known for older releases.

## vCenter 9.x: tiny size, sub-tiny, resizing

### Takeaway
vCenter 9.1 still defines Tiny at 2 vCPU / 14 GB (up to 10 hosts / 100 VMs). William Lam's VCF 9.0 installer JSON uses `vcenterSpec.vmSize: "tiny"`, so a fresh install can be Tiny. Broadcom says shrinking an existing vCenter is not supported: resizing is one-way, upward only. Going to Tiny now means a manual VM edit (unsupported) or a redeploy. I found no VCF 9-era report of a vCenter running below Tiny. The only "8 GB vCenter" tip is from a VCF 5.x guide.

### Cited Findings
- [DOC] vCenter 9.1 hardware requirements:

  | Size | vCPU | Memory | Limit |
  |---|---|---|---|
  | Tiny | 2 | 14 GB | 10 hosts / 100 VMs |
  | Small | 4 | 21 GB | 100 hosts / 1,000 VMs |
  | Medium | 8 | 30 GB | |
  | Large | 16 | 39 GB | |
  | X-Large | 24 | 58 GB | |

  — [Broadcom TechDocs vSphere 9.1](https://techdocs.broadcom.com/us/en/vmware-cis/vsphere/vsphere/9-1/vcenter-installation-and-setup/deploying-the-vcenter-server-appliance/vcenter-server-appliance-requirements/vcenter-server-appliance-hardware-requirements.html)
- [DOC] The VCF 9.1 resize blog lists `deployment_size` values `small`, `medium`, `large`, `xlarge`, each with `-lstorage` and `-xlstorage` variants. It lists no tiny value. — [VMware VCF blog, "Resizing VMware vCenter in VCF 9" (2026-05-12)](https://blogs.vmware.com/cloud-foundation/2026/05/12/resizing-vmware-vcenter/)
- [DOC] The same blog gives four resize methods:
  - the VCF 9.1 API, `PATCH deployment/size`, polled with `deployment/size/status`. When the state reaches `PENDING_SERVICE_RESTART`, the services are restarted.
  - a Reduced Downtime Upgrade.
  - backup and restore into a larger appliance.
  - a manual CPU/RAM edit with the VM powered off.

  It states: "Downsizing is not supported ... You cannot reduce vCPU, memory, or disk size." The API resize is also not supported while vCenter runs on snapshots. — [VMware VCF blog](https://blogs.vmware.com/cloud-foundation/2026/05/12/resizing-vmware-vcenter/)
- [PROVEN, 9.0 installer] Lam's VCF 9.0 deployment JSON uses `vcenterSpec.vmSize: "tiny"` with `vcenterSpec.storageSize: ""`. — [williamlam.com Ultimate Lab Resource for VCF 9.0](https://williamlam.com/2025/06/ultimate-lab-resource-for-vcf-9-0.html). A community VCF 9.0 installer walkthrough sets the same `vmSize: "tiny"`. — [vxworld.co.uk](https://vxworld.co.uk/2025/06/29/vmware-cloud-foundation-9-vcf-installer-walkthrough/)
- Conflict: the minimal-resources table in Lam's own post lists vCenter at 4 vCPU / 21 GB, which is Small. That table does not match his tiny JSON. — [williamlam.com minimal resources VCF 9.0](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html)
- The Leaha VCF 9.1 installer size tables show vCenter at 4/21 even in "Small Non-HA". — [Leaha VCF 9.1.x guide](https://blog.leaha.co.uk/2026/05/06/vcf-9-1-x-ultimate-deployment-guide/)
- Driftar (unofficial) says the VCF installer's vCenter storage choices differ from the manual-deploy defaults: Default 694 GB, Large 2,044 GB, XL 4,304 GB. — [driftar.ch VCF 9 sizing (2026-02)](https://www.driftar.ch/2026/02/20/vcf-9-appliance-sizing-information/)
- [5.x/8.x] A StarWind guide built on VCF 5.1 says a Tiny vCenter "actually consumes 6 GB out of 12 GB" and that a lab can "reduce vCenter RAM to 8 GB (although this is below the minimum requirements)". It is explicitly unsupported. — [StarWind (2025-04, VCF 5.1)](https://www.starwindsoftware.com/blog/creating-a-virtual-test-lab-for-vmware-cloud-foundation-vcf-on-a-single-server/)

### Inferences
- [THEORY] The lab's vcsa shows 4.2 GB active right after startup. The 2 vCPU / 14 GB Tiny shape is the documented floor and fits a 3-host lab, but it is reachable only by an unsupported power-off VM edit, because Broadcom says downsizing is unsupported. Reported risks:
  - A later in-place resize API call or upgrade precheck may see a non-standard size.
  - The vCenter RDU or upgrade path may assume the recorded `DeploymentSize`.
- [THEORY] Going below 14 GB, such as 10-12 GB, is untested on 9.x. vCenter 9 runs more services than 8.x, so the 5.x "8 GB" tip should not be relied on.

### Gaps
- I found no VCF 9 source on disabling vCenter services to save RAM, such as stopping vsphere-ui extras, vAPI endpoints or vLCM-related services. No such trick is documented for 9.x.
- No source says what an SDDC Manager or LCM vCenter upgrade precheck does when the VM is edited below its recorded deployment size.
- I could not confirm whether the 9.1 installer UI offers Tiny, or whether only the JSON accepts it.

## NSX Manager 9.x: form factors, single-node shrink, memory floor

### Takeaway
NSX defines these form factors:
- Extra Small (2/8): Cloud Service Manager only, never NSX Manager.
- Small (4/16): lab/PoC only.
- Medium (6/24): the VCF default.
- Large (12/48) and XL (24/96).

The VCF installer and the SDDC Manager API accept only medium, large and xlarge. Broadcom KB 403604 documents changing Small, Medium and Large by shutting the VM down, editing CPU/RAM to a standard form factor, and powering it on. SDDC Manager needs no update afterwards. The KB describes this for a 3-node cluster, one node at a time, and does not say whether downsizing is supported. Small 4/16 is therefore the "standard" floor to aim for. I found no VCF 9 report of NSX running at 4/16, and nothing from Lam on shrinking NSX in VCF 9.

### Cited Findings
- [DOC, NSX 4.x] Form factors:
  - Extra Small 2 vCPU / 8 GB: Cloud Service Manager only, "not supported for NSX Manager".
  - Small 4 / 16 GB: lab and proof-of-concept only.
  - Medium 6 / 24 GB: default, up to 128 hypervisors.

  — [VMware NSX 4.1 docs](https://docs.vmware.com/en/VMware-NSX/4.1/installation/GUID-AECA2EE0-90FC-48C4-8EDB-66517ACFE415.html)
- [DOC] The SDDC Manager API `SddcNsxtSpec.nsxtManagerSize` accepts only `medium`, `large` and `xlarge`, and defaults to medium. — [VMware developer SddcNsxtSpec](https://developer.vmware.com/apis/vcf/latest/data-structures/SddcNsxtSpec). Lam's 9.0 JSON uses `nsxtSpec.nsxtManagerSize: "medium"`. — [williamlam.com Ultimate Lab Resource](https://williamlam.com/2025/06/ultimate-lab-resource-for-vcf-9-0.html)
- Driftar agrees that the installer offers only Medium, Large and Extra Large. — [driftar.ch](https://www.driftar.ch/2026/02/20/vcf-9-appliance-sizing-information/)
- [DOC] Broadcom KB 403604, "Changing the form factor of NSX Manager in a VCF environment":
  - Small, Medium and Large changes do not require redeploying.
  - Per node: confirm the cluster is stable, shut the VM down in vSphere, edit CPU and memory, power it on, then wait until the cluster is Stable again.
  - "The configuration must match a supported standard form factor."
  - The change "cannot be performed from SDDC Manager", and SDDC Manager needs no update for Small, Medium or Large.
  - Small is lab/PoC only.
  - XL needs a redeploy, and SDDC Manager then has to re-trust the new certificates.

  The KB does not discuss downsizing or a single-node cluster. — [Broadcom KB 403604](https://knowledge.broadcom.com/external/article/403604/changing-the-form-factor-of-nsx-manager.html)
- [DOC] VCF 9.1 has a "Simple NSX Manager Model": one NSX Manager appliance with an internal VIP and reduced networking scale. — [Broadcom TechDocs VCF 9.1 design](https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-1/design/design-library/nsx-management-and-control-plane-detailed-design/simple-nsx-manager-model.html)
- Sources disagree on whether Simple mode still runs Medium:
  - Leaha says "if you opt for simple, you are stuck with small", but her own Small Non-HA table lists NSX at 6/24/300 GB, which is Medium hardware. She writes: "I cant see a way to resize NSX, however you can possibly shut the managers down and manually resize them" (not verified by her). — [Leaha VCF 9.1.x guide](https://blog.leaha.co.uk/2026/05/06/vcf-9-1-x-ultimate-deployment-guide/)
  - A nested VCF 9.1 write-up says "NSX Manager: Medium is the smallest size that exists" in the VCF flow. — [Lubomir Tobek, Medium, Nested VCF 9.1 Part 1](https://medium.com/@lubomir-tobek/nested-vcf-9-1-lab-part-1-my-overview-prerequisites-21c72da5cf0e)
- Lam's VCF 9.0 resource page has a reader question about shrinking NSX to 4 vCPU. No confirmed answer is shown. — [williamlam.com Ultimate Lab Resource](https://williamlam.com/2025/06/ultimate-lab-resource-for-vcf-9-0.html)
- Single node in a converged or JSON flow: in comments on his 9.1 workarounds post, Lam says to export the deployment JSON and remove the two extra NSX Manager nodes from the array. A commenter speculates that the 3-node requirement may be tied to vSAN. — [williamlam.com VCF 9.1 workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)
- Memory pressure signals: Broadcom KBs on the "Manager memory usage high" and "very high" alarms suggest reviewing the form factor and using `get processes monitor` on the NSX CLI. — [KB 375204](https://knowledge.broadcom.com/external/article/375204/manager-memory-usage-high-alarm.html), [KB 375215](https://knowledge.broadcom.com/external/article/375215/nsx-manager-memory-usage-very-high-alarm.html)
- Reservations: the NSX OVA carries CPU/memory reservations. In VCF 5.x Lab removed them by editing the OVA in the SDDC Manager bundle store before bring-up. — [williamlam.com 2023, VCF 5.x](https://williamlam.com/2023/02/removing-nsx-cpu-memory-reservations-when-deploying-a-vmware-cloud-foundation-vcf-management-or-workload-domain.html)
  - For VCF 9.0 the OVA path is cited as `/nfs/vmware/vcf/nfs-mount/bundle/`. — (snippet of the [williamlam minimal-resources 9.0 post](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html), not re-verified)
  - A homelab write-up removes them after install. — [amayacitta.co.uk VCF9 single host lab](https://amayacitta.co.uk/vcf9-lab/)
  - This lab already has no reservations.
- [5.x/8.x] Past bring-up used the `nsxt.manager.formfactor=small` property. — (search snippet referencing [vstellar.com 2024 one-node mgmt domain, VCF 5.x](https://vstellar.com/2024/10/one-node-management-domain-deployment-in-vmware-cloud-foundation/), not re-verified)
- [5.x/8.x] StarWind suggests NSX Manager "to 12 GB". This is VCF 5.1 and unsupported. — [StarWind](https://www.starwindsoftware.com/blog/creating-a-virtual-test-lab-for-vmware-cloud-foundation-vcf-on-a-single-server/)

### Inferences
- [THEORY] The cleanest unsupported-but-standard path for this lab:
  1. Power off nsxa.
  2. Set it to 4 vCPU / 16 GB, the Small form factor, following the KB 403604 mechanics.
  3. Power it on and wait for cluster status STABLE.

  NSX then reads the Small profile, which is a defined form factor. A non-standard shape like 4/12 is not.
- [THEORY] Small is a tight fit. nsxa shows 13.9 GB active right after startup, and NSX's Corfu/Java services size their heaps per form factor. Expect "memory usage high" alarms. Also expect lower scale maximums: Medium allows 2 compute managers, and Small is presumably 1. A single management vCenter fits that.
- [THEORY] For LCM, the KB says SDDC Manager does not track Small, Medium or Large changes. NSX upgrades are less likely to be blocked by a size precheck, but this is unconfirmed for 9.1.x.

### Gaps
- I found no first-hand VCF 9.x report of NSX Manager at Small (4/16) or below. I found nothing from Lam on resizing NSX in VCF 9.
- Whether NSX upgrade prechecks in 9.1.x flag a Small Manager in a VCF-managed domain is unknown.
- I found no VCF 9 JSON or feature flag that deploys NSX at Small. The API enum excludes it.

## SDDC Manager 9.x: minimum, still needed?

### Takeaway
SDDC Manager has a single size, 4 vCPU / 16 GB, with roughly 931-980 GB provisioned. I found no source, Lam included, reporting it running below 16 GB on 9.x. VCF 9 still uses it: the VCF Installer becomes SDDC Manager, and it is the target for the lab workaround properties and Day-N domain operations.

### Cited Findings
- [DOC/PROVEN] SDDC Manager has one size: 4 vCPU / 16 GB, 931 GB provisioned and about 77 GB used in Lam's lab. — [williamlam.com minimal resources 9.0](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html). Driftar gives 4/16 with 980 GB. — [driftar.ch](https://www.driftar.ch/2026/02/20/vcf-9-appliance-sizing-information/)
- [PROVEN] Lam's 9.0 JSON has no size property in `sddcManagerSpec`. — [williamlam.com Ultimate Lab Resource](https://williamlam.com/2025/06/ultimate-lab-resource-for-vcf-9-0.html)
- [PROVEN] Lab workarounds in 9.1 are applied on the VCF Installer and SDDC Manager, in `/home/vcf/feature.properties` and `/etc/vmware/vcf/domainmanager/application*.properties`. Lam also notes that Day-N domain creation should go through the SDDC Manager API, so SDDC Manager stays in the Day-N path. — [williamlam.com VCF 9.1 workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)
- [5.x/8.x] StarWind says SDDC Manager "usually 4 CPU/16 GB" and gives no reduction. — [StarWind](https://www.starwindsoftware.com/blog/creating-a-virtual-test-lab-for-vmware-cloud-foundation-vcf-on-a-single-server/)

### Inferences
- [THEORY] This lab's SDDC Manager has 2.1 GB active at idle, but SDDC Manager runs Java microservices (domainmanager, operationsmanager, lcm, commonsvcs) plus PostgreSQL. A cut to 8-12 GB will probably work at idle. The risk is OOM during bundle downloads, upgrade prechecks, or domain/cluster workflows. None of this is tested.
- [THEORY] Powering SDDC Manager off between maintenance windows is an alternative to shrinking it. It holds no data-path role. This is an inference, not a documented lab practice.

### Gaps
- I found no 9.x data on SDDC Manager at reduced RAM or vCPU, and no list of what fails.
- I found no source on whether VCF 9.1 health checks or VCF Operations alert when SDDC Manager is powered off.

## VCF Operations 9.x and the Operations collector / cloud proxy

### Takeaway
VCF Operations offers Extra Small at 2 vCPU / 8 GB (max 16 GB). The lab's ops VM is already at that floor, and Broadcom positions it as non-production. Collector/cloud proxy sizes:
- Cloud Proxy Small: 2 vCPU / 8 GB.
- Unified Cloud Proxy (UCP) Small: 4 vCPU / 16 GB, which matches the lab's opscollector.
- The installer and API collector spec offer only `small` and `standard`.

Broadcom VCF 9.1 calls resizing an active cloud proxy "deprecated". The supported way to change size is a new collector group at the new size, moving the adapters over, then deleting the old group. I found no source on removing the collector entirely in a VCF 9 fleet.

### Cited Findings
- [DOC] KB 397782 "VCF Operations 9.0 Sizing Guidelines":

  | Size | vCPU | RAM (default / max) |
  |---|---|---|
  | Extra Small | 2 | 8 / 16 GB |
  | Small | 4 | 16 / 32 GB |
  | Medium | 8 | 32 / 64 GB |
  | Large | 16 | 48 / 96 GB |
  | XL | 24 | 128 / 256 GB |

  - Cloud Proxy: Small 2 vCPU / 8 GB, Standard 4 / 32 GB.
  - UCP: Small 4 / 16 GB, Standard 8 / 48 GB.
  - CP Small and UCP Small handle up to 16,000 objects and 25 vCenter adapters.
  - vCPU can be reduced on lightly loaded clusters (<60% CPU), within NUMA. Avoid snapshots.

  — [Broadcom KB 397782](https://knowledge.broadcom.com/external/article/397782/)
- Extra Small capacity is cited as about 700 objects / 140K metrics, "not intended for production" (the blog's interpretation). — [driftar.ch](https://www.driftar.ch/2026/02/20/vcf-9-appliance-sizing-information/)
- [DOC] The SDDC Manager API `VcfOperationsCollectorSpec.applianceSize` accepts `small` (the default) and `standard`. — [Broadcom developer VcfOperationsCollectorSpec](https://developer.broadcom.com/xapis/sddc-manager-api/latest/data-structures/VcfOperationsCollectorSpec/)
- Lam's 9.0 JSON uses `vcfOperationsSpec.applianceSize: "small"` and spells the collector key `vcfOperationsCollectorSpec.applicationSize: "small"`. The spelling differs from the API reference. — [williamlam.com Ultimate Lab Resource](https://williamlam.com/2025/06/ultimate-lab-resource-for-vcf-9-0.html)
- [PROVEN] Lam's 9.0 minimal lab ran VCF Operations at 4/16 (290 GB) and the Collector at 4/16 (280 GB). — [williamlam.com minimal resources 9.0](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html)
- [PROVEN] Leaha's 9.1 Small Non-HA layout has VCF Operations 4/16 and Cloud Proxy 4/16. She writes: "Small will be fine for most scenarios." — [Leaha VCF 9.1.x guide](https://blog.leaha.co.uk/2026/05/06/vcf-9-1-x-ultimate-deployment-guide/)
- [DOC] VCF 9.1: "modifying the size of an actively deployed Cloud Proxy node is deprecated". Add or remove same-size nodes, or build a new collector group at the new size, migrate the adapters, and delete the old group. Mixing sizes in one group is unsupported. — [Broadcom KB 457248](https://knowledge.broadcom.com/external/article/457248/scaling-cloud-proxy-nodes-in-vcf-operati.html)

### Inferences
- [THEORY] For opscollector (UCP Small 4/16, 5.1 GB active), there are two routes:
  - Deploy a classic Cloud Proxy Small (2/8) in a new collector group and move the vCenter, NSX and other adapters to it. This is documented and saves 2 vCPU and 8 GB, but some VCF 9.1 fleet functions may require the UCP type. Unverified.
  - Edit the existing VM down to 2/8. This is "deprecated" rather than blocked, and may cause collector services to OOM. The 5.1 GB active at startup leaves little headroom at 8 GB.
- [THEORY] ops at 2/8 with 5.3 GB active is already at the documented floor. Going lower is not advisable.

### Gaps
- I found no source on deleting the collector entirely in a VCF 9 fleet, or on running adapters on the Operations node itself as a built-in collector. I also found no Lam post on VCF Operations lab tweaks beyond choosing small.
- I could not confirm whether the installer accepts an "xsmall" Operations size through the JSON.

## VCF License Server

### Takeaway
The VCF License Server is mandatory in 9.1. I found no official vCPU/RAM table for it. One deployment guide lists it at 2 vCPU / 4 GB, the same as this lab. I found no source reporting a smaller size.

### Cited Findings
- [PROVEN] A VCF 9.1 bill of materials lists "VCF License Server – 2vCPU 4GB". The separate vDefend licensing server is cited as "requiring 6vCPU and 24GB RAM". — [Leaha VCF 9.1.x guide](https://blog.leaha.co.uk/2026/05/06/vcf-9-1-x-ultimate-deployment-guide/)
- [DOC] The license server is deployed automatically as an embedded service during a new VCF Operations 9.1 deployment or upgrade. For full VCF stacks it is a mandatory step in the SDDC Manager 9.1 process. It is IPv4-only. — [Broadcom KB 446056](https://knowledge.broadcom.com/external/article/446056/licensing-vcf-operations-91-in-disconnec.html)
- A 9.1 post calls it a mandatory separate appliance, but the sizing is behind a link I could not reach. — [vcdx181.com](https://vcdx181.com/2026/05/page/2/)

### Inferences
- [THEORY] With 1.7 GB active, a 1 vCPU / 3 GB edit might run, but the saving is tiny. Lab memory notes say the license is 180-day fail-closed, and losing the license server risks licensing state. Not worth it.

### Gaps
- I found no Broadcom TechDocs sizing page for the License Server appliance.

## VCF Installer lab options, exact property names, and upgrade/LCM gotchas

### Takeaway
None of Lam's 9.1 installer workarounds shrinks an appliance. They cover single host, NIC speed, vSAN ESA HCL, connectivity checks, single-node AVI and timeouts. Sizing is controlled only through the JSON size fields:
- `vcenterSpec.vmSize`
- `vcenterSpec.storageSize`
- `nsxtSpec.nsxtManagerSize` (medium minimum)
- `vcfOperationsSpec.applianceSize`
- `vcfOperationsCollectorSpec.applianceSize` (`small` or `standard`)

After deployment, VCF has a supported resize path only for growing an appliance, plus KB 403604 for the NSX Small, Medium and Large standard shapes.

### Cited Findings
- [PROVEN, 9.1] Lam's VCF 9.1 workaround properties:
  - Single or dual host:
    - `feature.vcf.vgl-29121.single.host.domain = true` in `/home/vcf/feature.properties`, on the installer and on SDDC Manager.
    - `bringup.mgmt.cluster.minimum.size = 1` in `/etc/vmware/vcf/domainmanager/application-prod.properties`.
  - NIC speed check: `enable.speed.of.physical.nics.validation = false`.
  - vSAN ESA HCL, not needed in 9.1.1: `feature.vcf.vgl-43370.vsan.esa.sddc.managed.disk.claim = true` and `vsan.esa.sddc.managed.disk.claim = true`.
  - Single-node AVI: `feature.vcf.vgl-41078.alb.single.node.cluster = true`.
  - Connectivity checks:
    - `validation.disable.vmotion.connectivity.check`
    - `validation.disable.vsan.connectivity.check`
    - `nsxt.mtu.validation.skip`
  - Timeouts:
    - `nsxt.manager.wait.minutes = 180`
    - `orchestrator.task.retry.max = 5`
    - `vsp.bootstrap.task.timeout.minutes = 240`

  The post contains no appliance-sizing property. — [williamlam.com VCF 9.1 workarounds (2026-05)](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)
- [PROVEN] VCF 9.0 "Simple" deployment uses one NSX Manager and one VCF Operations node. In Lam's lab the stack totalled about 194 GB with VCF Automation, which alone is 96 GB. — [williamlam.com minimal resources 9.0](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html)
- [PROVEN] Tomas Fojta reduced the VCF Automation vCPU from 24 to 16 by editing the VM. He reports that 12 may work. Lam: "There are no workarounds or hacks for the memory requirements." — [williamlam.com minimal resources 9.0](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html), [fojta.wordpress.com minilab](https://fojta.wordpress.com/2025/06/19/deploying-vcf-9-in-a-minilab/)
- [PROVEN, 9.1] Cutting a VCF management-services worker from 12 to 8 vCPU caused pod startup problems. This is a commenter anecdote on Lam's 9.1 post and concerns the VSP-adjacent layer, out of scope here. — [williamlam.com VCF 9.1 workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)
- [PROVEN] Lam's single-host 9.1.1 post drives deployment from PowerShell variable files and gives no appliance sizes. — [williamlam.com VCF 9.1.1 single ESX host (2026-09)](https://williamlam.com/2026/09/vcf-9-1-1-single-esx-host-for-vcf-fleet-vcf-workload-domain-or-vcf-cluster.html)
- [DOC] Upgrade and LCM gotchas:
  - vCenter resize is one-way, and the API resize refuses to run on a snapshotted vCenter. — [VMware VCF blog](https://blogs.vmware.com/cloud-foundation/2026/05/12/resizing-vmware-vcenter/)
  - NSX resize must match a standard form factor and cannot be done from SDDC Manager. — [KB 403604](https://knowledge.broadcom.com/external/article/403604/changing-the-form-factor-of-nsx-manager.html)
  - Cloud proxy in-place resize is deprecated. — [KB 457248](https://knowledge.broadcom.com/external/article/457248/scaling-cloud-proxy-nodes-in-vcf-operati.html)
  - Driftar: shrinking after deployment is "technically possible" but "might be NOT supported". — [driftar.ch](https://www.driftar.ch/2026/02/20/vcf-9-appliance-sizing-information/)

### Inferences
- [THEORY] Summary of savings versus the current lab, all unsupported except where noted:
  - vcsa 4/21 to 2/14 (Tiny shape): saves 2 vCPU / 7 GB.
  - nsxa 6/24 to 4/16 (Small shape, KB mechanics): saves 2 vCPU / 8 GB.
  - opscollector 4/16 to 2/8 by redeploying a CP Small in a new collector group (documented path, but CP vs UCP compatibility is unverified): saves 2 vCPU / 8 GB.
  - SDDC Manager 16 to 12 GB: speculative.
  - Total: about 23-27 GB of configured RAM.
- [THEORY] Because no reservations are set, ESXi already only backs active pages (plus memory tiering). Configured-RAM cuts mainly help admission and headroom accounting. Guest JVM heaps sized at boot from total RAM may shrink with them, which is where OOM risk appears.
- [THEORY] Before any VCF upgrade, consider restoring the default sizes so LCM prechecks see standard shapes. vCenter going back up to Small uses the supported resize path.

### Gaps
- I found no source documenting a guest-side JVM or heap tuning procedure for vCenter, NSX, SDDC Manager or Operations in 9.x.
- I found no reports of LCM upgrade prechecks failing because of downsized appliances in 9.x, either positive or negative.
- Reddit and Broadcom community threads on VCF 9 appliance downsizing did not surface in the searches.
