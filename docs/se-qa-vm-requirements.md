# Requirements: the Space Engineers QA guest (VMID 170)

Build and operate one Windows virtual machine on the Proxmox host so another project can drive a
real Space Engineers game client against a Torch dedicated server. This document is the contract.
It assumes no knowledge of the conversation that produced it.

**Scope boundary.** This repository owns the *machine*: the guest, its network, its storage, and
anything touching the Proxmox host. It does not own what runs inside. The plugins, test scripts and
result logs live in `SE-DX2.0Code` and are somebody else's job. Deliver a guest that meets the
acceptance criteria below and stop there.

---

## 1. Why this exists

The `SE-DX2.0Code` project ships a Torch server plugin, Expanse2.Fixes v2.3.0, containing a guard
against a crash that kills production game servers. Most of its test plan has passed headlessly, but
the remaining rows need a real game client attached to a server, and nobody has one that is safe to
use. Two reasons it must be virtualised rather than run on a desktop:

- The candidate QA tooling can execute arbitrary code inside the game process.
- The tests exist specifically to provoke crashes.

Both are hypervisor-boundary problems. Isolation is the product here, not a nicety.

---

## 2. Verified environment

Everything below was measured on the host on 2026-09-07. Re-check anything you are about to depend
on rather than trusting the date.

| Fact | Value |
|---|---|
| Host | `gpu-cluster`, 192.168.6.175, Proxmox VE 9.2.11, kernel 7.0.14-12-pve |
| CPU | AMD Ryzen 5 7600, 6 cores / 12 threads, load average around 0.2 |
| Memory | 124 GB total, about 94 GB available |
| VM storage | `local-lvm`, roughly 768 GB free, under 10 percent used |
| ISO storage | `local`, at `/var/lib/vz/template/iso` |
| Existing guests | Seven containers, IDs 151 to 158. **No qemu guests at all** |
| Network | Only `vmbr0` is declared in `/etc/network/interfaces`, holding the host address and the default gateway |

The `sdxguest` and `sdxmgmt` bridges are present at runtime but are absent from
`/etc/network/interfaces`, so they are not persistent configuration and must not be relied on.

Installation media already staged: a Windows Server evaluation image and two paravirtual driver
discs. Prefer the version-numbered driver disc over the unversioned one so a rebuild is reproducible.

### Graphics, and why you must not touch the accelerators

| Device | Memory in use | Capacity |
|---|---|---|
| First accelerator | 27,367 MB | 30,704 MB |
| Second accelerator | 19,218 MB | 30,704 MB |
| Integrated graphics | 15 MB | 512 MB |

Both accelerators carry live model weights for the inference container, and the active chat profile
is split across the pair. Removing either does not merely halve capacity, it breaks the layout that
profile depends on. **Do not pass either one to this guest.** The provisioning script refuses by
device address; leave that guard in place.

The integrated GPU is unused, has no displays attached, and sits alone in its own isolation group,
so it is the fallback if graphics turn out to be unavoidable. Understand the cost first: the
accelerators have no display outputs at all, so handing the integrated GPU to a guest leaves the
host with no local video console permanently. Weigh that against a cheap add-in card in the
chipset-fed slot, which touches neither the processor lanes nor the accelerators.

Hardware partitioning of the accelerators is a dead end. They advertise the capability, but the host
driver cannot act on it, the vendor's open partitioning module does not cover this generation, and
nobody has reported it working on Proxmox. Do not put it on a schedule.

---

## 3. What already exists

`scripts/70-vm-se-qa.sh`, committed on branch `feat/se-qa-vm`, with its row in the phase table in
`scripts/README.md`. It is idempotent and follows the conventions in `AGENTS.md`.

It creates VMID 170 named `se-qa`: UEFI firmware, the q35 machine type so later passthrough stays
possible, six cores, 16 GB of memory, a 200 GB disk on `local-lvm`, a paravirtual disk controller,
the guest agent enabled, and both installation discs attached with the boot order set. It does not
start the guest and does not install Windows.

Configuration is by environment variable, all with defaults, in the style of the other phase
scripts. The ones you will actually change are the bridge and, only if graphics prove unavoidable,
the passthrough device.

---

## 4. The design, and the two constraints that produced it

**The guest holds both halves of the rig.** It runs the Torch dedicated server *and* the game
client. Torch is Windows-native and needs no graphics. The two then talk over the guest's own
loopback, which is what allows the guest to need no route to anywhere. Do not split them across two
guests without revisiting this whole document.

**Control is through the guest agent, not SSH.** Commands run over the virtual serial channel. The
warning in `AGENTS.md` about nested quoting applies with force here: pass payloads in
base64-encoded, never as inline heredocs.

**Networking has two phases, and this is the part most likely to bite you.** The isolated bridge is
the end state, not the build state. Windows updates, Steam, the game download and the Torch fetch
all need egress, and Steam needs a genuine sign-in at least once. So build on `vmbr0`, then move the
interface to the isolated bridge for test runs. The script prints both commands.

---

## 5. Open question you are expected to settle

**Can the pair work with zero egress?** The client and the dedicated server both authenticate
through Steam. Steam offline mode is not proven to carry a local multiplayer join between two
processes on the same isolated guest. This is unresolved and it is the single biggest risk to the
design.

Settle it by testing, not by assuming. Record the answer in this document.

If zero egress proves impossible, the fallback is to keep the guest on `vmbr0` with a firewall
policy that permits what Steam needs and denies the local network and the `10.60.0.0/16` fleet
range. That is a weaker boundary but still a real one. What is **not** acceptable is quietly leaving
the guest on `vmbr0` with no policy because it was easier, and not saying so.

### Answer (recorded 2026-09-07)

**Zero-egress gameplay is not yet proven, and could not be settled from the host side.** The
server half is in place (Torch 1.3.1.347 at `C:\Torch`, SE installed, Steam signed in), but the
*client-join* half cannot be exercised here: launching the SE **client** needs a rendering path,
and no GPU is attached. The only ways to give it one are (a) the `ForceNullRender` / Pulsar
preloader, which is **`SE-DX2.0Code`'s tooling and out of this repo's scope**, or (b) passing
through the iGPU, which permanently removes the host's local console. So the definitive "does a
local multiplayer join work with Steam offline" test belongs to the client-side team using this
delivered machine.

**Decision: the section-4 fallback — `vmbr0` + a PVE firewall policy — is what shipped**, so the
guest is not left open while that question is open. Policy applied (deny outbound to the LAN and
the fleet; allow the rest so Steam keeps working):

- Datacenter firewall enabled (`/etc/pve/firewall/cluster.fw` → `enable: 1`); it was previously
  fully disabled. Only VM 170's NIC has `firewall=1`, so **only VM 170 is filtered** — every LXC
  has `firewall=0` and is untouched.
- Host left open (`/etc/pve/nodes/gpu-cluster/host.fw` → `enable: 1` + `IN ACCEPT`) so host
  services (cluster-monitor `:8888`, node metrics `:9100`, SSH, web UI) keep working exactly as
  before. Verified `:8888` → HTTP 200 after the change.
- `/etc/pve/firewall/170.fw` rules (outbound): `ACCEPT → 192.168.6.1 udp/67` (DHCP renew),
  `DROP → 192.168.6.0/24` (LAN: PVE host + all inference LXCs), `DROP → 10.60.0.0/16` (fleet),
  default `ACCEPT` (Steam/internet).
- Verified from inside the guest: internet `1.1.1.1:443` REACHABLE; host `:8006`, anythingllm
  `154:3001`, router `153:8000`, fleet `10.60.0.1` all **blocked**.

Caveat: the drop rules are IPv4-only, matching the IPv4 LAN/fleet ranges in this document. When the
client team proves (or disproves) the offline join, flip to the stronger end state by moving the NIC
to `vmbrseqa` (`qm set 170 --net0 virtio,bridge=vmbrseqa,firewall=1`) — the isolated bridge already
exists.

**This policy is now reproducible from the repo.** It was applied by hand first and lived only on
the host; `scripts/71-vm-se-qa-firewall.sh` codifies it. The script regenerates the three files
byte for byte, so running it against the current host is a no-op. It is separate from
`70-vm-se-qa.sh` deliberately: creating a guest affects one guest, whereas enabling the datacenter
switch is cluster-wide. Before touching that switch it asserts that no other guest has opted into
filtering, and afterwards it verifies which interfaces are actually filtered by looking for `fwbr*`
bridges rather than trusting config flags.

**Latent trap worth knowing.** Containment rests on the fact that only VM 170 opts in. Containers
151 to 157 carry an explicit `firewall=0`, but **container 158 carries no firewall setting at
all** and is unfiltered only because absent means off. Ticking the firewall box for it in the web
UI would put it behind a default-deny inbound policy with no rules file, immediately. The guard in
`71-vm-se-qa-firewall.sh` catches this on every run and refuses to proceed; do not weaken it.

---

## 6. Acceptance criteria

The guest is done when all of these hold.

1. VMID 170 exists, starts, and runs Windows Server with the Desktop Experience. Server Core will
   not host a game client.
2. The guest agent answers from the host. A trivial echo through `qm guest exec` returns its output.
3. Steam, Space Engineers and Torch are installed, and Steam has been signed in at least once.
4. The interface is on the isolated bridge, **or** it is on `vmbr0` with a written firewall policy
   and a recorded reason, per section 5.
5. Neither accelerator is attached to the guest, and the inference container is still healthy with
   both cards carrying their weights.
6. This document records what was actually built, including the network decision and any deviation
   from the defaults.

---

## 7. Things that must not happen

- Do not pass either accelerator to this guest.
- Do not remove the guard in the script that refuses to.
- Do not give the guest a route to the `10.60.0.0/16` fleet range under any circumstances. That
  network reaches production game servers, and this guest exists to run code that provokes crashes.
- Do not install the third-party QA plugin here. Whether that plugin is used at all is a decision
  belonging to `SE-DX2.0Code`, and it may be avoided entirely.
- Do not convert this to a container. The workload is Windows.
- Do not enable the guest at boot. It should run when someone is testing and not otherwise.

---

## 8. Rollback

```
qm stop 170 ; qm destroy 170 --purge
```

Then remove the isolated bridge stanza from `/etc/network/interfaces` and reload the network
configuration. The script writes a timestamped backup of that file before it appends anything.

Nothing this work does touches the inference containers, the accelerators or `vmbr0`. If any of
those change state, something has gone wrong; stop and investigate rather than continuing.

---

## 9. Build log

Fill this in as you go. A fresh session should be able to read this section alone and know where
things stand.

| Date | Who | What happened |
|---|---|---|
| 2026-09-07 | — | Provisioning script and this document written. Guest not yet created. |
| 2026-09-07 | mavlite | Preflight re-verified against host (VM 170 free, both ISOs staged, V620s confirmed at `03:00.0`/`07:00.0` matching the script guard, iGPU `7e:00.0`, `local-lvm` 768 GB free, 97 GB RAM free, LXC 151 healthy). Ran `70-vm-se-qa.sh`: created isolated bridge `vmbrseqa` (backup `/etc/network/interfaces.bak.20260907-123540`) and VM 170 — q35/OVMF, 6c/16 GB, 200 GB virtio-scsi disk, agent on, `onboot 0`, no GPU; install ISO on ide2 + `virtio-win-0.1.266` on ide0, boot `ide2;scsi0`. NIC set to `vmbr0` for the **build phase**. Guest **not started**; Windows not yet installed. `vmbr0` and inference cluster untouched. |
| 2026-09-07 | mavlite | Windows Server 2022 Standard **(Desktop Experience)** installed; qemu-ga + NetKVM in place; Steam + Space Engineers + **Torch 1.3.1.347** (`C:\Torch`) installed, Steam signed in once. Verified: guest agent echoes (exit 0); `InstallationType=Server` + explorer shell (not Core); SE `appmanifest_244850` + `Bin64\SpaceEngineers.exe`; 1 Steam account in `loginusers.vdf`; no `hostpci` on 170; LXC 151 running. Guest DHCP `192.168.6.103`, DNS is public (1.1.1.x/8.8.8.8). **Acceptance criteria 1, 2, 3, 5 met.** |
| 2026-09-07 | mavlite | **Criterion 4 — network posture (fallback, per §5).** Zero-egress gameplay unproven (client can't render without a GPU / the out-of-scope null-renderer), so shipped `vmbr0` + PVE firewall instead of full isolation. Enabled the datacenter firewall (was off); host left fully open (`host.fw` `IN ACCEPT`) so cluster-monitor `:8888` (verified HTTP 200), `:9100`, SSH, web UI keep working; **only VM 170 filtered** (all LXCs `firewall=0`). `170.fw` OUT: allow DHCP→gw, **DROP `192.168.6.0/24` + `10.60.0.0/16`**, default allow. Guest-verified: internet REACHABLE; host/LXCs/fleet all blocked. Applied with a dead-man auto-revert (cancelled after access confirmed). See §5 for full details + how to switch to `vmbrseqa` later. |
| | | **REMAINING (client-side team, `SE-DX2.0Code`):** launch the SE client (needs null-renderer or a GPU) and prove/disprove a local multiplayer join under Steam offline while isolated; then either move NIC to `vmbrseqa` for zero egress, or keep the current firewalled `vmbr0`. Also: guest is `onboot 0` (start only when testing); do **not** install the QA plugin on this repo's behalf. |
| 2026-09-07 | se-dx2 session | Firewall policy codified as `scripts/71-vm-se-qa-firewall.sh` (verified byte-identical to the live files). Blast radius re-verified independently via `fwbr*` interfaces: only VM 170 is filtered. |
