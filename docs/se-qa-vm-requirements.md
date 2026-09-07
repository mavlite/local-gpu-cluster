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
