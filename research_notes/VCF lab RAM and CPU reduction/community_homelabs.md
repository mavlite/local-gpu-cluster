# Community VCF 9.x Homelab Deployments: Hardware, Footprints, Tricks, Problems

Research date: 2026-10-25. All sources are 2025-2026 VCF 9.0/9.1 write-ups unless labelled otherwise. Our reference point: 3x Minisforum BD795i SE (Ryzen 16C/32T, 64 GB DRAM + NVMe tiering, vSAN ESA), VCF 9.1.1, about 54 vCPU of appliances plus 42 vCPU of VSP, about 110 GB of RAM.

## Who has written up VCF 9.x homelabs, and what footprints did they reach?

### Takeaway
Most write-ups use Minisforum MS-A2 hosts (Ryzen 16C/32T, 128 GB DDR5, so 2x our DRAM per host) with NVMe memory tiering, and usually only two hosts on NFS or vSAN. Only two give a full per-VM RAM table: William Lam's 9.0 minimum (48 vCPU / 194 GB with VCF Automation) and Daniel Krieger's 9.0.1 build (76 vCPU / 314 GB with everything). Leaha's installer-wizard table gives the 9.1 VCFMS (VSP) sizes. No published 9.1 lab runs on 64 GB hosts the way ours does.

### Cited Findings

**Comparison table**

| Author / date | VCF ver | Hardware | Hosts | Components (deployed / skipped) | Per-VM / totals | Tiering | Key problems |
|---|---|---|---|---|---|---|---|
| William Lam, "Minimal resources" (Jun 2025) | 9.0 | n/a (reference sizing) | (not stated) | vCenter, 1 NSX, SDDC Mgr, Ops, Fleet Mgr, Ops Collector, VCFA | vCenter 4/21 GB; NSX 6/24; SDDC 4/16; Ops 4/16; Fleet 4/12; Collector 4/16; VCFA 24/96. Stated total **48 vCPU / 194 GB / 3.2 TB** (rows actually sum to ~50 vCPU / 201 GB) | n/a | VCFA needs a host with >=12C/24T; "no workarounds" for VCFA's 96 GB — [Lam](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html) |
| William Lam, "Minimal VCF 9.0 Lab" + vcf-9x-in-box (Jul 2025) | 9.0 | Minisforum MS-A2 | 2 (comments) | Full fleet incl. Ops + Automation, vSAN ESA, NSX Edge A/S, VPC, Supervisor, VKS, DSM, Avi | not given in post | yes (BOM) | Nested-vSAN-ESA VIB "Failed to extract image" loop (fixed by kickstart/Secure Boot off) — [Lam](https://williamlam.com/2025/07/minimal-vmware-cloud-foundation-vcf-9-0-lab-setup.html) |
| William Lam, vcf-91-in-box (2026) | 9.1.1 | MS-A2 (Ryzen 7945HX), 128 GB, 500 GB boot, 1 TB 990 PRO vSAN ESA, 1 TB 980 PRO tier | 1, 2 or 3 physical (no nested) | vCenter, SDDC Mgr, Ops, Ops Proxy Collector, VCFMS (msr01), Fleet/Instance, License Server, Identity Broker, NSX (1 node), VNAs, VCFA + ASR; optional: Edges, Logs, Supervisor | no sizing table | tier device in kickstart | MikroTik MTU pre-check false failures; AMD Zen4/5 NSX/VCFA high-CPU tweaks — [GitHub](https://github.com/lamw/vcf-91-in-box) |
| Daniel Krieger (SDN-Warrior), 24 Oct 2025 | 9.0.1 | 2x MS-A2, 128 GB each, NFS on Unraid (no vSAN), + 1 MS-01 for nested WLD | 2 mgmt (+1) | Everything: VCFA, 2 NSX (mgmt+WLD), 2 vCenter, Ops, Logs, Ops for Networks + collector, Fleet, 2 Edges, collector, SDDC Mgr; SSP deferred | VCFA 16/96 (cut from 24); NSX 6/24 x2; vCenter 4/21 x2; NI 8/32; Edge 4/8 x2; Fleet 4/12; Ops 4/16; Logs 4/8; SDDC 4/16; NI-col 4/12; cloud proxy 4/16. **Total 76 vCPU / 314 GB** | 200% tier (~384 GB theoretical/host); ~188 GB Tier0 alloc with VCFA; ~130 GB RAM used without Ops appliance | **CPU, not RAM, was the bottleneck**: 2.4:1 overcommit without VCFA, 4.75:1 with it on one host; vCenter timeouts with VCFA running; Ryzen apichv/CPU-brand workarounds; 9.0.1 Edge uplink port groups deleted after 24 h power-off — [SDN-Warrior](https://sdn-warrior.org/posts/vcf9-ms-a2-special/) |
| Tomas Fojta, 19 Jun 2025 | 9.0 | 2x GMKtec K8 Plus (8C/16T AMD), 96 GB + 128 GB (224 GB), NFS on QNAP | 2 | vCenter, NSX Mgr, SDDC Mgr (smallest), 2-node Edge, Supervisor (1 CP VM), VCFA (Day-N), VCF Logs (small); skipped vSAN, AVNs, Supervisor HA, NSX EDP | VCFA cut **24 -> 16 vCPU**, 96 GB kept | tiering -> ~448 GB total | apichv for tiering on AMD; duplicate SMBIOS UUID; entropy exhaustion -> NSX transport nodes UNKNOWN (fix entropySources=4); Edge non-EPYC CPU check patched — [Fojta](https://fojta.wordpress.com/2025/06/19/deploying-vcf-9-in-a-minilab/) |
| vkasaert, 3 Feb 2026 | 9.0.1 | 2x MS-A2 (9955HX), 128 GB, 1 TB 990 PRO tier, 2 TB vSAN | 2 | vCenter, NSX (1 node + VIP), Ops, Fleet Mgr, cloud proxy, VCFA, 2 Edges | not stated | tier active (~256 GB reported/host) | Lam's 2-node script failed, flags applied by hand; thumbprints needed despite skip flag — [vkasaert](https://vkasaert.com/2026/02/03/homelab-vcf-9-in-a-box/) |
| thinkingoutcloud, 10 Nov 2025 | 9.0.1 | 2x MS-A2 (2-node vSAN) + N305 32 GB helper box | 2 (+1) | Mgmt domain + NSX; VCFA, Logs, Networks, Avi, DSM not yet built | not stated | enabled | Deploy fails if VMs are on a target host; NSX backup task fails on password drift (KB 399414) — [thinkingoutcloud](https://thinkingoutcloud.org/2025/11/10/every-foundation-needs-a-home-my-vmware-cloud-foundation-9-0-1-home-lab-build/) |
| Devyn Harrington, Day 1 (2026) | 9.1 | 1x MS-A2 9955HX 16C/32T, 128 GB, 1 TB tier, 4 TB nested datastore | 1 physical | ESXi + vCenter Tiny (2 vCPU / 14 GB) + VIS appliance only (foundation for nested) | — | **1:4 (400%)**, ~628 GB reported | NTP; maintenance mode after tiering — [Harrington Day 1](https://devynharrington.com/homelab/building-my-vcf-9-1-homelab-bare-metal-esxi-vcenter-vis/) |
| Devyn Harrington, mgmt domain (21 Aug 2026) | 9.1 | same single MS-A2 | 1 physical, 6 nested ESX (3 mgmt) | vCenter, vSAN, NSX (1 node), SDDC Mgr, Ops + collector, VCFMS, Fleet, Identity Broker, License, VCFA + ASR | Nested mgmt ESX 112 GB each (336 GB for 3) on 128 GB DRAM; vCPU 32 -> 16 -> 24 | 1:4 | Cutting nested hosts to 16 vCPU blocked the 24-vCPU VCFA; NSX host-prep recovery; stale VSP record after a failed VCFA — [Harrington mgmt](https://devynharrington.com/homelab/deploying-a-complete-vcf-9-1-management-domain-nested-esxi-nsx-recovery-and-automation/) |
| Adrian Heissler, Sep 2026 | 9.1 | 1x Xeon Gold 5412U 24C, 512 GB | 1 physical, nested | VCF 9.1 + VCFA | VCFA 24 -> 20 vCPU (16 broke Kafka scheduling) | — | %CSTP from 24-vCPU VM; ghost pods past 224-pod cap; etcd starvation with 3 Kafka controllers — [Heissler](https://adrian.heissler.at/2026/09/vcf-automation-9-1-homelab-performance-optimizations/) |
| Justin Raley, Nov 2025 (upd. Oct 2026) | 9.0 | 6x MS-A2 (3x 7940HX mgmt, 3x 9955HX WLD) | 6 | mgmt + WLD | WLD hosts 96 GB; no per-VM data | "flexibility" | none listed — [Raley](https://justinraley.com/new-hardware-update/) |
| Leaha, 9.1.x guide (6 May 2026) | 9.1 | nested, 3 mgmt + 1 WLD host | 3+1 nested | Small Non-HA | wizard table (below) | — | internally inconsistent BOM (VCFMS 42 vs 28 vCPU) — [Leaha guide](https://blog.leaha.co.uk/2026/05/06/vcf-9-1-x-ultimate-deployment-guide/) |
| Holodeck 9.0 (Broadcom), Jun 2025 | 9.0/5.2 | single big host | nested 4 mgmt + 3 WLD | full | ~30 cores, **325 GB**, 1.1 TB (whitepaper says 2 TB) | — | — [Broadcom blog](https://blogs.vmware.com/cloud-foundation/2025/06/30/announcing-the-general-availability-of-holodeck-9-0/) |

**VCF 9.1 installer deployment sizes** (vCPU / RAM GB / disk GB), copied from Leaha's wizard screenshots — [Leaha guide](https://blog.leaha.co.uk/2026/05/06/vcf-9-1-x-ultimate-deployment-guide/):
- VCF Management Services (VSP): Small Non-HA 28/58/2900; Small HA 42/78/3201; Medium 48/102/3600; Large 88/170/4402
- VCF Automation: Small 24/96 (no smaller size); Medium 72/288
- NSX Manager: Small Non-HA 6/24; Small HA 18/72
- VCF Operations: Small Non-HA 4/16; Small HA 8/32
- Cloud Proxy (collector) 4/16; vCenter 4/21 (Small)
- Leaha's BOM also lists License Server 2/4, Log Management ~8/16, Real-Time Metrics ~16/20 — [Leaha guide](https://blog.leaha.co.uk/2026/05/06/vcf-9-1-x-ultimate-deployment-guide/). MrPlanB repeats the License Server at 2 vCPU / 4 GB and gives a vDefend License Hub at 6 vCPU / 24 GB, but cites no primary source — [MrPlanB](https://www.mrplanb.com/blog/vmware-s-vcf-9-1-requirements-feel-like-a-door-slamming-shut-on-smaller-labs).
- VCFMS nodes: control plane about 4 vCPU / 10 GB, which can't be shrunk; default Small worker 12 vCPU / 24 GB — [Leaha downsize](https://blog.leaha.co.uk/2026/07/21/vcf-9-management-services-lab-downsize/). After the 9.1.1 rightsizing, a worker is `management.small`, about 10 vCPU / 16 GB — [virtualbytes](https://virtualbytes.io/vcf-9-1-1-worker-rightsizing-poddisruptionbudgets-and-why-it-does-not-stick/)
- 9.1.0's smallest Services Runtime was reported as 40 vCPU / 82 GB with "no smaller mode". This is second-hand: [MrPlanB](https://www.mrplanb.com/blog/vmware-s-vcf-9-1-requirements-feel-like-a-door-slamming-shut-on-smaller-labs) (cites no primary source), and a Medium post I could not open (HTTP 403).

### Inferences
- Our VSP at 42 vCPU matches the wizard's **Small HA** VCFMS size exactly (42 vCPU / 78 GB). It also matches 3 control-plane nodes (4/10) plus 3 rightsized workers (10/16): 3x4 + 3x10 = 42 vCPU and 3x10 + 3x16 = 78 GB. Small Non-HA in 9.1.1 is 1 CP + 2 workers = 28 vCPU / 58 GB. If our lab is Small HA, Non-HA would save about 14 vCPU / 20 GB. But no source documents scaling HA back down to Non-HA (see the downsizing section).
- Without VCFA, our appliance set is close to Lam's 9.0 minimum (vCenter, NSX, SDDC, Ops, collector come to ~22 vCPU / 93 GB) plus the 9.1 additions (License Server, VCFMS). Our ~110 GB total looks about as lean as anything published for 9.1 without VCFA.
- Every published working build has at least 2x our DRAM per host (128 GB MS-A2) or a large host. Three 64 GB hosts is a less-documented, tighter configuration.

### Gaps
- No Reddit r/homelab or r/vmware threads came up in searches (the search tool did not index them). There is no community data from Reddit.
- I found no posts from Erik Bussink, Vladan Seget or "Dennis/Wesley" with VCF 9.x footprints. The comdivision "VCF9 era" and Aaron Rombaut equipment posts were found but not read.
- The Medium nested 9.1 series by Lubomir Tobek returned HTTP 403, so its "557 GB total" figure is unverified (it is cited only through a search snippet).
- Lam's vcf-91-in-box does not publish a total RAM figure for 9.1.

## What is the smallest documented working VCF 9 deployment, and does it include VSP / fleet management?

### Takeaway
The smallest physical builds are **2 hosts** (2x MS-A2 at 128 GB, or GMKtec at 96+128 GB on NFS), and on 9.1 Lam supports **1 physical host** through the single-host-domain flags. Lam's 9.0 minimum is 194 GB with VCFA, and about 98 GB of appliances without it. In 9.1 the VSP/VCFMS is not optional for VCF: every 9.1 VCF build includes VCFMS, Fleet, License and usually Identity Broker.

### Cited Findings
- 9.0 minimal: 2 hosts with NFS, or 3 with vSAN/FC; a single 10GbE NIC; singleton appliances — [vdan.cz (Explore 2025 session)](https://vdan.cz/vmware/vcf/deploying-a-minimal-vcf-9-0-lab-insights-from-explore-2025/)
- 2x MS-A2 is "sufficient" for a minimal VCF 9 footprint; 3x is recommended for VKS/DSM — [Lam BOM / minimal posts](https://williamlam.com/2025/07/minimal-vmware-cloud-foundation-vcf-9-0-lab-setup.html)
- 9.1 single- or dual-host domains: `feature.vcf.vgl-29121.single.host.domain=true` plus `bringup.mgmt.cluster.minimum.size=1`; single-node Avi; NFS single-host check bypass; VSP bootstrap timeouts raised to 240 min — [Lam 9.1 workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)
- One reader reported that the 9.1 installer UI demanded 3 valid hosts even with the override, which blocked a single-host deploy through the UI. Lam suggested exporting the JSON and deploying through the API — [Lam minimal 9.0 comments](https://williamlam.com/2025/07/minimal-vmware-cloud-foundation-vcf-9-0-lab-setup.html)
- vcf-91-in-box targets 1-3 physical hosts and includes VCFMS (msr01), Fleet/Instance, License Server and Identity Broker in its IP plan — [GitHub](https://github.com/lamw/vcf-91-in-box)
- 9.1.1 Simple (non-HA) VCFMS has "one less VCFMS Worker Node (12 vCPU / 24GB)" than 9.1.0 — [Lam 9.1.1 enhancements](https://williamlam.com/2026/09/10-exciting-enhancements-in-vmware-cloud-foundation-9-1-1.html)
- A commenter on three 8-core, 64 GB NUCs (the closest hardware to ours) cut a VCFMS worker from 12 to 8 vCPU during deployment. Pods still stalled on Telemetry, and another commenter planned to rebuild on VVF instead — [Lam 9.1 workarounds comments](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)

### Inferences
- The smallest *documented* 9.1 VCF with VSP and no VCFA works out to roughly vCenter 21 + NSX 24 + SDDC 16 + Ops 16 + collector 16 + License 4 + VCFMS Non-HA 58 ≈ **155 GB configured** (≈ 50 vCPU). That is my sum from the wizard sizes, not a figure anyone published.
- The 3x 64 GB NUC report is the only published attempt on our class of hardware, and it was struggling.

### Gaps
- No write-up states a measured total RAM for a working 9.1 lab that includes VSP.

## Did anyone deploy VCF 9 without some components, and what did it cost them?

### Takeaway
VCF Automation is the component labs skip or defer most often (96 GB, 24 vCPU). Skipping it costs self-service IaaS and namespaces. VVF avoids VCFMS entirely. The Operations collector is never reported as skipped.

### Cited Findings
- Krieger: with VCFA powered off, "the entire setup runs smoothly and quickly". With it on, vCenter timed out and components malfunctioned — [SDN-Warrior](https://sdn-warrior.org/posts/vcf9-ms-a2-special/)
- Fojta skipped vSAN, AVNs, Supervisor HA and NSX Enhanced Datapath, and deployed VCFA later as Day-N — [Fojta](https://fojta.wordpress.com/2025/06/19/deploying-vcf-9-in-a-minilab/)
- thinkingoutcloud runs the management domain without VCFA, Logs, Networks, Avi or DSM — [thinkingoutcloud](https://thinkingoutcloud.org/2025/11/10/every-foundation-needs-a-home-my-vmware-cloud-foundation-9-0-1-home-lab-build/)
- Without VCFA, "self-service IaaS and namespace automation are not meaningfully accessible"; VCFA is optional at bring-up — [vStellar](https://vstellar.com/2026/05/vcf-9-1-home-lab-series-part-2-high-level-design/)
- VVF = vSphere + VCF Operations (per Lam, 9.0) — [Lam minimal resources](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html). Lam's 9.1 workaround post says VVF also deploys management services in 9.1 (1 control plane + 2 workers, or 3 workers with Log Management). This conflicts with the claim that VVF has no VCFMS — [Lam 9.1 workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html)
- The MrPlanB community "fallback" is to keep vSphere, vSAN, NSX, Ops and Supervisor and drop SDDC Manager and Service Runtime: "less integrated but more livable" — [MrPlanB](https://www.mrplanb.com/blog/vmware-s-vcf-9-1-requirements-feel-like-a-door-slamming-shut-on-smaller-labs)
- In 9.1, Log Management is a Day-N addition from the Ops UI, not part of the scripted deploy — [Lam nested 9.1](https://williamlam.com/2026/05/vcf-9-1-automated-vmware-cloud-foundation-vcf-vmware-vsphere-foundation-vvf-nested-lab-deployment.html)

### Inferences
- In 9.1, Log Management and Real-Time Metrics are the components that grow VCFMS (more or larger workers). Leaving them out is the main way to keep VSP at its minimum (see the next section).

### Gaps
- The sources conflict on whether VVF 9.1 runs VCFMS. No VVF 9.1 homelab footprint has been published.
- No report of skipping the Operations collector (cloud proxy).

## Did anyone downsize VMs after deployment, and did it survive upgrades?

### Takeaway
Yes, and often. VCFA vCPU went from 24 to 16 or 20 (memory left at 96 GB), and VCFMS workers went from 12/24 to 8/16 by patching Kubernetes requests. Broadcom shipped an official 9.1.1 rightsizing script (KB 455842). Rightsizing **does not stick** once Log Management or Real-Time Metrics is added, and nobody has reported whether the manual downsizes survive upgrades.

### Cited Findings
- VCFA 24 -> 16 vCPU (maybe 12) through the vSphere UI. There is no workaround for its 96 GB, which VCFA really uses — [Lam / Fojta comment](https://williamlam.com/2025/06/minimal-resources-for-deploying-vcf-9-0-in-a-lab.html); Fojta ran 16/96 — [Fojta](https://fojta.wordpress.com/2025/06/19/deploying-vcf-9-in-a-minilab/); Krieger ran 16/96 — [SDN-Warrior](https://sdn-warrior.org/posts/vcf9-ms-a2-special/)
- VCFA 9.1 24 -> 20 vCPU worked. At 16 vCPU, CPU requests hit 97% and Kafka pods went Pending. Kafka controllers were cut from 3 to 1, which leaves a single point of failure — [Heissler](https://adrian.heissler.at/2026/09/vcf-automation-9-1-homelab-performance-optimizations/)
- Leaha's unsupported VCFMS downsize: workers 12/24 -> 8/16 by patching the `vmsp-global-config` ReleaseTemplate; Kyverno 3 -> 2 replicas; Postgres, Redis, Salt, Zot and Antrea requests reduced. Layouts: 2x 8/16 without Log Management, 3x 8/16 (or 3x 6/16 with advanced tweaks) with Identity Broker and Log Management. The control plane stays at 4/10. Real-Time Metrics needs a 4th worker. **Upgrades untested** — [Leaha downsize](https://blog.leaha.co.uk/2026/07/21/vcf-9-management-services-lab-downsize/)
- Official 9.1.1 rightsizing: new 9.1.1 deployments get it automatically; upgraded ones must run the KB 455842 script on the control-plane node — [Lam 9.1.1 adopt](https://williamlam.com/2026/09/vcf-9-1-1-adopting-the-reduced-vcf-management-services-vcfms-footprint-after-an-upgrade.html). A commenter reports the script aborts if Log Management or Real-time Metrics is present (same source); virtualbytes confirms this — [virtualbytes](https://virtualbytes.io/vcf-9-1-1-worker-rightsizing-poddisruptionbudgets-and-why-it-does-not-stick/)
- Rightsizing reverted: installing Log Management afterwards bumped the workers from small (~10/16) to **large (16/32)**, about +18 vCPU and +48 GB across 3 workers. `log-store-0` alone requests 5 CPU / 10 Gi. Single-replica Postgres PodDisruptionBudgets (PDBs) stall node drains for 1800 s — [virtualbytes](https://virtualbytes.io/vcf-9-1-1-worker-rightsizing-poddisruptionbudgets-and-why-it-does-not-stick/)
- On worker resizing, a commenter resized a worker on the fly. Virtualbytes says the CAPI machine type is effectively immutable, so a change needs a rolling replacement. The two accounts conflict — [Lam 9.1 workarounds comments](https://williamlam.com/2026/05/vcf-9-1-comprehensive-vcf-installer-sddc-manager-configuration-workarounds-for-lab-deployments.html), [virtualbytes](https://virtualbytes.io/vcf-9-1-1-worker-rightsizing-poddisruptionbudgets-and-why-it-does-not-stick/)
- Small Non-HA VCFMS can scale **up** to Small HA in the Ops UI — [Lam 9.1.1 enhancements](https://williamlam.com/2026/09/10-exciting-enhancements-in-vmware-cloud-foundation-9-1-1.html). Enabling HA on a Small deployment through the backend API is unsupported and **cannot be scaled back down** — [Lam Small HA](https://williamlam.com/2026/06/vcf-9-1-enabling-high-availability-for-a-small-vcf-management-services-vcfms-deployment.html) (search snippet only; not fetched in full)
- vCenter in VCF 9.1 can be resized through a `deployment/size` PATCH API, or by shutting it down and editing CPU/RAM — [VMware blog](https://blogs.vmware.com/cloud-foundation/2026/05/12/resizing-vmware-vcenter/) (search snippet only)
- Krieger cut VCFA from 24 to 16 vCPU and kept going on 9.0.1 — [SDN-Warrior](https://sdn-warrior.org/posts/vcf9-ms-a2-special/)

### Inferences
- For us, the realistic VSP savings are: the official rightsizing (already in effect if we deployed fresh on 9.1.1), and Leaha-style request patching to 8/16 workers, which is unsupported, has no upgrade evidence, and is reverted by Day-N Log Management or Metrics.
- Shrinking a VM's vCPU count is easier than shrinking its RAM: every report that cut vCPU kept the RAM.

### Gaps
- No source confirms whether manually downsized NSX, Ops or VSP VMs keep their sizes through a 9.1.x patch upgrade.
- No report of downsizing NSX Manager below 6/24 or Ops below 4/16.

## How much did memory-tiering homelabs lean on tiering?

### Takeaway
Heavily on paper: tier ratios ran from 100% up to 400% of DRAM. In practice, CPU overcommit became the bottleneck before tiered RAM did, and nested ESX labs pushed ratios the highest.

### Cited Findings
- Krieger: tier at 200% (~384 GB theoretical per host); Tier1 sat unused (-1 MB) in the management domain while Tier0 allocation was ~188 GB with VCFA running — [SDN-Warrior](https://sdn-warrior.org/posts/vcf9-ms-a2-special/)
- Harrington: 1:4 (400%) on one 128 GB host, ~628 GB reported, hosting 3x 112 GB nested management hosts. He notes the hot working set must still fit in DRAM — [Harrington Day 1](https://devynharrington.com/homelab/building-my-vcf-9-1-homelab-bare-metal-esxi-vcenter-vis/), [Harrington mgmt](https://devynharrington.com/homelab/deploying-a-complete-vcf-9-1-management-domain-nested-esxi-nsx-recovery-and-automation/)
- Fojta: 224 GB DRAM tiered to ~448 GB — [Fojta](https://fojta.wordpress.com/2025/06/19/deploying-vcf-9-in-a-minilab/)
- vkasaert expects about 2x DRAM reported (~256 GB per 128 GB host) — [vkasaert](https://vkasaert.com/2026/02/03/homelab-vcf-9-in-a-box/)
- A comment on Lam's 9.1.1 post mentions 1:1 or 1:2 tier ratios in a homelab — [Lam 9.1.1 enhancements](https://williamlam.com/2026/09/10-exciting-enhancements-in-vmware-cloud-foundation-9-1-1.html)
- In 9.0, nested ESXi or VHV VMs fail to power on with tiering enabled; in 9.1 it works, and a VM advanced setting can extend tiering into nested guests — [Lam nested 9.1](https://williamlam.com/2026/05/vcf-9-1-automated-vmware-cloud-foundation-vcf-vmware-vsphere-foundation-vvf-nested-lab-deployment.html)
- On AMD, tiering needs `monitor_control.disable_apichv` or VMs won't boot — [Fojta](https://fojta.wordpress.com/2025/06/19/deploying-vcf-9-in-a-minilab/), [SDN-Warrior](https://sdn-warrior.org/posts/vcf9-ms-a2-special/)
- MrPlanB's community view: tiering "only helps so far" against 9.1's RAM demand — [MrPlanB](https://www.mrplanb.com/blog/vmware-s-vcf-9-1-requirements-feel-like-a-door-slamming-shut-on-smaller-labs)
- AMD Zen4/5 consumer CPUs drive high NSX CPU through slow entropy; the fixes are an entropy VM setting and host entropySources. VCFA crash-loops on 9.1 on MS-A2, fixed by an unsupported FIPS_MODE=disabled — [Lam VCFA Zen tip](https://williamlam.com/2026/08/quick-tip-reducing-high-cpu-utilization-in-vcf-automation-vcfa-on-amd-zen4-zen5-cpus.html), [Lam ESX workarounds](https://williamlam.com/2026/05/vcf-9-1-comprehensive-esx-configuration-workarounds-for-lab-deployments.html)

### Inferences
- Only Krieger reports measured tier use, and it was near zero for the management domain on 2x 128 GB. On our 64 GB hosts, the management VMs are far more likely to touch the NVMe tier. Our own record shows tier-drive hangs crash VMs (see project memory on tier drive failures). Nobody in the community has reported tier-drive stability problems like ours.
- On Ryzen hosts, CPU contention (Krieger's 2.4-4.75:1, Heissler's %CSTP) is a co-equal constraint. Cutting vCPU may matter as much as cutting RAM.

### Gaps
- No community lab reports long-term stability or failure data for heavily tiered (>=200%) management VMs, or for tier-device hangs.
