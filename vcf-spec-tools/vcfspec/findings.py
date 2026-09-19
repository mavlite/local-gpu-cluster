"""Findings are the contract every layer and every tool returns."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    """CRITICAL stops processing; ERROR means the Installer would reject it;
    WARNING is supported but risky or disputed; INFO is advisory."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


BLOCKING = (Severity.CRITICAL, Severity.ERROR)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: Severity
    path: str
    message: str
    fix: str = ""
    source: str = "docs"        # docs | schema | table
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class Result:
    findings: tuple[Finding, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(x.severity in BLOCKING for x in self.findings)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(x.code for x in self.findings)

    def by_severity(self, severity: Severity) -> tuple[Finding, ...]:
        """Findings at exactly one severity -- e.g. the `error`/`critical`
        subset a caller wants to act on first, separate from `warning`/
        `info` noise. Kept (not deleted, despite zero call sites inside
        this package) because Result is the return type every one of the
        five MCP tools and both CLI subcommands hands to a consumer, and
        filtering by severity is an ordinary thing for a library or an
        agent to want to do with it -- unlike Finding.with_path, which
        this package removed as genuinely dead: a thin, unused wrapper
        around dataclasses.replace() with no signal that anything actually
        needed it.
        """
        return tuple(x for x in self.findings if x.severity == severity)

    def merge(self, other: "Result") -> "Result":
        return Result(self.findings + other.findings)


def json_pointer(path) -> str:
    """Render an iterable of raw path segments (as jsonschema's
    ``error.absolute_path`` yields them) as a JSON pointer string.

    Finding 13: inventory.py and validate/schema_layer.py each defined a
    byte-identical private ``_pointer()`` for exactly this. One copy here,
    imported by both, so there is one place left to get RFC 6901 escaping
    right if this ever needs it (it does not today: neither caller's
    segments contain '/' or '~', since they come from JSON Schema
    property names and array indices, not from arbitrary document keys).
    """
    parts = list(path)
    return "/" + "/".join(str(p) for p in parts) if parts else "/"
