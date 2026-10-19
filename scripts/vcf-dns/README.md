# Lab DNS records

Creates and verifies the forward and reverse records the VCF lab components
need, on the Windows DNS server `dns01` (172.16.10.150).

```powershell
.\Set-VCFLabDnsRecord.ps1                                   # dry run, audits the set
.\Set-VCFLabDnsRecord.ps1 -Apply -Credential (Get-Credential)
```

Dry run unless `-Apply`. A record already correct is left alone; a record whose
address disagrees with the table is reported as a **conflict** and not changed
without `-Force`.

## Not PowerCLI

PowerCLI talks to vSphere and has no DNS cmdlets. `dns01` is a Windows DNS
server — RPC 135, LDAP 389 and WinRM 5985 all answer, so it is a domain
controller — and it is managed with the **`DnsServer`** module from RSAT over a
CIM session:

```powershell
Add-WindowsCapability -Online -Name Rsat.Dns.Tools~~~~0.0.1.0
Get-Command -Module DnsServer          # should list Add-DnsServerResourceRecordA
```

That module is **not currently installed** on the workstation, so install it
first. WinRM to `dns01` is reachable from there; port 53 is not, which is also
why `nslookup` against the lab fails from Windows while working fine from the
Proxmox host.

## What the two products need, and why they differ

| | Logs 9.1.1 | Networks 9.1.1 |
|---|---|---|
| Shape | supervisor workload behind an ingress | two OVA appliances |
| DNS needed | one name + one VIP | one name per appliance |
| Source | `ingress.component.fqdns[]` and `ingress.component.vips.ipv4[]` in `configuration-schema-operations-logs-9.1.1.0.25679624.yaml` | `*-platform.ova` and `*-collector.ova` in the catalogue |

This is the reason the record set is not symmetrical. Logs needs a single
ingress VIP, exactly like `fleet` → 172.16.10.116. Networks needs two
addresses, and only the platform one existed.

## State (2026-09-28)

All three records exist, verified resolving in both directions from inside the
lab, not just read back from the DNS console:

| Record | Address | Status |
|---|---|---|
| `log-insight.knowledgeondemand.net` | 172.16.10.137 | A + PTR |
| `vrni.knowledgeondemand.net` | 172.16.10.136 | A + PTR |
| `vrni-collector.knowledgeondemand.net` | 172.16.10.138 | A + PTR, created 2026-09-28 |

Nothing answers on .136, .137 or .138 yet — the names are reserved ahead of the
deployments. 172.16.10.138 was verified free before use (no A, no PTR, no ping).
If you move the collector, `.139` and `.140` are also clear; avoid `.142` and
`.144`, which sit between the hypervisors at `.141`, `.143` and `.145`.

So this script is now an audit rather than a creator: a dry run should report
all three already correct. It stays useful for the next component.

## Zones

- Forward: `knowledgeondemand.net` and `lab.knowledgeondemand.net`, both SOA
  `dns01`. The VCF components split across both — `fleet`, `ops`, `vcsa`,
  `sddc-manager` and `idb` are in `lab.`, while `log-insight`, `vrni`,
  `opsfm` and `opsproxy` are in the parent zone.
- Reverse: `10.16.172.in-addr.arpa`, SOA `dns01`.

`dns01` drops direct SOA queries for the reverse zone, so `dig ... SOA` times
out even though the zone is healthy — read the authority section of any PTR
query instead. Do not take that timeout as evidence the zone is missing.

Both directions matter: VCF checks reverse resolution during bring-up, and a
missing PTR has already caused a bring-up failure here that 300+ unit tests
did not catch.
