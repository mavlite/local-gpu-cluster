from vcfspec.inventory import validate_inventory


def test_example_lab_inventory_is_valid(inventory):
    result = validate_inventory(inventory)
    assert result.valid is True, [f.message for f in result.findings]


def test_missing_required_section_is_an_error(make_inventory):
    doc = make_inventory()
    del doc["networks"]
    result = validate_inventory(doc)
    assert result.valid is False
    assert "VCF-INV-SCHEMA" in result.codes


def test_credential_value_instead_of_reference_is_critical(make_inventory):
    doc = make_inventory(**{"credentials.esxRoot": "RealPassword123!"})
    assert "VCF-CRED-NOT-A-REFERENCE" in validate_inventory(doc).codes


def test_reference_grammar_boundaries(make_inventory):
    for bad in ("${1bad}", "$notbraced", "${}", "", "${a b}"):
        doc = make_inventory(**{"credentials.esxRoot": bad})
        assert "VCF-CRED-NOT-A-REFERENCE" in validate_inventory(doc).codes, bad
    doc = make_inventory(**{"credentials.esxRoot": "${esx_root_2}"})
    assert "VCF-CRED-NOT-A-REFERENCE" not in validate_inventory(doc).codes


def test_non_string_credential_is_rejected(make_inventory):
    doc = make_inventory(**{"credentials.esxRoot": 12345})
    assert "VCF-CRED-NOT-A-REFERENCE" in validate_inventory(doc).codes


def test_host_name_must_be_short_not_an_fqdn(make_inventory):
    doc = make_inventory()
    doc["hosts"][0]["name"] = "esx01.lab.local"
    assert "VCF-INV-SCHEMA" in validate_inventory(doc).codes


def test_tep_as_a_network_is_rejected(inventory):
    inventory["networks"]["hostTep"] = {"vlan": 1613, "subnet": "10.50.13.0/24",
                                        "gateway": "10.50.13.1", "mtu": 9000}
    result = validate_inventory(inventory)
    assert result.valid is False
    assert "VCF-INV-SCHEMA" in result.codes


def test_unknown_nested_key_is_rejected(inventory):
    inventory["hosts"][0]["hardware"]["bogusField"] = 1
    assert validate_inventory(inventory).valid is False


def test_unknown_nsx_key_is_rejected(inventory):
    inventory["nsx"]["HOST_TEP"] = {"vlan": 1613}
    assert validate_inventory(inventory).valid is False
