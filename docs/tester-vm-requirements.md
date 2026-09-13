# Requirements: the external tester guest (VMID 172)

**Status:** built. VM 172 is live and confined (see the build log in §11). The
external-SSH path — a router port-forward reaching it from outside the
network — is not yet proven; see §11's "Outstanding". Written 2026-09-12,
updated 2026-09-13.

This document is the contract for a Debian guest handed to a party outside this network.
It assumes no knowledge of the conversation that produced it. Read section 8 before you
change anything.

---

## 1. Why this exists

An external tester — a human, and later an autonomous agent — needs a machine to install
software on and orchestrate workloads in. They get SSH and root on that machine. They get
nothing else.

The threat model is not "a hostile tester". It is: **an agent with sudo and an internet
connection, installing arbitrary software, on a host that also runs the inference cluster.**
Mistakes there are routine rather than exceptional, and the blast radius has to be shaped
in advance because nobody will be watching each command.

That framing drives every decision below. Where a simpler option existed, it was rejected
if it made isolation depend on a rule staying correct forever.

---

## 2. Verified environment

Measured on the host 2026-09-12, not copied from a config file.

| Fact | Value |
|---|---|
| Host | `192.168.6.175`, PVE 9.2.11, kernel 7.0.14-12-pve |
| CPU | Ryzen 7600, 6 cores / 12 threads |
| RAM | 127,943 MB total; 98,714 MB available with the current running set |
| ZFS ARC | pinned 16 GB in `/etc/modprobe.d` (`zfs_arc_max=17179869184`) |
| Swap | 8 GB, unused |
| Running LXCs | 151, 153, 154, 155, 156 — ceilings total 90 GB |
| Stopped | VM 170, LXC 157, LXC 158 — 40 GB of ceilings freed for this guest |
| `tank-lxc` | zfspool, 513 GiB free, `content rootdir,images`, lz4, **not sparse** |
| `local-lvm` | lvmthin, 644 GB free — **carries the root disks of LXC 151 and 153** |

**LXC 151's real footprint is ~7.1 GB** (`memory.current`), peak 7.7 GB over three days,
with 1.4 GB mlocked. Its 64 GB ceiling is *allocation headroom for model load* — shrinking
the ceiling causes SDMA faults — but it is not 32 GB of consumed RAM. Budget 151 at roughly
12 GB of actual use, not at its ceiling.

---

## 3. What already exists, and is reused

**SDN zone `sdx`** (type `simple`, IPAM `pve`) with vnet **`sdxguest`** carrying subnet
`10.78.0.0/24`, gateway `10.78.0.254`, `snat 1`. The host already holds `10.78.0.254/24` on
the `sdxguest` interface, `net.ipv4.ip_forward=1` is set, and the SNAT rule to `vmbr0` is
live. **At design time no guest used this vnet;** IPAM held only `.0` and `.254`, so `.10`
was free. VM 172 now holds `10.78.0.10/24` on `sdxguest` (built per §11's log).

This guest is built on that. Nothing new was created at the network layer except one DNAT.

**`70-vm-se-qa.sh` / `71-vm-se-qa-firewall.sh`** established the precedent this follows:
creating a guest and changing firewall state are different blast radii and get separate
scripts with separate confirmations. `71` also supplies the per-guest `.fw` idiom and a
blast-radius guard worth copying — it asserts no *other* guest has `firewall=1` before
touching the datacenter switch.

**`net.ipv6.conf.all.forwarding = 0`** on the host. The policy below is IPv4-only, and that
is only acceptable because of this. The script must **assert** it rather than assume it.

---

## 4. The design, and the two constraints that produced it

**Constraint 1 — the guest must never be L2-adjacent to the LAN.** A guest on `vmbr0` can
ARP and broadcast alongside the PVE host and all seven inference containers; its isolation
is then entirely a firewall rule, and one cleared `firewall=1` flag puts an agent-driven box
on the same segment as the cluster. So the guest lives on `sdxguest` (`10.78.0.0/24`) and has
**no interface on `192.168.6.0/24` at all**. Reaching the LAN requires routing through the
host, which the policy denies. A rule failure degrades to "reachable but on a foreign
subnet" instead of "on your network".

**Constraint 2 — the guest must not be able to starve the cluster's disks.** `local-lvm` is
thin-provisioned and holds the root disks of LXC 151 and 153. A tester filling a thin volume
there drives the pool to 100% and takes the inference containers' filesystems read-only. So
the disk goes on **`tank`**, a different pool, where the storage is **not sparse** and PVE
therefore creates a zvol with a `refreservation` equal to its size — the space is committed
up front and cannot overcommit the pool.

### Guest specification

| | |
|---|---|
| VMID | **172** (170 is `se-qa`; scripts 72/73 → VM 172, per the 70/71 → 170 convention) |
| OS | Debian 13.5, genericcloud amd64 |
| Image | `debian-13-genericcloud-amd64-20260518-2482.qcow2` |
| SHA512 | `7752ad2adce1bc49dd964dae8300ed7a239d0bf3c13112f55953b111447fe642d2cc01afeead234aa6ebe3605513f2e7c0e7c56785d675c38ff40110d5c8332b` |
| RAM | 64 GB, fixed (no ballooning — see below) |
| vCPU | 8, with `cpuunits` lowered so it loses contention to inference |
| Disk | 200 GB on `tank-lxc`, thick zvol |
| NIC | `virtio`, bridge `sdxguest`, `firewall=1` |
| Address | `10.78.0.10/24`, gateway `10.78.0.254`, **static** |
| DNS | public resolvers — see section 5 |

**Why the image is pinned to a dated snapshot.** `latest/` moves. `20260518-2482` is the
first cloud snapshot after Debian 13.5's release on 2026-05-16, so it is the 13.5-vintage
build, and pinning it makes the build reproducible. Its checksum differs from the `latest/`
build's; do not copy a checksum between directories.

**Why 13.5 is a base, not a freeze.** The guest runs `apt full-upgrade` on first boot and
enables `unattended-upgrades`. It therefore *reports* 13.7 in `/etc/debian_version` once
patched, because that string tracks `base-files`, which point releases bump. Shipping a
genuinely frozen 13.5 to an internet-facing box with an autonomous agent would mean ~4 months
of unpatched kernel, OpenSSH, sudo and systemd, and was explicitly rejected. If the tester
requires the system to *report* 13.5, that is a different build: pin `snapshot.debian.org`
for `main` at a May 2026 timestamp while keeping `trixie-security` current.

**Why RAM is fixed and not ballooned.** Ballooning depends on a cooperative guest driver. A
tester running arbitrary workloads will consume the maximum, so 64 GB is treated as spent the
moment the guest starts. There is no scenario where we get it back on demand.

**Why 64 GB.** ARC 16 + host ~4 + the five running LXCs with growth headroom ~26 leaves ~79 GB
theoretical. 64 GB keeps ~15 GB of genuine slack. **This number assumes LXC 157 and 158 stay
stopped.** Starting all three oversubscribes the host.

---

## 5. The security policy

Applied to `/etc/pve/firewall/172.fw`. The datacenter firewall is already enabled and only
filters NICs that opt in with `firewall=1`.

```
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN  ACCEPT -p tcp -dport 22        # the only way in
OUT DROP -dest 10.78.0.254         # the PVE host itself — no host services
OUT DROP -dest 192.168.6.0/24      # the LAN: host + every inference LXC
OUT DROP -dest 10.77.0.0/24        # the sibling SDN vnet
OUT DROP -dest 10.60.0.0/16        # production fleet VPN, same reason as VM 170
OUT DROP -dest 169.254.0.0/16      # link-local and cloud metadata
                                   # everything else → internet
```

Rule order matters: the DROPs precede the implicit accept.

**Why the gateway is blocked, and what it costs.** `10.78.0.254` is the PVE host. Denying it
stops the guest reaching host services while still allowing it to *route through* the host —
forwarded traffic is addressed to the internet, not to the gateway. The cost is that the guest
cannot use the host as a DNS resolver, so cloud-init sets public resolvers. That is deliberate.

### Inbound path

Router forwards `<external port>` → `192.168.6.175:21422`; the host DNATs `21422` → `10.78.0.10:22`.
**As deployed 2026-09-13 the port is 21422, not the 2222 originally specified**: the operator's
router only supports 1:1 forwarding, so external and internal must match. `TESTER_SSH_PORT`
carries this. Port 21422 is free on the host (in use: 22, 25, 85, 111, 3128, 8006, 8888, 9100). A non-22
external port is used to cut scan noise, not as a security control.

**The DNAT must not go in `/etc/network/interfaces.d/sdn`.** That file is generated by PVE SDN
from `/etc/pve/sdn/.running-config`, and the next SDN apply overwrites it — the port-forward
would vanish silently and present as "the tester cannot SSH in" with no obvious cause. It goes
in a dedicated **idempotent systemd oneshot** instead, using `iptables` to match the host's
existing idiom (`nftables.service` is present but disabled).

### Guest hardening (cloud-init)

- SSH **key only**; `PasswordAuthentication no`; root login disabled; a non-root sudo user.
- `qemu-guest-agent` installed, so control survives losing SSH.
- `unattended-upgrades` enabled.
- A **ZFS snapshot immediately after provisioning**, so the guest resets between testers in
  seconds rather than being rebuilt.

---

## 6. Build order

Two scripts, deliberately separate, following the 70/71 precedent.

1. **`scripts/72-vm-tester.sh`** — download and verify the image, create VM 172, import the
   disk to `tank-lxc`, attach the cloud-init drive, set the static address, boot, verify the
   guest answers the agent. Touches one guest.
2. **`scripts/73-vm-tester-firewall.sh`** — assert host IPv6 forwarding is off, assert no
   other guest has `firewall=1`, write `172.fw`, install the DNAT unit, and probe that the
   LAN is unreachable from the guest while the internet is. Touches cluster-wide state.

Both must be added to the `scripts/README.md` phase table: `doc-lint.py` fails any
`scripts/NN-*.sh` not listed there by filename.

---

## 7. Acceptance criteria

From the guest, after `73` has run:

- `ssh` in from outside the network on the forwarded port succeeds with a key; password auth is refused.
- `curl https://deb.debian.org` succeeds — egress works.
- `ping 192.168.6.175` and `curl http://192.168.6.153:8000/healthz` both **fail** — the LAN is unreachable.
- `curl http://10.78.0.254:8006` **fails** — host services are unreachable.
- `ip -4 addr` shows only `10.78.0.10/24`; no address on `192.168.6.0/24`.
- `ip -6 addr` shows no global address.

From the host:

- `pct exec 151 -- ...` and the router `healthz` still answer — the datacenter firewall did not disturb the cluster.
- `zfs list -t volume` shows the guest's zvol carrying a `refreservation` equal to its size.

---

## 8. Things that must not happen

- **Do not move this guest to `vmbr0`.** The whole design is that it has no L2 path to the LAN.
- **Do not put its disk on `local-lvm`.** That pool holds LXC 151 and 153's root disks.
- **Do not add the DNAT to `/etc/network/interfaces.d/sdn`.** SDN regenerates it.
- **Do not enable ballooning** and count the RAM as recoverable.
- **Do not start LXC 157 and 158 while this guest is running** without first lowering its RAM.
- **Do not remove the `firewall=1` flag** from the NIC. Every rule in section 5 is inert without it.
- **Do not give this guest a GPU.** It is not an inference host.
- **Do not enable it at boot.** It runs when a tester is working and not otherwise.

---

## 9. Known limits

Stated rather than glossed, in the spirit of `71-vm-se-qa-firewall.sh`'s own IPv4 note.

- **The image is checksum-verified, not GPG-verified.** `SHA512SUMS.sign` returns 404 in both
  `latest/` and the dated snapshot directory, and Debian's published signing guidance covers
  ISO/CD media rather than cloud images. Provenance rests on HTTPS to `cloud.debian.org` plus
  a pinned SHA512. This is integrity, not authenticity.
- **The policy is IPv4-only.** Acceptable only because host IPv6 forwarding is off; `73`
  asserts this. If IPv6 is ever enabled on the host, this policy is incomplete.
- **`refreservation` on thick zvols is inferred** from `tank-lxc` lacking a `sparse` flag plus
  documented PVE behaviour. There are no zvols on `tank` yet to confirm against, so `72`
  verifies it at build time, immediately after the disk is attached and resized, rather than
  trusting it.
- **The 64 GB figure is a snapshot.** It assumes the current running set. Re-derive it if the
  cluster's composition changes.

---

## 10. Rollback

```bash
qm stop 172 && qm destroy 172 --purge          # guest and its zvol
systemctl disable --now tester-vm-dnat.service  # the port-forward
rm -f /etc/systemd/system/tester-vm-dnat.service /etc/pve/firewall/172.fw
```

The SDN vnet, the datacenter firewall switch and the SNAT rules predate this guest and stay.

---

## 11. Build log

**2026-09-12 — first build.** VM 172 created on `sdxguest` at `10.78.0.10/24`: 64 GB RAM
(fixed, no balloon), 8 vCPU, 200 GB thick zvol on `tank-lxc`. Image was Debian 13.5
genericcloud pinned to snapshot `20260518-2482`, which self-patched to 13.7 on first boot via
`unattended-upgrades` (expected — see section 4, "13.5 is a base, not a freeze").

All of section 7's acceptance criteria were verified **except the first** — ssh in from
outside the network on the forwarded port — which could not be tested because the router's
port-forward does not exist yet (see "Outstanding," below). The rest: egress to `https://deb.debian.org`
returned 200; the router, the PVE UI and the sibling vnet (VM 170) were all unreachable from
the guest; LAN ping was blocked; `ip -6 addr` showed no global address. The zvol's
`refreservation` (`218109313024`) closely matches its `volsize` (`214748364800`) — the
capacity guarantee from section 4 constraint 2 holds. The inbound DNAT (`21422` →
`10.78.0.10:22`) was proven from off-host — a client on the LAN, not `localhost`, reaching
`192.168.6.175:21422` and landing on the guest's SSH.

A rollback point was taken: `zfs snapshot tank/vm-172-disk-0@provisioned`.

**Deviation from the plan (see D3 in the plan's "Corrections after review"):** the
genericcloud image does not ship `qemu-guest-agent`, so `72-vm-tester.sh`'s step 7 (as run)
hung waiting for an agent that was never installed. `qemu-guest-agent` was installed manually
on the guest post-boot to unblock this run; the script itself did not yet carry the fix. It
has since been corrected to install the agent via a cloud-init vendor-data snippet (`vendor=`,
not `user=`, so the generated user-data is not discarded) and to enable the `snippets` content
type on the `local` storage's existing content list rather than replacing it.

**Also found and fixed post-run, not re-run against this live guest** (see D1/D2 in the
plan's "Corrections after review"): `73-vm-tester-firewall.sh`'s host-service probe was
hardcoded to `http://127.0.0.1:8888/`, but cluster-monitor binds `192.168.6.175:8888` (per
`/etc/cluster-monitor.json`) and does not listen on loopback — the probe could never succeed
on this host and nearly caused a healthy, correctly-confined firewall change to be rolled
back. And `lib/common.sh`'s `write_file_if_changed` used `install -m`, which fails on
`/etc/pve` (pmxcfs enforces its own `root:www-data 0640` and rejects the chmod step) even
though the content is written correctly first — hit once by `73`'s single call and three times
by `71-vm-se-qa-firewall.sh`'s.

**Also found on the host, unrelated to this build, and corrected per operator directive:**
IPv6 forwarding was enabled on `sdxguest`, `sdxmgmt` and `vmbr0` — a gap in the assumption
section 3 documents (`net.ipv6.conf.all.forwarding = 0`) that would have made VM 172's
IPv4-only firewall policy (section 5) incomplete. Disabled and pinned in
`/etc/sysctl.d/99-no-ipv6-forwarding.conf` so it survives reboot.

**Outstanding:** the router's port-forward to `192.168.6.175:21422` and the off-network SSH
test (section 6, Task 3 step 2) have not yet been done.
