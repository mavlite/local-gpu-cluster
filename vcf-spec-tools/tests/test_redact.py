import vcfspec.inventory
import vcfspec.redact
from vcfspec.redact import MASK, redact


def test_reference_re_is_the_one_from_inventory_not_a_second_copy():
    """Finding 13: redact.py used to define its own REFERENCE_RE,
    byte-identical to inventory.py's -- two independently maintained
    copies of the project's single most load-bearing pattern, which is
    exactly the drift risk credentials.py's own docstring warns about.
    Asserting `is` pins the mechanism (one compiled pattern, imported,
    not re-typed) rather than just its current, coincidentally-matching
    text."""
    assert vcfspec.redact.REFERENCE_RE is vcfspec.inventory.REFERENCE_RE


def test_masks_scalar_secrets_but_keeps_the_containing_dict():
    doc = {"hosts": [{"credentials": {"username": "root", "password": "hunter2"},
                      "hostname": "esx01"}]}
    out = redact(doc)
    assert out["hosts"][0]["credentials"]["password"] == MASK
    assert out["hosts"][0]["credentials"]["username"] == "root"
    assert out["hosts"][0]["hostname"] == "esx01"


def test_masks_vcf_credential_keys_that_do_not_say_password():
    doc = {"credentials": {"esxRoot": "hunter2", "ssoAdmin": "hunter3",
                           "nsxAdmin": "hunter4"}}
    out = redact(doc)
    assert list(out["credentials"].values()) == [MASK, MASK, MASK]


def test_thumbprints_are_not_masked_because_the_installer_needs_them():
    doc = {"sslThumbprint": "AA:BB:CC", "sshThumbprint": "DD:EE:FF"}
    assert redact(doc) == doc


def test_reference_placeholders_survive():
    assert redact({"rootVcenterPassword": "${vcenter_root}"})[
        "rootVcenterPassword"] == "${vcenter_root}"


def test_redacts_a_secret_embedded_in_free_text():
    text = "'hunter2' is too short - 'rootPassword'"
    assert "hunter2" not in redact(text)


def test_does_not_mutate_input():
    doc = {"password": "hunter2"}
    redact(doc)
    assert doc["password"] == "hunter2"


def test_masks_secrets_while_sparing_thumbprints_in_one_payload():
    doc = {"credentials": {"password": "hunter2", "esxRoot": "hunter3"},
           "sslThumbprint": "AA:BB:CC", "rootVcenterPassword": "${vcenter_root}"}
    out = redact(doc)
    assert out["credentials"]["password"] == MASK
    assert out["credentials"]["esxRoot"] == MASK
    assert out["sslThumbprint"] == "AA:BB:CC"
    assert out["rootVcenterPassword"] == "${vcenter_root}"


def test_redacts_a_secret_in_a_pattern_mismatch_message():
    text = "'hunter2pass' does not match '^[a-z]+$'"
    assert "hunter2pass" not in redact(text)


def test_redact_is_idempotent():
    """The orchestrator redacts assembled output at its boundary even though
    schema_layer already redacts its own messages, so redacting twice must
    be harmless: a second pass over already-redacted output must be a no-op.
    """
    doc = {"hosts": [{"credentials": {"username": "root", "password": "hunter2"},
                      "hostname": "esx01"}],
           "credentials": {"esxRoot": "hunter3", "ssoAdmin": "${sso_admin}"},
           "sslThumbprint": "AA:BB:CC",
           "message": "'hunter2pass' does not match '^[a-z]+$'",
           "note": "password: hunter4 must be rotated"}
    once = redact(doc)
    twice = redact(once)
    assert twice == once


def test_masks_a_password_written_in_python_repr_form():
    """jsonschema reprs the whole containing dict, which writes
    'password': 'x' -- a quote sits between the key name and the colon, so
    the inline pattern's "password, optional whitespace, then = or :" never
    matched it. This is the shape that actually leaked, not the shape
    someone imagined.
    """
    text = ("{'hostname': 'esx01', 'credentials': {'username': 'root', "
            "'password': 'VMw@re123!Real'}} is not of type 'array'")
    out = redact(text)
    assert "VMw@re123!Real" not in out
    assert MASK in out


def test_masks_a_short_quoted_secret():
    """A five-character root password is still a root password; the old
    {6,} floor in the quoted-value rule let it through verbatim."""
    assert "Ab3!x" not in redact("'Ab3!x' is too short")


def test_repr_form_reference_placeholders_still_survive():
    assert redact("'password': '${esx_root}'") == "'password': '${esx_root}'"


def test_repr_form_masking_is_idempotent():
    once = redact("{'credentials': {'password': 'VMw@re123!Real'}}")
    assert redact(once) == once
