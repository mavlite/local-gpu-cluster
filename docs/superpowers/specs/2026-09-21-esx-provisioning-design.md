# Stage 1: ESX provisioning artifacts from the inventory

**Date:** 2026-09-21
**Status:** proposed
**Code:** `vcf-spec-tools/`
**Grounded in:** `[[vcf_baremetal_no_bmc]]` (spike, 2026-09-17)

## Goal

One inventory file already renders an `SddcSpec` (stage 3). Make the **same
file** render the artifacts that get ESX onto bare metal (stage 1), so the
operator describes the lab once.

Deliberately small. This is a text generator with no network access, buildable
and testable today, and it is the "from bare metal" end of the long-range goal.

## What the spike already settled

- **VCF does not image bare metal.** ESX must be installed and basic-configured
  before commissioning. VIA is gone; stateless ESX is unsupported.
- **UEFI HTTP boot is the mechanism** for 9.1 — no TFTP, no iPXE chainload.
- **Kickstart remains first-class**: `ks=http://…`, with boot-time `ip=`,
  `netmask=`, `gateway=`, `vlanid=`.
- Commissioning target is **ESX 9.1.1.0 build 25714478**; pin `mboot.efi` from
  the newest ISO.
- The ESX installer **does not validate TLS** for `ks=https://`, so the
  provisioning VLAN is the trust boundary, not the transport.
- A DHCP + HTTP + per-MAC config generator beats MAAS/Foreman/Tinkerbell, none
  of which can deploy ESX 9.

## Artifacts generated

Per host: a **kickstart file**. Per host: a **UEFI HTTP boot config** keyed by
MAC. Plus a **manifest** listing what to place where on the boot server.

Nothing is written to a boot server by this tool; it emits files and tells the
operator where they go. Publishing is theirs.

## Inventory additions

Two genuinely new decisions, both per host, both currently unexpressible:

- `provisioningMac` — the MAC of the NIC that UEFI HTTP boots. Required,
  because per-MAC config is the whole mechanism.
- `bootDisk` — which device ESX installs to. The lab has three NVMe devices
  (4 TB vSAN, 500 GB tiering, 1 TB other) and nothing currently says which is
  the boot target. Expressed as a kickstart disk selector
  (e.g. `--firstdisk=local`, or a device path), passed through rather than
  interpreted.

And one block, because a boot server is a property of the provisioning
environment rather than of any host:

- `provisioning.bootServerUrl` — base URL the kickstart is fetched from.
- `provisioning.esxVersion` — defaults to the commissioning target above.

`vmnics` already exists in the schema and is **read by no code today**; the
kickstart is its first consumer.

## Credentials

The kickstart needs a root password. It stays a `${reference}` in generated
output, exactly as in the rendered spec — the tool never emits a literal, and
the operator substitutes when publishing to the boot server.

This matters more here than in the SddcSpec: a kickstart sits on an unauthenticated
HTTP server for any host on the provisioning VLAN to fetch. A literal password in
that file is readable by anything that can reach it. State that in the manifest.

## Non-goals

- Running a DHCP or HTTP server, or writing to one.
- Powering hosts on. No BMC means a switched PDU with an API is the substitute,
  and that is its own work.
- BIOS configuration. The spike's human floor stands: one BIOS pass per chassis
  (UEFI, HTTP boot, NIC boot order, AC-recovery).
- Post-install configuration beyond what kickstart's `%firstboot` needs to make
  a host commissionable.

## Acceptance

- The example inventory renders a kickstart per host and a boot config per MAC.
- Generated kickstart contains no literal credential; `${esx_root}` survives to
  output and is masked from any finding.
- Network values are consistent with what the same inventory renders into the
  `SddcSpec` — one source, two artifacts, no drift.
- A host missing `provisioningMac` or `bootDisk` is a finding with a fix, not a
  silently incomplete file.
