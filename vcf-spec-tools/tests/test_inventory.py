from vcfspec.inventory import load_inventory_schema, validate_inventory


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


# --- Security review 2026-09-19, finding 7: probe cost is hosts x 3 x timeout
#
# Each probe target was bounded (timeout_s, enforced by thread.join), but
# the NUMBER of targets was not: `hosts` declared minItems 1 and no
# maxItems, so host count was limited only by MAX_BYTES/MAX_NODES --
# roughly 20,000 hosts in a legal document. At 3 probes per host (forward
# DNS, reverse DNS, TCP 443) and the default 2.0 s timeout that is ~33
# hours of serial wall-clock from one --probe run.
#
# 64 is VMware's own supported maximum for a vSAN cluster, and this
# inventory describes a single VCF management domain, so no real lab this
# tool targets can approach it. It caps the worst case at 64 x 3 x 2.0 s
# = ~6.4 minutes: still slow, but bounded, operator-initiated and
# Ctrl-C-able, which is what takes this from unbounded to merely patient.

HOSTS_MAXITEMS = 64


def test_hosts_has_an_upper_bound_at_all():
    schema = load_inventory_schema()
    assert schema["properties"]["hosts"]["maxItems"] == HOSTS_MAXITEMS


def test_an_over_large_host_list_is_refused(inventory):
    host = inventory["hosts"][0]
    inventory["hosts"] = [dict(host, name=f"esx-{i:03d}")
                          for i in range(HOSTS_MAXITEMS + 1)]
    result = validate_inventory(inventory)
    assert not result.valid
    assert any(f.code == "VCF-INV-SCHEMA" and f.path == "/hosts"
               for f in result.findings)


def test_the_bound_comfortably_exceeds_any_real_lab(inventory):
    """The bound must not be so tight it rejects a legitimate inventory --
    a guard that blocks real work gets deleted, not fixed."""
    host = inventory["hosts"][0]
    inventory["hosts"] = [dict(host, name=f"esx-{i:03d}")
                          for i in range(HOSTS_MAXITEMS)]
    assert not [f for f in validate_inventory(inventory).findings
                if f.path == "/hosts"]
    assert HOSTS_MAXITEMS > 3 * 8      # the documented lab is 3 hosts
