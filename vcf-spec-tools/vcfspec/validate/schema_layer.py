"""Layer 1: the vendored vendor schema.

Password fields carry minLength 8-15, so a ${reference} is too short to validate.
We validate a substituted copy: structure is checked, secrets are never present.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

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


# --- Recursive $defs walk -------------------------------------------------
#
# declared_properties() above checks one node, one level deep. That is not
# enough to guard a renderer that hand-builds nested dicts (credentials,
# NSX manager lists, IP pool ranges, ESA config, ...): a wrong key three
# levels down would pass both declared_properties() and full jsonschema
# validation, because no $def in this vendored schema sets
# additionalProperties: false, so Draft202012Validator never rejects an
# invented key at any depth. walk_declared_properties() is the only check
# that can catch that class of bug; it must actually descend.

@dataclass(frozen=True, slots=True)
class SchemaWalkResult:
    """Result of recursively checking rendered data against a $defs entry.

    `undeclared` holds a JSON pointer for every data key that has no
    matching schema property anywhere in its $ref/combinator chain.
    `visited` holds the pointer of every object node the walk actually
    inspected, including free-form ones (no declared properties at all).
    Recording free-form nodes too means a walk that silently stops
    descending -- e.g. a $ref that fails to resolve -- shows up as lost
    coverage in `visited` rather than as a quiet pass.
    """
    undeclared: frozenset[str]
    visited: frozenset[str]


def _resolve_ref(schema: dict, subschema: dict) -> dict:
    seen: set[str] = set()
    while "$ref" in subschema:
        ref = subschema["$ref"]
        if ref in seen:
            raise ValueError(f"circular $ref while walking schema: {ref}")
        seen.add(ref)
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported external $ref: {ref}")
        node = schema
        for part in ref[2:].split("/"):
            node = node[part]
        subschema = node
    return subschema


def _combinator_branches(subschema: dict) -> list[dict]:
    branches: list[dict] = []
    for key in ("allOf", "anyOf", "oneOf"):
        branches.extend(subschema.get(key, []))
    return branches


def _declared_keys(schema: dict, subschema: dict) -> set[str]:
    subschema = _resolve_ref(schema, subschema)
    keys = set(subschema.get("properties", {}))
    for branch in _combinator_branches(subschema):
        keys |= _declared_keys(schema, branch)
    return keys


def _property_schema(schema: dict, subschema: dict, key: str) -> dict | None:
    subschema = _resolve_ref(schema, subschema)
    props = subschema.get("properties", {})
    if key in props:
        return props[key]
    for branch in _combinator_branches(subschema):
        found = _property_schema(schema, branch, key)
        if found is not None:
            return found
    return None


def walk_declared_properties(schema: dict, def_name: str, data: object) -> SchemaWalkResult:
    """Recursively confirm every key under `data` is declared somewhere in
    `schema["$defs"][def_name]`.

    Follows $ref and allOf/anyOf/oneOf, descends through `properties` for
    dicts and `items` for lists. A subschema with no declared properties
    anywhere in its resolved chain is free-form: its own keys are not
    checked (there is nothing to check them against), but its pointer is
    still added to `visited`.
    """
    undeclared: set[str] = set()
    visited: set[str] = set()
    _walk(schema, {"$ref": f"#/$defs/{def_name}"}, data, "", undeclared, visited)
    return SchemaWalkResult(frozenset(undeclared), frozenset(visited))


def _walk(schema, subschema, data, pointer, undeclared, visited):
    if isinstance(data, dict):
        subschema = _resolve_ref(schema, subschema)
        visited.add(pointer or "/")
        declared = _declared_keys(schema, subschema)
        if declared:
            undeclared.update(f"{pointer}/{key}" for key in data if key not in declared)
        for key, value in data.items():
            child = _property_schema(schema, subschema, key)
            if child is not None:
                _walk(schema, child, value, f"{pointer}/{key}", undeclared, visited)
    elif isinstance(data, list):
        subschema = _resolve_ref(schema, subschema)
        items = subschema.get("items")
        if items is not None:
            for index, value in enumerate(data):
                _walk(schema, items, value, f"{pointer}/{index}", undeclared, visited)
