r"""Every code the package can emit must resolve in the catalogue.

An uncatalogued code has no fix template and no source/source_url
provenance, and (per the MCP layer built on top of this library) it is a
code a consumer can receive and then fail to look up when it asks the
catalogue what the code means -- a broken contract. This walks the
package's own source for every `code="..."`, `finding_for("...")` and
`load_catalogue()["..."]` literal, rather than hand-maintaining a list here
that would silently drift out of date the next time a new code is added
without a matching catalogue entry (exactly what happened with
VCF-RENDER-FAILED and VCF-RENDER-INSECURE-CREDENTIAL in vcfspec/api.py, and
again with VCF-EXPLAIN-UNKNOWN-CODE, referenced only as a
`load_catalogue()[...]` subscript in vcfspec/mcp_server.py until this scan
was widened to see that shape too).

A `load_catalogue()[code]` subscript with a *variable* rather than a string
literal (as in `rules.finding_for`'s own body) is not, and cannot be, caught
here -- there is nothing for a literal-matching regex to read. That is not a
coverage hole in practice: every caller of `finding_for(...)` passes a
literal code, and every one of those call sites is already covered by the
`finding_for\(\s*["']...["']` branch below.
"""
from __future__ import annotations

import re
from pathlib import Path

import vcfspec
from vcfspec.rules import load_catalogue

_PACKAGE_ROOT = Path(vcfspec.__file__).resolve().parent

# Matches `code="VCF-..."` / `code='VCF-...'` (Finding(...) call sites),
# `finding_for("VCF-...", ...)` (catalogue-driven call sites) and
# `load_catalogue()["VCF-..."]` (a direct subscript lookup) -- the three
# ways this package spells "emit this code" anywhere in its source.
_CODE_LITERAL_RE = re.compile(
    r"""(?:code\s*=\s*|finding_for\(\s*|load_catalogue\(\)\[\s*)"""
    r"""["']([A-Za-z][A-Za-z0-9_-]*)["']"""
)


def _codes_referenced_in_source() -> set[str]:
    codes: set[str] = set()
    for path in _PACKAGE_ROOT.rglob("*.py"):
        codes.update(_CODE_LITERAL_RE.findall(path.read_text(encoding="utf-8")))
    return codes


def test_the_scan_itself_finds_a_representative_sample():
    """Guards the guard: if this ever finds nothing, the regex broke, not
    the codebase -- fail loudly instead of vacuously passing an empty diff.
    """
    referenced = _codes_referenced_in_source()
    assert {"VCF-INV-SCHEMA", "VCF-RENDER-FAILED",
            "VCF-RENDER-INSECURE-CREDENTIAL"} <= referenced


def test_the_scan_also_finds_load_catalogue_subscript_call_sites():
    """The widened branch actually widens: vcfspec/mcp_server.py references
    VCF-EXPLAIN-UNKNOWN-CODE only via a `load_catalogue()["..."]` subscript
    -- neither the `code=` nor the `finding_for(` branch could ever have
    caught it."""
    referenced = _codes_referenced_in_source()
    assert "VCF-EXPLAIN-UNKNOWN-CODE" in referenced


def test_every_code_the_package_can_emit_resolves_in_the_catalogue():
    referenced = _codes_referenced_in_source()
    catalogue = load_catalogue()
    missing = sorted(code for code in referenced if code not in catalogue)
    assert missing == []
