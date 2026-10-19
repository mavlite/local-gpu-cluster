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
    assert nsxt["vipFqdn"] == "nsx.vcf.lab.knowledgeondemand.net"
    assert nsxt["transportVlanId"] == 1613
    subnet = nsxt["ipAddressPoolSpec"]["subnets"][0]
    assert subnet["cidr"] == "10.50.13.0/24"
    assert subnet["ipAddressPoolRanges"] == [{"start": "10.50.13.20",
                                              "end": "10.50.13.60"}]


def test_vsp_cluster_uses_an_iprange_pool(inventory):
    spec, _ = render(inventory)
    vsp = spec["vspClusterSpec"]
    assert vsp["platformFqdn"] == "platform.vcf.lab.knowledgeondemand.net"
    assert vsp["instanceFqdn"] == "lab01.vcf.lab.knowledgeondemand.net"
    assert vsp["fleetFqdn"] == "fleet.vcf.lab.knowledgeondemand.net"
    assert vsp["ipv4Pool"] == {"ipRange": {"startIpAddress": "10.50.10.160",
                                           "endIpAddress": "10.50.10.189"}}


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


# render() is called straight from api.render_document(), and a wrong-typed
# scalar used to be iterated character by character INTO THE RETURNED SPEC:
# nsx.managers as a string produced 24 phantom managers, nameservers became
# ['1','0','.','5',...]. Nothing raised -- a string is iterable -- so api.py's
# broad except never fired, the garbled lists passed the vendored-schema
# verify step, and `spec` was returned rather than withheld. api.py's own
# docstring names that risk: "an operator (or an agent) pipes .spec into a
# file and never reads the findings."
@pytest.mark.parametrize("section,key,pointer", [
    ("nsx", "managers", ("nsxtSpec", "nsxtManagers")),
    ("dns", "nameservers", ("dnsSpec", "nameservers")),
    ("ntp", "servers", ("ntpServers",)),
])
@pytest.mark.parametrize("scalar", ["a-string-value", 5, True, {"a": 1}])
def test_a_wrong_typed_sequence_never_reaches_the_rendered_spec(
        make_inventory, section, key, pointer, scalar):
    doc = make_inventory(**{f"{section}.{key}": scalar})
    spec, _ = render(doc)
    node = spec
    for step in pointer:
        node = node[step]
    # Empty, not a per-character explosion and not the raw scalar.
    assert node == [], f"{section}.{key}={scalar!r} rendered as {node!r}"


@pytest.mark.parametrize("scalar", ["esx01", 5, True])
def test_a_wrong_typed_hosts_field_renders_no_host_specs(make_inventory, scalar):
    doc = make_inventory(**{"hosts": scalar})
    spec, _ = render(doc)
    assert spec["hostSpecs"] == []


def test_a_valid_document_renders_exactly_what_it_did_before(inventory):
    """The coercion must be invisible to every well-formed document -- it is
    a guard on malformed input, not a change to the rendering."""
    spec, _ = render(inventory)
    assert spec["dnsSpec"]["nameservers"] == inventory["dns"]["nameservers"]
    assert spec["ntpServers"] == inventory["ntp"]["servers"]
    assert len(spec["hostSpecs"]) == len(inventory["hosts"])
    assert [m["hostname"] for m in spec["nsxtSpec"]["nsxtManagers"]] == \
        inventory["nsx"]["managers"]


# The ninth site, found by re-deriving the list rather than trusting the
# eight the brief enumerated. Unlike the other four this one already failed
# SAFELY -- r["start"] on a string element raised and api.py withheld the
# spec -- so the fix is about diagnostics, not containment: the spec is still
# withheld (an empty ipAddressPoolRanges fails the vendored schema's minItems)
# but the operator now gets VCF-SCHEMA naming the field instead of a generic
# VCF-RENDER-FAILED carrying only an exception class name.
@pytest.mark.parametrize("ranges", ["10.50.60.10-10.50.60.20", 5, True,
                                    {"start": "a", "end": "b"},
                                    ["not-a-dict"], [{"start": "x"}]])
def test_a_malformed_tep_pool_ranges_never_raises_out_of_render(
        make_inventory, ranges):
    doc = make_inventory(**{"nsx.tepPool.ranges": ranges})
    spec, _ = render(doc)           # must not raise
    pool = spec["nsxtSpec"]["ipAddressPoolSpec"]["subnets"][0]
    assert isinstance(pool["ipAddressPoolRanges"], list)
    for entry in pool["ipAddressPoolRanges"]:
        assert set(entry) == {"start", "end"}


def test_a_malformed_tep_pool_ranges_still_withholds_the_spec(make_inventory):
    """Coercing must not turn a withheld spec into a returned one: an empty
    range list fails the vendored schema, which is what keeps it withheld."""
    from vcfspec.api import render_document
    import yaml
    doc = make_inventory(**{"nsx.tepPool.ranges": "10.50.60.10-10.50.60.20"})
    result = render_document(yaml.safe_dump(doc))
    assert result.get("spec") is None
    assert "VCF-SCHEMA" in {f["code"] for f in result["findings"]}


def test_valid_tep_pool_ranges_render_unchanged(inventory):
    spec, _ = render(inventory)
    rendered = spec["nsxtSpec"]["ipAddressPoolSpec"]["subnets"][0]["ipAddressPoolRanges"]
    assert rendered == [{"start": r["start"], "end": r["end"]}
                        for r in inventory["nsx"]["tepPool"]["ranges"]]


# --- fleetFqdn and the sections a new VCF fleet requires --------------------
# Regression cover for the 2026-09-26 bring-up failure: a spec missing
# vspClusterSpec.fleetFqdn validates clean and then fails at deploy time with
# PUBLIC_LCM_COMPONENTS_DEPLOY_FLEET_LCM_FETCH_FAILED, because the installer
# PATCHes fleetLcm with a null fqdn.

def test_fleet_fqdn_is_emitted(inventory):
    spec, _ = render(inventory)
    assert spec["vspClusterSpec"]["fleetFqdn"] == inventory["appliances"]["vsp"]["fleetFqdn"]


def test_the_three_vsp_fqdns_are_distinct(inventory):
    spec, _ = render(inventory)
    vsp = spec["vspClusterSpec"]
    names = [vsp["platformFqdn"], vsp["instanceFqdn"], vsp["fleetFqdn"]]
    assert len(set(names)) == 3, f"fleet/instance/platform must differ: {names}"


def test_inventory_without_fleet_fqdn_is_rejected(inventory):
    """The field is optional in SddcVspClusterSpec.required (VCF_EXTEND must
    omit it), so only the inventory schema can stop a primary instance from
    shipping without it."""
    from vcfspec.inventory import validate_inventory
    del inventory["appliances"]["vsp"]["fleetFqdn"]
    result = validate_inventory(inventory)
    assert any(f.code == "VCF-INV-SCHEMA" for f in result.findings)


def test_operations_spec_has_one_master_node(inventory):
    spec, _ = render(inventory)
    ops = spec["vcfOperationsSpec"]
    assert [n["type"] for n in ops["nodes"]] == ["master"]
    assert ops["nodes"][0]["hostname"] == inventory["appliances"]["operations"]["hostname"]
    assert ops["useExistingDeployment"] is False


def test_required_new_fleet_sections_are_all_emitted(inventory):
    spec, _ = render(inventory)
    for section in ("vcfOperationsSpec", "vcfOperationsCollectorSpec",
                    "vidbSpec", "licenseServerSpec"):
        assert section in spec, f"{section} missing: the installer would stub it"


def test_automation_is_not_emitted(inventory):
    """VCF Automation wants 24 vCPU and is deferrable. Emitting it silently
    would overcommit a 48-core cluster."""
    spec, _ = render(inventory)
    assert "vcfAutomationSpec" not in spec


@pytest.mark.parametrize("section", ["operations", "operationsCollector",
                                     "identityBroker", "licenseServer"])
def test_each_new_appliance_is_required_by_the_schema(inventory, section):
    from vcfspec.inventory import validate_inventory
    del inventory["appliances"][section]
    result = validate_inventory(inventory)
    assert any(f.code == "VCF-INV-SCHEMA" for f in result.findings), \
        f"appliances.{section} must be required, not optional"
