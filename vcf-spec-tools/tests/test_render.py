from vcfspec.render import render
from vcfspec.schema import load_schema
from vcfspec.validate.schema_layer import declared_properties, validate_against_schema


def test_rendered_spec_satisfies_the_vendor_schema(inventory):
    spec, result = render(inventory)
    schema_result = validate_against_schema(spec)
    assert schema_result.valid is True, [f.message for f in schema_result.findings]
    assert result.valid is True


def test_rendered_spec_uses_only_declared_properties(inventory):
    spec, _ = render(inventory)
    schema = load_schema()
    checks = [
        (spec, "SddcSpec"),
        (spec["dnsSpec"], "DnsSpec"),
        (spec["vcenterSpec"], "SddcVcenterSpec"),
        (spec["nsxtSpec"], "SddcNsxtSpec"),
        (spec["sddcManagerSpec"], "SddcManagerSpec"),
        (spec["vspClusterSpec"], "SddcVspClusterSpec"),
        (spec["datastoreSpec"], "SddcDatastoreSpec"),
        (spec["datastoreSpec"]["vsanSpec"], "VsanSpec"),
        (spec["networkSpecs"][0], "SddcNetworkSpec"),
        (spec["hostSpecs"][0], "SddcHostSpec"),
    ]
    for node, def_name in checks:
        undeclared = set(node) - declared_properties(schema, def_name)
        assert undeclared == set(), f"{def_name} has undeclared keys {undeclared}"


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
