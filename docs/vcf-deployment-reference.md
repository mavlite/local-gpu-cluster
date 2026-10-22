# VCF lab deployment reference

The **structural** reference for the physical VMware Cloud Foundation lab: nodes,
versions, VMs, addresses, storage, and networks. It is the answer to "what is
deployed, where, and at what address."

**Last verified: 2026-10-22**, live, against vCenter via the `vmware-monitor` MCP
(read-only, `svc-vmware-ro@vsphere.local`) and each host's unauthenticated
`ServiceContent.about`. See [Verify / refresh](#verify--refresh) to re-check.

> **Live state is deliberately not here.** Utilization, active alarms, uptime and
> free capacity change daily and would be wrong within a week. Get them live:
> `vmware-monitor summary`, `vmware-monitor health alarms`,
> `vmware-storage vsan capacity lab01-cluster-001`, or ask an opencode agent (the
> `vmware-monitor` / `vmware-storage` / `vmware-harden` MCP servers are wired in).
> See [vmware-skills-operator-runbook.md](vmware-skills-operator-runbook.md).

This is separate from the nested VCF 8 lab on the Proxmox host (`vmbrlab`,
`vcf8.lab.knowledgeondemand.net`, VMs 191–194 + CT 190).

---

## Platform

| Item | Value |
|---|---|
| Hardware | 3× Minisforum nodes (`hyp01`–`hyp03`) |
| ESXi | **9.1.1.0 build 25714478** (all three hosts) |
| vCenter | **9.1.1.0 build 25712839** |
| Cluster | `lab01-cluster-001` (MoID `domain-c9`), 3 hosts, DRS on, HA on |
| Per host | 16 cores, 187 GB presented memory |
| Memory tiering | software tiering on all hosts, **1:1 DRAM:NVMe** (93.7 GB DRAM + 93.7 GB NVMe tier = 187 GB, 2.0× uplift) |
| Cluster total | 48 cores, ~561 GB presented (~281 GB of it DRAM) |
| DNS domain | `lab.knowledgeondemand.net` |
| Certificates | Custom CA `knowledgeondemand-LabRoot-CA`; ESXi certificate mode is **custom** (managed through VCF Operations) |
| Host Secure Boot | Off (by design; the vSphere SCG v9 scan flags it on all three hosts) |

## Hosts

| Host | FQDN | Mgmt IP |
|---|---|---|
| hyp01 | `hyp01.lab.knowledgeondemand.net` | 172.16.10.145 |
| hyp02 | `hyp02.lab.knowledgeondemand.net` | 172.16.10.143 |
| hyp03 | `hyp03.lab.knowledgeondemand.net` | 172.16.10.141 |

## VCF management components

| Component | VM | vCPU | RAM | IP | Guest FQDN | Host* |
|---|---|---|---|---|---|---|
| vCenter Server | `vcsa` | 4 | 21 GB | 172.16.10.129 | `vcsa.lab.knowledgeondemand.net` | hyp02 |
| SDDC Manager | `sddc-manager` | 4 | 16 GB | 172.16.10.133 | `sddc-manager.lab.knowledgeondemand.net` | hyp03 |
| NSX Manager | `nsxa` | 6 | 24 GB | 172.16.10.132 | `nsxa` (short name only) | hyp01 |
| VCF Operations | `ops` | 2 | 8 GB | 172.16.10.122 | `ops.lab.knowledgeondemand.net` | hyp03 |
| VCF Operations cloud proxy | `opscollector` | 4 | 16 GB | 172.16.10.123 | `opscollector.lab.knowledgeondemand.net` | hyp03 |
| VCF License Server | `licsrv` | 2 | 4 GB | 172.16.10.134 | `licsrv.lab.knowledgeondemand.net` | hyp02 |

NSX Manager is deployed (VCF installs it) but NSX networking is **not in active use**.

### VCF platform services (6 VMs)

| VM | vCPU | RAM | IP | Host* |
|---|---|---|---|---|
| `platform-lmsgx` | 4 | 10 GB | 172.16.10.160 | hyp03 |
| `platform-dlq9c` | 4 | 10 GB | 172.16.10.162 | hyp02 |
| `platform-8n62z` | 10 | 16 GB | 172.16.10.163 | hyp02 |
| `platform-pfnmx` | 10 | 16 GB | 172.16.10.164 | hyp01 |
| `platform-rptxg` | 4 | 10 GB | 172.16.10.165 | hyp01 |
| `platform-c5t7j` | 10 | 16 GB | 172.16.10.167 | hyp03 |

Total 42 vCPU / 78 GB. The addresses are not contiguous (.161 and .166 are unused).
The `vcf-services-runtime-template-9.1.1.0.25714471` VM (6 vCPU, 24 GB, powered
off) is the template these are built from — do not delete it.

## Workload VMs

| VM | vCPU | RAM | IP | Network | Host* | Purpose |
|---|---|---|---|---|---|---|
| `devvm01` | 8 | 48 GB | 172.16.70.10 | `dev-vm1-vlan70` | hyp01 | dev VM |
| `devvm02` | 8 | 48 GB | 172.16.71.10 | `dev-vm2-vlan71` | hyp02 | dev VM |
| `devvm03` | 8 | 48 GB | 172.16.72.10 | `dev-vm3-vlan72` | hyp03 | dev VM |

### Parked (normally powered off)

| VM | vCPU | RAM | Notes |
|---|---|---|---|
| `llmbench01`–`03` | 12 each | 24 GB each | LLM benchmark VMs |
| `wf-runner-01`–`03` | 8 each | 16 GB each | Workforce runners; the CPU agent tier was retired 2026-10-15 |

Powering these on costs 60 vCPU / 120 GB in total — check headroom first.

\* Host placement is point-in-time. DRS can move VMs, so confirm with
`vmware-monitor vm info <name>` before relying on it (e.g. during a host outage).

## Storage

| Datastore | Type | Capacity | Notes |
|---|---|---|---|
| `vsan-lab01` | vSAN **ESA** | 11,178 GB | compression on, dedup off (the ESA default) |
| `hyp01-local` | VMFS | 803 GB | per-host local disk |
| `hyp02-local` | VMFS | 803 GB | per-host local disk |
| `hyp03-local` | VMFS | 803 GB | per-host local disk |

The NVMe memory-tier devices are separate from vSAN; vSAN health does not cover them.

## Networks

| Network | Subnet / VLAN | Use |
|---|---|---|
| `SDDC-DPortGroup-VM-Mgmt` | 172.16.10.0/24 | VCF management VMs (vCenter, SDDC Manager, NSX, Ops, platform) |
| `SDDC-DPortGroup-Mgmt` | 172.16.10.0/24 | host management (vmk) |
| `SDDC-DPortGroup-vMotion` | — | vMotion (vmk only) |
| `SDDC-DPortGroup-VSAN` | — | vSAN (vmk only) |
| `dev-vm1-vlan70` | VLAN 70, 172.16.70.0/24 | devvm01 |
| `dev-vm2-vlan71` | VLAN 71, 172.16.71.0/24 | devvm02 |
| `dev-vm3-vlan72` | VLAN 72, 172.16.72.0/24 | devvm03 |
| `lab01-cluster-00-DVUplinks-20` | — | distributed switch uplinks |

**Reaching the lab from the Proxmox host:** `172.16.0.0/16 via 192.168.6.11`
(the MikroTik). CT 190 `lab-svc` reaches it through the host and has a
`/etc/hosts` entry for `vcsa`.

## Access

| Purpose | How |
|---|---|
| Read-only monitoring / health / compliance | `vmware-monitor`, `vmware-storage`, `vmware-harden` on CT 190 `lab-svc`, account `svc-vmware-ro@vsphere.local` (built-in Read-only role at the root). Also wired into opencode as MCP servers. |
| Certificate operations | VCF Operations only — never out-of-band on vCenter or ESXi |

## Verify / refresh

Run on CT 190 (`pct exec 190 -- …`); the binaries are in `/opt/vmware-venv*/bin/`:

```bash
vmware-monitor inventory hosts        # hosts, cores, memory, VM counts
vmware-monitor inventory vms          # every VM: power, vCPU, RAM, guest OS, IP
vmware-monitor inventory datastores   # datastores and capacity
vmware-monitor inventory networks     # port groups
vmware-monitor vm info <name>         # host placement, guest FQDN, disks
vmware-monitor memory tiering         # DRAM/NVMe split per host
vmware-storage vsan efficiency lab01-cluster-001
```

Version and build, with no credentials (from the Proxmox host): POST a SOAP
`RetrieveServiceContent` to `https://<host-ip>/sdk` and read `about.fullName`.

When a value here stops matching, update it and the "Last verified" date.
