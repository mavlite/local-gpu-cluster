"""scripts/vendor_schema.py refuses to silently re-vendor a changed schema.

Security review 2026-09-19, finding 4. The script used to compute the
checksum from the bytes it had just written, which attests
integrity-at-rest and nothing about provenance: a MITM'd fetch, a
typosquatted host or a wrong `source` argument produced a fully
self-consistent schema+sidecar pair that vcfspec/schema.py would then
load and certify without complaint.

Every test here fails against the pre-fix script, which overwrote
unconditionally and had no flag at all.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parent.parent / "scripts" / "vendor_schema.py")
VERSION = "9.9.9.9"


def _load_script():
    spec = importlib.util.spec_from_file_location("vendor_schema", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vendor(tmp_path, monkeypatch):
    """Run the script against a throwaway OUT_DIR and a local source file."""
    module = _load_script()
    out_root = tmp_path / "schemas"
    monkeypatch.setattr(module, "OUT_DIR", out_root)

    def run(sddc_properties: dict, *, accept: bool = False) -> tuple[int, Path]:
        upstream = tmp_path / "openapi.json"
        upstream.write_text(json.dumps({
            "info": {"version": VERSION},
            "components": {"schemas": {
                "SddcSpec": {"type": "object", "properties": sddc_properties}}},
        }), encoding="utf-8")
        argv = [VERSION, str(upstream)] + ([module.ACCEPT_FLAG] if accept else [])
        return module.main(argv), out_root / VERSION

    return run


def test_a_first_vendor_writes_the_schema_and_both_digests(vendor):
    code, out = vendor({"sddcId": {"type": "string"}})
    assert code == 0
    text = (out / "sddc-spec.schema.json").read_text(encoding="utf-8")
    assert (out / "sddc-spec.schema.json.sha256").read_text().strip() == \
        hashlib.sha256(text.encode()).hexdigest()
    # The provenance record: the digest of the FETCHED document, which is
    # the thing the old script never recorded at all.
    assert (out / "sddc-spec.source.sha256").read_text().strip()


def test_re_vendoring_the_same_upstream_is_a_no_op(vendor):
    props = {"sddcId": {"type": "string"}}
    assert vendor(props)[0] == 0
    code, out = vendor(props)
    assert code == 0, "an unchanged re-vendor must not need the override flag"


def test_a_changed_upstream_is_refused_and_nothing_is_overwritten(vendor):
    """The finding, exactly: a hostile or simply wrong upstream must not
    be written with a perfectly valid checksum beside it."""
    code, out = vendor({"sddcId": {"type": "string"}})
    assert code == 0
    before = (out / "sddc-spec.schema.json").read_text(encoding="utf-8")
    before_digest = (out / "sddc-spec.schema.json.sha256").read_text()

    code, out = vendor({"sddcId": {"type": "string"},
                        "backdoor": {"type": "string"}})
    assert code == 1
    assert (out / "sddc-spec.schema.json").read_text(encoding="utf-8") == before
    assert (out / "sddc-spec.schema.json.sha256").read_text() == before_digest
    assert "backdoor" not in before


def test_the_override_flag_is_what_makes_a_change_land(vendor):
    assert vendor({"sddcId": {"type": "string"}})[0] == 0
    changed = {"sddcId": {"type": "string"}, "newField": {"type": "string"}}
    assert vendor(changed)[0] == 1
    code, out = vendor(changed, accept=True)
    assert code == 0
    text = (out / "sddc-spec.schema.json").read_text(encoding="utf-8")
    assert "newField" in text
    # The sidecar tracks the accepted content, so the runtime check passes.
    assert (out / "sddc-spec.schema.json.sha256").read_text().strip() == \
        hashlib.sha256(text.encode()).hexdigest()


def test_a_refused_run_does_not_even_create_the_directory(vendor, tmp_path,
                                                          monkeypatch):
    """A refusal leaves the filesystem exactly as it found it."""
    module = _load_script()
    monkeypatch.setattr(module, "OUT_DIR", tmp_path / "schemas")
    # Nothing vendored yet, so there is nothing to refuse -- the first run
    # must still succeed. This asserts the guard is a change detector, not
    # a blanket "refuse if the directory is absent".
    code, out = vendor({"sddcId": {"type": "string"}})
    assert code == 0 and out.exists()


def test_a_changed_upstream_with_an_identical_bundle_is_still_refused(
        vendor, tmp_path, monkeypatch):
    """The extraction is lossy -- to_draft_2020() drops `example`,
    `discriminator`, `xml` and `externalDocs` -- so "the part we vendor is
    unchanged" is a weaker claim than "upstream is unchanged". Only a
    person can decide the difference does not matter."""
    module = _load_script()
    out_root = tmp_path / "schemas"
    monkeypatch.setattr(module, "OUT_DIR", out_root)
    upstream = tmp_path / "openapi.json"

    def run(example_value, accept=False):
        upstream.write_text(json.dumps({
            "info": {"version": VERSION},
            "components": {"schemas": {"SddcSpec": {
                "type": "object", "example": example_value,
                "properties": {"sddcId": {"type": "string"}}}}},
        }), encoding="utf-8")
        argv = [VERSION, str(upstream)] + ([module.ACCEPT_FLAG] if accept else [])
        return module.main(argv)

    assert run("before") == 0
    out = out_root / VERSION
    bundle = (out / "sddc-spec.schema.json").read_text(encoding="utf-8")
    assert "example" not in bundle          # the change is invisible in the bundle
    assert run("after") == 1                # ...and is refused anyway
    assert run("after", accept=True) == 0


# --- Follow-up: the guard must not fail OPEN when a sidecar is absent ----
#
# Found in re-review. _refuse_on_change treated "no digest recorded" as
# "first run" unconditionally, so deleting both sidecars let a different
# bundle be written with rc=0. Absence of evidence became evidence of
# absence -- the wrong default for an integrity control. A schema already
# on disk is what distinguishes an anomaly from a genuine first vendor.

def test_a_missing_sidecar_beside_an_existing_schema_is_refused(vendor):
    code, out = vendor({"sddcId": {"type": "string"}})
    assert code == 0
    before = (out / "sddc-spec.schema.json").read_text(encoding="utf-8")
    (out / "sddc-spec.schema.json.sha256").unlink()
    (out / "sddc-spec.source.sha256").unlink()

    code, out = vendor({"sddcId": {"type": "string"},
                        "backdoor": {"type": "string"}})
    assert code == 1, "guard failed OPEN with the sidecars deleted"
    assert (out / "sddc-spec.schema.json").read_text(encoding="utf-8") == before
    assert "backdoor" not in before


@pytest.mark.parametrize("victim", ["sddc-spec.schema.json.sha256",
                                    "sddc-spec.source.sha256"])
def test_either_sidecar_going_missing_is_enough_to_refuse(vendor, victim):
    """Neither digest is optional. Removing just one must still refuse --
    otherwise an attacker deletes the one that would have caught them."""
    code, out = vendor({"sddcId": {"type": "string"}})
    assert code == 0
    (out / victim).unlink()
    assert vendor({"sddcId": {"type": "string"},
                   "evil": {"type": "string"}})[0] == 1


def test_a_genuine_first_vendor_is_still_clean(vendor):
    """The fix must not turn every first run into a refusal: no schema and
    no digests is a first vendor, not an anomaly."""
    assert vendor({"sddcId": {"type": "string"}})[0] == 0


def test_the_override_still_resolves_a_missing_sidecar(vendor):
    """The operator's escape hatch has to actually work, or the guard just
    wedges them."""
    code, out = vendor({"sddcId": {"type": "string"}})
    assert code == 0
    (out / "sddc-spec.source.sha256").unlink()
    assert vendor({"sddcId": {"type": "string"}})[0] == 1
    assert vendor({"sddcId": {"type": "string"}}, accept=True)[0] == 0
    assert (out / "sddc-spec.source.sha256").read_text().strip()
