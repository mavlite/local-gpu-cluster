# Ubiquiti lab network migration: design

Status: approved by the owner 2026-11-05, including the gateway path below for reaching the
lab from `192.168.6.x`. Amended the same day to drop the CRS309's
home-LAN link. Nothing in this document has been applied.

## Intent

Make UniFi the primary network for the VCF lab side and retire the CSS610. The CRS309 stays
because it provides the 10G SFP+ fabric and the lab's routing. The owner wants one place to
see the network (the Cloud Gateway) and wants vSAN and NSX traffic separated from management
and vMotion traffic.

## Goals

1. UniFi carries TrueNAS's lab traffic, `dns01`, `mgmt`, the Intel NUCs and the lab's connection
   to the gateway.
2. The CRS309 connects to the Flex 2.5G 8 PoE at 10G over a DAC and keeps routing the lab
   VLANs. That DAC becomes its only link outside the lab: its `192.168.6.11` home-LAN link is
   removed (owner's decision), after the replacement path below is proven.
3. The CSS610 is retired, after everything on it has been identified and moved.
4. On each Minisforum host, vSAN and TEP normally use one X520 port, and management and
   vMotion use the other, with failover between them.
5. Each Minisforum host gets a second, emergency management path over its RTL8125 that does
   not depend on the CRS309 or the VDS.

## Non-goals

- Moving the Proxmox GPU host (`gpu-cluster`, `192.168.6.175`) or anything on it. It stays on
  the Google Fiber LAN for now. The only change on it is the next hop of its existing
  `172.16.0.0/16` route, made with the owner's go-ahead at the time.
- Moving `192.168.6.0/24` to UniFi, renumbering it, or removing the Google Fiber router.
  The gateway stays behind that router at `192.168.6.89`, so double NAT remains.
- Running vSAN, TEP, vMotion or `vmk0` on 2.5G, and the VCF NIC speed override.
- GPUs in the VCF lab. The owner dropped the 3060 plan on 2026-11-05.
- Jumbo frames on any UniFi switch.

## Anti-goals (a failure even if the rest works)

- The home LAN and the UniFi network joined at layer 2 through the CRS309.
- A second DHCP server or gateway appearing on any lab VLAN.
- A broken PXE path or depot, found only when a host needs rebuilding.
- Proxmox or the workstation losing the lab, or dev VMs losing their isolation or the
  `devvm-tester-filecopy` path.
- vSAN or TEP traffic crossing a 1500-MTU UniFi link.
- The UCG's WAN side opened to anything beyond the named `192.168.6.x` hosts and the lab
  subnets they need.
- The CRS309's home-LAN link removed before the new path has been tested from every client
  that uses it.

## Current state

### Verified from owner screenshots, 2026-11-05

- Google Fiber router `192.168.6.1` connects to UCG Ultra port 5 (WAN1) at GbE. The UCG's
  WAN address is `192.168.6.89`. UniFi OS 5.1.35, Network 10.6.109.
- UCG port 2 connects to Flex 2.5G 5 port 4 at GbE.
- Flex 2.5G 5 port 5 connects to Flex 2.5G 8 PoE port 8 at 2.5 GbE.
- Flex 2.5G 8 PoE port 1 connects to Flex Mini port 1 at GbE.
- UniFi networks: `CoreNetwork-Mgmt` VLAN 1 `10.83.1.0/24`, `CleanRoom-Mgmt` VLAN 2
  `10.83.2.0/24`, `CleanRoom-Talos` VLAN 3 `10.83.3.0/24`. Teleport VPN is enabled.
- Switch addresses: Flex 2.5G 5 `10.83.1.2`, 8 PoE `10.83.1.148`, Flex Mini `10.83.1.189`.
- Offline clients `pve02`, `pve03` and `pve04` are listed on Flex 2.5G 5 ports 3, 2 and 1.

### From repository docs and notes, not re-checked live

See `docs/vcf-deployment-reference.md` and `docs/vcf-lab-build-record.md`.

- CRS309-1G-8S+IN, RouterOS 7.20, `192.168.6.11`, gateway `.1` on lab VLANs, firewall, DHCP
  and TFTP. Changes to it are deliberate and manual; no automation touches it.
- Hosts use `sfp-sfpplus2` to `sfp-sfpplus7` (`hyp01` 2/3, `hyp03` 4/5, `hyp02` 6/7).
- VLANs 10, 20, 30, 40, 50, 60, 70-72 and untagged 99 on host ports, at `mtu=9000 l2mtu=9196`.
- VDS `lab01-cluster-001-vds-001`, MTU 9000, uplinks `vmnic0` and `vmnic1`, X520. Provisioning
  uses `vmnic0` over PXE.
- Proxmox and the workstation route `172.16.0.0/16` via `192.168.6.11`. Things that depend on
  that route or address, found in the repo:
  - CT 190 `lab-svc` (the VMware MCP servers, `10.50.10.100` on `vmbrlab`) reaches the lab
    through the Proxmox host's route (`docs/vmware-skills-operator-runbook.md`).
  - The SE-QA VM's firewall test adds a temporary route via `192.168.6.11`
    (`scripts/71-vm-se-qa-firewall.sh`).
  - `scripts/files/wf_boundary_proof.py` and its test check that `192.168.6.11` is denied.
  - `docs/vcf-deployment-reference.md`, `vcf-spec-tools/inventories/lab01-physical.yaml` and
    older plans name it.
- TrueNAS (the Beelink ME Mini, confirmed by the owner) serves the depot and boot media at
  `172.16.10.50:8080`. It has a VLAN 10 interface and a home-LAN interface, default route via
  `192.168.6.1`. It previously failed lab clients because of a `/32` on VLAN 10, and its Docker
  bridges use `172.16.0-4.x`.
- `dns01` (`172.16.10.150` and `192.168.6.197`) and `mgmt` (`172.16.10.21`) are in the lab, and
  their physical connections are not recorded.

### Unknown; must be captured in Phase 0

- The CRS309's full configuration: bridge, VLAN membership, routes, firewall, NAT, DHCP.
- Which CRS309 port is the `192.168.6.11` uplink, and which of `sfp-sfpplus1`, `sfp-sfpplus8`
  and `ether1` are free.
- The CRS309's default route and any NAT on its home-LAN interface, since both move to the
  transit VLAN.
- Every `192.168.6.x` client with a route to `172.16.x` or traffic to `192.168.6.11`, beyond the
  repo list above (the owner's other devices, Proxmox guests).
- Whether the UCG masquerades traffic from routed (static-route) subnets going out its WAN, and
  whether a firewall rule can admit `192.168.6.x` sources to internal networks. Both are
  unconfirmed for UniFi Network 10.6.
- The CSS610's management address, VLAN layout, and everything plugged into it.
- How TrueNAS, the NUCs, `dns01`, `mgmt`, Wi-Fi and the workstation are cabled today.
- Which devices use the Google Fiber router's LAN ports, how many are free, and whether Proxmox
  or the workstation reach that router through the CSS610. The owner doesn't know yet; this is
  the first Phase 0 step, and the builder stops if ports run short.
- What `dns01` and `mgmt` use their `192.168.6.x` addresses for. The deployment reference says
  VLAN 10 has no internet and that `dns01` resolves public names, so that address may be its
  internet path.
- Whether the 8 PoE's 10G RJ45 and SFP+ can run together. Retail listings call them a combo.
- Whether the RTL8125 appears as a NIC on ESX 9.1.1 today. Notes suggest it does not.

## Target design

### Physical

| Link | Speed | Notes |
|---|---|---|
| Google Fiber router → UCG port 5 | GbE | Uses the cable that goes to the CSS610 today (owner's decision); confirm in Phase 0 that this cable is the router link |
| TrueNAS port 2 → Google Fiber router LAN port | 2.5G NIC, router port speed unknown | Keeps its `192.168.6.x` address without touching UniFi (builder's call) |
| UCG LAN port → 8 PoE | GbE | Moved off the Flex 2.5G 5 (builder's recommendation, marked below) |
| CRS309 free SFP+ port → 8 PoE port 10 (SFP+) | 10G | New; DAC (owner's choice), must suit both ends |
| CRS309 `192.168.6.11` uplink | Removed | Owner's decision; unplugged only after the Phase 4b cutover passes |
| `dns01` and `mgmt` → 8 PoE | Up to 2.5G | Owner's decision; VLAN 10, plus the services network below |
| Flex 2.5G 5 port 5 → 8 PoE port 8 | 2.5G | Unchanged |
| `hyp01`-`hyp03` RTL8125 → Flex 2.5G 5 | 2.5G | Emergency path only; leaves one spare port |
| Flex Mini port 1 → 8 PoE port 1 | GbE | Unchanged |
| NUCs (`pve02`-`pve04`) → Flex Mini | GbE | Moved from the Flex 2.5G 5; being wiped, network chosen at rebuild |
| TrueNAS port 1 → 8 PoE | 2.5G | VLAN 10; see below |
| `hyp01`-`hyp03` X520 → CRS309 | 10G | Unchanged, and PXE stays on `vmnic0` |

### The CRS309-to-UniFi link

The link carries exactly two VLANs, both tagged:

1. A new transit VLAN, routed by the CRS309 and the UCG.
2. VLAN 10, layer 2 only on UniFi, so TrueNAS keeps `172.16.10.50`. UniFi must have no gateway
   and no DHCP on it; the CRS309 stays its gateway.

It must not carry the home LAN, VLAN 99, vSAN, vMotion or TEP. VLANs 20, 30, 40 and 50-72 stay
on the CRS309.

Routes: the UCG sends `172.16.0.0/16` to the CRS309's transit address. Once the home-LAN link
goes, the CRS309's default route points at the UCG's transit address, and its management
address becomes its transit address. Lab internet access then goes CRS309 → UCG → Google Fiber
router, so it is double NAT like the rest of UniFi.

Phase 0 found (2026-11-05 export) that the CRS309 masquerades only dev VLANs 70-72; no other lab
VLAN is NATed. So VLAN 10's "no internet" today comes from missing NAT, not from a rule. If the
UCG masquerades routed subnets, VLAN 10 would gain internet after cutover. The UCG therefore
blocks `172.16.0.0/16` to the internet explicitly, allowing only traffic that the CRS309 has
already masqueraded to its transit address (dev VLANs, and the CRS309's own NTP and DNS).

Phase 0 also found every lab VLAN set `tagged=all` on the CRS309's bridge, including the free
`sfp-sfpplus8`. Before the DAC goes in, the CRS309's VLAN table must list ports explicitly:
host ports `sfp-sfpplus2`-`7` and the bridge for VLANs 10-72, and `sfp-sfpplus8` for VLANs 10
and the transit VLAN only. Otherwise vSAN, TEP and the untagged home-LAN bridge leak into UniFi.

The CRS309's dev VLAN "deny-all-private" rules must still block the `10.83.x` networks. Verify
this from a dev VM; don't assume it from the rule names.

### Access across the link (builder's call)

Block by default on the UCG and the CRS309, then allow only:

- The new services network below to the internet, and to VLAN 10 if a named service needs it.
- Teleport clients to the emergency network.
- `CleanRoom-Mgmt` and `CleanRoom-Talos` get no lab access in either direction. That project
  includes scanning tools such as Metasploit, OpenVAS and nmap, and a console documented with
  default `admin` / `admin` credentials.
- The lab starts no connections into UniFi networks, and Proxmox and the workstation get no new
  paths into them.

### Reaching the lab from `192.168.6.x` (owner's decision: no CRS309 home-LAN link)

The Proxmox GPU host (`192.168.6.175`), its guests that route through it (CT 190), and the workstation reach the lab
through the UCG's WAN side instead of `192.168.6.11`:

- Each client's `172.16.0.0/16` route changes its next hop from `192.168.6.11` to the UCG WAN
  address `192.168.6.89`. Reserve that address on the Google Fiber router first, so it can't
  change under the routes.
- The UCG admits traffic from those named source addresses, to the lab subnets they need, with
  a firewall rule from its WAN side. No port forwarding, no other sources.
- Replies leave the lab through the CRS309's default route to the UCG and go back out the WAN as
  part of the same connection.
- The dev VM `devvm-tester-filecopy` path to `192.168.6.175` tcp/21422 now goes out through the
  UCG. If the UCG masquerades it, the source the Proxmox side sees changes to `192.168.6.89`;
  check the Proxmox side's rules for that before cutover.
- Lab-side connections to `192.168.6.x` other than that path stay blocked, as today.

This adds the UCG to the path between Proxmox and the lab, so a UCG reboot or firmware update
now interrupts CT 190 and the workstation's lab access too.

If the UCG can't admit WAN-side traffic this way, stop and bring it back to the owner; the
fallbacks are keeping the CRS309 link, or moving the workstation onto UniFi.

### `dns01` and `mgmt`

Phase 0 found that `mgmt` is a standalone ESXi host with one 1G NIC, plugged into CSS610
Port4. One `vSwitch0` carries the home LAN untagged (`vmk0` `192.168.6.167`) and VLAN 10 tagged
(`vmk1` `172.16.10.21`). It runs `dns01` (`.197` and `172.16.10.150`) and, on the home LAN only,
`Panel` (`192.168.6.203`), `Wings01` (`.60`) and `traefik` (`.181`), which look like the game-
server panel stack.

The whole host moves to the 8 PoE (owner's decision), panel stack included (owner's decision,
2026-11-05). Its one cable carries VLAN 10 tagged and the services network untagged. The lab
addresses stay on VLAN 10, so hosts keep the same DNS, NTP and CA addresses. `vmk0`, `dns01`'s
second leg, Panel, Wings01 and traefik get services-network addresses.

The panel stack adds work before the move:

- Capture any Google Fiber router port forwards to `.181`, `.203` or `.60`. Recreate them on
  the UCG, and forward the same ports from the Google Fiber router to `192.168.6.89`.
- The agent runner (`192.168.6.78`) and GamePilot (behind `192.168.6.175`) reach the panel
  today. The UCG WAN-side rule must admit them to the panel's new addresses, and any configs
  that name the old addresses must change. None were found under `Documents/GitHub` by IP, so
  check hostnames and DNS records too.

Internet access for all of them comes from the gateway (owner's decision): the services network,
routed and NATed by the UCG. Before moving them:

- List what uses `192.168.6.197` and `192.168.6.167`, including repo scripts, AD DNS records and
  CA settings, and repoint it.
- Confirm that `dns01` can still resolve public names and that its NTP source still works.
- Check whether `mgmt` needs any VLANs beyond 10. If so, change the link's VLAN list deliberately
  and don't carry vSAN or TEP.

**New dependency:** a reboot or firmware update of the 8 PoE, or a fault on the 10G link, cuts
VCF off from DNS, NTP and the depot. Turn off automatic UniFi firmware updates and treat 8 PoE
updates as lab maintenance.

### NUCs

The NUCs are the `Talos-CleanRoom` Proxmox nodes. The owner will re-image and wipe them
(2026-11-05), so this project only cables them to the Flex Mini and makes no changes on them,
and nothing on them needs preserving. Their old addresses and VLANs (2 and 3) are not a
constraint; their ports get whatever network the owner picks when rebuilding them, and stay
isolated from the lab until then.

The Flex Mini is 1G, and all NUCs share its 1G uplink to the 8 PoE. Worth knowing before
choosing what runs on them next.

### TrueNAS

Phase 0 found that TrueNAS plugs only into the Google Fiber router today (owner, 2026-11-05).
Its VLAN 10 leg reaches the CRS309 as tagged frames through the router's LAN switch, CSS610
Port1 and the CRS309's `sfp-sfpplus1`, and the depot did not answer on 2026-11-05.

Port 1 carries VLAN 10 (`172.16.10.50/24`) at MTU 1500. Port 2 keeps its `192.168.6.x` address
(owner's decision) by plugging directly into a Google Fiber router LAN port, and remains the
default route for internet access. It must not connect to a UniFi switch. No LACP.

Before moving it: confirm the VLAN 10 netmask is `/24`, and check Docker's `default-address-pools`.
After moving it, test from a lab VLAN as well as `192.168.6.x`, including a depot fetch and the
PXE boot files.

### X520 traffic split

Change active and standby uplinks on the VDS port groups so that:

| Traffic | Active | Standby |
|---|---|---|
| `SDDC-DPortGroup-VSAN`, NSX TEP | `vmnic0` | `vmnic1` |
| `SDDC-DPortGroup-Mgmt`, `SDDC-DPortGroup-VM-Mgmt`, `SDDC-DPortGroup-vMotion` | `vmnic1` | `vmnic0` |

The dev VM port groups are left as they are. Teaming for TEP comes from NSX, so it must be
changed through NSX, not on the VDS. First confirm that VCF and NSX do not report or revert
this as drift. If they do, stop and bring it back to the owner.

The CRS309 is still a single point of failure for all of this traffic; this split separates
traffic but adds no switch redundancy.

### Emergency management path

- Install the Realtek Fling (`VMware-Re-Driver` 1.101.01, tech preview) on one host. Run it for
  an agreed soak period before the others.
- Add the driver to the cluster's vLCM image so remediation does not remove it. How to do this
  on VCF 9.1.1 is unconfirmed and is part of this phase.
- Create a standard vSwitch with the RTL8125 as its only uplink and a new VMkernel adapter
  tagged for management, on a new UniFi network routed by the UCG. MTU 1500.
- `vmk0` stays on the VDS. Nothing else uses this path.

**Routing constraint:** ESXi has one default gateway on its default TCP/IP stack, and it stays
on VLAN 10. Replies from the emergency adapter to any other subnet would leave through the
CRS309, and would fail when it is down. So:

- Reach the emergency adapter from a device on its own subnet, or from one dedicated source
  subnet with a static route on the host through the UCG.
- The Teleport client subnet is the planned source. Teleport can be reached without the CRS309.
- Do not add a static route for `192.168.6.0/24` on hosts. It would also redirect replies to
  connections made to `vmk0`.

Confirm that the added management tag does not change HA behavior. With vSAN enabled, HA should
use the vSAN network, but verify this on the cluster before tagging a second adapter.

## Phases

Each phase has its own check and rollback. Stop on any unexpected result.

**Phase 0: capture.** First, the Google Fiber router's port map. Then a CRS309 read-only export
with secrets hidden (the builder runs it; owner approved), the CSS610 backup (the owner exports
it from the SwOS web UI), UniFi backup, a cable map, the `192.168.6.x` client list above, and a
VCF health baseline. Commit the CRS309 export to the repo after checking it for secrets; this
also closes the gap listed in `docs/vcf-lab-build-record.md`.

**Phase 1: UniFi layout.** Move the gateway link to the 8 PoE. Create the transit VLAN and the
VLAN 10 definition. Check: all three switches stay adopted and internet works. Rollback: move
the cable back.

**Phase 2: 10G link.** Connect the CRS309 and add the routes. Check: 10G link speed, no spanning
tree or MAC flapping, `172.16.x` reachable from a UniFi test client, `192.168.6.x` clients still
reaching the lab through `192.168.6.11`, dev VM isolation unchanged. Rollback: disable the CRS309
port.

**Phase 3: TrueNAS.** Do this in a quiet window with no host rebuild or depot use in progress.
Check from a lab VLAN and from `192.168.6.x`. Rollback: move the cables back.

**Phase 4: NUCs, then `dns01` and `mgmt`.** Re-cable the NUCs to the Flex Mini (they are being
wiped, so no checks on them beyond link up). Then move `dns01` and `mgmt` one at a time. Move `dns01` and `mgmt` in
a lab maintenance window after repointing their old addresses, and check DNS, NTP and vCenter
after each. Rollback: move the cable back and restore the old address.

**Phase 4b: home-LAN cutover.** Only after Phases 3 and 4: the CRS309's home-LAN link (`sfp-sfpplus1` to CSS610 SFP+1) also carries VLAN 10 to `mgmt` (and so `dns01`) and to TrueNAS today, so unplugging it earlier would cut DNS for the whole lab (Phase 0 finding, 2026-11-05). Add the UCG WAN-side rule. On the workstation, test one lab
address with a temporary single-host route via `192.168.6.89`, then switch its `172.16.0.0/16`
route. Then, with the owner's go-ahead, the same on the Proxmox host, and check CT 190's MCP
servers and the tester file-copy path. Then move the CRS309's default route and management
address to the transit VLAN, and only then unplug its home-LAN link. Update the repo files that
name `192.168.6.11`, including the SE-QA test route and the boundary-proof check. Rollback:
replug the link and restore the old routes.

**Phase 5: retire the CSS610.** Move its Google Fiber uplink cable to UCG port 5 last, after
confirming nothing still needs the CSS610. This briefly interrupts the gateway's internet. If
the home-LAN devices depend on the CSS610, stop and bring it back to the owner. Keep it and its
backup for an agreed period.

**Phase 6: X520 split.** Run the drift check, then change one port group at a time, watching vSAN
health and connectivity. Test failover on one host by disabling one CRS309 port during a quiet
period. Keep DRS on Manual. Rollback: restore the teaming order from the Phase 0 record.

**Phase 7: emergency path.** One host, in maintenance mode, then a soak, then the others. Check:
reach the host through this path with the host's CRS309 ports disabled, in a planned test.
Rollback: `esxcli software component remove -n VMware-Re-Driver`, then reboot.

## Constraints

- CRS309 changes are made deliberately and by hand. Read-only `print` and `export` only, unless
  the owner asks for more. Credentials live in `~/.vcflab/credentials.env` and must never be
  printed. When its management address changes in Phase 4b, update the address there by key
  name only.
- RouterOS 7.20: `print where` and `find` can silently match nothing, and `/ping interface=` can
  give false negatives.
- No host or VCF change while it could affect VMs without the owner's go-ahead. Dev VMs must
  never affect VCF.
- Host advanced settings belong in vSphere Configuration Profiles, because `esxcli` changes are
  reverted as drift.
- Before any host goes into maintenance mode, check the tier device is healthy. Keep DRS on
  Manual. Secure Boot is off on these hosts and stays off.

## Decided by the owner

- UniFi primary, CRS309 kept, CSS610 retired.
- CRS309 to the 8 PoE's SFP+ port; Minisforum RTL8125s to the Flex 2.5G 5; NUCs to the Flex Mini.
- Proxmox out of scope for now; TrueNAS in scope, keeping a `192.168.x.x` address for now.
- The gateway takes the uplink cable that goes to the CSS610 today, and LAN access comes from it.
- `dns01`, `mgmt` and all their functions move to UniFi networks.
- The NUCs are `pve02`-`pve04` from the `Talos-CleanRoom` project.
- No GPUs in the VCF lab.
- Split traffic across the X520 ports, with the RTL8125s as secondary management using the Fling.
- Jumbo frames stay on the CRS309.
- The design and the builder's calls below approved as written (2026-11-05).
- The CRS309 connects outside the lab only through the DAC to the 8 PoE; its `192.168.6.11`
  home-LAN link is dropped.
- The Proxmox GPU host (`gpu-cluster`, `192.168.6.175`, web UI `:8006`) stays out of scope
  apart from its lab route. The Talos NUCs will be re-imaged and wiped, so no changes are made
  on them.
- Phase 0: the builder may run a read-only CRS309 export; the owner exports the CSS610 backup.
- The Google Fiber router's free ports are checked in Phase 0.

## Builder's calls, for the owner to check

- Moving the gateway link from the Flex 2.5G 5 to the 8 PoE.
- A routed transit VLAN instead of carrying every lab VLAN into UniFi.
- TrueNAS on the 8 PoE, without LACP.
- Which traffic shares which X520 port (based on the owner's grouping of management and vMotion).
- Teleport as the emergency path's source subnet.
- TrueNAS port 2 going directly to a Google Fiber router LAN port.
- A new UCG-routed services network for `dns01` and `mgmt`.
- The default-deny access rules, including CleanRoom isolation from the lab.
- Runbook model: the builder writes steps and runs read-only checks; the owner makes CRS309 and
  UniFi changes by hand, and the builder verifies each step.
- Phase order, and doing the VCF changes last.
- The NIOC vMotion cap is left out of this project; it would be a separate, optional change.
- Added after approval: reaching the lab from `192.168.6.x` through the UCG's WAN side with
  per-client routes to `192.168.6.89`, a source-limited WAN-side rule, and the Phase 4b cutover
  order.

## Left to the builder

VLAN IDs and subnets for the transit and emergency networks (avoid `10.78`, `10.81` and `10.82`,
which the Proxmox host uses, and the VLAN IDs above), spanning tree priority, SFP+ module choice,
UniFi port profile details, and soak length unless the owner sets one.
