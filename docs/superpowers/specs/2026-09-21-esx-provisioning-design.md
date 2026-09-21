# Stage 1: ESX provisioning artifacts from the inventory

**Date:** 2026-09-21
**Status:** implemented; revised 2026-09-20 after domain review
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

  The install line is `--disk=<id> --overwritevmfs --overwritevsan`.
  `--overwritevmfs` and `--novmfsondisk` read as a contradictory pair and only
  one survives: with no BMC the only failure that costs a physical visit is an
  install that stops and waits, and without `--overwritevmfs` a reinstall onto
  a disk that already carries a VMFS aborts. `--novmfsondisk` buys nothing but
  the absence of a local datastore, which is cosmetic and removable over SSH.
  `--overwritevsan` is there for the same reason: a device the previous install
  gave to vSAN otherwise fails the install outright, and that is the normal
  state of a lab host being rebuilt.

And one block, because a boot server is a property of the provisioning
environment rather than of any host:

- `provisioning.bootServerUrl` — base URL the kickstart is fetched from, and
  the directory `mboot.efi` and the `01-<mac>/` directories sit in.
- `provisioning.esxVersion` — defaults to the commissioning target above.
- `provisioning.installerPayloadUrl` — where the ISO contents are unpacked,
  which is what `prefix=` points at. Defaults to
  `<bootServerUrl>/esx-<esxVersion>`.

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
