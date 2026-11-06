# UniFi lab network migration: phase log

Plan: `docs/superpowers/plans/2026-11-05-ubiquiti-lab-network-migration.md`.
Design: `docs/superpowers/specs/2026-11-05-ubiquiti-lab-network-design.md`.

Each phase records the owner's steps, the rollback, the builder's read-only checks with their
real output, and any rulings. The owner makes every network change by hand.

## Phase 0: capture (started 2026-11-05)

### `192.168.6.x` clients that reach the lab (read 2026-11-05)

Proxmox GPU host (`gpu-cluster`, `192.168.6.175`):

```
172.16.0.0/16 via 192.168.6.11 dev vmbr0
/etc/network/interfaces:14  post-up   ip route replace 172.16.0.0/16 via 192.168.6.11 || true
/etc/network/interfaces:15  post-down ip route del 172.16.0.0/16 via 192.168.6.11 2>/dev/null || true
```

Guests the host source-NATs onto `vmbr0` (`iptables -t nat -S POSTROUTING`):

| Guest network | Bridge | Leaves as | What is on it |
|---|---|---|---|
| `10.81.0.0/24` | `vmbrar` | `192.168.6.78` | Agent runner VM 178 sandbox |
| `10.82.0.0/24` | `vmbrgp` | `192.168.6.175` | GamePilot conductor CT 160 |
| `10.78.0.0/24` | `sdxguest` | `192.168.6.175` | Tester VM 172 |
| `10.50.10.0/24` | `vmbrlab` | `192.168.6.175` | CT 190 `lab-svc` (VMware MCP servers) |

So a UCG rule admitting `192.168.6.175` also admits GamePilot, the tester VM and CT 190, as the
`192.168.6.11` route does today. The runner sandbox leaves as `.78` and is not admitted unless
named. Each guest's own firewall still has to deny the lab where intended (Review Focus 1).

Dev VM file copy: `-A PREROUTING -i vmbr0 -p tcp --dport 21422 -j DNAT --to-destination 10.78.0.10:22`.
No source match, so a UCG-masqueraded source (`192.168.6.89`) is still accepted by this rule.

Workstation (`192.168.6.226`; also on the fleet VPN `10.60.59.2`):

```
172.16.0.0  255.255.0.0  192.168.6.11  (persistent)
```

Repo files naming `192.168.6.11`: see the design's current-state list.

### CRS309 export (read 2026-11-05, read-only, `/export terse`)

Saved as `crs309-export-2026-11.rsc`; the live route table as `crs309-routes-2026-11.txt`.
Secret scan: no `password|secret|passphrase|psk|private-key|auth-key` values. The password key
is `CRS309_PASS` (written with spaces before `=`); a comment in the credentials file says to
rotate it.

What it shows:

- **Uplink:** `sfp-sfpplus1` holds `192.168.6.11/24`, and is also a bridge port. Default route
  `0.0.0.0/0 via 192.168.6.1` (static, active, resolved through `bridge`); `/export` omits it.
- **Free ports:** `sfp-sfpplus8` and `ether1` (both bridge ports, no host). Host ports
  `sfp-sfpplus2`-`7` came up when the owner powered the hosts on.
- **Every lab VLAN is `tagged=all`**: 10, 20, 30, 40, 50, 60 and 70-72 are tagged on every
  bridge port, including `sfp-sfpplus1` (home-LAN uplink), `sfp-sfpplus8` and `ether1`. Plugging
  the DAC into `sfp-sfpplus8` as things stand would carry all of them, vSAN and TEP included,
  plus the untagged home-LAN bridge, into UniFi. Phase 2 must first set explicit tagged port
  lists. (Ruling recorded in the plan ledger.)
- **NAT:** masquerade only for dev VLANs 70-72. No other lab VLAN is NATed, so "VLAN 10 has no
  internet" is because nothing NATs it, not because a rule blocks it. If the UCG masquerades
  routed subnets, VLAN 10 would gain internet after cutover unless a rule blocks it.
- **Firewall:** forward rules only (dev VM file copy accept, then dev VLANs dropped to all
  private ranges). No `input` rules, so the CRS309's own services answer from every VLAN.
- **DHCP:** servers on VLANs 10-60 and 99; `dns01` lease `172.16.10.150` (MAC
  `00:0C:29:48:EC:B2`). Every network hands out `ntp-server=` the CRS309's own address.
- **NTP and clock:** the CRS309 clock reads 2026-10-28 (real date 2026-11-05) and its NTP client
  status is `using-local-clock`. It resolves names through `dns01` (`/ip dns servers=172.16.10.150`)
  and serves NTP from its local clock (`use-local-clock=yes`). Anything taking NTP from the
  CRS309 via DHCP gets a clock about 8 days slow. Not changed; for the owner.

### `mgmt` and `dns01` (read 2026-11-05)

`mgmt` is the standalone ESXi host at `192.168.6.167` (HTTPS 200); `172.16.10.21` does not
answer HTTPS. Its VMs:

| VM | State | Networks | Home-LAN address |
|---|---|---|---|
| `DNS01.knowledgeondemand.net` | on | `WAN-Mgmt`, `VLAN10-Mgmt-PG` | `192.168.6.197` |
| `Panel` | on | `WAN-Mgmt` | `192.168.6.203` |
| `Wings01` | on | `WAN-Mgmt` | `192.168.6.60` |
| `traefik` | on | `WAN-Mgmt` | `192.168.6.181` |
| `installer` | off | `VLAN10-Mgmt-PG` | none |

`mgmt` has one NIC (`vmnic0`, 1G, `vSwitch0`, MTU 1500): home LAN untagged (`vmk0`
`192.168.6.167`, `WAN-Mgmt`) and VLAN 10 tagged (`vmk1` `172.16.10.21`, `VLAN10-Mgmt-PG`).

**Owner's decision (2026-11-05):** the panel stack moves to UniFi with `mgmt`. One cable to the
8 PoE; Panel, Wings01, traefik, `dns01`'s second leg and `mgmt`'s `vmk0` get UniFi addresses.
Before moving: any Google Fiber port forwards to `.181`/`.203`/`.60` move to the UCG (with a
forward to `192.168.6.89` on the Google Fiber router), and the runner (`192.168.6.78`) and
GamePilot (`192.168.6.175`) paths to the panel need the UCG WAN-side rule too.

Earlier note: moving `mgmt` onto UniFi also moves Panel, Wings01 and traefik off the home LAN. No repo
under `Documents/GitHub` names their addresses. `dns01`'s VLAN 10 leg most likely reaches the
CRS309 through the home-LAN uplink, since VLAN 10 is tagged there; confirm with the CSS610 port
map.

### Users of `192.168.6.197` and `192.168.6.167` (2026-11-06, read-only)

- Repo: `docs/vcf-deployment-reference.md:224,253,255`, `docs/vcf-lab-build-record.md:377`,
  `scripts/vcf-ca/New-Dns01RollbackClone.ps1:47` (`$MgmtHost = '192.168.6.167'` default) and
  `scripts/vcf-ca/README.md:158`. The script default must change when `mgmt`'s `vmk0` gets its
  UniFi address (Task 6).
- Lab CA (`knowledgeondemand-LabRoot-CA`, read from vCenter's certificate): CDP and AIA are LDAP
  plus `http://pki.knowledgeondemand.net/CertEnroll/…`. Queried from the Proxmox host (the
  workstation VPN blocks DNS), `pki` and `dns01.knowledgeondemand.net` both resolve to
  `172.16.10.150` from either `dns01` address. Revocation checks don't depend on
  `192.168.6.197`.

### CSS610 (owner screenshots, 2026-11-05)

CSS610-8G-2S+IN, SwOS Lite 2.20, at `192.168.6.10` (MAC `2C:C8:1B:9C:02:49`). Backup:
`~/Downloads/css610g_2.20.swb` (sha256 prefix `f1f3c16218903b28`); not committed, since a SwOS
backup may hold its password.

| Port | Link | What is on it (from the Hosts table) |
|---|---|---|
| Port1 | 1G | Google Fiber router (`00:bc:2f:31:ad:c0`) and the home LAN behind it, including the workstation (`b4:0e:de:7f:74:52`) |
| Port2 | 1G | UCG Ultra WAN (`58:d6:1f:71:2c:8d`, answers for `192.168.6.89` and `.90`-`.95`) |
| Port4 | 1G | `mgmt` ESXi (`1c:69:7a:6d:2d:32`) with `dns01`, Panel, Wings01, traefik |
| SFP+1 | 10G | CRS309 `sfp-sfpplus1`, with the VCF hosts and their VMs behind it |
| Port3, 5-8, SFP+2 | no link | |

VLANs 10, 20, 30, 40 and 50 are defined with every port a member; every port is in `optional`
mode, receive `any`, default VLAN 1. So the lab VLANs, vSAN and TEP included, are tagged towards
the Google Fiber router and the UCG WAN port today.

What this settles:

- The Proxmox host (`30:c5:99:ee:3a:5d`) is not on the CSS610, so retiring it does not touch
  Proxmox.
- The cable "going to the CSS610" from the Google Fiber router is Port1's. The UCG reaches the
  router through the CSS610 today (Port2), so after retirement it needs that Port1 cable, or a
  free router port, directly.
- TrueNAS is not on the CSS610 and has no CRS309 port. The owner says it plugs directly into
  the Google Fiber router, so its VLAN 10 leg can only reach the CRS309 as tagged frames through
  the router's LAN switch, the CSS610 Port1 and SFP+1. That path is fragile and unconfirmed;
  `http://172.16.10.50:8080/` did not answer from the workstation on 2026-11-05.

### Owner answers (2026-11-06)

- **Teleport is not set up yet.** The spec's current state says "Teleport VPN is enabled", but
  there's no client subnet to admit. The emergency network's Teleport source (spec lines 168,
  290, 386) waits until the owner configures Teleport, and the phase that builds the emergency
  path records the subnet then.
- Google Fiber router port forwards (owner screenshot, 2026-11-06). UPnP is off. There are two
  rules, both TCP single-port:

  | WAN port | Target | Target port | What it is |
  |---|---|---|---|
  | 20422 | `192.168.6.170` | 22 | TrueNAS SSH (`br0 192.168.6.170`; ARP MAC `c6:6d:0c:79:47:40`) |
  | 21422 | `192.168.6.175` | 21422 | Proxmox host → DNAT to dev VM `10.78.0.10:22` (file copy) |

  There's **no forward to the panel stack** (`.181`, `.203`, `.60`), so moving it needs no
  Google Fiber forward changes. If TrueNAS's home-LAN address changes in Phase 3, the 20422 rule
  must follow it. Observation for the owner: TrueNAS SSH is reachable from the internet through
  20422.
- TrueNAS also serves the offline depot on its home-LAN address (`http://192.168.6.170:8080`,
  per `.superpowers/2026-09-22-host-cleanup-and-esxi-ping.md`), which is why
  `172.16.10.50:8080` didn't answer on 2026-11-05.
- Google Fiber router LAN ports (owner, 2026-11-06): one goes to the VCF lab (the CSS610 Port1
  cable), one to the Proxmox GPU host (`192.168.6.175`, which stays put), and two to TrueNAS.
  The workstation (`192.168.6.226`) isn't on a router port, so it's on Wi-Fi or another path on
  the router side; the CSS610 sees it through Port1. One TrueNAS cable can come out. Ports needed after the move:
  the UCG WAN (Phase 5), which takes either the freed CSS610 Port1 cable's router port or the
  freed TrueNAS port, and one TrueNAS home-LAN port. That's enough; the Step 1 stop condition
  doesn't trigger.
- Still to come from the owner: the UniFi backup, and TrueNAS's VLAN 10 netmask and Docker
  `default-address-pools`.

### VCF health baseline (2026-11-06 01:06 UTC, read-only)

- Hosts `hyp01`-`hyp03`: connected, not in maintenance mode, overall green; vSAN healthy,
  three members on each.
- NSX Manager cluster `STABLE`; transport nodes `hyp01`, `hyp02` (new `4aa47fb6-…`) and `hyp03`
  all `success`; collection `720030c6-…` `SUCCESS`.
- Only alarm: "Improved virtual disk infrastructure namespaces storage policy alarm" (yellow,
  on `lab01-datacenter` and `vsan-lab01`), raised 2026-09-26 at build time.

`hyp02`'s stale NSX node was fixed on 2026-11-06 before this baseline (see the NSX section below).
`hyp02` cut out three times on 2026-11-05/06 at the hardware level. The cause was a physically
blocked CPU fan (thermal protection under vSAN resync load). The owner cleared it, and the next
full vSAN resync finished without a cut. It's not a network issue.

### CRS309 clock (2026-11-06 01:07 UTC)

Synchronised again (`synced-server: 0.pool.ntp.org`, time zone `America/Chicago`), because
`dns01` is running and can resolve the pool names. It still depends on `dns01` for NTP; the
change to IP-address NTP servers is optional hardening, not needed for the migration.

### NSX at lab start-up (read 2026-11-05, NSX API, read-only)

NSX Manager cluster `STABLE` (management and control). Transport nodes: `hyp01` and `hyp03`
`success`; MP node `c39eba80-4fbd-491d-8e81-1a52dbca8228` `orphaned`, "Host configuration failed.
Number of retries : 109". The transport node collection is `FAILED_TO_REALIZE` with
`601: … host-transport-nodes/hyp02-4163234c-1477-4799-b225-61b39329d18fhost-26 is already present`.
This is the known stale `hyp02` node (`reports/NSX stale host transport node fix.md`), present
before this migration. It is the baseline, not a migration fault.

### VCF hosts (read 2026-11-05, after the owner powered them on)

`hyp01` `172.16.10.145`, `hyp02` `.143`, `hyp03` `.141`: ping and HTTPS 200. Every VM on them is
powered off, including vCenter, SDDC Manager and NSX. The health baseline (Step 8) waits until
the owner brings the stack up.

TrueNAS `http://172.16.10.50:8080/` did not answer from the workstation (HTTP 000) at the same
time.

### Host NICs and VDS teaming (2026-11-06, vCenter, read-only)

Every host shows only the X520 ports: `vmnic0` and `vmnic1`, `ixgben`, 10000 Mb, PCI
`0000:01:00.0`/`.1`. The RTL8125 is not visible to ESX 9.1.1, so Phase 7 needs the Realtek Fling.

VDS `lab01-cluster-001-vds-001`, MTU 9000, uplinks `uplink1` and `uplink2`. Teaming today, the
rollback record for Phase 6:

| Port group | VLAN | Policy | Active | Standby |
|---|---|---|---|---|
| `SDDC-DPortGroup-Mgmt` | 10 | load-based | uplink1, uplink2 | none |
| `SDDC-DPortGroup-VM-Mgmt` | 10 | load-based | uplink1, uplink2 | none |
| `SDDC-DPortGroup-VSAN` | 30 | load-based | uplink1, uplink2 | none |
| `SDDC-DPortGroup-vMotion` | 20 | load-based | uplink1, uplink2 | none |
| `dev-vm1/2/3-vlan70/71/72` | 70-72 | source port ID | uplink2, uplink1 | none |

`SDDC-DPortGroup-VM-Mgmt` is on VLAN 10, not VLAN 60. NSX TEP teaming is set in NSX and was not
read here.
