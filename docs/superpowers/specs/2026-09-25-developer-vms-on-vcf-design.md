# Developer VMs on the VCF cluster — design

**Status:** design. Nothing built. Blocked on VCF bring-up completing.

**Goal:** give an external developer build/test VMs on the three VCF hosts with
the same security posture as the Proxmox tester (VM 172): reachable only
through a Cloudflare Access tunnel, able to reach the internet, and unable to
reach anything on any private network.

**Decisions taken 2026-09-25:** VMs are **mutually isolated** (a segment and a
policy each, no east-west path). **One VM first**, sized like VM 172, proving
the pattern end to end before it is multiplied.

---

## 1. Capacity, honestly

Measured from `rules/tables.py` and `inventories/lab01-physical.yaml`:

| | |
| --- | --- |
| VCF mandatory stack | 50 vCPU, 139.25 GB RAM, 2536 GB |
| Cluster | 48 cores, 558 GB usable RAM, ~12 TB raw vSAN |
| Headroom, 3 hosts | **-2 cores**, 418.75 GB RAM |
| Headroom, N-1 | **-18 cores**, 232.75 GB RAM |

Two consequences that shape the design:

- **CPU is negative before a single developer VM exists.** The stack alone
  wants 50 vCPU on 48 cores, which is the `48 vs 56` validation warning.
  Oversubscription is normal on ESXi and fine for build work, but vCPU counts
  are not a capacity plan here. Size by expected concurrent load.
- **RAM is only abundant because of tiering** — 288 GB physical plus 288 GB
  NVMe-backed tier. Tiered pages are materially slower. Keep the developer VM
  inside the physical 288 GB rather than treating 558 GB as equivalent.

vSAN storage is not a constraint: ~12 TB raw, ~2.5 TB for the stack.

## 2. What does NOT port from Proxmox

Every isolation mechanism is implemented differently. The posture ports; the
commands do not.

| Proxmox (VM 172) | VCF equivalent |
| --- | --- |
| SDN vnet `sdxguest` | NSX overlay segment on a Tier-1 gateway |
| `/etc/pve/firewall/172.fw` | NSX Distributed Firewall policy |
| `qm guest exec` | VMware Tools guest operations, or cloud-init `guestinfo` |
| `qemu-guest-agent` | `open-vm-tools` |
| host DNAT unit | none — the tunnel is the only ingress |
| `72-`/`73-` bash scripts | `govc`-driven scripts in the same numbered series |

What ports unchanged: the pinned Debian 13 image, the `factory`/`viktor`
account split, key-only SSH with NOPASSWD sudo for `viktor`, and a per-VM
Cloudflare tunnel.

## 3. Network and firewall

One overlay segment per VM, e.g. `seg-dev01` / `10.79.1.0/24`, attached to a
Tier-1 gateway with NAT egress. No static route to `172.16.10.0/24` or
`192.168.6.0/24` anywhere in the path.

The DFW policy applied to each VM's group:

```
allow  <vm> -> any            (internet, via T1 NAT)
deny   <vm> -> 10.0.0.0/8
deny   <vm> -> 172.16.0.0/12
deny   <vm> -> 192.168.0.0/16
deny   <vm> -> 100.64.0.0/10
deny   <vm> -> 169.254.0.0/16
deny   <vm> -> any other <vm> group     (mutual isolation)
default deny inbound
```

**Deny all private space; never enumerate known subnets.** On 2026-09-25 the
Proxmox policy enumerated `192.168.6.0/24`, `10.77.0.0/24`, `10.60.0.0/16` and
`169.254.0.0/16` and therefore silently permitted `172.16.10.0/24` — the VCF
lab, created after the policy was written. The tester VM could reach the VCF
Installer, the depot, DNS and all three ESXi management interfaces. See
`scripts/73-vm-tester-firewall.sh`.

**The stakes are higher here than on Proxmox.** These VMs run *on* the VCF
cluster. A wrong rule exposes vCenter, NSX Manager, SDDC Manager and the ESXi
hosts that are hosting the VM itself. The management networks must be
unreachable by construction (no route) *and* by policy (DFW), not by one alone.

## 4. Guest build

- Debian 13 `genericcloud`, pinned to a snapshot and SHA512-verified before
  use, as `72-vm-tester.sh` does.
- Configured by cloud-init through `guestinfo.userdata` /
  `guestinfo.metadata` (base64, with `guestinfo.*.encoding`). This replaces the
  `--cicustom` vendor-data snippet used on Proxmox.
- `open-vm-tools` installed by cloud-init, not by hand. The Proxmox equivalent
  shipped broken for months because `genericcloud` carries no guest agent and
  the fix was written but never executed — install it in the image
  configuration and assert it answers before declaring success.
- Accounts: `factory` (no sudo) and `viktor` (`NOPASSWD:ALL`), both key-only.
  `PasswordAuthentication no`. Both Unix passwords locked.
- **A different SSH key per account.** One key for both collapses the sudo
  boundary, since the Unix account is the real privilege boundary — the
  hostname and token are not.
- Sizing to match VM 172: 8 vCPU, 64 GB, 200 GB thin on vSAN.

## 5. Access

One `cloudflared` per VM, running as a systemd service from a token in an
`EnvironmentFile` (mode 0600), reaching Cloudflare outbound only.

**Not a shared connector.** A single connector proxying for several VMs would
have to reach them across the network, which is exactly what section 3
forbids. One tunnel per VM keeps the isolation intact and makes revocation
per-VM.

Each VM gets its own Access application and its own service token, so access
can be withdrawn for one VM without touching the others. Client setup is
unchanged from the tester: a `ProxyCommand` entry per host, tokens inline,
nothing running in the background on the client.

## 6. Acceptance

The VM is not done until these pass **from inside the guest**, the same set
used for VM 172:

```
addressing: its segment IP only, 0 global IPv6
egress:     https://deb.debian.org -> 200, GitHub 22 and 443 -> open
blocked:    vCenter, NSX Manager, SDDC Manager, all 3 ESXi mgmt IPs
blocked:    192.168.6.0/24, the Proxmox host, the other dev VMs
blocked:    169.254.169.254
accounts:   factory has no sudo; viktor's sudo needs no password
tunnel:     ssh via ProxyCommand reaches sshd (publickey denial = pass)
```

Test by connecting, not by reading rules. Every isolation claim made this
session that was checked by reading configuration was wrong at least once.

## 7. Prerequisites and sequence

1. **VCF bring-up completes.** NSX Manager must exist before any segment or
   DFW policy can be created. Nothing here can start earlier.
2. Create the Tier-1 gateway, segment and DFW policy; verify from a throwaway
   VM that the deny rules bite before putting a developer on it.
3. Build the guest from a pinned image with cloud-init.
4. Stand up the tunnel, Access application and service token.
5. Run section 6 end to end.
6. Only then template the VM for the second one.

## 8. Open questions

- **Automation surface.** The numbered `scripts/` series is the existing
  pattern and `govc` covers the VM side, but NSX objects need the NSX API.
  Whether that becomes `74-`/`75-` scripts or part of `vcfspec` is undecided.
  Note the standing constraint: the MCP server performs no network I/O, so
  any renderer emits specs and an actuator applies them.
- **Whether the VMs should be VCF-managed at all,** or live in a separate
  non-VCF cluster. Running tenant workloads on the management domain is
  convenient and is what the hardware allows, but it puts an external party's
  VM on the same hosts as the management appliances.
- Backup/rebuild expectations: VM 172 is disposable and scripted. Same here?
