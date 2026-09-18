"""The compact lab inventory: our input format."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .findings import Finding, Result, Severity

_HERE = Path(__file__).resolve().parent
INVENTORY_SCHEMA_PATH = _HERE / "schemas" / "inventory" / "v1.schema.json"
EXAMPLE_PATH = _HERE / "examples" / "lab-3-host.yaml"

REFERENCE_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


@lru_cache(maxsize=1)
def _schema_text() -> str:
    return INVENTORY_SCHEMA_PATH.read_text(encoding="utf-8")


def load_inventory_schema() -> dict:
    return json.loads(_schema_text())        # fresh dict per caller


def load_example() -> dict:
    return yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))


def validate_inventory(doc: dict) -> Result:
    findings: list[Finding] = []
    validator = Draft202012Validator(load_inventory_schema())
    for error in sorted(validator.iter_errors(doc),
                        key=lambda e: [str(p) for p in e.absolute_path]):
        findings.append(Finding(
            code="VCF-INV-SCHEMA", severity=Severity.ERROR,
            path=_pointer(error.absolute_path), message=error.message,
            fix="Correct the inventory to match the documented schema.",
            source="schema"))
    findings.extend(_credential_findings(doc))
    return Result(tuple(findings))


def _credential_findings(doc: dict) -> list[Finding]:
    out: list[Finding] = []
    for name, value in (doc.get("credentials") or {}).items():
        if not isinstance(value, str) or not REFERENCE_RE.match(value):
            out.append(Finding(
                code="VCF-CRED-NOT-A-REFERENCE", severity=Severity.CRITICAL,
                path=f"/credentials/{name}",
                message=(f"Credential '{name}' is not a reference. These tools "
                         "never hold secrets."),
                fix="Use ${name}, e.g. ${esx_root}; resolve it at submit time.",
                source="docs"))
    return out


def _pointer(path) -> str:
    parts = list(path)
    return "/" + "/".join(str(p) for p in parts) if parts else "/"
