"""Findings are the contract every layer and every tool returns."""
from __future__ import annotations

from dataclasses import dataclass, replace
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

    def with_path(self, path: str) -> "Finding":
        return replace(self, path=path)


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
        return tuple(x for x in self.findings if x.severity == severity)

    def merge(self, other: "Result") -> "Result":
        return Result(self.findings + other.findings)
