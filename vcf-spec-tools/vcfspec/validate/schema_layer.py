"""Layer 1: the vendored vendor schema.

Password fields carry minLength 8-15, so a ${reference} is too short to validate.
We validate a substituted copy: structure is checked, secrets are never present.
"""
from __future__ import annotations

import copy

from jsonschema import Draft202012Validator

from ..findings import Finding, Result, Severity
from ..inventory import REFERENCE_RE
from ..redact import redact
from ..schema import DEFAULT_VERSION, load_schema

# 16 chars: satisfies rootVcenterPassword (8-20), nsxt (>=12), sddcManager (>=15).
PLACEHOLDER_SECRET = "Vcf!Placeholder1"

SOURCE_URL = ("https://github.com/vmware/vcf-api-specs/blob/main/"
              "specifications/vcf-installer/vcf-installer-openapi.json")


def substitute_secrets(spec: object) -> object:
    """Replace ${references} with a schema-valid placeholder, without mutating."""
    if isinstance(spec, dict):
        return {k: substitute_secrets(v) for k, v in spec.items()}
    if isinstance(spec, list):
        return [substitute_secrets(v) for v in spec]
    if isinstance(spec, str) and REFERENCE_RE.match(spec):
        return PLACEHOLDER_SECRET
    return copy.copy(spec)


def declared_properties(schema: dict, def_name: str) -> set[str]:
    return set(schema["$defs"][def_name].get("properties", {}))


def validate_against_schema(spec: dict, version: str = DEFAULT_VERSION) -> Result:
    validator = Draft202012Validator(load_schema(version))
    candidate = substitute_secrets(spec)
    findings = tuple(
        Finding(
            code="VCF-SCHEMA", severity=Severity.ERROR,
            path=_pointer(error.absolute_path),
            message=str(redact(error.message)),
            fix="Correct the field to match the VCF Installer schema.",
            source="schema", source_url=SOURCE_URL)
        for error in sorted(validator.iter_errors(candidate),
                            key=lambda e: [str(p) for p in e.absolute_path])
    )
    return Result(findings)


def _pointer(path) -> str:
    parts = list(path)
    return "/" + "/".join(str(p) for p in parts) if parts else "/"
