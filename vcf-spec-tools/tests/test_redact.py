from vcfspec.redact import MASK, redact


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
