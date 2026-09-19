import copy

import pytest

from vcfspec.render import InsecureCredentialError, render
from vcfspec.schema import load_schema
from vcfspec.validate.schema_layer import validate_against_schema, walk_declared_properties


def test_rendered_spec_satisfies_the_vendor_schema(inventory):
    spec, result = render(inventory)
    schema_result = validate_against_schema(spec)
    assert schema_result.valid is True, [f.message for f in schema_result.findings]
    assert result.valid is True


def test_rendered_spec_uses_only_declared_properties(inventory):
    """The strongest guard in this suite: a genuine recursive walk of the
    rendered tree against schema $defs, not a fixed list of shallow checks.
    No $def in the vendored schema sets additionalProperties: false, so
    jsonschema validation alone (see the test above) cannot catch an
    invented field name at any depth -- this walk is the only thing that
    can. If it reports anything, fix render.py, never this assertion.
    """
    spec, _ = render(inventory)
    schema = load_schema()
    result = walk_declared_properties(schema, "SddcSpec", spec)
    assert result.undeclared == frozenset()


# Deep nodes render.py hand-builds, named explicitly so a walk that quietly
# stops descending is caught by test_walk_visits_named_deep_nodes below,
# not just inferred from an absence of failures.
DEEP_INJECTION_CASES = [
    pytest.param(("hostSpecs", 0, "credentials"), "bogusField",
                 "/hostSpecs/0/credentials/bogusField", id="host-credentials"),
    pytest.param(("nsxtSpec", "nsxtManagers", 0), "bogusManagerField",
                 "/nsxtSpec/nsxtManagers/0/bogusManagerField", id="nsx-manager"),
    pytest.param(("nsxtSpec", "ipAddressPoolSpec", "subnets", 0), "bogusSubnetField",
                 "/nsxtSpec/ipAddressPoolSpec/subnets/0/bogusSubnetField", id="nsx-tep-subnet"),
    pytest.param(("nsxtSpec", "ipAddressPoolSpec", "subnets", 0, "ipAddressPoolRanges", 0),
                 "bogusRangeField",
                 "/nsxtSpec/ipAddressPoolSpec/subnets/0/ipAddressPoolRanges/0/bogusRangeField",
                 id="nsx-tep-range"),
    pytest.param(("vspClusterSpec", "ipv4Pool", "ipRange"), "bogusRangeField",
                 "/vspClusterSpec/ipv4Pool/ipRange/bogusRangeField", id="vsp-ip-range"),
    pytest.param(("datastoreSpec", "vsanSpec", "esaConfig"), "bogusEsaField",
                 "/datastoreSpec/vsanSpec/esaConfig/bogusEsaField", id="vsan-esa-config"),
]


@pytest.mark.parametrize("path, key, pointer", DEEP_INJECTION_CASES)
def test_declared_property_walk_detects_an_injected_key(inventory, path, key, pointer):
    """Proves the guard is alive: inject a bogus key at a depth render.py
    itself builds by hand and confirm the walk reports it at the right
    pointer. A fixed-list check (the previous version of this test) passed
    with all five of these injected -- this is what closes that gap.
    """
    spec, _ = render(inventory)
    spec = copy.deepcopy(spec)
    node = spec
    for part in path:
        node = node[part]
    node[key] = "x"

    schema = load_schema()
    result = walk_declared_properties(schema, "SddcSpec", spec)
    assert pointer in result.undeclared


def test_walk_visits_named_deep_nodes(inventory):
    """A count alone is too weak to prove the walk descends -- assert the
    exact pointers it reached, so a walk that quietly stops early (e.g. an
    unresolved $ref that silently short-circuits) is caught by name.
    """
    spec, _ = render(inventory)
    schema = load_schema()
    result = walk_declared_properties(schema, "SddcSpec", spec)
    expected = {
        "/hostSpecs/0/credentials",
        "/nsxtSpec/nsxtManagers/0",
        "/nsxtSpec/ipAddressPoolSpec/subnets/0",
        "/nsxtSpec/ipAddressPoolSpec/subnets/0/ipAddressPoolRanges/0",
        "/vspClusterSpec/ipv4Pool/ipRange",
        "/datastoreSpec/vsanSpec/esaConfig",
    }
    assert expected <= result.visited


def test_host_names_are_short_not_fqdns(inventory):
    spec, _ = render(inventory)
    assert [h["hostname"] for h in spec["hostSpecs"]] == ["esx01", "esx02", "esx03"]


def test_vlan_and_mtu_are_integers(inventory):
    spec, _ = render(inventory)
    assert all(isinstance(n["vlanId"], int) for n in spec["networkSpecs"])
    assert all(isinstance(n["mtu"], int) for n in spec["networkSpecs"] if "mtu" in n)


def test_esa_is_explicitly_enabled(inventory):
    spec, _ = render(inventory)
    assert spec["datastoreSpec"]["vsanSpec"]["esaConfig"]["enabled"] is True
    assert spec["datastoreSpec"]["vsanSpec"]["failuresToTolerate"] == 1


def test_nsx_carries_managers_vip_and_tep_pool(inventory):
    spec, _ = render(inventory)
    nsxt = spec["nsxtSpec"]
    assert nsxt["nsxtManagers"] == [{"hostname": "nsx01"}]
    assert nsxt["vipFqdn"] == "nsx.lab.local"
    assert nsxt["transportVlanId"] == 1613
    subnet = nsxt["ipAddressPoolSpec"]["subnets"][0]
    assert subnet["cidr"] == "10.50.13.0/24"
    assert subnet["ipAddressPoolRanges"] == [{"start": "10.50.13.20",
                                              "end": "10.50.13.60"}]


def test_vsp_cluster_uses_an_iprange_pool(inventory):
    spec, _ = render(inventory)
    vsp = spec["vspClusterSpec"]
    assert vsp["platformFqdn"] == "vcf.lab.local"
    assert vsp["instanceFqdn"] == "lab01.lab.local"
    assert vsp["ipv4Pool"] == {"ipRange": {"startIpAddress": "10.50.10.100",
                                           "endIpAddress": "10.50.10.115"}}


def test_credentials_stay_references_in_the_output(inventory):
    spec, _ = render(inventory)
    assert spec["vcenterSpec"]["rootVcenterPassword"] == "${vcenter_root}"
    assert spec["hostSpecs"][0]["credentials"]["password"] == "${esx_root}"


def test_defaults_are_reported_as_info_with_provenance(inventory):
    _, result = render(inventory)
    infos = [f for f in result.findings if f.code == "VCF-RENDER-DEFAULT-APPLIED"]
    assert infos and all(f.source_url for f in infos)


def test_missing_required_source_is_critical_not_silent(inventory):
    del inventory["dns"]
    spec, result = render(inventory)
    assert result.valid is False
    assert "VCF-RENDER-UNMAPPED" in result.codes


def test_network_specs_are_ordered_deterministically(inventory):
    first, _ = render(inventory)
    second, _ = render(inventory)
    assert [n["networkType"] for n in first["networkSpecs"]] == \
           [n["networkType"] for n in second["networkSpecs"]]


def test_network_pool_becomes_include_ip_address_ranges(make_inventory):
    """The example inventory has no `pool` on any network, so this branch
    of _network_spec was previously never exercised by the suite. Field
    names (startIpAddress/endIpAddress) verified against
    SddcNetworkSpec.includeIpAddressRanges -> IpRange in the vendored schema.
    """
    inventory = make_inventory(**{
        "networks.management.pool": {"start": "10.50.10.50", "end": "10.50.10.60"},
    })
    spec, result = render(inventory)
    assert result.valid is True

    management = next(n for n in spec["networkSpecs"] if n["networkType"] == "MANAGEMENT")
    assert management["includeIpAddressRanges"] == [
        {"startIpAddress": "10.50.10.50", "endIpAddress": "10.50.10.60"}]

    schema_result = validate_against_schema(spec)
    assert schema_result.valid is True, [f.message for f in schema_result.findings]

    schema = load_schema()
    walk = walk_declared_properties(schema, "SddcSpec", spec)
    assert walk.undeclared == frozenset()


def test_render_refuses_a_literal_credential(inventory):
    """render() is the last code that touches credentials before they leave
    the process; it must not trust that validate_inventory() already ran.
    A literal password raises, and the exception message must never
    contain the value itself.
    """
    inventory["credentials"]["vcenterRoot"] = "Sup3rSecret!"
    with pytest.raises(InsecureCredentialError) as excinfo:
        render(inventory)
    assert "Sup3rSecret!" not in str(excinfo.value)
