"""Loading untrusted spec documents, and deciding what they are."""
from __future__ import annotations

import re
from enum import StrEnum

import yaml

from .findings import Finding, Result, Severity

MAX_BYTES = 2_000_000
MAX_DEPTH = 40
MAX_ALIASES = 100

# The bound that MAX_ALIASES only looked like. Aliases compose
# multiplicatively, so counting occurrences in the source text does not
# bound anything: 320 bytes with 56 aliases -- 44 under the limit -- is
# seven levels of eight, which is 8**7 = 2,097,152 leaves and 21,913,097
# nodes in total.
#
# Where that costs is worth stating precisely, because it is NOT where it
# looks. PyYAML memoises alias construction (BaseConstructor keeps
# constructed_objects keyed by node), so an aliased node is built once and
# the same object is shared: yaml.safe_load of that 320-byte document takes
# about 2ms, and a loader counting *constructed* nodes would see roughly as
# many as the source has and pass it. The 4.4 seconds is spent afterwards,
# in the first traversal that does not memoise -- which was _depth(), and
# would equally have been jsonschema validation, redact(), render() or the
# diff engine, none of which memoise either. Sharing an object does not
# make a consumer's walk of it cheaper.
#
# So the budget is on *materialised* nodes: what a non-memoising consumer
# actually has to visit. It is enforced by _inspect() below, in the same
# single traversal that measures depth, before the document is handed to
# any layer, and that traversal stops at the first node past the budget --
# so an abusive document costs milliseconds, not seconds, and never
# reaches a layer that would spend longer on it. In the CLI the old
# behaviour was an annoyance; in the long-lived MCP server it is a
# denial-of-service vector, which is why this is a guard and not a tuning
# knob. A real lab inventory is a few hundred nodes, so this leaves three
# orders of magnitude of headroom.
MAX_NODES = 200_000

SDDC_SPEC_KEYS = {"sddcId", "dnsSpec", "networkSpecs", "vcenterSpec"}
_ALIAS_RE = re.compile(r"(?m)(?<![\w*])\*[A-Za-z0-9_][\w-]*")


class DocumentTooLarge(ValueError):
    """Document exceeds MAX_BYTES."""


class DocumentTooDeep(ValueError):
    """Document nests deeper than MAX_DEPTH."""


class DocumentTooManyAliases(ValueError):
    """Document uses more YAML aliases than MAX_ALIASES."""


class DocumentTooManyNodes(ValueError):
    """Document expands to more than MAX_NODES materialised nodes."""


class DocumentKind(StrEnum):
    INVENTORY = "inventory"
    SDDC_SPEC = "sddc_spec"
    UNKNOWN = "unknown"


def load_document(text: str) -> dict:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise DocumentTooLarge(f"document exceeds {MAX_BYTES} bytes")
    if len(_ALIAS_RE.findall(text)) > MAX_ALIASES:
        raise DocumentTooManyAliases(f"document uses more than {MAX_ALIASES} aliases")
    try:
        doc = yaml.safe_load(text)
    except RecursionError as exc:
        raise DocumentTooDeep(
            "document nests too deeply for the YAML parser") from exc
    if not isinstance(doc, dict):
        raise ValueError("document root must be a mapping")
    depth, nodes = _inspect(doc)
    if depth > MAX_DEPTH:
        raise DocumentTooDeep(f"document nests deeper than {MAX_DEPTH}")
    if nodes > MAX_NODES:
        raise DocumentTooManyNodes(
            f"document expands to more than {MAX_NODES} nodes")
    return doc


# The only two values an override may usefully take. DocumentKind itself
# also has UNKNOWN, but that is an internal sentinel for "detection could
# not tell" -- accepting override="unknown" via a bare `DocumentKind(override)`
# construction used to succeed silently (UNKNOWN is a real enum member),
# which sent a caller straight to the "kind unknown, skip everything"
# branch with no explanatory finding attached: a typo'd or nonsensical
# input_kind of exactly "unknown" reported `valid: true` having validated
# nothing. Restricting overrides to this explicit, closed tuple closes
# that: any override that isn't one of these two -- "unknown" included --
# is now the same VCF-INPUT-BAD-KIND finding as any other bad string.
_VALID_OVERRIDES = (DocumentKind.INVENTORY, DocumentKind.SDDC_SPEC)


def detect_kind(doc: dict, override: str | None = None) -> tuple[DocumentKind, Result]:
    if override:
        try:
            kind = DocumentKind(override)
        except ValueError:
            kind = None
        if kind in _VALID_OVERRIDES:
            return kind, Result()
        return DocumentKind.UNKNOWN, Result((Finding(
            code="VCF-INPUT-BAD-KIND", severity=Severity.CRITICAL, path="/",
            message=f"Unknown input_kind {override!r}.",
            fix="Use 'inventory' or 'sddc_spec', or omit it.",
            source="schema"),))
    if str(doc.get("apiVersion", "")).startswith("vcfspec/") and \
            doc.get("kind") == "LabInventory":
        return DocumentKind.INVENTORY, Result()
    if SDDC_SPEC_KEYS.issubset(doc.keys()):
        return DocumentKind.SDDC_SPEC, Result()
    return DocumentKind.UNKNOWN, Result((Finding(
        code="VCF-INPUT-UNRECOGNISED", severity=Severity.CRITICAL, path="/",
        message="Document is neither a lab inventory nor a VCF SddcSpec.",
        fix=("Add 'apiVersion: vcfspec/v1' and 'kind: LabInventory', or pass "
             "input_kind explicitly."),
        source="schema"),))


def _inspect(node: object) -> tuple[int, int]:
    """One traversal answering both limits: (deepest level, nodes visited).

    Stops at the first node past either limit, so the traversal is itself
    bounded -- the point of a denial-of-service guard is lost if checking
    it costs what it is meant to prevent. The count is incremented on
    *push* rather than on pop, which additionally bounds the stack: a node
    with high fan-out would otherwise queue its children before any of
    them was counted, and the pending list, not the visit count, would be
    what grew without limit.

    Aliased nodes are deliberately counted once per occurrence, not once
    per object. PyYAML shares the underlying object, but every consumer
    downstream (jsonschema, redact, render, the diff engine) walks it
    again for each reference, so occurrences are what they pay for and
    occurrences are what must be bounded.
    """
    deepest = 0
    count = 1
    stack = [(node, 1)]
    while stack:
        current, level = stack.pop()
        deepest = max(deepest, level)
        if level > MAX_DEPTH:
            return deepest, count
        if isinstance(current, dict):
            children = current.values()
        elif isinstance(current, list):
            children = current
        else:
            continue
        for value in children:
            count += 1
            if count > MAX_NODES:
                return deepest, count
            stack.append((value, level + 1))
    return deepest, count
