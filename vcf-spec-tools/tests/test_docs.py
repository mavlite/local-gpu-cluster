"""Docs are code too: keep the skill and the README honest against the
package they describe, so they cannot silently drift from the codes, tools
and behaviour an operator or agent will actually see.
"""
from __future__ import annotations

import re
from pathlib import Path

from vcfspec.mcp_server import TOOLS, call_handler
from vcfspec.rules import load_catalogue

PACKAGE = Path(__file__).resolve().parent.parent / "vcfspec"
README = Path(__file__).resolve().parent.parent / "README.md"
SKILL = (Path(__file__).resolve().parents[2] / ".claude" / "skills" /
         "vcf-spec-authoring" / "SKILL.md")

DOC_CODE_RE = re.compile(r"\bVCF-[A-Z]{2,}(?:-[A-Z0-9]+)+\b")
_VCF_PREFIXED_RE = re.compile(r"\bvcf_[a-z_]+\b")


def _known_tool_parameter_names() -> set[str]:
    """Every argument key any real tool's inputSchema actually declares.

    A bare `vcf_[a-z_]+` scan of the skill text matches tool names
    (`vcf_validate_spec`) and legitimate parameter names (`vcf_version`)
    alike -- both share the `vcf_` prefix, but only one is a tool. This
    set is what lets the test below tell them apart without hardcoding a
    denylist of "known non-tool words": it is read from TOOLS itself, the
    same live source of truth the tool-name half of the check already
    uses, so it can never drift from the real schemas.
    """
    names: set[str] = set()
    for spec in TOOLS.values():
        names |= set(spec["inputSchema"].get("properties", {}))
    return names


def test_skill_exists_with_frontmatter():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---") and "name: vcf-spec-authoring" in text


def test_every_tool_named_in_the_skill_exists():
    """Every vcf_-prefixed identifier in the skill must be a real tool
    name or a real parameter name of some tool -- never a hallucinated
    tool, and never flagged just for sharing the vcf_ prefix with one
    (e.g. the vcf_version parameter is not a tool called vcf_version).
    """
    known_parameters = _known_tool_parameter_names()
    for name in set(_VCF_PREFIXED_RE.findall(SKILL.read_text(encoding="utf-8"))):
        assert name in TOOLS or name in known_parameters, (
            f"skill names unknown tool {name}")


def test_every_catalogued_code_explains_through_the_real_tool():
    """The consumption-side counterpart to tests/test_catalogue_coverage.py.

    That file already scans the package's own source for every code
    literal a layer can emit (`code="..."`, `finding_for(...)`,
    `load_catalogue()[...]`) and asserts each one resolves in the
    catalogue -- the *production* side of "vcf_explain_finding can explain
    everything a user will actually see". Re-running the same regex walk
    here from tests/test_docs.py would only be a narrower copy of that
    scan (this task's own draft test, `code="..."` only, misses the
    `finding_for(` and `load_catalogue()[` call sites the other file
    already learned to catch -- see its own docstring for the
    VCF-RENDER-FAILED / VCF-EXPLAIN-UNKNOWN-CODE history of that gap).

    So this test walks the catalogue the *other* way instead: through
    call_handler(), the exact boundary an MCP client reaches, for every
    code the catalogue holds. It fails if any catalogued code does not
    resolve to itself with a real, non-empty summary and severity -- i.e.
    if it silently fell back to the VCF-EXPLAIN-UNKNOWN-CODE path
    tool_explain_finding takes for a code it cannot find, or if an entry
    exists as a dict key but carries no usable explanation. A code that
    is present in the catalogue but has an empty summary would pass the
    source-scan test (it *is* a dict key) while still failing an operator
    who calls vcf_explain_finding on it -- that is the gap this test
    closes, and the two files together cover both directions: every
    emitted code is catalogued, and every catalogued code explains.
    """
    catalogue = load_catalogue()
    assert catalogue, "the catalogue loaded empty -- the scan below would pass vacuously"
    for code in catalogue:
        result = call_handler("vcf_explain_finding", {"code": code})
        assert result.get("code") == code, (
            f"{code} did not resolve to itself through vcf_explain_finding: {result}")
        assert result.get("summary", "").strip(), f"{code} has an empty summary"
        assert result.get("severity"), f"{code} has no severity"


def test_every_code_cited_in_docs_exists():
    catalogue = load_catalogue()
    for path in (SKILL, README):
        for code in DOC_CODE_RE.findall(path.read_text(encoding="utf-8")):
            assert code in catalogue, f"{path.name} cites unknown code {code}"


def test_readme_documents_the_credential_rule_and_the_version():
    text = README.read_text(encoding="utf-8")
    assert "${" in text and "9.1.1" in text
