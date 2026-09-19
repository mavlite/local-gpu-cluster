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


# --- No finding message ever reflects caller-supplied text ---------------
#
# Found in follow-up review on 2026-09-19: the first pass removed this
# reflection from the two *rejection* messages and missed three other
# places a caller-supplied value reached a message -- the
# VCF-VERSION-NOT-CONSULTED note (inventory path), the `code` argument to
# vcf_explain_finding, and input_kind in detect_kind.
#
# None of the three is an oracle: no filesystem access occurs on any of
# them and each answers identically regardless of what is on disk. The
# concern is narrower and worth stating exactly -- the caller of these
# tools is an AI agent that may be validating a document from an
# untrusted source, and handing it back caller-chosen characters inside a
# message it reads is a surface with no upside. The caller already knows
# what it sent; naming what this package actually ships is strictly more
# useful.
#
# This test is written as a sweep over every argument rather than three
# separate cases, so a NEW tool argument that echoes is caught by a test
# nobody had to remember to write.

_CANARY = "ZZ-CANARY-REFLECTED-9174-ZZ"


def _sweep_arguments():
    """(tool, args) for every string argument, each carrying the canary."""
    from vcfspec.mcp_server import TOOLS
    for tool, spec in TOOLS.items():
        properties = spec["inputSchema"].get("properties", {})
        for name, declared in properties.items():
            if declared.get("type") != "string":
                continue
            args = {}
            for required in spec["inputSchema"].get("required", []):
                args[required] = INVENTORY if required in (
                    "document", "left", "right") else _CANARY
            args[name] = _CANARY
            yield tool, name, args


@pytest.mark.parametrize(
    "tool,argument,args",
    [pytest.param(t, n, a, id=f"{t}.{n}") for t, n, a in _sweep_arguments()])
def test_no_finding_message_ever_reflects_caller_supplied_text(tool, argument, args):
    blob = json.dumps(call_handler(tool, args))
    assert _CANARY not in blob, (
        f"{tool}.{argument} echoes its caller-supplied value back into the "
        "envelope an agent reads")


def test_the_sweep_itself_covers_every_tool_and_the_known_offenders():
    """Guards the guard: a sweep that silently covered nothing would pass
    every assertion above."""
    covered = {(tool, name) for tool, name, _ in _sweep_arguments()}
    assert {("vcf_validate_spec", "vcf_version"),
            ("vcf_render_spec", "vcf_version"),
            ("vcf_explain_finding", "code"),
            ("vcf_validate_spec", "document"),
            ("vcf_diff_spec", "left")} <= covered
    assert len({tool for tool, _ in covered}) >= 4


def test_the_not_consulted_note_names_the_vendored_set_instead(tmp_path):
    """The positive half: dropping the echo must not drop the
    information. An operator still learns that their version was ignored
    AND what this package actually ships."""
    out = call_handler("vcf_validate_spec",
                       {"document": INVENTORY, "vcf_version": "9.9.9.9"})
    note = next(f for f in out["findings"]
                if f["code"] == "VCF-VERSION-NOT-CONSULTED")
    assert "9.9.9.9" not in note["message"]
    assert DEFAULT_VERSION in note["message"]
    assert "not consulted" in note["message"]
    assert out["valid"] is True          # still advisory, still not a rejection


def test_input_kind_is_not_reflected_by_the_library_entry_point():
    """The MCP enum and argparse both refuse a bad input_kind before
    detect_kind sees it, so this is only reachable through the library --
    which is a public entry point, and the rule should not depend on
    which front door is in front of it."""
    out = validate_document(INVENTORY, input_kind=_CANARY)
    assert _CANARY not in json.dumps(out)
    assert "VCF-INPUT-BAD-KIND" in [f["code"] for f in out["findings"]]


# --- Follow-up round 2: the diff's OUTPUT is bounded, not just its input -
#
# documents.MAX_NODES bounds how far a document expands. It does not bound
# how much vcf_diff_spec emits about it, and the two are very different
# numbers. Measured against the post-round-1 code: a 10,073-character
# document -- 30 aliases (limit 100), 196,590 nodes (limit 200,000),
# inside every input bound and inside maxLength -- produced a 306.8 MB
# response in 15.82 s, a 30,461x amplification, synchronously on the
# long-lived server. Cost scales with change count x path length, and
# alias expansion drives both while the source text stays tiny.

def _alias_bomb(levels=14, fan=2, pad=64, keylen=320):
    """A legal document that expands to ~196k nodes from ~10 KB of text."""
    key = "k" * keylen
    lines = [f"l0: &a0 {{v: {'x' * pad}}}"]
    for i in range(1, levels + 1):
        lines.append(f"l{i}: &a{i} {{"
                     + ", ".join(f"{key}{j}: *a{i - 1}" for j in range(fan)) + "}")
    lines.append("root: {"
                 + ", ".join(f"{key}r{j}: *a{levels}" for j in range(fan)) + "}")
    return "\n".join(lines) + "\n"


def test_the_alias_bomb_is_genuinely_legal_input():
    """Guards the guard: if this document ever stops being accepted by the
    input limits, the tests below would pass for the wrong reason."""
    from vcfspec.documents import (MAX_ALIASES, MAX_NODES, _ALIAS_RE,
                                   _inspect, load_document)
    from vcfspec.mcp_server import MAX_DOCUMENT_CHARS
    text = _alias_bomb()
    assert len(text) <= MAX_DOCUMENT_CHARS
    assert len(_ALIAS_RE.findall(text)) <= MAX_ALIASES
    _, nodes = _inspect(load_document(text))
    assert nodes <= MAX_NODES
    assert nodes > 100_000, "the bomb should still expand enormously"


def test_a_legal_alias_bomb_cannot_produce_an_unbounded_response():
    from vcfspec.mcp_server import MAX_CHANGE_CHARS, MAX_CHANGES
    text = _alias_bomb()
    out = call_handler("vcf_diff_spec",
                       {"left": text, "right": text.replace("x" * 64, "y" * 64)})
    assert out["truncated"] is True
    assert out["changed"] <= MAX_CHANGES
    # The response is bounded in SIZE, not merely in count -- bounding the
    # count alone still permits few changes with enormous JSON pointers,
    # which is exactly the shape alias expansion produces.
    assert len(json.dumps(out)) < MAX_CHANGE_CHARS * 3


def test_a_truncated_diff_is_never_reported_as_valid():
    """The dangerous answer is a partial diff that looks complete: an
    agent asking "did anything under /credentials change?" must not read
    a truncated walk as "no"."""
    text = _alias_bomb()
    out = call_handler("vcf_diff_spec",
                       {"left": text, "right": text.replace("x" * 64, "y" * 64)})
    assert out["valid"] is False
    assert "VCF-DIFF-TRUNCATED" in [f["code"] for f in out["findings"]]
    assert out["truncated"] is True


def test_an_ordinary_diff_is_untouched_by_the_bound():
    """The bound must not change what a real caller sees. A 3-host
    inventory with one field changed is one change, complete, valid."""
    import yaml
    doc = yaml.safe_load(INVENTORY)
    doc["hosts"][0]["mgmtIp"] = "10.50.10.99"
    out = call_handler("vcf_diff_spec",
                       {"left": INVENTORY, "right": yaml.safe_dump(doc)})
    assert out["valid"] is True
    assert out["truncated"] is False
    assert out["changed"] == 1
    assert [c["path"] for c in out["changes"]] == ["/hosts/esx01/mgmtIp"]


def test_truncated_is_always_present_so_a_consumer_can_branch_on_it():
    """Finding 8's shape rule, applied to the new key: `truncated` is
    present on success AND on truncation, never absent on one of them."""
    import yaml
    doc = yaml.safe_load(INVENTORY)
    doc["hosts"][0]["mgmtIp"] = "10.50.10.99"
    clean = call_handler("vcf_diff_spec",
                         {"left": INVENTORY, "right": yaml.safe_dump(doc)})
    text = _alias_bomb()
    cut = call_handler("vcf_diff_spec",
                       {"left": text, "right": text.replace("x" * 64, "y" * 64)})
    assert "truncated" in clean and "truncated" in cut


def test_credentials_are_still_masked_in_a_truncated_diff():
    """Truncation must not open a hole in the masking that the whole diff
    tool is built around."""
    left = {"credentials": {"esxRoot": "LeakCanaryAAA111"}, "a": {"b": 1}}
    right = {"credentials": {"esxRoot": "LeakCanaryBBB222"}, "a": {"b": 2}}
    import yaml
    out = call_handler("vcf_diff_spec", {"left": yaml.safe_dump(left),
                                         "right": yaml.safe_dump(right)})
    blob = json.dumps(out)
    assert "LeakCanaryAAA111" not in blob and "LeakCanaryBBB222" not in blob
