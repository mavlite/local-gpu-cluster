"""The `%firstboot` block ported from lamw/vcf-91-in-box.

Split from tests/test_provision.py, which was over the 800-line bound once
this section landed. The division is by subject, not by size: everything here
asserts something about what runs on the host *after* the install, and every
test says which upstream command it kept, adapted or rejected -- because the
upstream file targets VCF 9.1.0.0 / ESX build 25370933 and we target 9.1.1.0 /
25714478, so "it was in the source" is never the reason a line is here.
"""
from __future__ import annotations

import copy

import pytest

from vcfspec.provision import render_provisioning
from vcfspec.render import InsecureCredentialError

from .test_provision import ISO_BOOT_CFG, MAC1, out       # noqa: F401

# Source: github.com/lamw/vcf-91-in-box, config/KS-ESX01.CFG -- a working
# three-node vSAN ESA kickstart for the same class of Minisforum/Ryzen lab.
# His repo targets VCF 9.1.0.0 / ESX build 25370933; we target 9.1.1.0 /
# 25714478, so each command below is pinned by a test that says what we chose
# and why, not by "it was in the upstream file".

TIER_DEVICE = "/vmfs/devices/disks/t10.NVMe____TIER______________________500G"
BOOT_DEVICE = "/vmfs/devices/disks/t10.NVMe____BOOT________________________1TB"

# A real ed25519 public key's shape. Not a credential: it authenticates its
# holder and discloses nothing to whoever reads it off the boot server.
PUBLIC_KEY = ("ssh-ed25519 "
              "AAAAC3NzaC1lZDI1NTE5AAAAIB2xLm7vQ9Zk0rTncHhfQ1aPmWcXo4dYjKs6RtUv"
              " operator@lab")


def _firstboot(body: str) -> str:
    return body[body.index("%firstboot"):]


# --- 11a. readiness and maintenance mode ------------------------------------

def test_firstboot_waits_for_hostd_before_issuing_any_command(out):
    """Lam's script gates everything on `vim-cmd hostsvc/runtimeinfo`; ours
    used to start issuing commands the instant %firstboot ran. hostd is not up
    then, so every vim-cmd races it -- and on a host with no BMC a
    half-configured install is invisible until commissioning rejects it.
    """
    body = _firstboot(out.kickstarts["ks-esx01.cfg"])
    gate = body.index("while ! vim-cmd hostsvc/runtimeinfo")
    for later in ("vim-cmd hostsvc/enable_ssh",
                  "esxcli system ntp set",
                  "esxcli system maintenanceMode set -e true",
                  "vim-cmd hostsvc/datastore/rename",
                  "esxcli memtier enable",
                  "/sbin/generate-certificates"):
        assert gate < body.index(later), f"{later} runs before hostd is up"


def test_firstboot_enters_maintenance_mode_and_leaves_it_before_the_reboot(out):
    """ESX 9.1 stopped rebooting to apply memory tiering and requires
    maintenance mode instead, so entering it is load-bearing rather than
    tidy. Leaving it is equally load-bearing in the other direction: VCF
    refuses to commission a host that is in maintenance mode, and a host that
    reboots while still in it comes back still in it.
    """
    body = _firstboot(out.kickstarts["ks-esx01.cfg"])
    enter = body.index("esxcli system maintenanceMode set -e true")
    tier = body.index("esxcli memtier enable")
    leave = body.index("esxcli system maintenanceMode set -e false")
    reboot = body.index("esxcli system shutdown reboot")
    assert enter < tier < leave < reboot
    assert body.count("maintenanceMode set -e true") == 1
    assert body.count("maintenanceMode set -e false") == 1


# --- 11b. memory tiering: the gap that motivated the port -------------------

def test_memory_tiering_is_actually_enabled_from_the_inventory(out):
    """The inventory has declared hardware.memoryTieringGb since the capacity
    rules were written, the lab's N-1 headroom depends on it, and nothing ever
    turned tiering on. The device comes from the inventory and the ratio is
    derived from it, not pinned at Lam's flat 100.
    """
    body = out.kickstarts["ks-esx01.cfg"]
    assert f"esxcli memtier enable -d {TIER_DEVICE} -r 100" in body


def test_the_tier_ratio_is_derived_and_not_a_constant(inventory):
    """96 GB of tier against 96 GB of DRAM is 100%, which is also Lam's
    hardcoded value -- so the reference lab alone cannot tell a derived ratio
    from a constant. Halve the tier and the ratio must follow, or
    memoryTieringGb is decorative in the artifact meant to act on it.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["memoryTieringGb"] = 48
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    assert f"-d {TIER_DEVICE} -r 50" in out.kickstarts["ks-esx01.cfg"]
    assert "-r 100" in out.kickstarts["ks-esx02.cfg"]   # its neighbour is not


def test_the_91_form_is_emitted_and_the_90_triple_is_not(out):
    """Lam version-branches on `vmware -r`: 9.0 gets a MemoryTiering kernel
    setting, /Mem/TierNvmePct and `esxcli system tierdevice create` plus a
    reboot; 9.1 gets one `esxcli memtier enable`. We target 9.1.1.0 only, so
    the 9.1 form is emitted unconditionally and the 9.0 path is absent -- no
    runtime branch, because the wrong branch on a 9.1 host sets a kernel knob
    that is no longer the control and looks configured while doing nothing.
    """
    body = out.kickstarts["ks-esx01.cfg"]
    assert "esxcli memtier enable" in body
    for nine_zero in ("tierdevice create", "TierNvmePct",
                      "kernel set -s MemoryTiering", "vmware -r"):
        assert nine_zero not in body


def test_a_bare_device_identifier_is_normalised_to_a_device_path(inventory):
    """`esxcli memtier enable -d` takes a path, not a device name, while
    `install --disk=` accepts either. An operator who pastes the bare t10.
    identifier must not get a command that fails on the host.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["memoryTieringDevice"] = "t10.NVMe____TIER500G"
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    assert ("-d /vmfs/devices/disks/t10.NVMe____TIER500G -r 100"
            in out.kickstarts["ks-esx01.cfg"])


def test_tiering_declared_without_a_device_is_a_finding_not_a_silent_gap(inventory):
    """The whole shape of the bug this port fixes: a number in the capacity
    plan with nothing that acts on it. A host still installs and commissions
    without tiering, so this does not withhold the kickstart -- it says so.
    """
    doc = copy.deepcopy(inventory)
    del doc["hosts"][0]["hardware"]["memoryTieringDevice"]
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-TIERING-DEVICE"]
    assert [f.path for f in out.findings] == ["/hosts/0/hardware"]
    assert "96" in out.findings[0].message      # names the tier it cannot set
    assert "memtier enable" not in out.kickstarts["ks-esx01.cfg"]
    assert "ks-esx01.cfg" in out.kickstarts     # still installs, just smaller
    assert "memtier enable" in out.kickstarts["ks-esx02.cfg"]


@pytest.mark.parametrize("selector", [
    "mpx.vmhba0:C0:T0:L0",
    "vmhba1:C0:T2:L0",
    "/vmfs/devices/disks/mpx.vmhba0:C0:T0:L0",
    "local",
    "/dev/nvme1n1",
])
def test_an_unstable_tiering_device_configures_no_tiering(inventory, selector):
    """`esxcli memtier enable` consumes whatever device it is given. A runtime
    name points at a different device after the next reboot, and on this
    hardware the other candidates are the 4 TB vSAN member and the boot disk.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["memoryTieringDevice"] = selector
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-TIERING-DEVICE-NOT-STABLE"]
    assert [f.path for f in out.findings] == [
        "/hosts/0/hardware/memoryTieringDevice"]
    assert "memtier enable" not in out.kickstarts["ks-esx01.cfg"]


def test_a_tier_ratio_inside_the_esx_range_is_emitted(inventory):
    """0.5 GB against 96 GB rounds to 1%, the lowest ESX accepts, and must
    still be emitted -- the range guard rejects what ESX rejects, not what
    looks small.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["memoryTieringGb"] = 0.5
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    assert "-r 1\n" in out.kickstarts["ks-esx01.cfg"]


def test_a_ratio_esx_cannot_accept_is_a_finding_that_names_both_numbers(inventory):
    """ESX takes 1-400%. Emitting `-r 521` gives a command that fails on a
    host nobody is watching, and the capacity plan still thinks it worked.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["memoryTieringGb"] = 500
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-TIERING-RATIO-UNSUPPORTED"]
    assert [f.path for f in out.findings] == [
        "/hosts/0/hardware/memoryTieringGb"]
    message = out.findings[0].message
    assert "521" in message and "500" in message and "96" in message
    assert "memtier enable" not in out.kickstarts["ks-esx01.cfg"]


def test_a_host_with_no_ram_declared_gets_no_tiering_and_no_zero_division(inventory):
    """A pure renderer must not raise on a document that merely failed the
    capacity rules elsewhere. Ratio 0 is outside ESX's range, so the same
    finding covers it and names the missing number.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["ramGb"] = 0
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-TIERING-RATIO-UNSUPPORTED"]
    assert "memtier enable" not in out.kickstarts["ks-esx01.cfg"]


def test_a_host_that_declares_no_tiering_gets_no_tiering_block(inventory):
    """memoryTieringGb 0 is a statement, not an omission: emit nothing and
    raise nothing.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["memoryTieringGb"] = 0
    del doc["hosts"][0]["hardware"]["memoryTieringDevice"]
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    assert "memtier" not in out.kickstarts["ks-esx01.cfg"]


# --- 11c. one device, one role ----------------------------------------------

@pytest.mark.parametrize("field,other", [
    ("memoryTieringDevice", "bootDisk"),
    ("memoryTieringDevice", "vsanDevice"),
    ("vsanDevice", "bootDisk"),
])
def test_two_roles_on_one_device_yields_no_artifacts_for_that_host(
        inventory, field, other):
    """The expensive mistake this whole area keeps circling. The install
    overwrites the device it is given, vSAN ESA claims the device it is given,
    and `esxcli memtier enable` consumes the device it is given -- none of the
    three asks whether something else is already there, and with no BMC the
    result is a drive to the rack. Better no file than that file.
    """
    doc = copy.deepcopy(inventory)
    host = doc["hosts"][0]
    shared = (host["bootDisk"] if other == "bootDisk"
              else host["hardware"][other])
    host["hardware"][field] = shared
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-DEVICE-COLLISION"]
    assert [f.path for f in out.findings] == ["/hosts/0"]
    assert field in out.findings[0].message and other in out.findings[0].message
    assert "ks-esx01.cfg" not in out.kickstarts
    assert MAC1 not in out.boot_configs
    assert "ks-esx02.cfg" in out.kickstarts     # its neighbours still render


def test_the_same_device_spelled_two_ways_is_still_one_device(inventory):
    """`t10.X` and `/vmfs/devices/disks/t10.X` are the same NVMe. A collision
    check that compares the strings misses exactly the case an operator
    creates by pasting one identifier from `esxcli storage core device list`
    and the other from a device path.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["hardware"]["memoryTieringDevice"] = BOOT_DEVICE.replace(
        "/vmfs/devices/disks/", "")
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-DEVICE-COLLISION"]
    assert "ks-esx01.cfg" not in out.kickstarts


def test_an_undeclared_vsan_device_simply_checks_less(inventory):
    """vsanDevice is optional: a lab that has not written it down still gets
    its kickstarts, and the collision check has one less pair to compare.
    """
    doc = copy.deepcopy(inventory)
    for host in doc["hosts"]:
        del host["hardware"]["vsanDevice"]
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    assert len(out.kickstarts) == 3


# --- 11d. MTU: which of two declared MTUs is the right source ---------------

def test_vswitch0_and_vmk0_take_the_management_mtu_not_the_fabric_mtu(out):
    """The inventory declares both: networks.management.mtu is 1500 and
    nsx.fabricMtu is 9000. vmk0 is the management vmkernel and must match the
    MTU the management VLAN actually carries; Lam's script hardcodes 9000,
    which here would black-hole exactly the commissioning traffic this host
    exists to receive. nsx.fabricMtu belongs to the NSX transport fabric on
    the VDS that VCF builds later -- vSwitch0 is replaced by then.
    """
    body = _firstboot(out.kickstarts["ks-esx01.cfg"])
    assert "esxcli network vswitch standard set -m 1500 -v vSwitch0" in body
    assert "esxcli network ip interface set -i vmk0 -m 1500" in body
    assert "-m 9000" not in body


def test_the_management_mtu_is_the_single_source_for_both(make_inventory):
    """Proven by moving it: a jumbo management network must carry vmk0 and
    vSwitch0 with it, and the fabric MTU must still not be consulted.
    """
    doc = make_inventory(**{"networks.management": {
        "vlan": 1610, "subnet": "10.50.10.0/24",
        "gateway": "10.50.10.1", "mtu": 9000}})
    doc["nsx"]["fabricMtu"] = 1600
    body = render_provisioning(doc, ISO_BOOT_CFG).kickstarts["ks-esx01.cfg"]
    assert "-m 9000 -v vSwitch0" in body
    assert "-i vmk0 -m 9000" in body
    assert "1600" not in body


def test_no_vm_network_portgroup_is_configured_because_none_is_created(out):
    """Lam sets the VM Network portgroup's VLAN because his install line
    passes --addvmportgroup=1. Ours passes 0, so the portgroup does not exist
    and the command would fail on every host. Both halves are asserted
    together: whichever one changes, this test is what notices.
    """
    body = out.kickstarts["ks-esx01.cfg"]
    assert "--addvmportgroup=0" in body
    assert "--addvmportgroup=1" not in body
    assert 'portgroup set -p "VM Network"' not in body


# --- 11e. the rest of his block, where it still applies ---------------------

def test_the_shell_warning_is_suppressed_but_the_esxi_shell_is_not_enabled(out):
    """Enabling SSH raises a permanent host warning; on three hosts that is
    three standing alarms in vCenter that hide real ones. The ESXi Shell is
    deliberately NOT enabled alongside it, unlike Lam's script: with no BMC
    there is no remote console to use a local shell from, so it would be
    attack surface with no operator benefit.
    """
    body = _firstboot(out.kickstarts["ks-esx01.cfg"])
    assert ("esxcli system settings advanced set -o "
            "/UserVars/SuppressShellWarning -i 1") in body
    assert "enable_esx_shell" not in body
    assert "start_esx_shell" not in body


def test_the_local_datastore_is_renamed_per_host_and_not_after_the_vsan_one(out):
    """Every ESX install names it "datastore1", so three hosts arrive at
    vCenter with three identically named datastores that it disambiguates in
    registration order. The name comes from the host; storage.datastoreName
    names the vSAN datastore this cluster is about to build, and reusing it
    here would collide with it.
    """
    for name in ("esx01", "esx02", "esx03"):
        body = out.kickstarts[f"ks-{name}.cfg"]
        assert f"vim-cmd hostsvc/datastore/rename datastore1 {name}-local" in body
        assert "vsan-lab01" not in body


def test_a_coredump_file_is_configured(out):
    """Without one a PSOD leaves nothing to read afterwards, and with no BMC
    the screen it painted is the only other copy.
    """
    assert ("esxcli system coredump file set -s -e true"
            in out.kickstarts["ks-esx01.cfg"])


def test_the_vsan_compression_setting_is_emitted_exactly_once(out):
    """Lam's block and our consumer-AMD block both carry
    /VSAN/Vsan2ZdomCompZstd. Porting his verbatim would have emitted it twice.
    """
    assert out.kickstarts["ks-esx01.cfg"].count("/VSAN/Vsan2ZdomCompZstd") == 1


@pytest.mark.parametrize("duplicable", [
    "esxcli system ntp set",
    "vim-cmd hostsvc/enable_ssh",
    "/sbin/generate-certificates",
    "esxcli system coredump file set",
    "vim-cmd hostsvc/datastore/rename",
])
def test_no_ported_setting_is_emitted_twice(out, duplicable):
    assert out.kickstarts["ks-esx01.cfg"].count(duplicable) == 1


def test_the_lro_and_tso_workaround_is_not_ported(out):
    """Lam disables /Net/TcpipDefLROEnabled and /Net/UseHwTSO "especially for
    Intel 710 nics". That is a NIC-specific workaround, not a property of this
    inventory, and emitting it unconditionally costs throughput on hardware
    that does not need it. If the lab ever fits X710s it belongs in
    provisioning.hostWorkarounds as its own enum value, not here.
    """
    body = out.kickstarts["ks-esx01.cfg"]
    assert "TcpipDefLROEnabled" not in body
    assert "UseHwTSO" not in body


# --- 11f. the SSH public key ------------------------------------------------

def test_no_ssh_key_is_injected_unless_the_inventory_declares_one(out):
    body = out.kickstarts["ks-esx01.cfg"]
    assert "authorized_keys" not in body
    assert "keys-root" not in body


def test_a_declared_public_key_reaches_authorized_keys(inventory):
    """An operator who wants key access should not have to hand-edit a
    generated file. A public key is not a credential, so a literal is fine
    here where a password never is.
    """
    doc = copy.deepcopy(inventory)
    doc["provisioning"]["sshPublicKey"] = PUBLIC_KEY
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    for name in ("esx01", "esx02", "esx03"):
        body = out.kickstarts[f"ks-{name}.cfg"]
        assert f"echo '{PUBLIC_KEY}' > /etc/ssh/keys-root/authorized_keys" in body
        assert "chmod 600 /etc/ssh/keys-root/authorized_keys" in body


def test_a_private_key_is_refused_outright_and_never_echoed(inventory):
    """The one shape of this mistake that is a secret headed for a
    world-readable file. It gets the same refusal a plaintext password gets,
    and the message must not reproduce what it refused.
    """
    doc = copy.deepcopy(inventory)
    doc["provisioning"]["sshPublicKey"] = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n"
        "-----END OPENSSH PRIVATE KEY-----")
    with pytest.raises(InsecureCredentialError) as excinfo:
        render_provisioning(doc, ISO_BOOT_CFG)
    assert "b3BlbnNzaC1rZXk=" not in str(excinfo.value)


@pytest.mark.parametrize("bad", [
    "${ssh_public_key}",                    # a reference, which would render raw
    "/home/operator/.ssh/id_ed25519.pub",   # a path, not the key
    "AAAAC3NzaC1lZDI1NTE5AAAAIB2xLm7v",     # blob with no type
    "ssh-ed25519",                          # type with no blob
    "ssh-ed25519 AAAA' ; rm -rf /",         # would break out of the echo quotes
])
def test_anything_that_is_not_a_public_key_injects_nothing(inventory, bad):
    """Not a credential, so not a refusal -- but it is written verbatim into a
    file that runs as root, so it is not written at all unless it is visibly a
    public key. The kickstarts still render: the host installs, it just has no
    key.
    """
    doc = copy.deepcopy(inventory)
    doc["provisioning"]["sshPublicKey"] = bad
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-SSH-KEY-NOT-PUBLIC"]
    assert [f.path for f in out.findings] == ["/provisioning/sshPublicKey"]
    assert len(out.kickstarts) == 3
    for body in out.kickstarts.values():
        assert "authorized_keys" not in body


# --- 11g. the whole order, once ---------------------------------------------

def test_the_firstboot_block_runs_in_the_order_the_host_needs(out):
    """One assertion for the shape of the ported block, so a future insertion
    in the wrong place fails here rather than on the hardware.
    """
    body = _firstboot(out.kickstarts["ks-esx01.cfg"])
    steps = [
        "while ! vim-cmd hostsvc/runtimeinfo",
        "esxcli system maintenanceMode set -e true",
        "vim-cmd hostsvc/enable_ssh",
        "/UserVars/SuppressShellWarning",
        "esxcli system ntp set",
        "vim-cmd hostsvc/datastore/rename",
        "esxcli system coredump file set",
        "esxcli network vswitch standard set",
        "esxcli network ip interface set -i vmk0",
        "esxcli memtier enable",
        "cpuid.brandstring",
        "esxcli system hostname set --fqdn=",
        "/sbin/generate-certificates",
        "esxcli system maintenanceMode set -e false",
        "esxcli system shutdown reboot",
    ]
    positions = [body.index(step) for step in steps]
    assert positions == sorted(positions), [
        step for step, _ in sorted(zip(steps, positions), key=lambda p: p[1])]


def test_ssh_is_persisted_across_the_reboot_this_file_performs(out):
    """enable_ssh/start_ssh set the RUNNING state; the startup POLICY is
    separate and defaults to off. %firstboot ends by rebooting, so without
    `chkconfig SSH on` the host comes back with SSH shut.

    Observed on all three lab hosts 2026-09-22: port 22 refused while ICMP,
    TLS and the hostd SDK all answered, so it did not present as an SSH
    fault -- it surfaced as VCF's host-connect check failing during
    commissioning.
    """
    body = out.kickstarts["ks-esx01.cfg"]
    assert "chkconfig SSH on" in body
    # Order matters: the policy must be set before the reboot at the end.
    assert body.index("chkconfig SSH on") < body.index("shutdown reboot")


def test_ssh_enable_start_and_policy_are_all_three_present(out):
    """Any one of these alone leaves a gap: policy without start means no SSH
    until the next boot, start without policy means none after it."""
    body = out.kickstarts["ks-esx01.cfg"]
    for command in ("vim-cmd hostsvc/enable_ssh",
                    "vim-cmd hostsvc/start_ssh",
                    "chkconfig SSH on"):
        assert body.count(command) == 1, f"{command!r} not emitted exactly once"
