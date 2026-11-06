# Ubiquiti Lab Network Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (recommended here,
> because the owner applies every network change by hand) or superpowers:subagent-driven-development
> for the repo-only tasks. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the VCF lab's edge onto UniFi (gateway, 8 PoE, Flex switches), connect the CRS309
to it by a 10G DAC as its only outside link, retire the CSS610, split X520 traffic, and add an
RTL8125 emergency management path, without the lab, Proxmox or the workstation losing access.

**Architecture:** A runbook in phases. For each phase the builder writes the exact owner steps into
the phase log, the owner applies CRS309/UniFi/vCenter changes by hand, and the builder runs the
read-only checks below and records the real output. Repo changes are limited to the phase log,
the CRS309 export, and files that name `192.168.6.11`.

**Tech Stack:** RouterOS 7.20 (CRS309), UniFi OS 5.1.35 / Network 10.6.109 (UCG Ultra, Flex 2.5G 8
PoE, Flex 2.5G 5, Flex Mini), SwOS Lite (CSS610), ESX/VCF 9.1.1, NSX, Proxmox, Windows workstation.

**Spec:** `docs/superpowers/specs/2026-11-05-ubiquiti-lab-network-design.md`

## Global Constraints

- The builder never makes a CRS309, UniFi, vCenter, NSX or host change. Read-only `print` and
  `export` on the CRS309 only. The owner applies each change; the builder verifies it.
- Never print secrets. `~/.vcflab/credentials.env` and `scripts/config.env` are read by one named
  key only (`CRS309_USER`, `CRS309_PASS`), never by line range, whole file or context flags, and
  their values never reach output, logs or commits. Subagents do not read these files.
- RouterOS 7.20: `print where` and `find` can silently match nothing; `/ping interface=` can give
  false negatives. Check counts against the full `print`.
- `/dev/tcp` gives false negatives in containers; verify connectivity with `curl`.
- Stop on any unexpected result and bring it to the owner. Every phase has a rollback, written
  in the phase log before the owner starts.
- No host or VCF change that could affect VMs without the owner's go-ahead at the time. DRS stays
  Manual. Check the tier device is healthy before any host enters maintenance mode.
- The Proxmox GPU host (`gpu-cluster`, `192.168.6.175`) gets one change only: the next hop of its
  `172.16.0.0/16` route, with the owner's go-ahead at the time. No changes on the Talos NUCs.
- Jumbo frames stay on the CRS309; no UniFi switch gets jumbo. vSAN and TEP never cross UniFi.
- The 10G link carries exactly two tagged VLANs: transit and VLAN 10 (layer 2 only on UniFi).
- Commits: conventional, by path (shared checkout), no `Co-Authored-By` or AI attribution.

### Builder's numbering (the spec leaves these to the builder)

| Network | VLAN | Subnet | Addresses |
|---|---|---|---|
| Transit | 80 | `10.83.80.0/29` | UCG `.1`, CRS309 `.2` |
| Services (`dns01`, `mgmt` second leg) | 81 | `10.83.81.0/24` | UCG gateway `.1` |
| Emergency management | 82 | `10.83.82.0/24` | UCG gateway `.1`; hosts `.11`-`.13` |

If Phase 0 shows any of these IDs or subnets in use, pick the next free ones and record the
change as a ruling in the phase log.

## Review Focus

1. Proxmox guests that SNAT behind `192.168.6.175` inherit the UCG WAN-side admit rule. Expect
   them to stay as isolated from the lab as today: the sandbox boundary proof and the SE-QA
   firewall check must still deny `172.16.10.1` after cutover (Task 4).
2. The UCG masquerading the dev VM file-copy path. Expect the tester copy on tcp/21422 to still
   work, with the Proxmox-side rule matching whatever source it now sees (Task 4).
3. A UCG reboot after cutover. Expect lab access from the workstation and CT 190 to return by
   itself, with no route or rule lost (Task 4 reboot check).
4. TrueNAS answering lab clients from the wrong interface (the old `/32` failure). Expect a depot
   fetch and PXE boot files to work from a lab VLAN and from `192.168.6.x` (Task 5).
5. Dev VMs reaching `10.83.x` after the transit VLAN exists. Expect the deny-all-private rules
   to block it, proven from a dev VM, not from rule names (Task 3).

---

### Task 1: Phase 0 capture

**Files:**
- Create: `docs/network/crs309-export-2026-11.rsc`
- Create: `docs/network/ubiquiti-migration-log.md` (the phase log; every later task appends to it)
- Modify: `docs/vcf-lab-build-record.md:372` (the "No RouterOS export" gap now points at the export)

**Interfaces:**
- Produces: the phase log with sections `## Phase 0` ... `## Phase 7`, each holding owner
  steps, builder checks with real output, rollback, and rulings. Later tasks read Phase 0's port
  maps and client list from it.

- [ ] **Step 1: Google Fiber router port map (owner).** Owner lists every device on the router's
  LAN ports and the free count. Record it. Stop if fewer than the ports needed: UCG WAN (moves
  in Phase 5) and TrueNAS port 2.
- [ ] **Step 2: CRS309 read-only export (builder).** Write a throwaway helper in the scratchpad
  (not the repo) that loads only `CRS309_USER` and `CRS309_PASS` from `~/.vcflab/credentials.env`
  by key name, connects over SSH, runs `/export terse` (RouterOS 7 hides sensitive values by
  default; never pass `show-sensitive`), and writes the output to a scratchpad file without
  printing it. Then save the same session's read-only `/interface print`, `/ip route print detail`,
  `/interface bridge vlan print`, `/ip firewall nat print` and `/ip firewall filter print` to the
  scratchpad.
- [ ] **Step 3: Secret scan of the export.**
  Run: `grep -nEi 'password|secret|passphrase|psk|key=|community' <scratchpad export>`
  Expected: no matches except names of settings with no value. Any value → redact it, rescan,
  then copy the export to `docs/network/crs309-export-2026-11.rsc`.
- [ ] **Step 4: Record from the export** in the phase log: the `192.168.6.11` uplink port, free
  ports among `sfp-sfpplus1`, `sfp-sfpplus8`, `ether1`, the default route, any NAT on the
  home-LAN interface, and whether VLAN IDs 80-82 or `10.83.80-82.x` appear.
- [ ] **Step 5: Owner exports** the CSS610 backup (SwOS web UI) and a UniFi backup, and gives the
  CSS610 port map. Record file locations and the port map (not the files' contents).
- [ ] **Step 6: Inventory of `192.168.6.x` clients that use the lab.** Builder runs, read-only:
  `ip route show 172.16.0.0/16` on the Proxmox host, `route print 172.16.0.0` on the
  workstation, and lists Proxmox guests that SNAT behind the host or route through it
  (`iptables -t nat -S POSTROUTING` on the host). Add the repo list from the spec. Owner adds any
  other device.
- [ ] **Step 7: Facts for later phases.** Record: what `dns01` and `mgmt` are (VM or physical) and
  where they connect; what uses `192.168.6.197` and `192.168.6.167` (repo grep plus owner's AD DNS
  and CA settings); TrueNAS's VLAN 10 netmask and Docker `default-address-pools`; the Teleport
  client subnet; the current VDS teaming order per port group; whether the RTL8125 shows in
  `esxcli network nic list` on each host.
- [ ] **Step 8: VCF health baseline.** Record vSAN health, NSX TEP status and host connection
  state from vCenter, with timestamps.
- [ ] **Step 9: Commit**

```bash
git add docs/network/crs309-export-2026-11.rsc docs/network/ubiquiti-migration-log.md docs/vcf-lab-build-record.md
git commit -m "docs(network): capture pre-migration state for the UniFi move"
```

### Task 2: Phase 1 UniFi layout

**Files:** Modify: `docs/network/ubiquiti-migration-log.md` (Phase 1)

- [ ] **Step 1: Write the owner steps** in the log: turn off automatic UniFi firmware updates;
  move the UCG LAN link from Flex 2.5G 5 port 4 to an 8 PoE port; create network `Lab-Transit`
  VLAN 80 `10.83.80.1/29` (no DHCP); create VLAN-only network VLAN 10 (no gateway, no DHCP);
  create `Lab-Services` VLAN 81 and `Lab-Emergency` VLAN 82 (gateway on the UCG). Rollback: move
  the cable back; delete the new networks.
- [ ] **Step 2: Owner applies.**
- [ ] **Step 3: Check.** All three switches adopted and online in UniFi; internet from a UniFi
  client (`curl -sI https://example.com` → `HTTP/`); VLAN 10 shows no DHCP and no gateway.
- [ ] **Step 4: Commit** the log: `docs(network): record UniFi layout phase`.

### Task 3: Phase 2 10G link and routes

**Files:** Modify: `docs/network/ubiquiti-migration-log.md` (Phase 2)

**Interfaces:**
- Consumes: Phase 0's free CRS309 SFP+ port; VLAN 80 and VLAN 10 from Task 2.
- Produces: CRS309 transit address `10.83.80.2`, UCG route `172.16.0.0/16 via 10.83.80.2`.

- [ ] **Step 1: Write the owner steps.** DAC from the free CRS309 SFP+ port to 8 PoE port 10.
  8 PoE port profile: native none, tagged 80 and 10 only. CRS309: first replace every `tagged=all` bridge VLAN entry with explicit ports (VLANs 10-72:
  bridge + `sfp-sfpplus1`-`7`; Phase 0 found `tagged=all` would put all of them on `sfp-sfpplus8`),
  then add that port tagged for 10 and 80 only, VLAN 80 interface `10.83.80.2/29`, route to the UniFi subnets that
  need the lab via `10.83.80.1`. UCG: static route `172.16.0.0/16` via `10.83.80.2`; firewall
  default-deny between the lab and UniFi networks except the services network to VLAN 10 if a
  named service needs it; CleanRoom VLANs 2 and 3 get no lab access. Default route on the CRS309
  unchanged in this phase. Rollback: disable the CRS309 port.
- [ ] **Step 2: Owner applies.**
- [ ] **Step 3: Check.**
  - CRS309 `/interface ethernet print` shows the port at 10G; UniFi shows port 10 at 10 GbE.
  - No STP topology changes or MAC-flap warnings in UniFi or `/log print` over 10 minutes.
  - From a UniFi test client on `Lab-Services`: `curl -k -s -o /dev/null -w '%{http_code}' https://172.16.10.150` or another named VLAN 10 service → expected result per the access rule.
  - From the workstation, the lab still answers through `192.168.6.11` (unchanged path).
  - From a dev VM (VLANs 70-72): `curl --max-time 5 http://10.83.80.1` and `http://10.83.1.1` →
    timeout (deny-all-private holds); `192.168.6.175` tcp/21422 still accepted.
- [ ] **Step 4: Commit** the log: `docs(network): record 10G link phase`.

### Task 4: Phase 4b home-LAN cutover and repo updates (run AFTER Tasks 5 and 6)

> Phase 0 ruling (2026-11-05): the CRS309's home-LAN link also carries VLAN 10 to `mgmt`/`dns01`
> and TrueNAS through the CSS610. Execution order is Tasks 1, 2, 3, 5, 6, 4, 7, 8, 9.

**Files:**
- Modify: `scripts/files/wf_boundary_proof.py:216`
- Modify: `scripts/files/tests/test_wf_boundary_proof.py:63`
- Modify: `scripts/71-vm-se-qa-firewall.sh:35,183`
- Modify: `docs/vcf-deployment-reference.md`, `docs/vmware-skills-operator-runbook.md:30`,
  `vcf-spec-tools/inventories/lab01-physical.yaml`, `docs/se-qa-vm-requirements.md` (each
  `192.168.6.11` mention: new path or new CRS309 management address `10.83.80.2`)
- Modify: `docs/network/ubiquiti-migration-log.md` (Phase 4b)
- Leave alone: `docs/superpowers/plans/*` and `specs/*` from October (historical records)

**Interfaces:**
- Consumes: Task 3's transit route. UCG WAN address `192.168.6.89`.

- [ ] **Step 1: Write the failing test.** In `test_wf_boundary_proof.py`, replace
  `"lan gateway 192.168.6.11"` in the must-deny tuple with `"unifi gateway wan 192.168.6.89"`, and
  add an assertion that no check targets `192.168.6.11`:

```python
def test_boundary_proof_targets_unifi_gateway_not_old_crs309_leg():
    checks = proof.checks()
    assert any(c["name"] == "unifi gateway wan 192.168.6.89" and c["expect"] == "deny" for c in checks)
    assert not any("192.168.6.11" in c["name"] for c in checks)
```

- [ ] **Step 2: Run it.**
  Run: `python3 -m pytest scripts/files/tests/test_wf_boundary_proof.py -q`
  Expected: FAIL on the new name.
- [ ] **Step 3: Change line 216** to `deny("unifi gateway wan 192.168.6.89", "192.168.6.89", 443)`.
  A deny passes only on `timeout` (`proof.passes`), so after cutover the sandbox firewall must
  drop this, not the UCG refuse it; the live re-run in Step 6 proves which.
  In `71-vm-se-qa-firewall.sh`, change the line 183 test route's gateway to `192.168.6.89` and
  the line 35 comment to say a route via the UniFi gateway now reaches the lab.
- [ ] **Step 4: Run the tests and bash checks.**
  Run: `python3 -m pytest scripts/rag/tests scripts/files/tests scripts/tools/gate/tests -q` and
  `bash -n scripts/71-vm-se-qa-firewall.sh` and `shellcheck scripts/71-vm-se-qa-firewall.sh`
  (scratchpad venv `shellcheck-py`).
  Expected: all pass; report the skip count.
- [ ] **Step 5: Write the owner steps** in the log, in this order, each with its check:
  1. Reserve `192.168.6.89` for the UCG on the Google Fiber router.
  2. UCG firewall: allow from WAN, source `192.168.6.175` and the workstation's address only, to
     the lab subnets they need. No port forwarding. Also block `172.16.0.0/16` to the internet
     except sources already masqueraded to `10.83.80.2` (Phase 0: VLAN 10 has no NAT, not a
     block rule).
  3. Workstation: temporary `route add 172.16.10.150 mask 255.255.255.255 192.168.6.89`, check
     DNS answers from `dns01`, then switch the persistent `172.16.0.0/16` route to `192.168.6.89`
     and delete the temporary one.
  4. With the owner's go-ahead: Proxmox host route `172.16.0.0/16 via 192.168.6.89` (runtime and
     persistent in `/etc/network/interfaces`).
  5. CRS309: default route to `10.83.80.1`; move or remove the home-LAN masquerade per Phase 0;
     management access on `10.83.80.2`. Owner updates the address in `credentials.env`.
  6. Unplug the CRS309 home-LAN link.
  Rollback: replug, restore the CRS309 default route and NAT, restore both clients' routes.
- [ ] **Step 6: Owner applies; builder checks after each item.**
  - Workstation: `Test-NetConnection 172.16.10.129 -Port 443` → `TcpTestSucceeded : True`.
  - Proxmox: `ip route get 172.16.10.129` → `via 192.168.6.89`;
    `pct exec 190 -- curl -k -s -o /dev/null -w '%{http_code}' https://vcsa.lab.knowledgeondemand.net` → `200`.
  - From a dev VM: the tester file copy to `192.168.6.175` tcp/21422 works. If it fails, read the
    Proxmox DNAT rule's source match against `192.168.6.89`; changing it is an owner decision.
  - From a lab host: `curl -sI https://example.com` follows the CRS309 policy (VLAN 10: no
    internet, as before).
  - Sandbox/SE-QA isolation: re-run the boundary proof and the SE-QA firewall check on the host;
    `vcf lab gateway` and `172.16.10.133` must still be denied.
  - Reboot test, in a quiet window with owner approval: reboot the UCG; after it returns, the
    workstation and CT 190 checks pass again with no manual step.
- [ ] **Step 7: Commit**

```bash
git add scripts/files/wf_boundary_proof.py scripts/files/tests/test_wf_boundary_proof.py scripts/71-vm-se-qa-firewall.sh docs/vcf-deployment-reference.md docs/vmware-skills-operator-runbook.md vcf-spec-tools/inventories/lab01-physical.yaml docs/se-qa-vm-requirements.md docs/network/ubiquiti-migration-log.md
git commit -m "fix(network): reach the VCF lab through the UniFi gateway instead of 192.168.6.11"
```

If the UCG cannot admit the WAN-side traffic (Step 6 first check fails with the rule in place),
roll back and stop: the spec's fallbacks are keeping the CRS309 link or moving the workstation
onto UniFi, and that is the owner's choice.

### Task 5: Phase 3 TrueNAS

**Files:** Modify: `docs/network/ubiquiti-migration-log.md` (Phase 3)

- [ ] **Step 1: Write the owner steps.** Quiet window: no host rebuild or depot use. Confirm VLAN 10
  netmask `/24` and no Docker pool overlapping `172.16.10.0/24`. Port 1 → 8 PoE, VLAN 10
  untagged, MTU 1500. Port 2 → Google Fiber router LAN port, keeps its `192.168.6.x` address and
  default route. No LACP. Rollback: move the cables back.
- [ ] **Step 2: Owner applies.**
- [ ] **Step 3: Check.** From a lab VLAN host and from the workstation:
  `curl -s -o /dev/null -w '%{http_code}' http://172.16.10.50:8080/` → `200`; fetch one depot
  file and one PXE boot file and compare sizes with Phase 0; TrueNAS `ip route` shows default via
  `192.168.6.1` and `172.16.10.0/24` on port 1.
- [ ] **Step 4: Commit** the log: `docs(network): record TrueNAS phase`.

### Task 6: Phase 4 NUCs, `dns01`, `mgmt`

**Files:**
- Modify: files Phase 0 found naming `192.168.6.197` or `192.168.6.167` (expected:
  `docs/vcf-lab-build-record.md`, `docs/vcf-deployment-reference.md`,
  `scripts/vcf-ca/New-Dns01RollbackClone.ps1`, `scripts/vcf-ca/README.md`)
- Modify: `docs/network/ubiquiti-migration-log.md` (Phase 4)

- [ ] **Step 1: NUCs.** Owner re-cables them to the Flex Mini on a port profile with no lab
  access (they are being wiped). Check: link up only.
- [ ] **Step 1b: Panel stack prep (owner decision 2026-11-05).** `mgmt` has one NIC and also
  runs Panel `192.168.6.203`, Wings01 `.60`, traefik `.181`. Capture Google Fiber port forwards
  to them; plan their `Lab-Services` addresses, the UCG port forwards plus a Google Fiber forward
  to `192.168.6.89`, and UCG WAN-side admits for the runner `192.168.6.78` and GamePilot
  `192.168.6.175`. `mgmt`'s single cable: VLAN 10 tagged + `Lab-Services` untagged.
- [ ] **Step 2: Write the `dns01` and `mgmt` owner steps.** Lab maintenance window. Before each
  move, repoint everything Phase 0 found using its old `192.168.6.x` address. Move it to the 8
  PoE: VLAN 10 keeps `172.16.10.150` / `172.16.10.21`; second leg on `Lab-Services` VLAN 81.
  If `mgmt` needs VLANs beyond 10, stop: adding one to the 10G link is an owner decision, and
  never vSAN or TEP. Rollback: move the cable back and restore the old address.
- [ ] **Step 3: Owner applies, one device at a time. Check after each:** `nslookup
  vcsa.lab.knowledgeondemand.net 172.16.10.150` and `nslookup example.com 172.16.10.150` both
  answer; `w32tm /stripchart /computer:172.16.10.150 /samples:3` returns offsets; vCenter shows
  all hosts connected; vSAN health unchanged from Phase 0.
- [ ] **Step 4: Update the repo files** to the new addresses, `bash -n`/PowerShell parse check on
  edited scripts (`[System.Management.Automation.Language.Parser]::ParseFile`).
- [ ] **Step 5: Commit** by path: `docs(network): move dns01 and mgmt onto UniFi`.

### Task 7: Phase 5 retire the CSS610

**Files:** Modify: `docs/network/ubiquiti-migration-log.md` (Phase 5)

- [ ] **Step 1: Check nothing remains on it** against the Phase 0 port map (UniFi client list
  plus owner's look). If home-LAN devices still depend on it, stop.
- [ ] **Step 2: Owner moves** the Google Fiber uplink cable that went to the CSS610 into UCG port 5,
  last. Brief internet interruption expected. Rollback: cable back, CSS610 restored from backup.
- [ ] **Step 3: Check:** UCG WAN `192.168.6.89` up; internet from UniFi; the Task 4 workstation and
  CT 190 checks pass. Keep the CSS610 and its backup for the period the owner sets.
- [ ] **Step 4: Commit** the log: `docs(network): retire the CSS610`.

### Task 8: Phase 6 X520 traffic split

**Files:** Modify: `docs/network/ubiquiti-migration-log.md` (Phase 6)

- [ ] **Step 1: Drift check (read-only).** Find whether VCF or NSX reports or reverts a changed
  failover order (VCF docs plus SDDC Manager config drift view). If it does, stop.
- [ ] **Step 2: Write the owner steps.** Teaming order per the spec's table: `SDDC-DPortGroup-VSAN`
  active `vmnic0` standby `vmnic1`; `SDDC-DPortGroup-Mgmt`, `-VM-Mgmt`, `-vMotion` active
  `vmnic1` standby `vmnic0`; NSX TEP through the NSX uplink profile, active `vmnic0`. Dev VM port
  groups unchanged. One port group at a time. Rollback: Phase 0 teaming order.
- [ ] **Step 3: Owner applies; check after each:** vSAN health and NSX TEP status match Phase 0;
  `esxcli network ip interface list` and vmk pings (`vmkping -I vmk<N> -s 8972 -d <peer>` for vSAN
  and vMotion) succeed.
- [ ] **Step 4: Failover test,** quiet period, one host: owner disables one of its CRS309 ports;
  vSAN health stays green and the vmk pings succeed; port re-enabled.
- [ ] **Step 5: Commit** the log: `docs(network): split X520 traffic`.

### Task 9: Phase 7 emergency management path

**Files:** Modify: `docs/network/ubiquiti-migration-log.md` (Phase 7)

- [ ] **Step 1: Research and record** how to add `VMware-Re-Driver` 1.101.01 to the cluster's vLCM
  image on VCF 9.1.1, and confirm HA uses the vSAN network with vSAN on. Stop if either is
  unclear.
- [ ] **Step 2: Write the owner steps** for one host in maintenance mode (tier device healthy
  first): `esxcli software component apply -d <bundle>`, reboot, RTL8125 cabled to the Flex
  2.5G 5 on `Lab-Emergency` VLAN 82, standard vSwitch with the RTL8125 as only uplink, new vmk
  tagged management, `10.83.82.11/24`, MTU 1500; host static route to the Teleport subnet via
  `10.83.82.1`. No route for `192.168.6.0/24`. Rollback:
  `esxcli software component remove -n VMware-Re-Driver`, then reboot.
- [ ] **Step 3: Check:** from a Teleport client, `curl -k -s -o /dev/null -w '%{http_code}'
  https://10.83.82.11/ui/` → `200` with the host's CRS309 ports disabled in a planned test;
  `vmk0` still reachable on VLAN 10 when they're re-enabled.
- [ ] **Step 4: Soak** for the period the owner sets, then repeat Steps 2-3 for the other two hosts
  (`.12`, `.13`).
- [ ] **Step 5: Commit** the log: `docs(network): add RTL8125 emergency management path`.
