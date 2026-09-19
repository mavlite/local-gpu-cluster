"""Regression tests for the 2026-09-19 security review, findings 1, 2 and 6.

Findings 1 and 2 were one root cause with two faces: `vcf_version` is a
caller-controlled string that was concatenated straight into a filesystem
path (`schema.py` and `render.py`), which made the SHA-256 sidecar
worthless (the attacker supplied both halves) and turned the tool into a
filesystem existence oracle.

Every test here fails against the pre-fix code. The first one is the
review's own reproduction, run verbatim.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib

import pytest
from vcfspec import render as render_module
from vcfspec import schema as schema_module
from vcfspec.api import validate_document
from vcfspec.inventory import EXAMPLE_PATH
from vcfspec.mcp_server import call_handler
from vcfspec.schema import (DEFAULT_VERSION, UnknownSchemaVersion,
                            known_versions, load_schema, resolve_version)

INVENTORY = EXAMPLE_PATH.read_text(encoding="utf-8")
JUNK_SPEC = "sddcId: x\ngarbage: 1\n"

# The five vectors the review named, each paired with a needle that must
# never show up in a path this package touches. All are short enough to
# clear the MCP schema's maxLength on vcf_version, so each one is rejected
# by resolve_version() -- the gate under test -- and not by the length
# bound sitting in front of it.
_VECTORS = [
    pytest.param("../../../../evil", "evil", id="traversal"),
    pytest.param("/etc/passwd", "passwd", id="absolute-path"),
    pytest.param("9.1.1.0\x00", "\x00", id="null-byte"),
    pytest.param("%2e%2e%2fevil", "%2e", id="url-encoded-traversal"),
    pytest.param("9.9.9.9", "9.9.9.9", id="unknown-but-well-formed"),
]
HOSTILE_VERSIONS = [pytest.param(p.values[0], id=p.id) for p in _VECTORS]


@pytest.fixture(autouse=True)
def clear_caches():
    load_schema.cache_clear()
    render_module._defaults_text_for.cache_clear()
    yield
    load_schema.cache_clear()
    render_module._defaults_text_for.cache_clear()


# --- Finding 1: the integrity bypass, as the review reproduced it ---------

def _plant_evil_schema(directory: pathlib.Path) -> str:
    """Write a schema + a matching sidecar, and return the traversing
    `vcf_version` that reaches it from SCHEMA_DIR. This is the review's
    repro: the attacker supplies BOTH halves of the integrity check, so a
    checksum that binds a file only to its own sibling certifies it."""
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema",
                       "type": "object"}, indent=2) + "\n"
    (directory / "sddc-spec.schema.json").write_text(
        text, encoding="utf-8", newline="\n")
    (directory / "sddc-spec.schema.json.sha256").write_text(
        hashlib.sha256(text.encode()).hexdigest() + "\n",
        encoding="utf-8", newline="\n")
    return os.path.relpath(directory, schema_module.SCHEMA_DIR).replace("\\", "/")


def test_planted_schema_outside_the_package_cannot_certify_a_junk_spec(tmp_path):
    """The review's reproduction. Pre-fix this returned valid=True with
    ZERO findings on `sddcId: x\\ngarbage: 1` -- precisely the outcome this
    package exists to prevent."""
    version = _plant_evil_schema(tmp_path / "evil")
    out = call_handler("vcf_validate_spec",
                       {"document": JUNK_SPEC, "input_kind": "sddc_spec",
                        "vcf_version": version})
    assert out["valid"] is False
    assert "VCF-MCP-BAD-ARGS" in [f["code"] for f in out["findings"]]


def test_a_planted_schema_is_never_read_at_all(tmp_path):
    """Stronger than the envelope check above: prove the file is not even
    opened. A guard that refuses *after* reading the attacker's file would
    still have loaded it."""
    evil_dir = tmp_path / "evil"
    version = _plant_evil_schema(evil_dir)
    target = evil_dir / "sddc-spec.schema.json"
    opened: list[str] = []
    real_read_text = pathlib.Path.read_text

    def spy(self, *args, **kwargs):
        opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pathlib.Path, "read_text", spy)
        call_handler("vcf_validate_spec",
                     {"document": JUNK_SPEC, "input_kind": "sddc_spec",
                      "vcf_version": version})
    assert str(target) not in opened
    assert not any(str(tmp_path) in path for path in opened)


# --- Finding 2: the existence oracle -------------------------------------

def _envelope_for(version: str) -> dict:
    return call_handler("vcf_validate_spec",
                        {"document": JUNK_SPEC, "input_kind": "sddc_spec",
                         "vcf_version": version})


@pytest.mark.parametrize("version", HOSTILE_VERSIONS)
def test_every_rejected_version_yields_one_identical_envelope(version):
    """The oracle fix, stated as the property that actually matters: a
    traversal, an absolute path, a NUL byte, a URL-encoded traversal and a
    plain unknown version are INDISTINGUISHABLE in the returned envelope.

    Pre-fix, the traversal cases differed from each other and from
    "9.9.9.9" -- and differed depending on what was on disk, which is the
    oracle itself.
    """
    assert _envelope_for(version) == _envelope_for("9.9.9.9")


def test_the_envelope_does_not_change_with_what_is_on_disk(tmp_path):
    """The oracle's defining property, tested directly: the SAME rejected
    version must produce the same answer whether or not the path it
    implies exists. Pre-fix, present -> VCF-RENDER-FAILED / a certified
    spec, absent -> VCF-MCP-BAD-ARGS."""
    present = _plant_evil_schema(tmp_path / "evil")
    absent = present + "-does-not-exist"
    assert not (tmp_path / "evil-does-not-exist").exists()
    assert _envelope_for(present) == _envelope_for(absent)


def test_render_defaults_are_not_an_existence_oracle_either(tmp_path):
    """render._defaults_text was the second path, and the worse one for
    disclosure: render.default() echoes `entry['value']!r` into a finding
    message and `entry['source']` into source_url, so any readable
    defaults-shaped .yaml would have had its contents returned verbatim."""
    (tmp_path / "present.yaml").write_text(
        "workflowType:\n  value: LEAKED\n  source: leaked-source\n",
        encoding="utf-8")
    rel = os.path.relpath(tmp_path, render_module.DEFAULTS_DIR).replace("\\", "/")
    present = call_handler("vcf_render_spec",
                           {"document": INVENTORY, "vcf_version": f"{rel}/present"})
    absent = call_handler("vcf_render_spec",
                          {"document": INVENTORY, "vcf_version": f"{rel}/absent"})
    assert present == absent
    assert "LEAKED" not in json.dumps(present)
    assert "leaked-source" not in json.dumps(present)


@pytest.mark.parametrize("version,needle", _VECTORS)
def test_no_hostile_version_reaches_the_filesystem(version, needle, monkeypatch):
    """Mutation-proof for the gate: if resolve_version() is ever removed
    or moved after the path is built, SOMETHING gets stat'd or opened with
    a fragment of the caller's text in it, and this reddens.

    The needle, not the whole version string, is what is searched for:
    pathlib rewrites separators on Windows, so a test looking for the raw
    argument would pass vacuously on exactly the platform the review ran
    the traversal on."""
    touched: list[str] = []
    for name in ("read_text", "exists", "is_file", "is_dir", "iterdir"):
        real = getattr(pathlib.Path, name)

        def spy(self, *args, _real=real, **kwargs):
            touched.append(str(self))
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, name, spy)
    _envelope_for(version)
    assert not any(needle in path for path in touched)


# --- The gate itself ------------------------------------------------------

def test_known_versions_is_discovered_from_disk_not_hardcoded():
    assert DEFAULT_VERSION in known_versions()
    # `schemas/inventory/` holds the inventory schema, not a VCF release:
    # it must not be reachable as a vcf_version.
    assert "inventory" not in known_versions()


@pytest.mark.parametrize("version", HOSTILE_VERSIONS)
def test_resolve_version_refuses_before_any_path_is_built(version):
    with pytest.raises(UnknownSchemaVersion):
        resolve_version(version)


def test_resolve_version_accepts_a_vendored_version():
    assert resolve_version(DEFAULT_VERSION) == DEFAULT_VERSION


def test_a_well_formed_version_is_not_enough_on_its_own():
    """The format regex is an additional filter on the directory listing,
    never the gate. "9.1.1.1" is perfectly well-formed and must still be
    refused, because nothing is vendored for it."""
    with pytest.raises(UnknownSchemaVersion):
        resolve_version("9.1.1.1")


def test_resolve_version_refuses_a_non_string():
    with pytest.raises(UnknownSchemaVersion):
        resolve_version(pathlib.Path(DEFAULT_VERSION))


def test_unknown_version_finding_never_quotes_the_requested_version():
    out = validate_document(JUNK_SPEC, input_kind="sddc_spec",
                            version="../../../../evil")
    blob = json.dumps(out)
    assert "VCF-SCHEMA-VERSION-UNKNOWN" in blob
    assert "evil" not in blob
    assert ".." not in blob


# --- Finding 6: a tampered vendored schema is not a bad argument ----------

def _tamper(tmp_path) -> None:
    """Copy the real vendored schema somewhere writable, corrupt it, and
    point SCHEMA_DIR at it -- so the checksum no longer matches while the
    version stays a legitimately vendored one."""
    dest = tmp_path / DEFAULT_VERSION
    dest.mkdir(parents=True)
    (dest / "sddc-spec.schema.json").write_text(
        json.dumps({"tampered": True}), encoding="utf-8")
    (dest / "sddc-spec.schema.json.sha256").write_text(
        (schema_module.SCHEMA_DIR / DEFAULT_VERSION
         / "sddc-spec.schema.json.sha256").read_text(encoding="utf-8"),
        encoding="utf-8")


def test_tampered_schema_is_critical_and_not_a_retryable_bad_argument(
        tmp_path, monkeypatch):
    """Finding 6. Pre-fix this surfaced as VCF-MCP-BAD-ARGS -- "you typo'd
    an argument, try again" -- for a supply-chain compromise."""
    _tamper(tmp_path)
    monkeypatch.setattr(schema_module, "SCHEMA_DIR", tmp_path)
    load_schema.cache_clear()
    out = call_handler("vcf_validate_spec",
                       {"document": JUNK_SPEC, "input_kind": "sddc_spec"})
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-SCHEMA-INTEGRITY" in codes
    assert "VCF-MCP-BAD-ARGS" not in codes
    assert "VCF-SCHEMA-VERSION-UNKNOWN" not in codes
    assert out["valid"] is False
    integrity = next(f for f in out["findings"] if f["code"] == "VCF-SCHEMA-INTEGRITY")
    assert integrity["severity"] == "critical"
    assert "integrity" in integrity["message"].lower()


def test_tampered_schema_on_the_render_path_is_also_an_integrity_finding(
        tmp_path, monkeypatch):
    _tamper(tmp_path)
    monkeypatch.setattr(schema_module, "SCHEMA_DIR", tmp_path)
    load_schema.cache_clear()
    out = call_handler("vcf_render_spec", {"document": INVENTORY})
    codes = [f["code"] for f in out["findings"]]
    assert "VCF-SCHEMA-INTEGRITY" in codes
    assert "VCF-MCP-BAD-ARGS" not in codes
    assert "spec" not in out


def test_integrity_and_unknown_version_are_different_codes(tmp_path, monkeypatch):
    """The two conditions shared one `except` clause pre-fix. They are
    different events -- one is a caller mistake, one is a compromised
    installation -- and must never collapse back together."""
    _tamper(tmp_path)
    monkeypatch.setattr(schema_module, "SCHEMA_DIR", tmp_path)
    load_schema.cache_clear()
    tampered = call_handler("vcf_validate_spec",
                            {"document": JUNK_SPEC, "input_kind": "sddc_spec"})
    unknown = call_handler("vcf_validate_spec",
                           {"document": JUNK_SPEC, "input_kind": "sddc_spec",
                            "vcf_version": "9.9.9.9"})
    assert ([f["code"] for f in tampered["findings"]]
            != [f["code"] for f in unknown["findings"]])


# --- The MCP argument length bounds (finding 5, server surface) ----------

def test_an_oversized_document_is_refused_at_the_mcp_boundary():
    from vcfspec.mcp_server import MAX_DOCUMENT_CHARS
    out = call_handler("vcf_validate_spec", {"document": "a" * (MAX_DOCUMENT_CHARS + 1)})
    assert out["valid"] is False
    assert [f["code"] for f in out["findings"]] == ["VCF-MCP-BAD-ARGS"]


def test_an_oversized_version_is_refused_at_the_mcp_boundary():
    from vcfspec.mcp_server import MAX_VERSION_CHARS
    out = call_handler("vcf_validate_spec",
                       {"document": INVENTORY,
                        "vcf_version": "9" * (MAX_VERSION_CHARS + 1)})
    assert out["valid"] is False
    assert [f["code"] for f in out["findings"]] == ["VCF-MCP-BAD-ARGS"]


def test_a_real_document_is_comfortably_within_the_bound():
    from vcfspec.mcp_server import MAX_DOCUMENT_CHARS
    assert len(INVENTORY) * 100 < MAX_DOCUMENT_CHARS
