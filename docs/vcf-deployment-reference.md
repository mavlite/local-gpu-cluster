# VCF lab deployment reference

What is deployed in the physical VMware Cloud Foundation lab, where it lives, and
how it is wired: hardware, versions, VMs, addresses, storage, networks, the switch,
DNS/NTP, the CA, and the supporting infrastructure around the cluster.

How it was **built** (vcfspec, PXE/iPXE, kickstart, Installer, depot) is in the
companion [vcf-lab-build-record.md](vcf-lab-build-record.md).

**Last verified: 2026-10-22.** Values marked ✓ were read live that day: from vCenter
via the read-only `vmware-monitor` MCP and pyVmomi (`svc-vmware-ro@vsphere.local`),
from each host's unauthenticated `ServiceContent.about`, from the switch's LLDP
advertisements as seen by the hosts, and from `dns01`. Unmarked values come from the
build inventory (`vcf-spec-tools/inventories/lab01-physical.yaml`) and the build
notes. See [Verify / refresh](#verify--refresh).

> **Live state is deliberately not here.** Utilization, active alarms, uptime and
> free capacity change daily. Get them from `vmware-monitor summary`,
> `vmware-monitor health alarms`, `vmware-storage vsan capacity lab01-cluster-001`,
> or an opencode agent (the `vmware-monitor` / `vmware-storage` / `vmware-harden`
> MCP servers are wired in — see
> [vmware-skills-operator-runbook.md](vmware-skills-operator-runbook.md)).

Not to be confused with the nested labs on the Proxmox host (`vmbrlab`, 10.50.10.x):
the September nested VCF 9.1.1 test lab (`vcf.lab.knowledgeondemand.net`, LXC
159–161) and the nested VCF 8 lab (`vcf8.lab.knowledgeondemand.net`, CT 190 + VMs
191–194).

---

## Platform

| Item | Value |
|---|---|
| VCF | 9.1.1.0, instance `lab01`, `workflowType=VCF` |
| ESXi | **9.1.1.0 build 25714478** on all hosts ✓ |
| vCenter | **9.1.1.0 build 25712839** ✓ |
| Datacenter | `lab01-datacenter` ✓ |
| Cluster | `lab01-cluster-001` (MoID `domain-c9`), 3 hosts ✓ |
| DRS | enabled, **partially automated**, migration threshold 3 ✓ |
| HA | enabled, host monitoring on, **admission control off** ✓ |
| EVC | not configured ✓ |
| SSO domain | `vsphere.local` |
| DNS domain | `lab.knowledgeondemand.net` ✓ (AD domain is `knowledgeondemand.net`) |
| Certificates | Custom CA `knowledgeondemand-LabRoot-CA` ✓; ESXi certificate mode **custom** (managed through VCF Operations only) |
| Licensing | No keys: evaluation, then subscription assigned in VCF Operations; must sync at least every 180 days |
| Not deployed | VCF Automation (deliberately; +20 vCPU / +96 GB), NSX Edge / T0, supplemental NFS |

## Hardware (identical ×3)

| Item | Value |
|---|---|
| Node | Minisforum 795S7 barebone, BD795i SE board (SMBIOS: "Micro Computer (HK) Tech Limited DeskMini Series" ✓). No BMC/IPMI/AMT — power on by hand |
| CPU | AMD Ryzen 9 **7945HX**, 1 socket, 16 cores / 32 threads ✓, AVX-512 |
| RAM | 96 GB DDR5-5200 SO-DIMM ×2, non-ECC (ESXi sees ~93.7 GB DRAM) |
| BIOS | **1.17 (2026-08-28)** ✓. Secure Boot and TPM **off** (permanent — see build record) |
| NICs | 2× 10GbE Intel X520 (82599, `ixgben`) = `vmnic0`/`vmnic1` ✓ — the only cabled ports. Onboard RTL8125 2.5G is not claimed by stock ESXi and not cabled |
| Boot disk | 1 TB Crucial CT1000P310 (M.2 #2) — EFI, bootbanks, OSDATA 120 GB, then the 803 GB `hypNN-local` VMFS |
| vSAN ESA disk | 4 TB Crucial CT4000P310 (x16 slot, bifurcated x8) |
| Memory-tier disk | 512 GB Samsung MZVLB512HAJQ (PM981, Lenovo OEM, firmware 5L2QEXA7) (M.2 #1). hyp02/hyp03 tier drives were replaced 2026-10-03 |

PCIe: the x16 slot is bifurcated x8/x8 (X520 + vSAN NVMe); both M.2 slots are used;
no root port is free.

**Memory tiering** ✓: software tiering on all hosts, **1:1** — 93.7 GB DRAM + 93.7 GB
NVMe tier = 187 GB presented per host (cluster: 48 cores, ~561 GB presented, ~281 GB
of it DRAM). The tier drives are consumer parts (~0.3 DWPD) and have failed under
burst load; see the build record's known-issues section.

## Hosts

| Host | Mgmt IP ✓ | vmnic0 MAC (provisioning) ✓ | Switch ports ✓ |
|---|---|---|---|
| `hyp01.lab.knowledgeondemand.net` | 172.16.10.145 | 98:b7:85:22:54:a4 | sfp-sfpplus2 / sfp-sfpplus3 |
| `hyp02.lab.knowledgeondemand.net` | 172.16.10.143 | 98:b7:85:22:65:3e | sfp-sfpplus6 / sfp-sfpplus7 |
| `hyp03.lab.knowledgeondemand.net` | 172.16.10.141 | 98:b7:85:23:a4:ce | sfp-sfpplus4 / sfp-sfpplus5 |

Switch ports are `vmnic0` / `vmnic1`, both on the single CRS309 — there is no switch
redundancy.

### VMkernel adapters ✓

| vmk | Service | Network | hyp01 | hyp02 | hyp03 | MTU |
|---|---|---|---|---|---|---|
| vmk0 | management | VLAN 10, `SDDC-DPortGroup-Mgmt` | .10.145 | .10.143 | .10.141 | **1500** |
| vmk1 | vMotion (vmotion stack) | VLAN 20, `SDDC-DPortGroup-vMotion` | .20.20 | .20.21 | .20.22 | 9000 |
| vmk2 | vSAN | VLAN 30, `SDDC-DPortGroup-VSAN` | .30.20 | .30.21 | .30.22 | 9000 |
| vmk10, vmk11 | NSX host TEP (vxlan stack) | VLAN 40 | .40.21, .40.22 | .40.24, .40.25 | .40.20, .40.23 | 9000 |
| vmk50 | NSX hyperbus | link-local | 169.254.1.1 | 169.254.1.1 | 169.254.1.1 | 1500 |

All addresses are 172.16.x.y/24. Default gateway 172.16.10.1, DNS and NTP
172.16.10.150, time zone UTC ✓.

## VCF management components

| Component | VM | vCPU | RAM | IP ✓ | FQDN | Host* |
|---|---|---|---|---|---|---|
| vCenter Server | `vcsa` | 4 | 21 GB | 172.16.10.129 | `vcsa.lab.knowledgeondemand.net` | hyp02 |
| SDDC Manager | `sddc-manager` | 4 | 16 GB | 172.16.10.133 | `sddc-manager.lab…` | hyp03 |
| NSX Manager node | `nsxa` | 6 | 24 GB | 172.16.10.132 | `nsxa.lab…` | hyp01 |
| VCF Operations | `ops` | 2 | 8 GB | 172.16.10.122 | `ops.lab…` | hyp03 |
| VCF Operations cloud proxy | `opscollector` | 4 | 16 GB | 172.16.10.123 | `opscollector.lab…` | hyp03 |
| VCF License Server | `licsrv` | 2 | 4 GB | 172.16.10.134 | `licsrv.lab…` | hyp02 |

NSX is installed and the hosts are prepared (TEPs on VLAN 40), but no NSX overlay
segments are used by workloads.

### Service endpoints (VIPs, no VM of their own)

| Name | IP | Notes |
|---|---|---|
| `nsx.lab…` | 172.16.10.131 ✓ | NSX Manager cluster VIP (answers HTTPS) |
| `fleet.lab…` | 172.16.10.116 ✓ | VCF fleet services; also the post-bring-up depot endpoint (`/depot-service/content-gateway`, routed by TLS SNI — use `curl --resolve`) |
| `platform.lab…` | 172.16.10.120 ✓ | VCF services platform (VSP / VCFMS) |
| `lab01.lab…` | 172.16.10.121 | VSP instance FQDN (inventory) |
| `idb.lab…` | 172.16.10.124 | VCF Identity Broker (VIDB), tenant `CUSTOMER` (inventory) |
| `installer.lab…` | 172.16.10.135 ✓ (DNS only) | VCF Installer used for bring-up; **removed** (no VM, does not answer). The record remains |

### VCF services platform nodes (6 VMs)

| VM | vCPU | RAM | IP ✓ | Host* |
|---|---|---|---|---|
| `platform-lmsgx` | 4 | 10 GB | 172.16.10.160 | hyp03 |
| `platform-dlq9c` | 4 | 10 GB | 172.16.10.162 | hyp02 |
| `platform-8n62z` | 10 | 16 GB | 172.16.10.163 | hyp02 |
| `platform-pfnmx` | 10 | 16 GB | 172.16.10.164 | hyp01 |
| `platform-rptxg` | 4 | 10 GB | 172.16.10.165 | hyp01 |
| `platform-c5t7j` | 10 | 16 GB | 172.16.10.167 | hyp03 |

Total 42 vCPU / 78 GB. They draw from the VSP pool **172.16.10.160–.189**
(non-contiguous use is normal; there are no PTR records for them). The 4-vCPU nodes are
the control plane, the 10-vCPU nodes the workers — node names change on rebuild,
so identify them by size. `vcf-services-runtime-template-9.1.1.0.25714471` (6 vCPU,
24 GB, powered off) is their template — do not delete it.

## Workload VMs

| VM | vCPU | RAM | IP ✓ | Network | Host* | Purpose |
|---|---|---|---|---|---|---|
| `devvm01` | 8 | 48 GB | 172.16.70.10 | `dev-vm1-vlan70` | hyp01 | external developer's VM — keep |
| `devvm02` | 8 | 48 GB | 172.16.71.10 | `dev-vm2-vlan71` | hyp02 | external developer's VM — keep |
| `devvm03` | 8 | 48 GB | 172.16.72.10 | `dev-vm3-vlan72` | hyp03 | external developer's VM — keep |

### Parked (normally powered off)

| VM | vCPU | RAM | Notes |
|---|---|---|---|
| `llmbench01`–`03` | 12 each | 24 GB each | LLM benchmark VMs |
| `wf-runner-01`–`03` | 8 each | 16 GB each | Workforce runners; the CPU agent tier was retired 2026-10-15 |

Powering these on costs 60 vCPU / 120 GB — size against the hosts' *DRAM*, not
the tiered total.

\* Host placement is point-in-time; DRS can move VMs. Confirm with
`vmware-monitor vm info <name>`.

## Storage ✓

| Datastore | Type | Capacity | Notes |
|---|---|---|---|
| `vsan-lab01` | vSAN **ESA** | 11,178 GB | 3 × 4 TB CT4000P310; FTT=1; compression on, dedup off; HCL auto-claim skipped (consumer drives) |
| `hyp01-local` | VMFS | 803 GB | on the boot disk |
| `hyp02-local` | VMFS | 803 GB | on the boot disk |
| `hyp03-local` | VMFS | 803 GB | on the boot disk |

The memory-tier NVMe devices are separate from vSAN; vSAN health does not cover them.

## Network

### Physical

| Device | Role |
|---|---|
| **MikroTik CRS309-1G-8S+IN**, RouterOS 7.20 ✓ (LLDP; chassis `d4:01:c3:32:4c:75`) | Lab L3 router and switch: all lab VLANs and their `.1` gateways, inter-VLAN routing, firewall, DHCP and TFTP for provisioning. Home-LAN address **192.168.6.11** ✓ |
| MikroTik CSS610-8G-2S+IN (SwOS Lite) | L2-only access switch |

All lab VLANs run `mtu=9000 l2mtu=9196` on the CRS309. The host ports carry VLAN 99
untagged (PVID, for PXE) with all other lab VLANs tagged.

### VLANs

| VLAN | Subnet | Use |
|---|---|---|
| 10 | 172.16.10.0/24 | Management: hosts (vmk0), VCF appliances, VSP nodes ✓ |
| 20 | 172.16.20.0/24 | vMotion ✓ |
| 30 | 172.16.30.0/24 | vSAN ✓ |
| 40 | 172.16.40.0/24 | NSX host TEP (pool `lab01-tep`, .20–.60) ✓ |
| 50 | — | NSX T0 uplink — defined on the CRS309, unused |
| 60 | — | VM-Mgmt — defined on the CRS309, unused |
| 70 / 71 / 72 | 172.16.70–72.0/24 | dev VMs ✓ |
| 99 | — | Untagged provisioning VLAN (PXE) on host ports |

### vSphere Distributed Switch ✓

`lab01-cluster-001-vds-001` — version 9.1.0, MTU 9000, LLDP, uplinks
`uplink1`/`uplink2` (= `vmnic0`/`vmnic1`).

| Port group | VLAN | Active uplinks | Notes |
|---|---|---|---|
| `SDDC-DPortGroup-Mgmt` | 10 | uplink1, uplink2 | host vmk0 |
| `SDDC-DPortGroup-VM-Mgmt` | 10 | uplink1, uplink2 | VCF appliances; **ephemeral** binding (attachable host-direct without vCenter) |
| `SDDC-DPortGroup-vMotion` | 20 | uplink1, uplink2 | vmk1 |
| `SDDC-DPortGroup-VSAN` | 30 | uplink1, uplink2 | vmk2 |
| `dev-vm1-vlan70` / `dev-vm2-vlan71` / `dev-vm3-vlan72` | 70 / 71 / 72 | uplink2, uplink1 | static binding, created post-bring-up by `scripts/devvm-portgroups.sh` |
| `lab01-cluster-00-DVUplinks-20` | trunk 0–4094 | — | uplink port group |

### Routing and firewall

- The lab is reached from the home LAN (192.168.6.0/24) via the CRS309:
  `172.16.0.0/16 via 192.168.6.11`, carried as a static route by the Proxmox host ✓
  and the workstation.
- VLAN 10 has **no internet egress** (not NATed out); dns01 resolves public names.
- The CRS309 forward chain isolates the dev VLANs with "deny-all-private" drop rules.
  Three narrow accepts sit above them (comment `devvm-tester-filecopy`):
  `src 172.16.7X.0/24 → dst 192.168.6.175 tcp/21422` — the one-way dev-VM → tester
  (Proxmox VM 172) file-copy path.
- Standing rule: changes to the CRS309 are deliberate and manual; no automation
  touches it.

## DNS, NTP and identity

- **dns01** = `DNS01.knowledgeondemand.net` — Windows Server 2025 domain controller
  for `knowledgeondemand.net`. Addresses **172.16.10.150** ✓ (lab) and
  192.168.6.197 (home LAN). Runs AD DNS, **NTP** (123/udp — the hosts' only time
  source ✓), and AD CS.
- **SOA/NS** for `lab.knowledgeondemand.net` is dns01 ✓. Zone transfer refused ✓.
  Zones: `knowledgeondemand.net`, `lab.knowledgeondemand.net`,
  reverse `10.16.172.in-addr.arpa`; dynamic updates off. Manage records with
  `scripts/vcf-dns/Set-VCFLabDnsRecord.ps1` (WinRM).
- DNS deliberately does **not** run on the CRS309: RouterOS auto-creates multiple PTRs
  per IP, which VCF rejects.
- Every VCF FQDN needs A **and** PTR, lowercase, unique ✓ (all appliances and hosts
  have both; VSP node IPs have none, by design).
- Reserved but not yet deployed (parent zone): `vrni` .136, `log-insight` .137,
  `vrni-collector` .138. `pki.knowledgeondemand.net` → .150 (A only) serves the CA's
  CRL/AIA.
- **Lab CA** `knowledgeondemand-LabRoot-CA`: single-tier Enterprise Root on dns01,
  issued 2026-10-01, valid to 2036-10-01, RSA 2048 / SHA-256. CRL/AIA over plain HTTP at
  `http://pki.knowledgeondemand.net/CertEnroll/`. Web enrolment (`/certsrv`) reachable
  on 443 only from SDDC Manager (.133) and Ops (.122); port 80 from 172.16.10.0/24.
  Registered in **VCF Operations** (not SDDC Manager) as a Microsoft CA, template
  `VMware`, enrolment account `svc-vcf-ca`. **Never snapshot or roll back dns01**
  (it would reissue serial numbers).
- **Identity:** VCF Operations has an AD identity source (bind account `svc-vcf-ldap`;
  log in with the source's display name `knowledgeondemand`). VIDB at
  `idb.lab…`. Monitoring uses the local SSO account `svc-vmware-ro@vsphere.local`.

## Supporting infrastructure (outside the cluster)

| Thing | Address | Role |
|---|---|---|
| CRS309 | 192.168.6.11 / 172.16.x.1 | router, switch, firewall, DHCP/TFTP |
| dns01 | 172.16.10.150 / 192.168.6.197 | AD DNS, NTP, AD CS |
| TrueNAS offline depot | `http://172.16.10.50:8080` (nginx app `vcf-offline-depot`, no auth) | VCF offline depot (`/PROD/…`) **and** host boot media (`/esx/…`) |
| `mgmt` | 172.16.10.21 | standalone non-VCF ESXi (`mgmt-datastore1`); hosted the VCF Installer during bring-up. The CA scripts name a standalone mgmt host 192.168.6.167 that runs dns01 — likely the same dual-homed host, unconfirmed |
| Proxmox host | 192.168.6.175 | CT 190 `lab-svc` runs the read-only VMware MCP servers against vcsa |

## Current host settings ✓ (drift check)

Read live 2026-10-22. "Intended" is what the kickstart `%firstboot` sets.

| Setting | hyp01 | hyp02 | hyp03 | Intended |
|---|---|---|---|---|
| ntpd service | on, running | on, running | on, running | on |
| SSH (`TSM-SSH`) | off | **on** | off | on at build; VCF validation turns it off |
| ESXi Shell | off | off | off | off |
| `UserVars.SuppressShellWarning` | 0 | 1 | 0 | 1 |
| `VSAN.Vsan2ZdomCompZstd` | 0 | 0 | 0 | 0 (LZ4) |
| `Power.CpuPolicy` | **High Performance** | **High Performance** | **High Performance** | Balanced (BIOS guidance) |
| `Misc.BlueScreenTimeout` | 0 | 0 | 0 | 60 recommended |
| `Mem.AllocGuestLargePage` | 1 | 1 | 1 | 1 |
| Lockdown mode | disabled | disabled | disabled | — |

Also set at build (in `/etc/vmware/config` and kernel settings; not readable with the
read-only account): CPU brand-string override for NSX Edge, `disable_apichv` for
tiering on Ryzen, `entropySources=2`. The RDSEED mask (`cpuid.7.ebx`) was added to the
generator after these hosts were built and is probably **not** applied — see the build
record.

## Access

| Purpose | How |
|---|---|
| Read-only monitoring / health / compliance | `vmware-monitor`, `vmware-storage`, `vmware-harden` on CT 190, account `svc-vmware-ro@vsphere.local` (built-in Read-only at the root). Also MCP servers in opencode |
| Lab power on/off | `scripts/vcf-lab/Start-VCFLab.ps1` / `Stop-VCFLab.ps1` (Full or Minimal mode) |
| DNS records | `scripts/vcf-dns/Set-VCFLabDnsRecord.ps1` |
| Certificate operations | VCF Operations only — never out-of-band on vCenter or ESXi |
| Host console | none (no BMC); SSH if enabled, or a monitor at the node |

## Verify / refresh

On CT 190 (`pct exec 190 -- …`, binaries in `/opt/vmware-venv*/bin/`):

```bash
vmware-monitor inventory hosts|vms|datastores|networks|clusters
vmware-monitor vm info <name>            # host placement, guest FQDN, disks
vmware-monitor memory tiering            # DRAM/NVMe split per host
vmware-storage vsan efficiency lab01-cluster-001
```

- vmk, vDS, port-group VLAN, DRS/HA, host service and advanced-option values: a
  read-only pyVmomi session with the same account (`HostSystem.config.network`,
  `config.service`, `configManager.advancedOption`, `DistributedVirtualSwitch.config`).
- Switch identity and port mapping: `HostNetworkSystem.QueryNetworkHint()` (LLDP).
- Version/build with no credentials: SOAP `RetrieveServiceContent` to
  `https://<ip>/sdk`, read `about.fullName`.
- DNS: `dig @172.16.10.150 <name>` and `dig -x` from the Proxmox host (the workstation's
  VPN blocks lab DNS).

When a value here stops matching, update it and the "Last verified" date.
