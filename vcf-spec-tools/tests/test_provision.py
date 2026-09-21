"""Stage 1 provisioning artifacts.

The test that earns its keep here is the last one: the kickstart and the
rendered SddcSpec must agree about the network, because the whole point of
generating both from one inventory is that they cannot drift.
"""
from __future__ import annotations

import copy

import pytest

from vcfspec.provision import render_provisioning
from vcfspec.render import InsecureCredentialError, render


def test_one_kickstart_per_host_and_one_boot_config_per_mac(inventory):
    out = render_provisioning(inventory)
    assert sorted(out.kickstarts) == ["ks-esx01.cfg", "ks-esx02.cfg", "ks-esx03.cfg"]
    assert sorted(out.boot_configs) == [
        "boot-bc-24-11-00-0a-01.cfg",
        "boot-bc-24-11-00-0a-02.cfg",
        "boot-bc-24-11-00-0a-03.cfg",
    ]
    assert out.findings == ()


def test_the_root_password_stays_a_reference(inventory):
    """A kickstart sits on unauthenticated HTTP for the whole provisioning
    VLAN to read. The tool must never be the thing that puts a literal there.
    """
    out = render_provisioning(inventory)
    for body in out.kickstarts.values():
        assert "rootpw ${esx_root}" in body
    joined = "\n".join(out.kickstarts.values()) + out.manifest
    assert "VMware1!" not in joined and "hunter2" not in joined


def test_a_literal_password_is_refused_outright(inventory):
    """render() refuses a literal credential; so must this. It matters more
    here, because a kickstart is published to an HTTP server that every host
    on the provisioning VLAN can read.
    """
    doc = copy.deepcopy(inventory)
    doc["credentials"]["esxRoot"] = "S3cret!Passw0rd"
    with pytest.raises(InsecureCredentialError) as excinfo:
        render_provisioning(doc)
    assert "S3cret!Passw0rd" not in str(excinfo.value)


def test_netmask_is_derived_from_the_declared_subnet(inventory):
    out = render_provisioning(inventory)
    assert "--netmask=255.255.255.0" in out.kickstarts["ks-esx01.cfg"]


def test_a_tagged_vlan_reaches_both_the_kickstart_and_the_boot_config(inventory):
    out = render_provisioning(inventory)
    assert "--vlanid=1610" in out.kickstarts["ks-esx01.cfg"]
    assert "vlanid=1610" in out.boot_configs["boot-bc-24-11-00-0a-01.cfg"]


@pytest.mark.parametrize("vlan", [0, 1])
def test_an_untagged_vlan_is_never_tagged(inventory, vlan):
    """Emitting --vlanid=1 makes the installer tag a network the switch
    expects untagged. With no BMC there is no console to recover the host.
    """
    doc = copy.deepcopy(inventory)
    doc["networks"]["management"]["vlan"] = vlan
    out = render_provisioning(doc)
    assert "vlanid" not in out.kickstarts["ks-esx01.cfg"]
    assert "vlanid" not in out.boot_configs["boot-bc-24-11-00-0a-01.cfg"]


def test_a_host_without_a_mac_yields_a_finding_and_no_files(inventory):
    doc = copy.deepcopy(inventory)
    del doc["hosts"][1]["provisioningMac"]
    out = render_provisioning(doc)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-MAC"]
    assert [f.path for f in out.findings] == ["/hosts/1"]
    assert "ks-esx02.cfg" not in out.kickstarts
    assert "ks-esx01.cfg" in out.kickstarts   # its neighbours still render


def test_a_host_without_a_boot_disk_yields_a_finding_and_no_files(inventory):
    """Better no file than a kickstart that installs over the vSAN device."""
    doc = copy.deepcopy(inventory)
    del doc["hosts"][2]["bootDisk"]
    out = render_provisioning(doc)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-BOOT-DISK"]
    assert "ks-esx03.cfg" not in out.kickstarts


def test_no_boot_server_stops_everything(inventory):
    doc = copy.deepcopy(inventory)
    del doc["provisioning"]
    out = render_provisioning(doc)
    assert [f.code for f in out.findings] == ["VCF-PROV-NO-BOOT-SERVER"]
    assert out.kickstarts == {} and out.boot_configs == {}


def test_the_manifest_warns_about_the_unauthenticated_boot_server(inventory):
    out = render_provisioning(inventory)
    assert "${reference}" in out.manifest
    assert "unauthenticated" in out.manifest
    for name in list(out.kickstarts) + list(out.boot_configs):
        assert name in out.manifest


def test_kickstart_and_sddcspec_agree_about_the_network(inventory):
    """One inventory, two artifacts, no drift. If the renderer and the
    provisioner ever disagree about an address the operator gets a host that
    installs fine and then fails commissioning, which is the expensive shape
    of this bug.
    """
    spec, _ = render(inventory)
    ks = render_provisioning(inventory).kickstarts

    mgmt = [n for n in spec["networkSpecs"] if n["networkType"] == "MANAGEMENT"][0]
    assert f"--gateway={mgmt['gateway']}" in ks["ks-esx01.cfg"]
    assert f"--vlanid={mgmt['vlanId']}" in ks["ks-esx01.cfg"]

    for host_spec, host in zip(spec["hostSpecs"], inventory["hosts"]):
        body = ks[f"ks-{host['name']}.cfg"]
        assert f"--ip={host['mgmtIp']}" in body
        # The SddcSpec carries the short name; the kickstart needs the FQDN
        # the same subdomain composes. Both come from one field.
        assert f"--hostname={host_spec['hostname']}." in body
