# Porting the `vcf-91-in-box` `%firstboot` body into the kickstart renderer

**Date:** 2026-09-21
**Branch:** `feat/esx-provisioning`
**Upstream:** [`lamw/vcf-91-in-box`](https://github.com/lamw/vcf-91-in-box),
`config/KS-ESX01.CFG` — a working three-node vSAN ESA VCF lab kickstart on
Minisforum/Ryzen hardware, i.e. the same shape as ours.
**Version caveat:** upstream targets VCF **9.1.0.0 / ESX build 25370933**; we
target **9.1.1.0 / 25714478**. Every command was re-verified against 9.1.1
rather than copied, and each choice is stated in a comment in the generated
file.

## What changed

- `vcfspec/firstboot.py` — new. All `%firstboot` template text plus three block
  builders. `provision.py` was 449 lines and the new text would have put it
  well past the 800-line bound; the seam is text vs. validation, and
  `firstboot.py` validates nothing and imports nothing from `provision.py`.
- `vcfspec/provision.py` — the port's guards and the wiring. Still pure: no
  file, no socket, no clock (the existing purity test still passes).
- `vcfspec/schemas/inventory/v1.schema.json` — `hardware.memoryTieringDevice`,
  `hardware.vsanDevice`, `provisioning.sshPublicKey`.
- `vcfspec/rules/catalogue.yaml` — five new codes.
- `vcfspec/examples/lab-3-host.yaml` — the two device fields on all three hosts.
- `tests/test_provision_firstboot.py` — new, 43 tests. Split out because
  `test_provision.py` went past 800 lines when this section landed.
- `docs/superpowers/specs/2026-09-21-esx-provisioning-design.md` — updated.

## Before → after, commands only

The generated file is heavily commented; this is the executable content.

**Before (8 commands):**

```sh
%firstboot --interpreter=busybox
esxcli system ntp set --enabled=yes --server=10.50.10.5
vim-cmd hostsvc/enable_ssh
vim-cmd hostsvc/start_ssh
echo 'cpuid.brandstring = "AMD EPYC 7945HX"' >> /etc/vmware/config
echo 'monitor_control.disable_apichv ="TRUE"' >> /etc/vmware/config
esxcli system settings kernel set -s entropySources -v 2
esxcli system settings advanced set -o /VSAN/Vsan2ZdomCompZstd -i 0
esxcli system hostname set --fqdn=esx01.vcf.lab.knowledgeondemand.net
/sbin/generate-certificates
esxcli system shutdown reboot -d 10 -r "hostname and certificate regenerated"
```

**After:**

```sh
%firstboot --interpreter=busybox
while ! vim-cmd hostsvc/runtimeinfo > /dev/null 2>&1; do
  sleep 10
done
esxcli system maintenanceMode set -e true
vim-cmd hostsvc/enable_ssh
vim-cmd hostsvc/start_ssh
esxcli system settings advanced set -o /UserVars/SuppressShellWarning -i 1
esxcli system ntp set --enabled=yes --server=10.50.10.5
vim-cmd hostsvc/datastore/rename datastore1 esx01-local
esxcli system coredump file set -s -e true
esxcli network vswitch standard set -m 1500 -v vSwitch0
esxcli network ip interface set -i vmk0 -m 1500
esxcli memtier enable -d /vmfs/devices/disks/t10.NVMe____TIER______________________500G -r 100
echo 'cpuid.brandstring = "AMD EPYC 7945HX"' >> /etc/vmware/config
echo 'monitor_control.disable_apichv ="TRUE"' >> /etc/vmware/config
esxcli system settings kernel set -s entropySources -v 2
esxcli system settings advanced set -o /VSAN/Vsan2ZdomCompZstd -i 0
esxcli system hostname set --fqdn=esx01.vcf.lab.knowledgeondemand.net
/sbin/generate-certificates
esxcli system maintenanceMode set -e false
esxcli system shutdown reboot -d 10 -r "hostname and certificate regenerated"
```

Plus, when `provisioning.sshPublicKey` is declared, between the MTU lines and
the tiering block:

```sh
mkdir -p /etc/ssh/keys-root
echo 'ssh-ed25519 AAAA… operator@lab' > /etc/ssh/keys-root/authorized_keys
chmod 600 /etc/ssh/keys-root/authorized_keys
```

## Decisions

### Memory tiering — the gap that motivated this

`hardware.memoryTieringGb` had been read only by the capacity rules. Nothing
ever enabled tiering, while the lab's N-1 headroom depends on it (192 GB
available against a 219 GB mandatory stack). `%firstboot` now emits the **9.1
form**:

```
esxcli memtier enable -d <device> -r <ratio>
```

The 9.0 sequence upstream version-branches to — a `MemoryTiering` kernel
setting, `/Mem/TierNvmePct`, and `esxcli system tierdevice create`, plus a
reboot — is **not emitted at all**, and neither is the `vmware -r` probe that
would pick between them. We install exactly one build; the 9.0 path on a 9.1
host sets a knob that is no longer the control, which is a silent no-op that
looks configured. Confirmed against the 9.1 docs: the unified `memtier` command
replaced the triple and no longer needs a reboot — it needs **maintenance
mode**, which is why the maintenance-mode wrapper below is load-bearing rather
than tidy.

`-r` is **derived** (`memoryTieringGb / ramGb` as a percentage), not pinned at
upstream's flat 100. The reference lab happens to be 96/96 = 100%, so the
regression test halves the tier and asserts `-r 50` — otherwise a constant and
a derivation are indistinguishable on this inventory. ESX accepts 1–400%;
outside that, nothing is emitted and `VCF-PROV-TIERING-RATIO-UNSUPPORTED` names
both numbers, because `-r 521` is a command that fails on a host nobody is
watching.

### Readiness and maintenance mode

Wait on `vim-cmd hostsvc/runtimeinfo` until hostd answers → enter maintenance
mode → do the work → leave maintenance mode → reboot. The previous block issued
commands immediately, which races hostd; with no BMC a half-configured install
is invisible until commissioning rejects it. Leaving maintenance mode is
equally load-bearing in the other direction: VCF will not commission a host that
is in it, and a host that reboots while in it comes back still in it.

### One device, one role

`bootDisk`, `vsanDevice` and `memoryTieringDevice` each consume the device they
are given and none asks whether something else is already there. Any two naming
one device — compared after normalising the `/vmfs/devices/disks/` prefix, so
the two spellings are one device — is `VCF-PROV-DEVICE-COLLISION` and
**withholds that host's artifacts**, the same judgement `bootDisk` already
makes. `vsanDevice` is new and optional, and exists only so this check has
something to compare against; a lab that has not written it down still renders.

### MTU source: `networks.management.mtu`, not `nsx.fabricMtu`

Upstream hardcodes 9000 on both `vSwitch0` and `vmk0`. Our inventory declares
both MTUs: management 1500, fabric 9000.

`vmk0` is the management vmkernel and must match the MTU the management VLAN
actually carries — 9000 there would black-hole exactly the commissioning
traffic the host exists to receive. `vSwitch0` takes the same value because
until VCF commissions the host it carries nothing but `vmk0`, and `fabricMtu`
describes the NSX transport fabric on the VDS that VCF builds *afterwards*,
replacing `vSwitch0` entirely. So: management, for both, and `nsx.fabricMtu`
never reaches a kickstart. A test moves `management.mtu` to 9000 and
`fabricMtu` to 1600 and asserts both lines follow management.

### Rejected as already covered (would otherwise have been emitted twice)

| Upstream line | Why |
|---|---|
| `esxcli system ntp set` | already emitted |
| `vim-cmd hostsvc/enable_ssh` / `start_ssh` | already emitted |
| `/VSAN/Vsan2ZdomCompZstd` | already in the consumer-AMD block |
| `generate-certificates` | already emitted (as `/sbin/`, not upstream's `/bin/`) |

A parametrised test asserts each appears exactly once.

### Rejected on the merits

- **`esxcli network vswitch standard portgroup set -p "VM Network"`** —
  upstream's install line passes `--addvmportgroup=1`; ours passes `0`, so the
  portgroup does not exist and the command would fail on every host. This is
  our VM Network VLAN handling: the management portgroup's tag comes from the
  `network` directive, and nothing else needs one before VCF builds the VDS.
  The test asserts both halves together, so whichever one changes gets noticed.
- **`/Net/TcpipDefLROEnabled` and `/Net/UseHwTSO`** — upstream scopes these to
  Intel X710 NICs. That is a property of a NIC, not of this inventory, and
  unconditional emission costs throughput on hardware that does not need it. If
  the lab ever fits X710s it belongs in `provisioning.hostWorkarounds` as its
  own enum value.
- **`enable_esx_shell` / `start_esx_shell`** — with no BMC there is no remote
  console to use a local shell from: attack surface, no operator benefit. The
  shell *warning* is still suppressed, since we do enable SSH.
- **The credential handling** — `VMware1!VMware1!` in plaintext throughout,
  including `rootpw`. Ours stays `rootpw --iscrypted ${esx_root_hash}` and
  refuses a plaintext literal outright. Not tempted for a moment: the kickstart
  sits on unauthenticated HTTP that the whole provisioning VLAN can read.
- **The boot path** — USB stick plus rEFInd. Our per-MAC UEFI HTTP boot
  generator has no upstream equivalent and is strictly more capable for three
  hosts: no media to carry, no per-host imaging pass, and the addressing comes
  from the same inventory as everything else. Also not tempted.

### SSH key injection

`provisioning.sshPublicKey`, optional, written to
`/etc/ssh/keys-root/authorized_keys` with `chmod 600`. A public key is **not a
credential** — it authenticates its holder and discloses nothing to whoever
reads it off the boot server — so a literal is allowed where a password never
is. Two guards, because the value is echoed inside single quotes into a file
that runs as root:

- a **private** key raises `InsecureCredentialError` without echoing it, the
  same refusal a plaintext password gets;
- anything else unrecognised raises `VCF-PROV-SSH-KEY-NOT-PUBLIC` and injects
  nothing, while the kickstarts still render — the host installs, it just has
  no key.

The schema `pattern` is generated from `provision.py`'s own regex, so the two
cannot drift. `${reference}` is rejected here on purpose: it is not a
credential, and rendering an unsubstituted reference into `authorized_keys`
would silently break key access.

## New rule codes

| Code | Effect |
|---|---|
| `VCF-PROV-NO-TIERING-DEVICE` | finding; kickstart still renders, without tiering |
| `VCF-PROV-TIERING-DEVICE-NOT-STABLE` | finding; no tiering command |
| `VCF-PROV-TIERING-RATIO-UNSUPPORTED` | finding; no tiering command (ESX accepts 1–400%) |
| `VCF-PROV-DEVICE-COLLISION` | **no artifacts for that host** |
| `VCF-PROV-SSH-KEY-NOT-PUBLIC` | finding; no key injected |

The three tiering codes are deliberately non-fatal: a host with no tiering
still installs and still commissions, it is just smaller than the capacity plan
assumed. A device collision is fatal to that host's artifacts, because the
failure it prevents is a repartitioned 4 TB vSAN member.

## Tests

453 → **496 passing** (43 new). Every new guard was mutation-tested: 22
mutants, all killed. The mutants include reverting each rejected upstream line
back into the template (VM Network, LRO/TSO, ESXi Shell, a duplicated Zstd
setting), so the *absence* of those lines is pinned by a test rather than by a
comment.

One pre-existing test was adjusted rather than broken:
`test_a_real_document_is_comfortably_within_the_bound` asserted the example
inventory is under 1/100th of `MAX_DOCUMENT_CHARS`. Three hosts' worth of
long device identifiers pushed it past that; the margin is now 50x, which is
still room for the example to double twice, and the property being guarded is
unchanged.

## One thing worth disagreeing with

The generated file is now mostly comments, and twice during this work a
comment's prose broke a test that greps the output for a command it must *not*
contain (`--addvmportgroup=1`, `tierdevice create`). I kept the tests strict
and reworded the comments, because a generated file whose comments read like
commands is exactly as confusing to an operator with `grep` as it is to a test.
But it is a real tension worth naming: the comments are the only place the
reasoning survives on the host, and they now have to avoid saying some things
plainly. If this recurs often, the honest fix is a test helper that strips
comment lines before asserting, not quieter comments.
