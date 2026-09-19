"""The compact lab inventory: our input format."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .findings import Finding, Result, Severity, json_pointer

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
            path=json_pointer(error.absolute_path), message=error.message,
            fix="Correct the inventory to match the documented schema.",
            source="schema"))
    # The ${reference} rule is the project's single hardest constraint, so
    # it does not live here as a loop over this document kind's own root
    # 'credentials' block -- that shape is what kept it from ever running
    # on sddc_spec documents. credentials.credential_findings() is the one
    # structural walk both kinds share; imported lazily because
    # credentials.py needs REFERENCE_RE from this module.
    from .credentials import credential_findings
    findings.extend(credential_findings(doc).findings)
    return Result(tuple(findings))
