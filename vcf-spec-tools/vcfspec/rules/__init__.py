"""The rule catalogue: every code the library can emit, with provenance."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..findings import Finding, Severity

CATALOGUE_PATH = Path(__file__).resolve().parent / "catalogue.yaml"


@dataclass(frozen=True, slots=True)
class RuleMeta:
    code: str
    severity: Severity
    source: str
    source_url: str
    summary: str
    fix: str


@lru_cache(maxsize=1)
def load_catalogue() -> dict[str, RuleMeta]:
    raw = yaml.safe_load(CATALOGUE_PATH.read_text(encoding="utf-8"))
    return {code: RuleMeta(code=code, severity=Severity(entry["severity"]),
                           source=entry["source"],
                           source_url=entry.get("source_url", ""),
                           summary=entry["summary"], fix=entry.get("fix", ""))
            for code, entry in raw.items()}


def finding_for(code: str, path: str, **fmt: object) -> Finding:
    meta = load_catalogue()[code]
    return Finding(code=meta.code, severity=meta.severity, path=path,
                   message=meta.summary.format(**fmt) if fmt else meta.summary,
                   fix=meta.fix, source=meta.source, source_url=meta.source_url)
