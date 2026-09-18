"""Loading untrusted spec documents, and deciding what they are."""
from __future__ import annotations

from enum import StrEnum

import yaml

from .findings import Finding, Result, Severity

MAX_BYTES = 2_000_000
MAX_DEPTH = 40
SDDC_SPEC_KEYS = {"sddcId", "dnsSpec", "networkSpecs", "vcenterSpec"}


class DocumentTooLarge(ValueError):
    """Document exceeds MAX_BYTES."""


class DocumentTooDeep(ValueError):
    """Document nests deeper than MAX_DEPTH."""


class DocumentKind(StrEnum):
    INVENTORY = "inventory"
    SDDC_SPEC = "sddc_spec"
    UNKNOWN = "unknown"


def load_document(text: str) -> dict:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise DocumentTooLarge(f"document exceeds {MAX_BYTES} bytes")
    doc = yaml.safe_load(text)
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


def _depth(node: object, current: int = 1) -> int:
    if isinstance(node, dict):
        return max((_depth(v, current + 1) for v in node.values()), default=current)
    if isinstance(node, list):
        return max((_depth(v, current + 1) for v in node), default=current)
    return current
