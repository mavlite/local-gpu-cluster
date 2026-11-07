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
- TrueNAS network (owner screenshot, "DR NAS", 2026-11-06). Hostname
  `truenas.knowledgeondemand.net`.

  | Interface | Address | MAC |
  |---|---|---|
  | `enp1s0` | none (member or parent) | `78:55:36:07:23:c9` |
  | `enp2s0` | none (member or parent) | `78:55:36:07:23:c8` |
  | `vlan10` | `172.16.10.50/24` | |
  | `br0` | `192.168.6.170/24` | (bridge; ARP shows `c6:6d:0c:79:47:40`) |

  - Static routes: `172.16.0.0/16 via 172.16.10.1` and `172.16.99.0/24 via 172.16.10.1` (the
    CRS309's VLAN 10 gateway).
  - Default route `192.168.6.1`. DNS `1.1.1.1` and `8.8.8.8`, not `dns01`, so TrueNAS doesn't
    resolve lab names.
  - The VLAN 10 netmask is `/24`, the same as the lab's. Both routes keep working after the move
    as long as `172.16.10.1` stays the CRS309's VLAN 10 address, which the design keeps.
  - Phase 3 needs to know which NIC is `vlan10`'s parent and which is in `br0`. That cable goes
    to the 8 PoE carrying VLAN 10 tagged, and the other stays on the Google Fiber router.
- UniFi backup (owner, 2026-11-06 10:20 local):
  `~/Downloads/unifi_os_backup_1793982015391_6fe86d40-03db-4b9a-8593-30f9f45c1231.unifi`,
  185,200 bytes, sha256 prefix `dcfa48c267da85fd`. Not committed (a UniFi backup holds
  credentials). This is Phase 1's rollback point.
- Still to come from the owner: `vlan10`'s parent NIC, `br0`'s members, and Docker
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

## Phase 1: UniFi layout (steps written 2026-11-06)

Rollback point: the UniFi backup above (`sha256 dcfa48c267da85fd`). Nothing in this phase
touches the CRS309, the CSS610, the Google Fiber router or the VCF hosts.

### Owner steps, in order

1. **Turn off automatic firmware updates.** UniFi Network → Settings → Control Plane → Updates:
   turn automatic updates off for the UCG and all devices.
2. **Create `Lab-Transit`.** Settings → Networks → New Virtual Network:
   - Name `Lab-Transit`, Router `UCG Ultra`, Gateway IP/Subnet `10.83.80.1/29`, VLAN ID `80`
   - DHCP mode **None**; leave Isolate Network off
3. **Create the VLAN 10 network (layer 2 only).** New Virtual Network, Router
   **Third-party Gateway**, name `Lab-VLAN10`, VLAN ID `10`. UniFi gives it no gateway and no
   DHCP. The CRS309 stays VLAN 10's gateway (`172.16.10.1`).
4. **Create `Lab-Services`.** New Virtual Network:
   - Router `UCG Ultra`, Gateway `10.83.81.1/24`, VLAN ID `81`
   - DHCP Server on, range `10.83.81.100`-`10.83.81.199`; reservations are added in Task 6 for
     `mgmt`, Panel, Wings01, traefik and `dns01`
5. **Create `Lab-Emergency`.** New Virtual Network, Router `UCG Ultra`, Gateway
   `10.83.82.1/24`, VLAN ID `82`, DHCP mode **None** (host addresses are static in Phase 7).
6. **Move the gateway link (the only disruptive step, about 1 minute).**
   - Unplug the cable from Flex 2.5G 5 **port 4**, keeping the UCG end on UCG port 2.
   - Plug it into Flex 2.5G 8 PoE **port 2**, set to the default port profile (all networks)
     with PoE off.
   - The switches re-adopt within a few minutes.

Ports reserved on the 8 PoE for later phases: port 3 for TrueNAS (Phase 3), port 4 for `mgmt`
(Task 6), port 10 SFP+ for the CRS309 DAC (Phase 2). Leave the 10G RJ45 port empty, because it
may be a combo with the SFP+.

### Rollback

- Move the gateway cable back to Flex 2.5G 5 port 4.
- Delete `Lab-Transit`, `Lab-VLAN10`, `Lab-Services` and `Lab-Emergency`.
- If anything else changed, restore the backup above.

### Builder checks after the owner applies

- [x] All three switches adopted and online, addresses unchanged (Flex 2.5G 5 `10.83.1.2`, 8 PoE
      `10.83.1.148`, Flex Mini `10.83.1.189`). Owner screenshot of UniFi Devices, 2026-11-06,
      still stable an hour later.
- [x] Internet from a UniFi client: `curl -sI https://example.com` → `HTTP/` (owner runs it on a
      UniFi client). Substituted 2026-11-06 with the UCG's own check, since no UniFi client was to
      hand: WAN `192.168.6.89` via Google Fiber 99.67 % up, speed test 937 Mbps down, latency
      8-9 ms (Microsoft, Google, Cloudflare), uptime 1 h 40 m. This proves the gateway's egress,
      not a client's; the WAN side was not changed in this phase. The first client on
      `Lab-Services` (Task 6) repeats the curl.
- UCG banner "IPv6 detected on WAN1 — Enable": left off. IPv6 is out of scope for this
  migration; enabling it would add a second address family to every network's firewall review.
- [x] `Lab-VLAN10` shows no gateway and no DHCP (owner screenshot of the network list,
      2026-11-06, after the network restart). All four lab networks are present as created.
      The list also holds pre-existing `CleanRoom-Mgmt` (VLAN 2, `10.83.2.0/24`) and
      `CleanRoom-Talos` (VLAN 3, `10.83.3.0/24`). Neither VLAN is used on the CRS309 (10-72), so
      there's no clash, but keep both off the CRS309 DAC port profile in Phase 2.
- [x] Home side unaffected: the Proxmox host and the workstation still reach the lab through
      `192.168.6.11`. The builder pings `172.16.10.150` from Proxmox and checks vCenter `:443`.
      2026-11-06, run after the cable move and again after the restart: 0% loss (~1.5 ms),
      vCenter HTTP 200, route via `192.168.6.11`.

### As applied (2026-11-06)

The cabling ended up different from step 6, with the same result: the 8 PoE is the first switch
after the gateway.

| Link | Planned | As applied |
|---|---|---|
| UCG LAN → 8 PoE | UCG port 2 → 8 PoE port 2 | UCG **port 1** → 8 PoE **port 8** (PoE off) |
| Flex 2.5G 5 → 8 PoE | port 5 → 8 PoE port 8 (unchanged) | port 5 → 8 PoE **port 2**, PoE++ (powers the Flex 2.5G 5) |
| Flex Mini → 8 PoE | port 1 → 8 PoE port 1 | unchanged |
| UCG WAN | port 5 → CSS610 Port2 | unchanged until Phase 5 |

The reserved 8 PoE ports (3, 4, 10) are still free. Flex 2.5G 5 ports 1 and 2 link at 2.5G to
the Minisforum RTL8125s; port 3 had no link at 14:54 (check in Phase 7).

**Incident: the Flex 2.5G 5 flapped online/offline after the move.**
- 14:46 both switches dropped (the 8 PoE then reached the gateway through the Flex 2.5G 5);
  14:48 both reconnected. From about 15:03 the Flex 2.5G 5 kept showing online for moments,
  then offline, while its LED stayed solid blue, its link stayed at 2.5G and it drew 4.6 W.
- Ruled out: PoE (on, drawing power), the Flex 2.5G 5's port 5 settings (native
  CoreNetwork-Mgmt, Allow All, no profile, STP on), and a PoE power cycle of 8 PoE port 2 (no
  change).
- The controller kept listing its parent as the old "Cloud Gateway Ultra Port 2", and no port
  on it showed the uplink marker.
- Cleared by the owner restarting the whole UniFi network. Afterwards the parent reads
  "8 PoE Port 2", and it was stable an hour later. Root cause not confirmed; a stale
  controller view of the old path is the likeliest.
- Lab traffic was unaffected throughout: VCF management runs over the CRS309.

## Phase 1b: swap the CSS610 for the 8 PoE (owner decision, 2026-11-06)

**Owner's decision:** retire the CSS610 now, with the VCF lab powered off, and re-IP nothing.
The 8 PoE takes over the CSS610's job like for like: it carries the home network (as a layer-2
network, `Home-LAN`, VLAN 6, no UniFi gateway or DHCP) and VLAN 10 side by side. The CRS309 keeps
`192.168.6.11` and its default route via `192.168.6.1`, Proxmox and the workstation keep their
`172.16.0.0/16 via 192.168.6.11` routes, and `mgmt`, `dns01`, Panel, Wings01, traefik and
TrueNAS keep every address. Routing the lab behind the UCG (Lab-Transit) becomes a later,
cable-free change; Task 6's re-IP is optional, not required.

The CRS309 still sends every lab VLAN tagged on `sfp-sfpplus1` (`tagged=all`). The 8 PoE drops
any VLAN that isn't in a port's tagged list, so only VLAN 10 crosses. Cleaning up `tagged=all`
remains worthwhile but isn't a precondition.

TrueNAS normally sits on the CSS610 with `br0` (home network, untagged) and `vlan10` (tagged) on
one cable; it is on the Google Fiber router only temporarily. It goes to the 8 PoE with the same
profile as `mgmt`, which should also bring back `172.16.10.50`.

### Port layout after the swap

| 8 PoE port | Device | Native | Tagged | PoE |
|---|---|---|---|---|
| 1 | Flex Mini | default | Allow All | as is |
| 2 | Flex 2.5G 5 | default | Allow All | PoE++ |
| 3 | TrueNAS (`br0` NIC) | `Home-LAN` | `Lab-VLAN10` only | off |
| 4 | `mgmt` ESXi | `Home-LAN` | `Lab-VLAN10` only | off |
| 5 | Google Fiber router (home network feed) | `Home-LAN` | Block All | off |
| 8 | UCG Ultra LAN | default | Allow All | off |
| 10 (SFP+) | CRS309 `sfp-sfpplus1` (DAC) | `Home-LAN` | `Lab-VLAN10` only | n/a |

Google Fiber router LAN ports afterwards: UCG WAN (the cable that was in CSS610 Port1), Proxmox,
the `Home-LAN` feed to 8 PoE port 5, and one spare.

### Owner steps

Preparation (no disruption):

1. **Create `Home-LAN`.** Settings → Networks → New Virtual Network, Router **Third-party
   Gateway**, VLAN ID `6`. If DHCP Guarding is offered, trust only `192.168.6.1`.
2. **Set 8 PoE ports 3, 4, 5 and 10** as in the table: Native VLAN `Home-LAN`; Tagged VLAN
   Management **Custom** with only `Lab-VLAN10` (ports 3, 4, 10) or **Block All** (port 5);
   PoE off. Then re-open each port and check the tagged list still holds only `Lab-VLAN10`
   (custom lists can silently gain or exclude new networks).

Cutover (the home network on the CSS610 drops for a few minutes; the lab is off):

3. **UCG WAN.** Unplug the CSS610 Port2 ↔ UCG port 5 cable at both ends and keep it. Move the
   Google Fiber cable from CSS610 Port1 into **UCG port 5**.
4. **Home network feed.** Unplug one TrueNAS cable from the Google Fiber router. Use the cable
   kept from step 3 to connect that router port to **8 PoE port 5**.
5. **CRS309.** Move the DAC from CSS610 SFP+1 to **8 PoE port 10** (the CRS309 end stays in
   `sfp-sfpplus1`).
6. **`mgmt`.** Move its cable from CSS610 Port4 to **8 PoE port 4**.
7. **TrueNAS.** Unplug its remaining router cable. Connect the NIC that is a member of `br0`
   (TrueNAS → Network → `br0`) to **8 PoE port 3**.
8. Power off the CSS610.

### Rollback

Put the cables back: Google Fiber → CSS610 Port1, UCG port 5 → CSS610 Port2, `mgmt` → CSS610
Port4, DAC → CSS610 SFP+1, both TrueNAS cables → Google Fiber router. Power the CSS610 on.
`Home-LAN` and the port settings can stay; nothing uses them once the cables are back.

### Builder checks after the owner applies

- [x] UniFi (owner screenshot, 2026-11-07): UCG Ultra, Flex 2.5G 5 (uplink 8 PoE port 2), 8 PoE
      (uplink UCG port 1) and Flex Mini (uplink 8 PoE port 1) all online. The per-port MACs
      weren't read.
- [x] From Proxmox (2026-11-07): `192.168.6.11` (CRS309), `.167` (`mgmt`, HTTPS 200), `.197`
      (`dns01`), `.203`, `.60`, `.181` (panel stack) and `.170` (TrueNAS) answer ping. `.89`
      (UCG WAN) does not answer ping, but ARP resolves it to the UCG WAN MAC
      `58:d6:1f:71:2c:8d` (REACHABLE). UniFi gateways drop WAN ping by default, so this counts as
      a pass.
- [ ] From Proxmox through the CRS309 (route `172.16.10.1 via 192.168.6.11`): `172.16.10.1` and
      `172.16.10.150` (`dns01`, VLAN 10 through 8 PoE ports 10 and 4) answer; the depot
      `http://192.168.6.170:8080/` returns HTTP 200. `172.16.10.50` (TrueNAS `vlan10`) answers
      ping with 0% loss, and the depot also returns 200 on `http://172.16.10.50:8080/`, once
      `enp2s0` was cabled to 8 PoE port 7 (see below). **Still open:** `172.16.10.21` (`mgmt`
      `vmk1`) doesn't answer. It didn't before the swap either; check it once the lab is
      powered on.
- [x] UCG online, uplink "Google Fiber" (owner screenshot, 2026-11-07), now through 8 PoE port 6.

### As applied (2026-11-06 to 2026-11-07)

The owner has **one** cable from the Google Fiber router, not the two that steps 3 and 4 assumed.
That cable feeds the home network into 8 PoE port 5, and the UCG's WAN now hangs off the 8 PoE as
well:

| 8 PoE port | Device | Native | Tagged | PoE |
|---|---|---|---|---|
| 1 | Flex Mini | default | Allow All | as is |
| 2 | Flex 2.5G 5 | default | Allow All | PoE++ |
| 3 | TrueNAS `enp1s0` (`78:55:36:07:23:c9`, `br0` member) | `Home-LAN` | `Lab-VLAN10` only | off |
| 4 | `mgmt` ESXi | `Home-LAN` | `Lab-VLAN10` only | off |
| 5 | Google Fiber router (the only cable from it) | `Home-LAN` | Block All | off |
| 6 | UCG Ultra port 5 (WAN), name `UCG-WAN` | `Home-LAN` | Block All | off |
| 7 | TrueNAS `enp2s0` (`78:55:36:07:23:c8`, parent of `vlan10`), name `TrueNAS-vlan10` | `Home-LAN` | `Lab-VLAN10` only | off |
| 8 | UCG Ultra port 1 (LAN) | default | Allow All | off |
| 10 (SFP+) | CRS309 `sfp-sfpplus1` (DAC) | `Home-LAN` | `Lab-VLAN10` only | n/a |

- **Dependency:** the UCG's internet now passes through the 8 PoE. If the 8 PoE reboots or its
  port 6 profile changes, the UCG loses its WAN. While the UCG has no WAN, adopt or configure
  the switches by moving the Google Fiber cable to UCG port 5 for a while. That cuts off the
  home side of the 8 PoE (`mgmt`, TrueNAS, the CRS309) until the cable goes back. Never plug
  the UCG WAN into an 8 PoE port that is still on the default network: that loops the UCG's
  WAN onto its own LAN.
- TrueNAS uses two cables, not one: `vlan10` is parented on `enp2s0`, not `br0`, so on the
  CSS610 that NIC must have had its own cable. This corrects the one-cable assumption in the
  Phase 1b introduction. `enp2s0` has no untagged address, so sharing `Home-LAN` with `br0`
  causes no loop.
- The CSS610 is out of service, and the VCF lab stayed powered off throughout.
- Rollback is the same as above, but the UCG WAN cable goes back from 8 PoE port 6 to
  CSS610 Port2.

### Lab power-on check after the swap (2026-11-07, read-only)

Matches the 2026-11-06 baseline:

- vCenter: `hyp01`-`hyp03` connected, not in maintenance mode, overall green. vSAN healthy with
  three members. The only alarm is the known yellow storage policy alarm from 09-26.
- NSX: management and control cluster `STABLE`. Transport nodes `hyp01`, `hyp02` (`4aa47fb6-…`)
  and `hyp03` `success`; collection `720030c6-…` `SUCCESS`.
- From Proxmox through the CRS309: the hosts (`.145`, `.143`, `.141`), vCenter `.129` (UI 200),
  NSX `.131`/`.132`, SDDC Manager `.133`, Ops `.122`/`.123`, licsrv `.134`, fleet `.116`,
  platform `.120`, `dns01` `.150` and TrueNAS `.50` all answer. `dns01` resolves `vcsa.lab…`
  and `example.com`.
- `mgmt` `vmk1` `172.16.10.21`: the CRS309 pings it on-link with 0% loss, and ARP resolves it on
  `Mgmt-VLAN10` (`00:50:56:69:38:07`). VLAN 10 to `mgmt` works. It doesn't answer from
  `192.168.6.x`. `mgmt` has `192.168.6.0/24` directly connected on `vmk0`, so replies to
  that range don't go back through the CRS309. Likely cause: asymmetric routing, by design and
  present before the migration. Not a migration fault; nothing to fix.


### After the owner's cable cleanup and full lab power cycle (2026-11-07, read-only)

Same results as the power-on check above:

- All home, `mgmt`, TrueNAS and lab addresses answer.
- vCenter UI, NSX, SDDC Manager, Ops and the depot return HTTP 200/30x on both addresses.
- `dns01` resolves lab and internet names.
- Hosts green, vSAN healthy with three members, NSX `STABLE`, all transport nodes `success`.
  The only alarm is the known yellow one from 09-26.
- CRS309:
  - `sfp-sfpplus1`-`7` link-ok at 10 Gbps; `sfp-sfpplus8` and `ether1` no link, as before.
  - NTP synchronised.
  - `172.16.10.21` and `.150` reach on-link with 0% loss.
- UCG WAN `.89`: ARP resolves to `58:d6:1f:71:2c:8d`, ping dropped as usual.

## CRS309 `tagged=all` cleanup (applied by the builder at the owner's request, 2026-11-07 14:30 CST)

Before, VLANs 10, 20-60 and 70-72 had `tagged=all`. That interface list put them on every port,
including the home uplink `sfp-sfpplus1` and the unconnected `ether1` and `sfp-sfpplus8`. VLAN 99
was already per port and is unchanged. `192.168.6.11` stays on `sfp-sfpplus1` untagged (VLAN 1),
so this change could not cut the management path.

After:

```
/interface bridge vlan set [find vlan-ids=10] tagged=bridge,sfp-sfpplus1,sfp-sfpplus2,sfp-sfpplus3,sfp-sfpplus4,sfp-sfpplus5,sfp-sfpplus6,sfp-sfpplus7
/interface bridge vlan set [find vlan-ids=20|30|40|50|60|70|71|72] tagged=bridge,sfp-sfpplus2,sfp-sfpplus3,sfp-sfpplus4,sfp-sfpplus5,sfp-sfpplus6,sfp-sfpplus7
```

(The second line is shorthand. Each VLAN ID was set with its own command.)

Only VLAN 10 now leaves on the uplink to the 8 PoE, which matches the 8 PoE's port 10 profile.
vSAN, TEP, vMotion, T0, VM-Mgmt and the dev VLANs stay on the host ports.
`docs/network/crs309-export-2026-11.rsc` is the 2026-11-05 snapshot, before this change.

**Safety net:** a one-shot `/system scheduler` entry was set to put `tagged=all` back at 14:40.
It was removed at 14:31 after the checks passed.

**Rollback:** `/interface bridge vlan set [find vlan-ids=<id>] tagged=all` for each of the nine IDs.

**Checks after the change:**

- From the CRS309, 0% loss to:
  - the hosts `172.16.10.141/.143/.145`, vCenter `.129`, `dns01` `.150`, TrueNAS `.50` and
    `mgmt` `vmk1` `.21`;
  - vSAN `172.16.30.20-.22`;
  - TEP `172.16.40.20/.21/.24`;
  - the dev VMs `172.16.70.10`, `.71.10` and `.72.10`;
  - `192.168.6.1` and `.175`.

  vMotion had no ARP neighbours to test; the healthy vSAN covers the host ports.
- vSAN healthy with three members, hosts green, NSX `STABLE`, all transport nodes `success`,
  collection `SUCCESS`.
- vCenter UI 200, SDDC Manager 301, depot on `172.16.10.50` 200, `mgmt` 200, `dns01` resolves
  `vcsa.lab…`.

**Router log (seen while checking):**

- 13:26 "rebooted without proper shutdown" was the owner's power-off for the cable cleanup.
- 07:30 had a burst of TFTP "permission denied" errors (PXE). It predates this change and is
  unexplained.
