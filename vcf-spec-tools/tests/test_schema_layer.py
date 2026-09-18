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


def test_a_pasted_secret_at_a_credential_position_never_reaches_the_message():
    """Stronger than "it is masked afterwards": at a credential position the
    offending instance is not put into the message at all, so there is
    nothing for a masker to miss. The message still has to be useful, so it
    must name the position and the constraint that failed.
    """
    doc = {**MINIMAL, "vcenterSpec": {"vcenterHostname": "vc01",
                                      "rootVcenterPassword": "hunter2"}}
    result = validate_against_schema(doc)
    rendered = " ".join(f.message for f in result.findings)
    assert "hunter2" not in rendered
    assert "/vcenterSpec/rootVcenterPassword" in rendered
    assert "minLength" in rendered


def test_nothing_is_echoed_then_masked_at_a_credential_position():
    """Pins the *mechanism*, not just the outcome.

    "The secret is absent" is satisfied two ways: the value was echoed and
    then masked, or it was never put in the message at all. Only the second
    is what this layer promises, and the difference is not academic --
    masking is pattern-matching, and this codebase has now twice shipped a
    masker that missed a shape nobody had thought of. Asserting the secret's
    absence alone passes with the structural message removed, because
    redact() catches these two particular shapes; asserting that MASK is
    absent too does not, because an echoed-then-masked message necessarily
    contains it.
    """
    doc = {**MINIMAL, "vcenterSpec": {"vcenterHostname": "vc01",
                                      "rootVcenterPassword": "hunter2"}}
    rendered = " ".join(f.message
                        for f in validate_against_schema(doc).findings)
    assert "***REDACTED***" not in rendered


def test_nothing_is_echoed_then_masked_for_a_container_instance():
    secret = "VMw@re123!Real"
    doc = {**MINIMAL, "hostSpecs": {"hostname": "esx01",
                                    "credentials": {"username": "root",
                                                    "password": secret}}}
    rendered = " ".join(f.message
                        for f in validate_against_schema(doc).findings)
    assert secret not in rendered
    assert "***REDACTED***" not in rendered


def test_a_short_secret_at_a_credential_position_is_not_echoed_either():
    """redact()'s quoted-value rule used to carry a {6,} length floor, so a
    five-character literal ("'Ab3!x' is too short") was echoed verbatim.
    Length is not a property that distinguishes a secret from a non-secret.
    """
    doc = {**MINIMAL, "vcenterSpec": {"vcenterHostname": "vc01",
                                      "rootVcenterPassword": "Ab3!x"}}
    result = validate_against_schema(doc)
    assert "Ab3!x" not in " ".join(f.message for f in result.findings)


def test_a_container_instance_is_replaced_by_a_pointer_not_reprinted():
    """jsonschema pretty-prints the whole offending instance as a Python
    repr, so a type error one level above a credentials block echoes every
    leaf under it -- including the password -- in a shape ('password': 'x')
    that no inline masker anticipated. A container instance must never be
    put into the message; the pointer says where the problem is instead.
    """
    secret = "VMw@re123!Real"
    doc = {**MINIMAL, "hostSpecs": {"hostname": "esx01",
                                    "credentials": {"username": "root",
                                                    "password": secret}}}
    result = validate_against_schema(doc)
    rendered = " ".join(f.message for f in result.findings)
    assert secret not in rendered
    assert "/hostSpecs" in rendered
    assert "'array'" in rendered


def test_declared_properties_reads_the_vendored_schema():
    props = declared_properties(load_schema(), "SddcHostSpec")
    assert props == {"hostname", "credentials", "sshThumbprint", "sslThumbprint"}
