"""The orchestrator: one entry point per tool, already redacted.

This is the first code that composes every layer built so far (documents,
inventory schema, vendor schema, rules, render, probes), so it is the first
place their interactions can go wrong. Three things this module owns and
every other layer explicitly does not:

1. Redaction happens here, once, on the assembled output -- not scattered
   across rule modules. schema_layer.py already redacts its own messages
   (jsonschema echoes raw spec values into strings it builds itself), so
   redacting again here must be a no-op; redact() is idempotent (see
   tests/test_redact.py::test_redact_is_idempotent).
2. Every loader failure (DocumentTooLarge, DocumentTooDeep,
   DocumentTooManyAliases, a yaml.YAMLError, a non-mapping root) becomes a
   structured finding, and so does anything render() raises -- not just
   the documented InsecureCredentialError. render() is not defensive the
   way the rules modules are (e.g. a bad int() or a missing dict key on
   schema-invalid input), and that exception surface is not enumerable in
   advance, so render_document() catches broadly at that boundary rather
   than trying to keep the list complete. Nothing here ever lets an
   exception reach the caller.
3. In validate_document(), a schema finding blocks rules only for the
   subtree at its JSON pointer: "/networks/vsan" blocks "/networks/vsan/..."
   and nothing else. Gating compares pointer *segments*, not characters --
   a raw `pointer.startswith(blocked)` would wrongly block a sibling like
   "/networks/vsanWitness". render_document() does NOT gate its rule pass:
   render() does not respect gating either, so filtering only the finding
   and not the spec value it warns about would silently hand an operator a
   spec with a real problem and one fewer finding explaining it.
"""
from __future__ import annotations

from dataclasses import asdict

from .documents import DocumentKind, detect_kind, load_document
from .findings import Finding, Result, Severity
from .inventory import validate_inventory
from .redact import redact
from .render import InsecureCredentialError, render
from .rules.network import check_networks
from .rules.platform import check_platform
from .schema import DEFAULT_VERSION
from .validate.probes import ProbeConfig, run_probes
from .validate.schema_layer import validate_against_schema

# Codes that mean "this JSON pointer's subtree failed schema validation".
_GATING_CODES = ("VCF-INV-SCHEMA", "VCF-SCHEMA")

# Both "/" (root) and "" are how a root-level error can be spelled; neither
# may ever gate, or every rule finding in the document is suppressed.
_ROOT_POINTERS = ("/", "")


def subtree_blocked(findings) -> set[str]:
    """Pointers whose subtree failed schema validation."""
    return {f.path for f in findings
            if f.code in _GATING_CODES and f.path not in _ROOT_POINTERS}


def _unescape_segment(segment: str) -> str:
    """RFC 6901 decoding: ~1 -> '/' must happen before ~0 -> '~'.

    Doing it in the other order would mis-decode a literal "~01": read
    left to right, that has to become "~1" (an escaped tilde followed by a
    literal '1'), not "/" (which is what decoding ~0 first would produce).
    """
    return segment.replace("~1", "/").replace("~0", "~")


def _segments(pointer: str) -> tuple[str, ...]:
    return tuple(_unescape_segment(part) for part in pointer.split("/") if part != "")


def _is_blocked(pointer: str, blocked: set[str]) -> bool:
    """True when pointer is the blocked pointer itself, or inside it.

    Compares JSON-pointer segments, not raw characters. Segment comparison
    is what keeps "/networks/vsan" from blocking "/networks/vsanWitness":
    they share a character prefix but not a segment prefix.

    A blocked entry with zero segments (root, spelled either "/" or "")
    is always skipped: an empty segment tuple is a prefix of every
    pointer's segments, so honouring it here would silently suppress
    every rule finding in the document. subtree_blocked() already keeps
    root pointers out of the set it builds, but that guard belongs on
    this function too -- it must hold regardless of which caller built
    `blocked`, not just for the one caller that remembers to filter first.
    """
    pointer_segments = _segments(pointer)
    for entry in blocked:
        entry_segments = _segments(entry)
        if entry_segments and pointer_segments[:len(entry_segments)] == entry_segments:
            return True
    return False


def _filter_blocked(result: Result, blocked: set[str]) -> Result:
    if not blocked:
        return result
    return Result(tuple(f for f in result.findings if not _is_blocked(f.path, blocked)))


def _unreadable_envelope(exc: Exception, skipped: dict[str, str]) -> dict:
    finding = Finding(
        code="VCF-INPUT-UNREADABLE", severity=Severity.CRITICAL, path="/",
        message=str(redact(str(exc))),
        fix="Provide a YAML mapping within the size, depth, and alias limits.",
        source="schema")
    return _envelope(Result((finding,)), ["detect"], skipped)


def _rules_for_inventory(doc: dict, blocked: set[str]) -> Result:
    rules = check_networks(doc).merge(check_platform(doc))
    return _filter_blocked(rules, blocked)


def validate_document(text: str, input_kind: str | None = None,
                      probe_config: ProbeConfig | None = None,
                      version: str = DEFAULT_VERSION) -> dict:
    layers_run: list[str] = ["detect"]
    skipped: dict[str, str] = {}

    try:
        doc = load_document(text)
    except Exception as exc:
        return _unreadable_envelope(exc, {
            "schema": "document unreadable", "rules": "document unreadable",
            "probes": "document unreadable"})

    kind, result = detect_kind(doc, input_kind)
    if kind is DocumentKind.UNKNOWN:
        for layer in ("schema", "rules", "probes"):
            skipped[layer] = "document kind unknown"
        return _envelope(result, layers_run, skipped)

    if kind is DocumentKind.INVENTORY:
        schema_result = validate_inventory(doc)
        result = result.merge(schema_result)
        layers_run.append("schema")

        blocked = subtree_blocked(schema_result.findings)
        result = result.merge(_rules_for_inventory(doc, blocked))
        layers_run.append("rules")
        if blocked:
            skipped["rules"] = f"suppressed under {sorted(blocked)}"
    else:
        result = result.merge(validate_against_schema(doc, version))
        layers_run.append("schema")
        skipped["rules"] = "rules operate on inventories; render first"

    if probe_config is None:
        skipped["probes"] = "no probe configuration supplied"
    elif any(f.severity is Severity.CRITICAL for f in result.findings):
        skipped["probes"] = "critical findings present"
    else:
        result = result.merge(run_probes(doc, probe_config))
        layers_run.append("probes")

    return _envelope(result, layers_run, skipped)


def render_document(text: str, version: str = DEFAULT_VERSION) -> dict:
    try:
        doc = load_document(text)
    except Exception as exc:
        return _unreadable_envelope(exc, {"render": "document unreadable"})

    kind, result = detect_kind(doc)
    if kind is not DocumentKind.INVENTORY:
        return _envelope(result, ["detect"], {"render": "input is not an inventory"})

    # No subtree gating here, unlike validate_document: render's rule pass
    # is never filtered. Gating a rule finding without also gating the
    # spec value it warns about would let an operator receive a rendered
    # spec with a real problem in it and one fewer finding explaining why.
    schema_result = validate_inventory(doc)
    rules_result = check_networks(doc).merge(check_platform(doc))
    combined = result.merge(schema_result).merge(rules_result)
    layers_run = ["detect", "schema", "rules"]

    # render() is documented as not trusting that validate_inventory() ran
    # first: it re-checks every credential and raises rather than emitting
    # a literal secret. That guard, and any other exception render() can
    # raise on malformed input (a bad int(), a missing key -- the set of
    # possible failures is not enumerable in advance), must never
    # propagate out of the orchestrator -- convert it to a finding instead.
    try:
        spec, render_result = render(doc, version)
    except InsecureCredentialError as exc:
        finding = Finding(
            code="VCF-RENDER-INSECURE-CREDENTIAL", severity=Severity.CRITICAL,
            path="/credentials", message=str(redact(str(exc))),
            fix="Use ${name} references for every credential; never a literal value.",
            source="schema")
        combined = combined.merge(Result((finding,)))
        return _envelope(combined, layers_run, {"render": "refused an insecure credential"})
    except Exception as exc:
        finding = Finding(
            code="VCF-RENDER-FAILED", severity=Severity.CRITICAL, path="/",
            message=str(redact(str(exc))),
            fix="Correct the inventory; it could not be rendered into a spec.",
            source="schema")
        combined = combined.merge(Result((finding,)))
        return _envelope(combined, layers_run,
                         {"render": "render() raised an unexpected exception"})

    combined = combined.merge(render_result)
    layers_run.append("render")
    envelope = _envelope(combined, layers_run, {})
    envelope["spec"] = redact(spec)
    return envelope


def _envelope(result: Result, layers_run: list[str], skipped: dict[str, str]) -> dict:
    return redact({
        "valid": result.valid,
        "findings": [asdict(f) for f in result.findings],
        "layers_run": layers_run,
        "layers_skipped": skipped,
    })
