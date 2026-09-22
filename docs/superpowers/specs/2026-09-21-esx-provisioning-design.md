# Stage 1: ESX provisioning artifacts from the inventory

**Date:** 2026-09-21
**Status:** implemented; revised 2026-09-20 after domain review, and again
2026-09-21 when the `%firstboot` block was ported from `lamw/vcf-91-in-box`
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

Per host: a **kickstart file**. Per host: a **UEFI HTTP boot config** at
`01-<mac>/boot.cfg`. Plus a **manifest** listing what to place where on the
boot server.

The per-MAC path is the mechanism and the `01-` is not decorative: it is the
ARP hardware type, and `mboot.efi` requests exactly
`01-<mac, lowercase, dash-separated>/boot.cfg` as a **sibling of itself**. A
flat `boot-<mac>.cfg` is never requested, so every host falls back to the
default `boot.cfg` and installs identically -- silently, with one host's
addressing on all three.

The boot config is **not synthesised**. `render_provisioning` takes the
*content* of the ISO's own `boot.cfg` as an optional argument and rewrites
only `prefix=` and `kernelopt=`, preserving `modules=`, `build=`, `updated=`
and relativising `kernel=` and `modules=` by stripping their leading `/`.
That is Broadcom's documented procedure: copy the ISO contents to a directory,
point `prefix=` at it, drop the leading slashes. The module list is
build-specific; a generated `boot.cfg` has none and `mboot.efi` will not boot.
Called without that argument the tool emits a finding and no boot configs,
and still emits the kickstarts.

`prefix=` points at the **unpacked ISO payload**, not at the kickstart
directory -- they are different places and only one of them contains `b.b00`.

Nothing is written to a boot server by this tool; it emits files and tells the
operator where they go. Publishing is theirs. The function is pure: it reads
no files and opens no sockets, which is why the ISO's `boot.cfg` arrives as an
argument rather than a path.

## Inventory additions

Two genuinely new decisions, both per host, both currently unexpressible:

- `provisioningMac` — the MAC of the NIC that UEFI HTTP boots. Required,
  because per-MAC config is the whole mechanism.
- `bootDisk` — which device ESX installs to. The lab has three NVMe devices
  (4 TB vSAN, 500 GB tiering, 1 TB other) and nothing currently says which is
  the boot target. It must be a **stable device identifier**:
  `/vmfs/devices/disks/t10.…`, `eui.…` or `naa.…`, e.g.
  `/vmfs/devices/disks/t10.NVMe____BOOT________________________1TB`. Read them
  off a live host with `esxcli storage core device list`.

  `--firstdisk` in any form is **refused**. It orders by driver and PCI
  enumeration, not by size or role, so on this hardware it can select the 4 TB
  vSAN ESA device and repartition it — with no BMC to watch it happen. Runtime
  names (`mpx.…`, `vmhbaN:C0:T0:L0`) are refused too: they are assigned at boot
  in discovery order and do not survive a reboot or a drive swap.

  The install line is `--disk=<id> --overwritevmfs[ --overwritevsan]`.
  `--overwritevmfs` and `--novmfsondisk` read as a contradictory pair and only
  one survives: with no BMC the only failure that costs a physical visit is an
  install that stops and waits, and without `--overwritevmfs` a reinstall onto
  a disk that already carries a VMFS aborts. `--novmfsondisk` buys nothing but
  the absence of a local datastore, which is cosmetic and removable over SSH.

  `--overwritevsan` is **conditional**, not unconditional -- an earlier version
  of this tool emitted it always, reasoning that a device a previous install
  gave to vSAN would otherwise fail the install outright. That reasoning is
  correct, but it is only half the story, and running the generated kickstart
  against a real ESX 9.1.1 installer on a fresh disk found the other half the
  hard way. Verbatim from the console:

  ```
  install --overwritevsan specified but disk t10.ATA_____QEMU_HARDDISK___________________________ESX01BOOT___________ is not claimed by vSAN.
  ```

  So both directions are real failures, not a theoretical concern in one of
  them: `--overwritevsan` present on a disk vSAN has never claimed aborts the
  install with the message above; `--overwritevsan` absent on a disk a
  previous install *did* give to vSAN aborts it too, because the existing vSAN
  partition blocks the install. The flag has to match the disk's actual
  on-host state, which this tool cannot know at generation time -- it never
  reads the disk. `hardware.bootDiskClaimedByVsan` (default `false`) is the
  operator's declaration of that state: true only for a rebuild of a host
  whose boot disk vSAN previously claimed. The default keeps a greenfield
  install -- the common case, and the one the example inventory describes --
  working, which is the case the unconditional flag broke.

Two more per-host device fields, added with the `%firstboot` port:

- `hardware.memoryTieringDevice` — the NVMe device `esxcli memtier enable -d`
  consumes. Same stable-identifier rule as `bootDisk`, for the same reason:
  the command consumes whatever device it is handed. A host declaring
  `memoryTieringGb > 0` without this raises `VCF-PROV-NO-TIERING-DEVICE` —
  the same shape as `VCF-CAP-TIERING-NEEDS-WORKAROUND`, and it does *not*
  withhold the kickstart, because a host with no tiering still installs and
  still commissions; it is just smaller than the capacity plan assumed.
- `hardware.vsanDevice` — optional, and declared for one purpose: so the tool
  can prove that `bootDisk` and `memoryTieringDevice` are not it.

  **One device, one role.** All three roles consume the device they are given
  and none of them asks whether something else is already there. Any two of
  them naming one device (compared after normalising the
  `/vmfs/devices/disks/` prefix, so `t10.X` and the path form are one device)
  raises `VCF-PROV-DEVICE-COLLISION` and withholds that host's artifacts
  entirely — the same judgement `bootDisk` already makes, for the same reason:
  better no file than a file that repartitions the 4 TB vSAN member with no
  BMC to watch it happen.

And one block, because a boot server is a property of the provisioning
environment rather than of any host:

- `provisioning.bootServerUrl` — base URL the kickstart is fetched from, and
  the directory `mboot.efi` and the `01-<mac>/` directories sit in.
- `provisioning.esxVersion` — defaults to the commissioning target above.
- `provisioning.installerPayloadUrl` — where the ISO contents are unpacked,
  which is what `prefix=` points at. Defaults to
  `<bootServerUrl>/esx-<esxVersion>`.
- `provisioning.sshPublicKey` — optional, written to
  `/etc/ssh/keys-root/authorized_keys` by `%firstboot`. Optional precisely so
  an operator who wants key access does not hand-edit a generated file. A
  public key is **not a credential**: it authenticates its holder and
  discloses nothing to whoever reads it off the boot server, so a literal is
  allowed here where a password never is. A *private* key is refused outright
  with `InsecureCredentialError`, and anything else unrecognised raises
  `VCF-PROV-SSH-KEY-NOT-PUBLIC` and injects nothing — the value is echoed
  inside single quotes into a file that runs as root, so nothing reaches the
  template unless it is visibly a public key.

`vmnics` already exists in the schema and is **read by no code today**, and
this code does not change that. `network --device=` is emitted from
`provisioningMac` instead: a MAC identifies the NIC that actually booted,
whereas `vmnicN` enumeration is assigned by the installer and is exactly the
ambiguity `--device=` exists to remove. The earlier claim that the kickstart
would be the first consumer of `vmnics` is withdrawn.

## Credentials

The kickstart needs a root credential, and this is the one place in the
toolchain where the published artifact need not hold a secret at all. ESX
accepts `rootpw --iscrypted` with a SHA-512 crypt hash, so the kickstart emits
`rootpw --iscrypted ${esx_root_hash}` and the substituted value is a
`$6$salt$hash` — not a password. Generate one with `openssl passwd -6`.

This is a new inventory credential, `credentials.esxRootHash`, distinct from
`credentials.esxRoot`: the SddcSpec's `hostSpecs[].credentials.password` needs
the real password for commissioning, and a hash will not do there.

Both stay `${reference}`s in generated output, exactly as in the rendered spec.
A plaintext literal is refused outright, as everywhere else. A `$6$…` literal
is allowed through, because a crypt hash is not a plaintext secret and
publishing one is the entire point of `--iscrypted`.

There is a collision worth naming: crypt hashes and this tool's reference
syntax both begin with `$`. `REFERENCE_RE` wants `${name}`, which `$6$salt$hash`
fails, so without an explicit branch a perfectly good hash falls straight into
the plaintext refusal.

This matters more here than in the SddcSpec: a kickstart sits on an unauthenticated
HTTP server for any host on the provisioning VLAN to fetch. Anything literal in
that file is readable by anything that can reach it. State that in the manifest.

## Consumer-AMD host workarounds

The lab's hosts are Minisforum 795S7 (Ryzen 9 7945HX, Zen 4) — consumer AMD,
not EPYC or Intel. William Lam's [VCF 9.1 comprehensive ESX configuration
workarounds for lab
deployments](https://williamlam.com/2026/05/vcf-9-1-comprehensive-esx-configuration-workarounds-for-lab-deployments.html)
documents settings without which VCF does not work on this hardware.

This is **opt-in, not unconditional**: `provisioning.hostWorkarounds` is an
array, empty by default, with one enum value today, `consumer-amd`. An
operator on real EPYC or Intel hardware sets nothing and gets none of this.

When `consumer-amd` is present, `%firstboot` emits four settings, in this
order, all before the reboot that already ends `%firstboot` for certificate
regeneration — that one reboot serves both settings below that need it:

- `cpuid.brandstring = "AMD EPYC <model>"` in `/etc/vmware/config`, no
  reboot. Load-bearing: NSX Edge/VNA deployment — part of every VCF bring-up
  — fails on consumer AMD because a DPDK vendor check rejects the real Ryzen
  string. `<model>` comes from the host's `hardware.cpuModel`; the brand
  string exists to satisfy that check, not to describe the hardware
  honestly, and falls back to a generic EPYC string when `cpuModel` is
  absent.
- `monitor_control.disable_apichv ="TRUE"` in `/etc/vmware/config`, reboot
  required. Load-bearing: NVMe memory tiering cannot power on VMs on AMD
  Ryzen without it.
- `esxcli system settings kernel set -s entropySources -v 2`, reboot
  required. Lower stakes: Zen 4/5 entropy collection is otherwise slow.
- `esxcli system settings advanced set -o /VSAN/Vsan2ZdomCompZstd -i 0`, no
  reboot. Lower stakes: vSAN's default Zstd compression costs more CPU on
  this hardware than LZ4.

**The rule that makes this worth doing.** The inventory already declares
`hardware.memoryTieringGb` per host, and the lab's capacity plan depends on
memory tiering to survive N-1 (192 GB available against the 219 GB
mandatory stack — see `VCF-CAP-N1-SHORTFALL`). If a host declares
`memoryTieringGb > 0` and `consumer-amd` is not in
`provisioning.hostWorkarounds`, `VCF-CAP-TIERING-NEEDS-WORKAROUND` fires:
tiering is declared, this hardware needs the workaround to make tiering
function at all, and the workaround was never requested. Without it the
capacity plan is counting RAM that will not actually be there. The example
inventory (`lab-3-host.yaml`) requests `consumer-amd` and declares
`cpuModel: 7945HX` on all three hosts for exactly this reason.

## Non-goals

- Running a DHCP or HTTP server, or writing to one.
- Powering hosts on. No BMC means a switched PDU with an API is the substitute,
  and that is its own work.
- BIOS configuration. The spike's human floor stands: one BIOS pass per chassis
  (UEFI, HTTP boot, **Secure Boot off**, NIC boot order, AC-recovery). Secure
  Boot is now an explicit item rather than an omission: `%firstboot` is
  *silently skipped* when it is enabled. SSH, NTP and the certificate
  regeneration simply do not run, `kickstart.log` records the skip, and the
  host looks installed right up until commissioning fails. It goes back on
  afterwards.
- Post-install configuration beyond what kickstart's `%firstboot` needs to make
  a host commissionable.

## What `%firstboot` must do

Beyond SSH and NTP, one thing that is not optional: **regenerate the
certificates**. ESX generates them before the hostname is configured, so a
freshly installed host presents `CN=localhost.localdomain`. VCF Installer
compares the certificate common name against the FQDN it is commissioning and
rejects the host. After the hostname is in place:

```
esxcli system hostname set --fqdn=<fqdn>
/sbin/generate-certificates
esxcli system shutdown reboot -d 10 -r "hostname and certificate regenerated"
```

The reboot is what makes hostd actually serve the new certificate.

### Ported from `lamw/vcf-91-in-box` (2026-09-21)

The block now follows the structure of a kickstart that has actually built
this lab's shape — [`config/KS-ESX01.CFG`](https://github.com/lamw/vcf-91-in-box)
from William Lam's three-node vSAN ESA VCF lab on Minisforum/Ryzen hardware.
The text lives in `vcfspec/firstboot.py`; `provision.py` keeps the validation
and the findings, and stays pure.

**The version caveat is the point.** That repo targets VCF 9.1.0.0 / ESX build
25370933; we target 9.1.1.0 / 25714478. Every command was re-checked rather
than copied, and each choice is stated in a comment in the generated file.

**Structure.** Wait on `vim-cmd hostsvc/runtimeinfo` until hostd answers →
enter maintenance mode → do the work → leave maintenance mode → reboot. The
previous block issued commands immediately, which races hostd; with no BMC a
half-configured install is invisible until commissioning rejects it. Leaving
maintenance mode is equally load-bearing: VCF will not commission a host that
is in it, and a host that reboots while in it comes back still in it.

**Memory tiering — the gap that motivated the port.** `memoryTieringGb` had
been read only by the capacity rules; nothing ever enabled tiering, while the
lab's N-1 headroom depends on it. `%firstboot` now emits the **9.1 form**:

```
esxcli memtier enable -d <device> -r <ratio>
```

The 9.0 sequence Lam version-branches to (a `MemoryTiering` kernel setting,
`/Mem/TierNvmePct`, and `esxcli system tierdevice create`, plus a reboot) is
**not emitted at all**, and neither is the `vmware -r` probe that would choose
between them: we install exactly one build, and the 9.0 path on a 9.1 host
sets a knob that is no longer the control — a silent no-op that looks
configured. 9.1 applies in maintenance mode with no reboot, which is why
maintenance mode above is load-bearing rather than tidy.

`-r` is **derived**, not pinned at Lam's flat 100: `memoryTieringGb / ramGb`
as a percentage, which is what makes `memoryTieringGb` act on the host rather
than only on a capacity table. The device is normally larger than the tier the
ratio asks for, and the ratio is what decides how much is used. ESX accepts
1–400%; outside that, `VCF-PROV-TIERING-RATIO-UNSUPPORTED` fires and nothing
is emitted, because `-r 521` is a command that fails on a host nobody is
watching.

**MTU — which of two declared MTUs.** `networks.management.mtu` (1500 here),
**not** `nsx.fabricMtu` (9000). `vmk0` is the management vmkernel and must
match the MTU the management VLAN actually carries; Lam hardcodes 9000, which
here would black-hole exactly the commissioning traffic the host exists to
receive. `vSwitch0` takes the same value because before VCF commissions the
host it carries nothing but `vmk0` — and `nsx.fabricMtu` describes the NSX
transport fabric on the VDS that VCF builds *afterwards*, replacing `vSwitch0`
entirely.

**Also ported:** `/UserVars/SuppressShellWarning` (three hosts otherwise carry
three standing alarms that hide real ones); the local VMFS datastore rename,
named `<host>-local` and deliberately **not** `storage.datastoreName`, which
names the vSAN datastore this cluster is about to build; and
`esxcli system coredump file set -s -e true`, because with no BMC a PSOD's
screen is otherwise the only copy.

**Deliberately not ported, and why:**

- **The `"VM Network"` portgroup VLAN.** Lam's install line passes
  `--addvmportgroup=1`; ours passes `0`, so the portgroup does not exist and
  the command would fail on every host.
- **`/Net/TcpipDefLROEnabled` and `/Net/UseHwTSO`.** His comment scopes these
  to Intel X710 NICs. That is a property of a NIC, not of this inventory, and
  emitting it unconditionally costs throughput on hardware that does not need
  it. If the lab ever fits X710s it belongs in `provisioning.hostWorkarounds`
  as its own enum value.
- **`enable_esx_shell` / `start_esx_shell`.** With no BMC there is no remote
  console to use a local shell from: attack surface with no operator benefit.
- **`/VSAN/Vsan2ZdomCompZstd`, NTP, SSH enablement, `generate-certificates`.**
  Already emitted. Porting his block verbatim would have emitted each twice.
- **His credential handling.** `VMware1!VMware1!` in plaintext throughout,
  including `rootpw`. Ours is `rootpw --iscrypted ${esx_root_hash}` and refuses
  a plaintext literal outright.
- **His boot path.** USB stick plus rEFInd. Our per-MAC UEFI HTTP boot
  generator has no upstream equivalent and is strictly more capable for three
  hosts: no media to carry, no per-host imaging pass, and the addressing comes
  from the same inventory as everything else.

## What the manifest must say

- The DHCP handover. Native UEFI HTTP boot still learns its URL from DHCP:
  option 67 (bootfile-name) is the **full URL** `<bootServerUrl>/mboot.efi`,
  and option 60 must echo `HTTPClient` — the client sends a vendor class
  beginning `HTTPClient` and the firmware ignores an offer that does not echo
  it, falling back to a PXE path that is not configured.
- The post-install step. The kickstart ends in `reboot` and the BIOS pass
  leaves the NIC ahead of the disk; together that is an infinite reinstall
  loop. Remove `<bootServerUrl>/01-<mac>/` for the host, or flip the boot
  order back. And turn Secure Boot back on.
- Secure Boot, off for the install and on afterwards, as its own BIOS item.

## Acceptance

- The example inventory renders a kickstart per host and a boot config per MAC,
  the latter at `01-<mac>/boot.cfg`.
- Given the ISO's `boot.cfg`, the rewritten one keeps `modules=`, `build=` and
  `updated=`, has `kernel=b.b00` with no leading slash, and a `prefix=` that
  points at the payload rather than the kickstart directory. Without it: a
  finding and no boot configs, but the kickstarts still render.
- Generated kickstart contains no literal credential; `${esx_root_hash}`
  survives to output and is masked from any finding.
- No kickstart can emit `--disk=--firstdisk…` or a runtime device name; each is
  a finding with a fix.
- Network values are consistent with what the same inventory renders into the
  `SddcSpec` — one source, two artifacts, no drift.
- A host missing `provisioningMac` or `bootDisk`, or naming an unstable one, is
  a finding with a fix, not a silently incomplete file.
- With `provisioning.hostWorkarounds` empty (the default), the kickstart is
  byte-identical to before this feature existed. With `consumer-amd` present,
  all four settings appear in `%firstboot` before its own reboot, and none of
  it appears unless requested.
- A host with `memoryTieringGb > 0` and no `consumer-amd` workaround is a
  finding (`VCF-CAP-TIERING-NEEDS-WORKAROUND`), not a silently optimistic
  capacity plan.
- `%firstboot` waits for hostd before its first command, enters maintenance
  mode before the tiering command, and leaves it before the reboot.
- A host with `memoryTieringGb > 0` and a stable `memoryTieringDevice` emits
  `esxcli memtier enable -d <path> -r <derived ratio>`; the 9.0 command
  sequence and any `vmware -r` version branch appear nowhere.
- A host with `memoryTieringGb > 0` and no device, an unstable device, or a
  ratio outside 1–400% is a finding and emits no tiering command — but still
  gets its kickstart.
- Any two of `bootDisk`, `vsanDevice` and `memoryTieringDevice` naming one
  device is `VCF-PROV-DEVICE-COLLISION` and no artifacts for that host, with
  the two spellings of a device treated as one.
- `vSwitch0` and `vmk0` take `networks.management.mtu`, and `nsx.fabricMtu`
  never appears in a kickstart.
- No setting is emitted twice, and none of the upstream lines listed as *not
  ported* appears at all.
- With no `provisioning.sshPublicKey`, no `authorized_keys` line exists; with
  one, every host gets it; with a private key, rendering is refused and the
  message does not reproduce the key.
