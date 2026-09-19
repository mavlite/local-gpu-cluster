# vcf-spec-tools/scripts/vendor_schema.py
"""Extract SddcSpec from Broadcom's VCF Installer OpenAPI document.

Usage: python scripts/vendor_schema.py [version] [path-or-url]

If your network blocks GitHub, download the file by hand and pass its path:
  specifications/vcf-installer/vcf-installer-openapi.json
  from https://github.com/vmware/vcf-api-specs
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from jsonschema import Draft202012Validator

RAW_URL = ("https://raw.githubusercontent.com/vmware/vcf-api-specs/main/"
           "specifications/vcf-installer/vcf-installer-openapi.json")
ROOT_SCHEMA = "SddcSpec"
OUT_DIR = Path(__file__).resolve().parent.parent / "vcfspec" / "schemas"


def iter_refs(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value.rsplit("/", 1)[-1]
            else:
                yield from iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_refs(item)


def collect(name: str, schemas: dict, seen: set[str], missing: set[str]) -> None:
    if name in seen:
        return
    if name not in schemas:
        missing.add(name)
        return
    seen.add(name)
    for ref in iter_refs(schemas[name]):
        collect(ref, schemas, seen, missing)


def to_draft_2020(node: object) -> object:
    """OpenAPI 3.0.1 dialect -> JSON Schema draft 2020-12."""
    if isinstance(node, list):
        return [to_draft_2020(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict = {}
    for key, value in node.items():
        if key in ("discriminator", "example", "xml", "externalDocs"):
            continue
        if key == "nullable":
            continue
        if key == "$ref" and isinstance(value, str):
            out[key] = value.replace("#/components/schemas/", "#/$defs/")
            continue
        if key in ("exclusiveMinimum", "exclusiveMaximum") and isinstance(value, bool):
            bound = node.get("minimum" if key == "exclusiveMinimum" else "maximum")
            if value and bound is not None:
                out[key] = bound
            continue
        out[key] = to_draft_2020(value)

    if node.get("nullable") is True and "type" in out:
        kind = out["type"]
        out["type"] = [kind, "null"] if isinstance(kind, str) else list(kind) + ["null"]
    if node.get("exclusiveMinimum") is True and "minimum" in out:
        out.pop("minimum", None)
    if node.get("exclusiveMaximum") is True and "maximum" in out:
        out.pop("maximum", None)
    return out


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "9.1.1.0"
    source = sys.argv[2] if len(sys.argv) > 2 else RAW_URL
    raw = (urllib.request.urlopen(source, timeout=60).read().decode()
           if source.startswith("http") else Path(source).read_text(encoding="utf-8"))
    doc = json.loads(raw)

    if doc["info"]["version"] != version:
        print(f"refusing: document is {doc['info']['version']}, asked for {version}")
        return 1

    schemas = doc["components"]["schemas"]
    seen: set[str] = set()
    missing: set[str] = set()
    collect(ROOT_SCHEMA, schemas, seen, missing)
    if missing:
        print(f"refusing: dangling references {sorted(missing)}")
        return 1

    bundle = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://vcfspec.invalid/sddc-spec/{version}",
        "x-source": source,
        "x-vcf-version": version,
        "$ref": f"#/$defs/{ROOT_SCHEMA}",
        "$defs": {name: to_draft_2020(schemas[name]) for name in sorted(seen)},
    }
    Draft202012Validator.check_schema(bundle)

    text = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    out = OUT_DIR / version
    out.mkdir(parents=True, exist_ok=True)
    (out / "sddc-spec.schema.json").write_text(text, encoding="utf-8")
    digest = hashlib.sha256(text.encode()).hexdigest()
    (out / "sddc-spec.schema.json.sha256").write_text(digest + "\n", encoding="utf-8")
    print(f"wrote {len(seen)} schemas for {version}, sha256={digest[:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
