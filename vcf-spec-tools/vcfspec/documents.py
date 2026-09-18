"""Loading untrusted spec documents, and deciding what they are."""
from __future__ import annotations

import re
from enum import StrEnum

import yaml

from .findings import Finding, Result, Severity

MAX_BYTES = 2_000_000
MAX_DEPTH = 40
MAX_ALIASES = 100
SDDC_SPEC_KEYS = {"sddcId", "dnsSpec", "networkSpecs", "vcenterSpec"}
_ALIAS_RE = re.compile(r"(?m)(?<![\w*])\*[A-Za-z0-9_][\w-]*")


class DocumentTooLarge(ValueError):
    """Document exceeds MAX_BYTES."""


class DocumentTooDeep(ValueError):
    """Document nests deeper than MAX_DEPTH."""


class DocumentTooManyAliases(ValueError):
    """Document uses more YAML aliases than MAX_ALIASES."""


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
    if _depth(doc) > MAX_DEPTH:
        raise DocumentTooDeep(f"document nests deeper than {MAX_DEPTH}")
    return doc


def detect_kind(doc: dict, override: str | None = None) -> tuple[DocumentKind, Result]:
    if override:
        try:
            return DocumentKind(override), Result()
        except ValueError:
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


def _depth(node: object) -> int:
    deepest = 0
    stack = [(node, 1)]
    while stack:
        current, level = stack.pop()
        deepest = max(deepest, level)
        if level > MAX_DEPTH:
            return level
        if isinstance(current, dict):
            stack.extend((v, level + 1) for v in current.values())
        elif isinstance(current, list):
            stack.extend((v, level + 1) for v in current)
    return deepest
