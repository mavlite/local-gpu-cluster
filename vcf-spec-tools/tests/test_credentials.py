"""The ${reference} rule must hold for *both* document kinds.

It used to run on inventories only, because it was written as a loop over
the lab inventory's root 'credentials' block. vcf_validate_spec advertises
input_kind: "sddc_spec" as a first-class entry point, and on that branch a
document holding two real root passwords came back valid: true with zero
findings -- the vendored VMware schema accepts a real password, because
real passwords are what it is for.
"""
from __future__ import annotations

import json
import re

from vcfspec.api import validate_document
from vcfspec.credentials import credential_findings
from vcfspec.mcp_server import call_handler
from vcfspec.schema import load_schema

SECRET = "VMw@re123!Real"

SDDC_SPEC = {
    "sddcId": "lab01",
    "dnsSpec": {"subdomain": "lab.local", "nameservers": ["10.0.0.1"]},
    "networkSpecs": [],
    "vcenterSpec": {"vcenterHostname": "vc01",
                    "rootVcenterPassword": "${vcenter_root}",
                    "adminUserSsoPassword": "${sso_admin}"},
    "sddcManagerSpec": {"hostname": "sddc01", "rootPassword": "${sddc_root}"},
    "nsxtSpec": {"rootNsxtManagerPassword": "${nsx_admin}",
                 "nsxtAdminPassword": "${nsx_admin}"},
    "hostSpecs": [{"hostname": "esx01",
                   "credentials": {"username": "root",
                                   "password": "${esx_root}"}}],
}

# Every credential position an SddcSpec actually uses, as a dotted path
# into SDDC_SPEC. hostSpecs[].credentials.password is the nested-container
# case; the rest sit outside any credentials block, which is why a
# structural-position-only rule would not have been enough on its own.
POSITIONS = {
    "/vcenterSpec/rootVcenterPassword": ("vcenterSpec", "rootVcenterPassword"),
    "/vcenterSpec/adminUserSsoPassword": ("vcenterSpec", "adminUserSsoPassword"),
    "/sddcManagerSpec/rootPassword": ("sddcManagerSpec", "rootPassword"),
    "/nsxtSpec/rootNsxtManagerPassword": ("nsxtSpec", "rootNsxtManagerPassword"),
    "/nsxtSpec/nsxtAdminPassword": ("nsxtSpec", "nsxtAdminPassword"),
}


def _with_secret_at(*path) -> dict:
    doc = json.loads(json.dumps(SDDC_SPEC))        # deep copy, no mutation
    node = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = SECRET
    return doc


def test_the_clean_spec_has_no_credential_findings():
    assert credential_findings(SDDC_SPEC).findings == ()


def test_every_sddc_credential_position_is_reached():
    for pointer, path in POSITIONS.items():
        result = credential_findings(_with_secret_at(*path))
        assert [f.path for f in result.findings] == [pointer], pointer
        assert result.findings[0].code == "VCF-CRED-NOT-A-REFERENCE"


def test_a_secret_inside_a_host_credentials_block_is_reached():
    doc = _with_secret_at("hostSpecs", 0, "credentials", "password")
    result = credential_findings(doc)
    assert [f.path for f in result.findings] == \
        ["/hostSpecs/0/credentials/password"]


def test_a_credentials_key_no_regex_knows_is_still_reached():
    """The inventory's 'credentials' block is free-form
    ({"type": "object", "minProperties": 1}), so there is no closed set of
    key names to check against. Structural position has to carry it.
    """
    result = credential_findings({"credentials": {"myCustomKey": SECRET}})
    assert [f.path for f in result.findings] == ["/credentials/myCustomKey"]


def test_username_inside_a_credentials_block_is_not_a_secret():
    """SddcCredentials declares username and password; render() emits
    username: root as a literal on purpose. Flagging it would report the
    renderer's own correct output as an insecure credential."""
    assert credential_findings(
        {"credentials": {"username": "root", "password": "${esx_root}"}}
    ).findings == ()


def test_a_settings_object_under_a_credential_shaped_name_is_not_swallowed():
    """Outside a credentials block a credential-shaped *key name* only
    flags a scalar: passwordPolicy is a settings object, not a secret."""
    assert credential_findings(
        {"passwordPolicy": {"minLength": 8}}).findings == ()


def test_validate_document_rejects_an_sddc_spec_holding_real_passwords():
    doc = _with_secret_at("vcenterSpec", "rootVcenterPassword")
    doc["hostSpecs"][0]["credentials"]["password"] = SECRET
    out = validate_document(json.dumps(doc), input_kind="sddc_spec")
    assert out["valid"] is False
    assert "VCF-CRED-NOT-A-REFERENCE" in {f["code"] for f in out["findings"]}
    assert SECRET not in json.dumps(out)


def test_the_mcp_entry_point_rejects_it_too():
    doc = _with_secret_at("vcenterSpec", "rootVcenterPassword")
    result = call_handler("vcf_validate_spec",
                          {"document": json.dumps(doc),
                           "input_kind": "sddc_spec"})
    assert result["valid"] is False
    assert "VCF-CRED-NOT-A-REFERENCE" in {f["code"] for f in result["findings"]}


def test_every_vendored_credential_property_is_covered():
    """An oracle independent of the key name: the vendored schema's own
    *descriptions* say which string properties hold a password. Every one
    of them must be reached, so this fails if VMware adds a credential
    field whose name no rule here anticipates.
    """
    wanted: set[str] = set()

    def collect(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for name, prop in value.items():
                        if isinstance(prop, dict) and prop.get("type") == "string" \
                                and re.search(r"password|passphrase",
                                              str(prop.get("description", "")),
                                              re.IGNORECASE):
                            wanted.add(name)
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(load_schema())
    assert wanted, "the description scan found nothing; the oracle is broken"
    missed = [name for name in sorted(wanted)
              if not credential_findings({name: SECRET}).findings]
    assert missed == []
