# ESX provisioning: domain-review fixes

**Branch:** `feat/esx-provisioning` · **Date:** 2026-09-20
**Commits:** `6c6a892`, `d4f1ca2`, `bbc4b28`
**Tests:** 407 → 439 passing (32 new). 16 mutations, 16 red, 0 survivors.

All nine findings fixed. The mechanism (native UEFI HTTP boot of `mboot.efi`
with a per-MAC `boot.cfg`) was confirmed correct and is unchanged; only the
details moved.

---

## 1. CRITICAL — the generated boot.cfg could not boot

**Before**

```
prefix=http://10.50.10.5/esx
kernel=/b.b00
kernelopt=ks=http://10.50.10.5/esx/ks-esx01.cfg ip=10.50.10.11 …
```

No `modules=`, no `build=`, no `updated=`, an absolute `kernel=`, and a
`prefix=` pointing at the kickstart directory. `mboot.efi` will not load an
installer with no module list, and that list is build-specific — nothing can
synthesise it.

**After** — `render_provisioning(inventory, esx_boot_cfg: str | None = None)`
takes the ISO's own `boot.cfg` *content* and rewrites exactly two lines:

```
bootstate=0
title=Loading ESX installer
timeout=5
prefix=http://10.50.10.5/esx/esx-9.1.1.0
kernel=b.b00
kernelopt=ks=http://10.50.10.5/esx/ks-esx01.cfg bootproto=static netdevice=bc:24:11:00:0a:01 ip=10.50.10.11 netmask=255.255.255.0 gateway=10.50.10.1 nameserver=10.50.10.5 vlanid=1610
modules=jumpstrt.gz --- useropts.gz --- features.gz --- k.b00
build=9.1.1.0-25714478
updated=1
```

Broadcom's procedure exactly: `prefix=` at the unpacked payload, leading `/`
stripped from `kernel=` and `modules=`, everything else preserved verbatim.
The function stays pure — content, not a path — which is why the argument
exists at all. Without it: `VCF-PROV-NO-ESX-BOOT-CFG` and no boot configs,
while the kickstarts still render.

One judgement call: the rewritten file carries **no comment lines**. The
`boot.cfg` grammar `mboot.efi` parses is `key=value` and comment support is
undocumented, and this is the one file that must load on a host nobody is
watching. Placement info moved to the manifest.

`prefix=` needed a home that is not the kickstart directory, so
`provisioning.installerPayloadUrl` is new, defaulting to
`<bootServerUrl>/esx-<esxVersion>`.

## 2. CRITICAL — per-MAC path `mboot.efi` never requests

**Before:** `boot-bc-24-11-00-0a-01.cfg` (flat file, never requested → all
three hosts silently fall back to the default `boot.cfg` and install with one
host's addressing).

**After:** `01-bc-24-11-00-0a-01/boot.cfg` — a subdirectory sibling to
`mboot.efi`, `01-` being the ARP hardware type. Artifact keys are now paths
and the manifest lists them as such.

## 3. CRITICAL — unsafe, self-contradictory disk selection

**Before**, with the spec's own `--firstdisk=local` example:

```
install --disk=--firstdisk=local --overwritevmfs --novmfsondisk
```

**After:**

```
install --disk=/vmfs/devices/disks/t10.NVMe____BOOT________________________1TB --overwritevmfs --overwritevsan
```

A stable device identifier (`t10.` / `eui.` / `naa.`, bare or under
`/vmfs/devices/disks/`) is now required. Refused with a finding:

| Input | Code |
|---|---|
| `--firstdisk`, `--firstdisk=local`, `--firstdisk=local,remote` | `VCF-PROV-BOOT-DISK-FIRSTDISK` |
| `mpx.vmhba0:C0:T0:L0`, `vmhba1:C0:T2:L0` | `VCF-PROV-BOOT-DISK-RUNTIME-NAME` |
| `local`, `/dev/sda`, anything else | `VCF-PROV-BOOT-DISK-NOT-STABLE` |

### `--novmfsondisk` vs `--overwritevmfs`

**Kept `--overwritevmfs`, dropped `--novmfsondisk`.** Reasoning, recorded in a
comment on the template: there is no BMC, so the only failure mode that costs a
physical visit is an install that *stops and waits*. Without `--overwritevmfs`
a rebuild onto a disk that already carries a VMFS aborts — and "already carries
a VMFS" is the normal state of a lab host being rebuilt. `--novmfsondisk` buys
nothing but the absence of a local datastore on the boot device, which is
cosmetic, harmless to VCF commissioning, and removable over SSH in one command
once the host is up. Trading a recoverable cosmetic defect for an unrecoverable
halt is the wrong way round.

`--overwritevsan` is added for the same reason: a device the previous install
gave to vSAN otherwise fails the install outright.

## 4. CRITICAL — no certificate regeneration

ESX generates its certificates before the hostname is configured, so a fresh
host presents `CN=localhost.localdomain` and VCF Installer rejects it on the
FQDN comparison. `%firstboot` now ends with:

```
esxcli system hostname set --fqdn=esx01.vcf.lab.knowledgeondemand.net
/sbin/generate-certificates
esxcli system shutdown reboot -d 10 -r "hostname and certificate regenerated"
```

The reboot is what makes hostd actually serve the new certificate. Order is
asserted positionally by the regression test, not just by substring presence.

## 5. HIGH — Secure Boot

`%firstboot` is *silently* skipped when UEFI Secure Boot is on: SSH, NTP and
the certificate work above do not run, `kickstart.log` records the skip, and
the host looks installed. Now an explicit BIOS-pass item in the manifest —
**off for the install, on afterwards** — and a note in the kickstart itself.

## 6. HIGH — vmk0 binding

`network … --device=bc:24:11:00:0a:01`, from `provisioningMac`.

### `vmnics`

**Dropped the claim rather than consuming it.** The spec said `vmnics` was
"read by no code today" and that the kickstart would be its first consumer;
it still reads nothing, and it should not. `--device=` wants the NIC that
actually booted, and a MAC names that unambiguously. `vmnicN` enumeration is
assigned by the installer at discovery time — precisely the ambiguity
`--device=` exists to remove — so consuming `vmnics` here would reintroduce
the defect in a different costume. The spec's claim is withdrawn; `vmnics`
remains available for a later consumer that genuinely needs vSwitch uplink
names.

## 7. HIGH — kernelopt networking

Added `bootproto=static`, `netdevice=<mac>`, `nameserver=<ip>`. The
provisioning VLAN has no DHCP for ESX; without these the installer falls back
to DHCP or brings up the wrong NIC and never fetches the kickstart.

`nameserver=` gets the **first** nameserver, not a comma list. The boot-time
option is documented as a single address and a comma list is not; one resolver
is all the installer needs to fetch the kickstart, and the kickstart's own
`--nameserver=` still carries the full list for the installed host. Conservative
on purpose — a malformed kernelopt is a blank screen on a host with no console.

## 8. HIGH — password hash

`rootpw --iscrypted ${esx_root_hash}`, from a new `credentials.esxRootHash`.
It is a separate key from `esxRoot` because the SddcSpec's
`hostSpecs[].credentials.password` needs the real password for commissioning
and a hash will not do there.

The collision, handled explicitly in `_root_hash()`: crypt hashes and this
tool's reference syntax both start with `$`. `REFERENCE_RE` wants `${name}`,
which `$6$salt$hash` fails, so without an explicit branch a perfectly good hash
falls straight into the plaintext refusal. A `$6$` literal (strictly `$6$`, not
`$1$` or `$5$`, and the full 86-character SHA-512 digest) is allowed through
because it is not a plaintext secret; anything else literal still raises
`InsecureCredentialError`. Spec's Credentials section rewritten to match.

## 9. MEDIUM — manifest gaps

- **Reinstall loop.** `reboot` in the kickstart plus a permanently NIC-first
  boot order reinstalls the host forever. The manifest now has an explicit
  post-install step: remove `<bootServerUrl>/01-<mac>/`, or flip the boot order
  back, and turn Secure Boot on.
- **DHCP.** Native UEFI HTTP boot still learns its URL from DHCP. The manifest
  now emits the exact option 67 value (`http://10.50.10.5/esx/mboot.efi`), the
  option 60 `HTTPClient` vendor class, and the fact that the server must *echo*
  `HTTPClient` or the firmware ignores the offer.

---

## Verification

```
407 passed  (baseline, before any change)
439 passed  (after)
```

Mutation test, 16 mutations of the new guards, all red, no survivors:
synthesising `boot.cfg`; dropping the `kernelopt` rewrite; keeping leading
slashes; `prefix=` at the kickstart directory; emitting boot configs with no
ISO file; dropping the `01-` prefix; disabling the boot-disk gate; letting
`--firstdisk` through; dropping `--overwritevsan`; dropping
`generate-certificates`; dropping the Secure Boot BIOS item; dropping
`--device=`; dropping the kernelopt network options; accepting a plaintext
literal; emitting plaintext `rootpw`; dropping the reinstall-loop step and the
DHCP handover.

One test guards the purity constraint directly: `builtins.open` and
`socket.socket` are monkeypatched to raise for the duration of a render.

`provision.py` is 374 lines, well under the 800 limit.

## Things worth a second opinion

- **`installerPayloadUrl` default.** `<bootServerUrl>/esx-<esxVersion>` is a
  convention this change invents. It is overridable, but if the boot server
  already has a layout, that is the field to set.
- **`prefix=` is per-artifact, not per-host.** All three hosts share one
  payload directory, which is right for a homogeneous lab and wrong the moment
  two hosts need different ESX builds. Not worth solving today.
- **`nameserver=` truncation to one entry** (see 7) is a deliberate
  conservatism, not a limitation of the inventory.
