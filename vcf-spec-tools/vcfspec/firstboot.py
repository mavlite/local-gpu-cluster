"""The `%firstboot` body of the generated kickstart.

Split out of provision.py because the text is now most of the file and the
text is the documentation: every line here is a command that will run
unattended on a host with no BMC, and the comment above it is the only place
the reason survives.

Nothing here validates anything. provision.py decides which blocks a host
earns and raises the findings; this module only fills templates with values
that have already been checked. That keeps it importable from anywhere and
keeps the guards in one place.

The shape -- wait for hostd, enter maintenance mode, do the work, leave
maintenance mode, reboot -- is ported from William Lam's `vcf-91-in-box`,
`config/KS-ESX01.CFG` (https://github.com/lamw/vcf-91-in-box). His repo
targets VCF 9.1.0.0 / ESX build 25370933; we target 9.1.1.0 / build 25714478,
so every command was re-checked against 9.1.1 rather than copied. Where the
answer differs, the comment says which form was chosen and why.
"""
from __future__ import annotations

# ESX supports a memory-tier ratio of 1..400 percent of DRAM. 100 is parity.
MAX_TIER_RATIO_PCT = 400
MIN_TIER_RATIO_PCT = 1

_PREAMBLE = """\
%firstboot --interpreter=busybox
# NOTE: %firstboot is silently skipped when UEFI Secure Boot is enabled. None
# of the following runs, kickstart.log records the skip, and the host looks
# installed. Secure Boot must be OFF for the install and turned back on after.
#
# Enough to make the host commissionable by VCF; everything else is the
# Installer's job once it takes over.
#
# Structure ported from William Lam's vcf-91-in-box, config/KS-ESX01.CFG
# (https://github.com/lamw/vcf-91-in-box): wait for hostd, enter maintenance
# mode, do the work, leave maintenance mode, reboot. The earlier version of
# this block started issuing commands immediately, which races hostd -- every
# vim-cmd below fails if it wins that race, and on a host with no BMC a
# half-configured install is invisible until commissioning rejects it.

# hostd is not up when %firstboot starts. runtimeinfo is the cheapest call
# that keeps failing until it is, so this loop is the gate for everything
# after it. There is deliberately no attempt limit: a host that never brings
# hostd up is broken in a way a timeout cannot improve, and looping leaves the
# evidence in kickstart.log instead of continuing into a pile of failures.
while ! vim-cmd hostsvc/runtimeinfo > /dev/null 2>&1; do
  sleep 10
done

# Maintenance mode. Not merely tidy: ESX 9.1 stopped requiring a reboot to
# apply NVMe memory tiering and requires maintenance mode instead, so the
# tiering block below will not apply without this. Exited further down,
# before the reboot, because VCF refuses to commission a host that is in it.
esxcli system maintenanceMode set -e true

"""

_SERVICES = """\
# SSH: the operator's only way in on a host with no BMC, and how VCF reaches
# it during commissioning.
vim-cmd hostsvc/enable_ssh
vim-cmd hostsvc/start_ssh
# Enabling SSH raises a permanent host warning. On one host that is cosmetic;
# on three it is three standing yellow alarms in vCenter that hide real ones.
# NOT enabling the ESXi Shell, which Lam's script does alongside this: with no
# BMC there is no remote console to use a local shell from, so it would be
# attack surface with no operator benefit here.
esxcli system settings advanced set -o /UserVars/SuppressShellWarning -i 1
esxcli system ntp set --enabled=yes {ntp_opts}

# Rename the local VMFS datastore. Every ESX install names it "datastore1", so
# three hosts arrive at vCenter with three datastores of the same name and
# vCenter disambiguates them by appending (1)/(2) in registration order --
# which is not stable, so the suffix a host gets is not either. Named from the
# host, not from storage.datastoreName: that field names the vSAN datastore
# this cluster is about to build, and reusing it here would collide with it.
vim-cmd hostsvc/datastore/rename datastore1 {local_datastore}

# A coredump target. Without one a PSOD leaves nothing to read afterwards, and
# with no BMC the screen it painted is the only other copy -- which means a
# photograph, if anyone happened to be in the room. -s picks the best existing
# dump file rather than assuming a path the installer may not have used.
esxcli system coredump file set -s -e true

# vSwitch0 and vmk0 MTU, from networks.management.mtu ({mtu}) and deliberately
# NOT from nsx.fabricMtu. vmk0 is the management vmkernel and must match the
# MTU the management VLAN actually carries; pinning it to the 9000 fabric MTU
# (as Lam's script hardcodes) would black-hole exactly the commissioning
# traffic this host exists to receive. nsx.fabricMtu describes the NSX
# transport fabric on the VDS that VCF builds during commissioning -- vSwitch0
# is replaced at that point and never carries a fabric frame.
esxcli network vswitch standard set -m {mtu} -v vSwitch0
esxcli network ip interface set -i vmk0 -m {mtu}

# No "VM Network" portgroup VLAN line here, deliberately. Lam's script sets
# one because his install line asks the installer to create that portgroup;
# ours does not, so no such portgroup exists and the command would fail on
# every host. vmk0's own portgroup takes its VLAN from the `network`
# directive in the kickstart above, which is the only VLAN tag this host needs
# before VCF replaces vSwitch0 with a VDS. (The tag itself is deliberately not
# repeated in this comment: a generated file that merely mentions a VLAN
# option is indistinguishable, to a grep, from one that sets it.)

"""

_SSH_KEY = """\
# Operator SSH public key (provisioning.sshPublicKey). A public key is not a
# credential: it authenticates its holder to this host and discloses nothing
# to anyone who reads it off the boot server. Without it the only way in is
# the root password, which is the thing this file works hardest not to carry.
mkdir -p /etc/ssh/keys-root
echo '{ssh_public_key}' > /etc/ssh/keys-root/authorized_keys
chmod 600 /etc/ssh/keys-root/authorized_keys

"""

# Version choice, stated out loud because it is the one thing most likely to
# be wrong later. Lam's script branches on `vmware -r`: 9.0 gets a triple
# (MemoryTiering kernel setting, /Mem/TierNvmePct, `esxcli system tierdevice
# create`) plus a reboot; 9.1 gets a single `esxcli memtier enable`. We target
# ESX 9.1.1.0 only, so the 9.1 form is emitted unconditionally and the 9.0
# triple is not emitted at all. No runtime version branch: a kickstart that
# carries both paths would need `vmware -r` parsing to pick one, and the
# wrong branch on a 9.1 host sets a kernel knob that is no longer the
# supported control -- a silent no-op that looks configured.
_MEMORY_TIERING = """\
# NVMe memory tiering (hardware.memoryTieringDevice, hardware.memoryTieringGb).
# ESX 9.1 form: one command, applied in maintenance mode, no reboot. The
# three-command 9.0 sequence it replaced -- a kernel memory-tiering flag, a
# /Mem percentage, and a separate tier-device creation -- is NOT emitted, and
# neither is the `vmware` version probe that would have to choose between
# them. See the block comment above this template in firstboot.py.
#
# (None of the 9.0 command names appear above on purpose: a generated file
# that merely mentions a command is indistinguishable, to a grep, from one
# that runs it.)
#
# -r is the tier size as a percentage of DRAM, derived from the inventory
# rather than pinned at Lam's flat 100: {tier_gb} GB tier against {ram_gb} GB
# DRAM = {ratio}%. The device is normally larger than the tier that ratio asks
# for, and the ratio is what decides how much of it gets used -- which is what
# makes hardware.memoryTieringGb load-bearing here rather than a number only
# the capacity rules ever read.
#
# This host's capacity plan depends on this line: without it the host boots
# with DRAM only, every capacity figure that counted tiered memory is wrong,
# and nothing says so until a VM fails to power on.
esxcli memtier enable -d {tiering_device} -r {ratio}

"""

# Only emitted when provisioning.hostWorkarounds contains "consumer-amd".
# Placed before the tail's reboot so that reboot serves disable_apichv and
# entropySources too: neither of them gets a reboot of its own.
#
# Source: William Lam, "VCF 9.1 Comprehensive ESX Configuration Workarounds
# for Lab Deployments" --
# https://williamlam.com/2026/05/vcf-9-1-comprehensive-esx-configuration-workarounds-for-lab-deployments.html
_CONSUMER_AMD_WORKAROUNDS = """\
# Consumer-AMD host workarounds (provisioning.hostWorkarounds: consumer-amd).
# William Lam, VCF 9.1 comprehensive ESX configuration workarounds for lab
# deployments:
# https://williamlam.com/2026/05/vcf-9-1-comprehensive-esx-configuration-workarounds-for-lab-deployments.html
#
# NSX Edge/VNA deployment fails on consumer AMD: a DPDK vendor check rejects
# the real Ryzen brand string. The value below exists to satisfy that check,
# not to describe this host honestly -- it is not this host's real CPU. No
# reboot needed.
echo 'cpuid.brandstring = "{brand}"' >> /etc/vmware/config
# NVMe memory tiering cannot power on VMs on AMD Ryzen without this. Needs a
# reboot, served by the one below.
echo 'monitor_control.disable_apichv ="TRUE"' >> /etc/vmware/config
# Zen 4/5 entropy collection is slow; widen the kernel's entropy source
# count. Needs a reboot, served by the one below.
esxcli system settings kernel set -s entropySources -v 2
# vSAN's default compression (Zstd) costs more CPU on this hardware than
# LZ4. No reboot needed.
esxcli system settings advanced set -o /VSAN/Vsan2ZdomCompZstd -i 0

"""

_TAIL = """\
# ESX generates its certificates before the hostname is configured, so a
# freshly installed host presents CN=localhost.localdomain. VCF Installer
# compares the certificate common name against the FQDN it is commissioning
# and rejects the host. Set the FQDN, regenerate, then reboot so hostd
# actually serves the new certificate.
esxcli system hostname set --fqdn={fqdn}
/sbin/generate-certificates

# Out of maintenance mode before the reboot, not after: VCF will not
# commission a host that is in it, nothing below needs it, and a host that
# reboots while still in maintenance mode comes back still in it.
esxcli system maintenanceMode set -e false
esxcli system shutdown reboot -d 10 -r "hostname and certificate regenerated"
"""

# Real host CPU models never appear here: the brand string exists to fool a
# DPDK vendor check, not to describe the hardware. Used when hardware.cpuModel
# is absent for a host that requested the consumer-amd workaround.
_GENERIC_EPYC_BRAND = "AMD EPYC 9124"


def workarounds_block(host_workarounds: list[str], cpu_model: str | None) -> str:
    """The %firstboot text for provisioning.hostWorkarounds, or "" when empty.

    consumer-amd is the only value today; an unrecognised one is ignored here
    (the schema enum is what refuses it at validation time), so this stays
    correct if the enum grows.
    """
    if "consumer-amd" not in host_workarounds:
        return ""
    brand = f"AMD EPYC {cpu_model}" if cpu_model else _GENERIC_EPYC_BRAND
    return _CONSUMER_AMD_WORKAROUNDS.format(brand=brand)


def memory_tiering_block(device: str, ratio: int, tier_gb: float,
                         ram_gb: float) -> str:
    """The `esxcli memtier enable` block. Callers gate on the guards first."""
    return _MEMORY_TIERING.format(
        tiering_device=device, ratio=ratio,
        tier_gb=_number(tier_gb), ram_gb=_number(ram_gb))


def ssh_key_block(public_key: str) -> str:
    """authorized_keys for root, or "" when no key was declared."""
    return _SSH_KEY.format(ssh_public_key=public_key) if public_key else ""


def _number(value: float) -> str:
    """96.0 -> "96". Whole GB should not render a decimal point."""
    return str(int(value)) if float(value).is_integer() else str(value)


def render_firstboot(*, fqdn: str, ntp_opts: str, mtu: int,
                     local_datastore: str, ssh_public_key: str,
                     tiering: str, workarounds: str) -> str:
    """Assemble the whole `%firstboot` section.

    `tiering` and `workarounds` arrive pre-rendered (or empty) because only
    provision.py knows whether the host earned them; `ssh_public_key` arrives
    as a value because it is the same for every host.
    """
    return (
        _PREAMBLE
        + _SERVICES.format(ntp_opts=ntp_opts, mtu=mtu,
                           local_datastore=local_datastore)
        + ssh_key_block(ssh_public_key)
        + tiering
        + workarounds
        + _TAIL.format(fqdn=fqdn)
    )
