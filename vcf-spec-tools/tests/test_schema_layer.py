from vcfspec.schema import load_schema
from vcfspec.validate.schema_layer import (PLACEHOLDER_SECRET, declared_properties,
                                           substitute_secrets, validate_against_schema)

MINIMAL = {
    "sddcId": "lab01",
    "dnsSpec": {"subdomain": "lab.local"},
    "networkSpecs": [{"networkType": "MANAGEMENT", "vlanId": 1610}],
    "vcenterSpec": {"vcenterHostname": "vc01",
                    "rootVcenterPassword": "${vcenter_root}"},
}


def test_minimal_spec_with_all_required_nested_fields_is_valid():
    assert validate_against_schema(MINIMAL).valid is True


def test_missing_required_key_reports_pointer_and_code():
    doc = {k: v for k, v in MINIMAL.items() if k != "dnsSpec"}
    result = validate_against_schema(doc)
    assert result.valid is False
    assert "VCF-SCHEMA" in result.codes
    assert any("dnsSpec" in f.message for f in result.findings)


def test_missing_nested_required_field_is_caught():
    doc = {**MINIMAL, "vcenterSpec": {"vcenterHostname": "vc01"}}
    result = validate_against_schema(doc)
    assert any("rootVcenterPassword" in f.message for f in result.findings)


def test_vlan_and_mtu_must_be_integers_not_strings():
    doc = {**MINIMAL, "networkSpecs": [{"networkType": "MANAGEMENT",
                                        "vlanId": "1610"}]}
    result = validate_against_schema(doc)
    assert any(f.path == "/networkSpecs/0/vlanId" for f in result.findings)


def test_placeholders_are_substituted_only_for_validation():
    spec = {"vcenterSpec": {"rootVcenterPassword": "${vcenter_root}"}}
    substituted = substitute_secrets(spec)
    assert substituted["vcenterSpec"]["rootVcenterPassword"] == PLACEHOLDER_SECRET
    assert spec["vcenterSpec"]["rootVcenterPassword"] == "${vcenter_root}"


def test_placeholder_satisfies_every_password_constraint():
    assert 15 <= len(PLACEHOLDER_SECRET) <= 20


def test_a_pasted_secret_in_a_schema_error_is_redacted():
    doc = {**MINIMAL, "vcenterSpec": {"vcenterHostname": "vc01",
                                      "rootVcenterPassword": "hunter2"}}
    result = validate_against_schema(doc)
    rendered = " ".join(f.message for f in result.findings)
    assert "hunter2" not in rendered
    assert "***REDACTED***" in rendered


def test_declared_properties_reads_the_vendored_schema():
    props = declared_properties(load_schema(), "SddcHostSpec")
    assert props == {"hostname", "credentials", "sshThumbprint", "sslThumbprint"}
