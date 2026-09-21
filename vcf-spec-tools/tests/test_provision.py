"""Stage 1 provisioning artifacts.

Two kinds of test here. The last one earns its keep because the kickstart and
the rendered SddcSpec must agree about the network, since the whole point of
generating both from one inventory is that they cannot drift. The rest guard
the things that cost a drive to the rack: there is no BMC, so an install that
halts, boots the wrong NIC, or claims the 4 TB vSAN device is not recoverable
from a keyboard.
"""
from __future__ import annotations

import copy

import pytest

from vcfspec.provision import render_provisioning
from vcfspec.render import InsecureCredentialError, render

# A faithful excerpt of the boot.cfg shipped in the ESX 9.1.1.0 ISO: absolute
# kernel and module paths, an empty prefix, and the build-specific lines that
# no generator can invent.
ISO_BOOT_CFG = """\
bootstate=0
title=Loading ESX installer
timeout=5
prefix=
kernel=/b.b00
kernelopt=runweasel cdromBoot
modules=/jumpstrt.gz --- /useropts.gz --- /features.gz --- /k.b00
build=9.1.1.0-25714478
updated=1
"""

MAC1 = "01-bc-24-11-00-0a-01/boot.cfg"


@pytest.fixture
def out(inventory):
    return render_provisioning(inventory, ISO_BOOT_CFG)


def test_one_kickstart_per_host_and_one_boot_config_per_mac(out):
    assert sorted(out.kickstarts) == ["ks-esx01.cfg", "ks-esx02.cfg", "ks-esx03.cfg"]
    assert sorted(out.boot_configs) == [
        "01-bc-24-11-00-0a-01/boot.cfg",
        "01-bc-24-11-00-0a-02/boot.cfg",
        "01-bc-24-11-00-0a-03/boot.cfg",
    ]
    assert out.findings == ()


# --- 1. the boot.cfg has to be the ISO's, rewritten -------------------------

def test_the_boot_cfg_is_the_isos_with_only_prefix_and_kernelopt_rewritten(out):
    """A synthesised boot.cfg cannot boot. The module list is build-specific
    and mboot.efi will not load the installer without it, so the ISO's file is
    the input and this tool rewrites exactly two lines of it.
    """
    body = out.boot_configs[MAC1]
    assert "modules=jumpstrt.gz --- useropts.gz --- features.gz --- k.b00" in body
    assert "build=9.1.1.0-25714478" in body
    assert "updated=1" in body
    # Broadcom's procedure: prefix= names the unpacked payload, and kernel=
    # and modules= lose their leading slash so they resolve under it.
    assert "kernel=b.b00" in body
    assert "kernel=/b.b00" not in body
    assert "prefix=http://10.50.10.5/esx/esx-9.1.1.0" in body
    assert "prefix=\n" not in body
    assert "runweasel" not in body     # the ISO's own kernelopt is replaced


def test_the_prefix_is_the_payload_not_the_kickstart_directory(out):
    """prefix= must reach b.b00 and the modules. The kickstarts live
    elsewhere; pointing prefix= at them gives mboot.efi nothing to load.
    """
    body = out.boot_configs[MAC1]
    assert "prefix=http://10.50.10.5/esx\n" not in body
    assert "ks=http://10.50.10.5/esx/ks-esx01.cfg" in body


def test_an_operator_supplied_payload_url_wins(inventory):
    doc = copy.deepcopy(inventory)
    doc["provisioning"]["installerPayloadUrl"] = "http://10.50.10.5/iso/esx/"
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert "prefix=http://10.50.10.5/iso/esx\n" in out.boot_configs[MAC1]


def test_without_the_isos_boot_cfg_there_are_no_boot_configs(inventory):
    """Emitting a boot.cfg we invented is worse than emitting none: it looks
    right, publishes cleanly, and hangs the host at mboot.
    """
    out = render_provisioning(inventory)
    assert out.boot_configs == {}
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-ESX-BOOT-CFG"]
    assert len(out.kickstarts) == 3     # still independently useful


# --- 2. the per-MAC path mboot.efi actually requests ------------------------

def test_the_per_mac_config_is_a_directory_with_the_arp_type_prefix(out):
    """mboot.efi requests `01-<mac>/boot.cfg` beside itself. The `01-` is the
    ARP hardware type and is mandatory; a flat `boot-<mac>.cfg` is never
    requested, so every host silently falls back to the default boot.cfg and
    all three install with one host's addressing.
    """
    for key in out.boot_configs:
        head, _, tail = key.partition("/")
        assert tail == "boot.cfg"
        assert head.startswith("01-")
        assert ":" not in head and head == head.lower()
    assert not any(k.startswith("boot-") for k in out.boot_configs)


def test_the_manifest_lists_the_boot_configs_by_path(out):
    for key in out.boot_configs:
        assert key in out.manifest


# --- 3. disk selection ------------------------------------------------------

@pytest.mark.parametrize("selector,code", [
    ("--firstdisk=local", "VCF-PROV-BOOT-DISK-FIRSTDISK"),
    ("--firstdisk", "VCF-PROV-BOOT-DISK-FIRSTDISK"),
    ("--firstdisk=local,remote", "VCF-PROV-BOOT-DISK-FIRSTDISK"),
    ("mpx.vmhba0:C0:T0:L0", "VCF-PROV-BOOT-DISK-RUNTIME-NAME"),
    ("vmhba1:C0:T2:L0", "VCF-PROV-BOOT-DISK-RUNTIME-NAME"),
    ("/vmfs/devices/disks/mpx.vmhba0:C0:T0:L0",
     "VCF-PROV-BOOT-DISK-RUNTIME-NAME"),
    ("local", "VCF-PROV-BOOT-DISK-NOT-STABLE"),
    ("/dev/sda", "VCF-PROV-BOOT-DISK-NOT-STABLE"),
])
def test_an_unstable_boot_disk_selector_is_refused(inventory, selector, code):
    """--firstdisk orders by driver and PCI enumeration, not by size or role.
    On this hardware it can select the 4 TB vSAN ESA device and repartition
    it, and there is no BMC to watch it happen.
    """
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["bootDisk"] = selector
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == [code]
    assert [f.path for f in out.findings] == ["/hosts/0/bootDisk"]
    assert "ks-esx01.cfg" not in out.kickstarts
    assert MAC1 not in out.boot_configs
    assert "ks-esx02.cfg" in out.kickstarts   # its neighbours still render


@pytest.mark.parametrize("selector", [
    "/vmfs/devices/disks/t10.NVMe____BOOT________________________1TB",
    "naa.6000c2954d3f1a2b3c4d5e6f70819293",
    "eui.0025385291b1f4d2",
    "/vmfs/devices/disks/naa.6000c2954d3f1a2b3c4d5e6f70819293",
])
def test_a_stable_device_identifier_is_accepted(inventory, selector):
    doc = copy.deepcopy(inventory)
    doc["hosts"][0]["bootDisk"] = selector
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    assert f"--disk={selector} " in out.kickstarts["ks-esx01.cfg"]


def test_the_install_line_never_contradicts_itself(out):
    """--novmfsondisk and --overwritevmfs were emitted together. Only one
    survives, and --overwritevsan is mandatory for a rebuild: a device the
    previous install gave to vSAN otherwise fails the install outright.
    """
    line = [ln for ln in out.kickstarts["ks-esx01.cfg"].splitlines()
            if ln.startswith("install ")][0]
    assert "--overwritevmfs" in line
    assert "--overwritevsan" in line
    assert "--novmfsondisk" not in line
    assert "--disk=--firstdisk" not in line


# --- 4. certificates --------------------------------------------------------

def test_firstboot_sets_the_fqdn_then_regenerates_certificates_then_reboots(out):
    """ESX generates certificates before the hostname exists, so the host
    presents CN=localhost.localdomain and VCF Installer rejects it on the FQDN
    comparison. Order matters: FQDN, then regenerate, then reboot.
    """
    body = out.kickstarts["ks-esx01.cfg"]
    fqdn = "esx01.vcf.lab.knowledgeondemand.net"
    hostname = body.index(f"esxcli system hostname set --fqdn={fqdn}")
    certs = body.index("/sbin/generate-certificates")
    reboot = body.index("esxcli system shutdown reboot")
    assert body.index("%firstboot") < hostname < certs < reboot


# --- 5. Secure Boot ---------------------------------------------------------

def test_secure_boot_is_an_explicit_bios_step_in_the_manifest(out):
    """%firstboot is silently skipped under Secure Boot: SSH, NTP and the
    certificate regeneration do not run and the host still looks installed.
    """
    assert "Secure Boot OFF" in out.manifest
    assert "Turn Secure Boot back ON." in out.manifest
    assert "%firstboot is silently" in out.manifest


# --- 6. the management NIC --------------------------------------------------

def test_the_network_line_pins_vmk0_to_the_provisioning_nic(out):
    """Without --device= the installer binds vmk0 to whichever NIC it liked.
    VCF 9.1 wants vmk0 on a single management NIC.
    """
    assert "--device=bc:24:11:00:0a:01" in out.kickstarts["ks-esx01.cfg"]
    assert "--device=bc:24:11:00:0a:03" in out.kickstarts["ks-esx03.cfg"]


# --- 7. boot-time networking ------------------------------------------------

def test_kernelopt_carries_static_addressing_and_the_boot_nic(out):
    """The provisioning VLAN has no DHCP for ESX. Without these the installer
    falls back to DHCP, or brings up the wrong NIC, and never fetches the
    kickstart -- which on a BMC-less host is a blank screen and a drive.
    """
    line = [ln for ln in out.boot_configs[MAC1].splitlines()
            if ln.startswith("kernelopt=")][0]
    for token in ("bootproto=static", "netdevice=bc:24:11:00:0a:01",
                  "nameserver=10.50.10.5", "ip=10.50.10.11",
                  "netmask=255.255.255.0", "gateway=10.50.10.1",
                  "ks=http://10.50.10.5/esx/ks-esx01.cfg"):
        assert token in line


# --- 8. the root credential -------------------------------------------------

def test_the_root_credential_is_a_hash_reference_never_a_password(out):
    """A kickstart sits on unauthenticated HTTP for the whole provisioning
    VLAN to read. The tool must never be the thing that puts a secret there,
    and with --iscrypted the published artifact need not hold one at all.
    """
    for body in out.kickstarts.values():
        assert "rootpw --iscrypted ${esx_root_hash}" in body
    joined = "\n".join(out.kickstarts.values()) + out.manifest
    assert "VMware1!" not in joined and "hunter2" not in joined


def test_a_plaintext_literal_password_is_refused_outright(inventory):
    doc = copy.deepcopy(inventory)
    doc["credentials"]["esxRootHash"] = "S3cret!Passw0rd"
    with pytest.raises(InsecureCredentialError) as excinfo:
        render_provisioning(doc, ISO_BOOT_CFG)
    assert "S3cret!Passw0rd" not in str(excinfo.value)


def test_a_sha512_crypt_hash_literal_is_allowed_through(inventory):
    """The collision: `$6$salt$hash` and `${reference}` both start with `$`,
    and REFERENCE_RE rejects the hash, so without an explicit branch a hash
    falls into the plaintext refusal. A hash is not a secret -- publishing one
    is the entire point of --iscrypted.
    """
    crypt = "$6$rounds9$" + "a" * 86
    doc = copy.deepcopy(inventory)
    doc["credentials"]["esxRootHash"] = crypt
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert out.findings == ()
    assert f"rootpw --iscrypted {crypt}" in out.kickstarts["ks-esx01.cfg"]


@pytest.mark.parametrize("weak", ["$1$salt$abc", "$5$salt$abc", "$6$", "$"])
def test_a_non_sha512_hash_is_not_mistaken_for_one(inventory, weak):
    doc = copy.deepcopy(inventory)
    doc["credentials"]["esxRootHash"] = weak
    with pytest.raises(InsecureCredentialError):
        render_provisioning(doc, ISO_BOOT_CFG)


def test_a_missing_root_hash_is_a_finding_not_a_passwordless_host(inventory):
    doc = copy.deepcopy(inventory)
    del doc["credentials"]["esxRootHash"]
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-ROOT-HASH"]
    assert out.kickstarts == {} and out.boot_configs == {}


# --- 9. manifest gaps -------------------------------------------------------

def test_the_manifest_breaks_the_reinstall_loop(out):
    """The kickstart ends in `reboot` and the BIOS pass leaves the NIC first.
    Both together reinstall the host forever, and nothing said to undo it.
    """
    assert "AFTER each host installs" in out.manifest
    assert "01-<mac>/" in out.manifest
    assert "reinstalls" in out.manifest
    # The instruction has to survive as one step, not as three lines that
    # happen to mention the right words somewhere in the file.
    after = out.manifest.split("AFTER each host installs", 1)[1]
    assert "01-<mac>/" in after and "boot order" in after


def test_the_manifest_gives_the_dhcp_handover(out):
    """Native UEFI HTTP boot still learns its URL from DHCP. The manifest
    never mentioned DHCP at all, so the mechanism had no starting point.
    """
    assert "option 67" in out.manifest
    assert "http://10.50.10.5/esx/mboot.efi" in out.manifest
    assert "HTTPClient" in out.manifest
    assert "option 60" in out.manifest


# --- unchanged guarantees ---------------------------------------------------

def test_netmask_is_derived_from_the_declared_subnet(out):
    assert "--netmask=255.255.255.0" in out.kickstarts["ks-esx01.cfg"]


def test_a_tagged_vlan_reaches_both_the_kickstart_and_the_boot_config(out):
    assert "--vlanid=1610" in out.kickstarts["ks-esx01.cfg"]
    assert "vlanid=1610" in out.boot_configs[MAC1]


@pytest.mark.parametrize("vlan", [0, 1])
def test_an_untagged_vlan_is_never_tagged(inventory, vlan):
    """Emitting --vlanid=1 makes the installer tag a network the switch
    expects untagged. With no BMC there is no console to recover the host.
    """
    doc = copy.deepcopy(inventory)
    doc["networks"]["management"]["vlan"] = vlan
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert "vlanid" not in out.kickstarts["ks-esx01.cfg"]
    assert "vlanid" not in out.boot_configs[MAC1]


def test_a_host_without_a_mac_yields_a_finding_and_no_files(inventory):
    doc = copy.deepcopy(inventory)
    del doc["hosts"][1]["provisioningMac"]
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-MAC"]
    assert [f.path for f in out.findings] == ["/hosts/1"]
    assert "ks-esx02.cfg" not in out.kickstarts
    assert "ks-esx01.cfg" in out.kickstarts   # its neighbours still render


def test_a_host_without_a_boot_disk_yields_a_finding_and_no_files(inventory):
    """Better no file than a kickstart that installs over the vSAN device."""
    doc = copy.deepcopy(inventory)
    del doc["hosts"][2]["bootDisk"]
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-BOOT-DISK"]
    assert "ks-esx03.cfg" not in out.kickstarts


def test_no_boot_server_stops_everything(inventory):
    doc = copy.deepcopy(inventory)
    del doc["provisioning"]
    out = render_provisioning(doc, ISO_BOOT_CFG)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-BOOT-SERVER"]
    assert out.kickstarts == {} and out.boot_configs == {}


def test_the_manifest_warns_about_the_unauthenticated_boot_server(out):
    assert "${reference}" in out.manifest
    assert "unauthenticated" in out.manifest
    for name in list(out.kickstarts) + list(out.boot_configs):
        assert name in out.manifest


def test_render_provisioning_touches_no_files_or_sockets(inventory, monkeypatch):
    """Purity is the reason esx_boot_cfg is an argument rather than a path.
    A generator that reads an ISO is a generator that cannot be unit-tested
    without one, and this one is the last check before a host we cannot see.
    """
    import builtins
    import socket

    from vcfspec.rules import load_catalogue

    load_catalogue()     # its lru_cache reads the catalogue; warm it first

    def refuse(*args, **kwargs):     # pragma: no cover - only on failure
        raise AssertionError("provision.py must not do I/O")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    render_provisioning(inventory, ISO_BOOT_CFG)


def test_kickstart_and_sddcspec_agree_about_the_network(inventory):
    """One inventory, two artifacts, no drift. If the renderer and the
    provisioner ever disagree about an address the operator gets a host that
    installs fine and then fails commissioning, which is the expensive shape
    of this bug.
    """
    spec, _ = render(inventory)
    ks = render_provisioning(inventory, ISO_BOOT_CFG).kickstarts

    mgmt = [n for n in spec["networkSpecs"] if n["networkType"] == "MANAGEMENT"][0]
    assert f"--gateway={mgmt['gateway']}" in ks["ks-esx01.cfg"]
    assert f"--vlanid={mgmt['vlanId']}" in ks["ks-esx01.cfg"]

    for host_spec, host in zip(spec["hostSpecs"], inventory["hosts"]):
        body = ks[f"ks-{host['name']}.cfg"]
        assert f"--ip={host['mgmtIp']}" in body
        # The SddcSpec carries the short name; the kickstart needs the FQDN
        # the same subdomain composes. Both come from one field.
        assert f"--hostname={host_spec['hostname']}." in body
