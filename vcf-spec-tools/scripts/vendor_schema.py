# vcf-spec-tools/scripts/vendor_schema.py
"""Extract SddcSpec from Broadcom's VCF Installer OpenAPI document.

Usage: python scripts/vendor_schema.py [version] [path-or-url]
                                       [--accept-new-upstream]

If your network blocks GitHub, download the file by hand and pass its path:
  specifications/vcf-installer/vcf-installer-openapi.json
  from https://github.com/vmware/vcf-api-specs

Provenance (security review 2026-09-19, finding 4)
--------------------------------------------------
This script used to fetch, write the schema, then hash the bytes it had
just written and record that as the checksum. That is integrity-at-rest
and nothing more: the sidecar attests only that the file has not changed
since this script wrote it. A MITM'd fetch, a typosquatted host or simply
the wrong `source` argument produced a fully self-consistent
schema+sidecar pair, and the runtime checksum check in vcfspec/schema.py
would then happily certify it. The only control was a human reading the
git diff of a 1,685-line machine-generated JSON file.

So this script now refuses to change anything it has already vendored.
Two digests are recorded, and both are checked before any write:

  sddc-spec.source.sha256   the *fetched upstream document*, bytes as
                            received -- the provenance record
  sddc-spec.schema.json.sha256
                            the extracted bundle -- the at-rest record
                            vcfspec/schema.py enforces at load time

If either differs from what is already on disk, the script prints both
digests and exits non-zero. Passing --accept-new-upstream is the operator's
explicit, deliberate statement that they have reviewed the change and
intend it. This does not make the upstream trustworthy; it makes accepting
a new one a decision someone had to make on purpose, and one that is
visible in the commit message rather than buried in the JSON diff.

A genuinely unchanged re-vendor is a no-op and needs no flag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from jsonschema import Draft202012Validator

RAW_URL = ("https://raw.githubusercontent.com/vmware/vcf-api-specs/main/"
           "specifications/vcf-installer/vcf-installer-openapi.json")
ROOT_SCHEMA = "SddcSpec"
OUT_DIR = Path(__file__).resolve().parent.parent / "vcfspec" / "schemas"
SCHEMA_NAME = "sddc-spec.schema.json"
SOURCE_DIGEST_NAME = "sddc-spec.source.sha256"
ACCEPT_FLAG = "--accept-new-upstream"


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


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _recorded(path: Path) -> str | None:
    """The digest already on disk, or None if nothing is recorded yet."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _refuse_on_change(out: Path, source_digest: str, bundle_text: str,
                      accepted: bool) -> int:
    """0 to proceed, 1 to refuse. Prints both digests either way.

    Checks the *upstream* document first and the extracted bundle second,
    because they answer different questions. A changed upstream digest
    with an identical bundle still stops here: the extraction is lossy
    (it drops `example`, `discriminator`, `xml`, `externalDocs`), so
    "the part we vendor happens to be unchanged" is not the same claim as
    "upstream is unchanged", and only a person can decide the difference
    does not matter. That decision is what ACCEPT_FLAG records.
    """
    changed: list[str] = []
    for name, fresh in ((SOURCE_DIGEST_NAME, source_digest),
                        (f"{SCHEMA_NAME}.sha256", _digest(bundle_text))):
        recorded = _recorded(out / name)
        if recorded is None:
            print(f"  {name}: (nothing vendored yet) -> {fresh[:16]}...")
            continue
        print(f"  {name}: {recorded[:16]}... -> {fresh[:16]}..."
              f"{'  CHANGED' if recorded != fresh else ''}")
        if recorded != fresh:
            changed.append(name)

    if changed and not accepted:
        print(f"refusing: {', '.join(changed)} would change. This script "
              "attests provenance, not just integrity-at-rest: a re-vendor "
              "that silently overwrites a vendored schema would write a "
              "perfectly valid checksum for a hostile one.")
        print(f"If you have reviewed the change and intend it, re-run with "
              f"{ACCEPT_FLAG} and quote both digests in the commit message.")
        return 1
    if changed:
        print(f"{ACCEPT_FLAG} given: accepting the change above.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vendor_schema.py")
    parser.add_argument("version", nargs="?", default="9.1.1.0")
    parser.add_argument("source", nargs="?", default=RAW_URL)
    parser.add_argument(
        ACCEPT_FLAG, action="store_true", dest="accept_new_upstream",
        help="Deliberately accept an upstream document or extracted bundle "
             "that differs from the one already vendored. Without this, any "
             "change is refused.")
    args = parser.parse_args(argv)
    version, source = args.version, args.source

    raw = (urllib.request.urlopen(source, timeout=60).read().decode()
           if source.startswith("http") else Path(source).read_text(encoding="utf-8"))
    source_digest = _digest(raw)
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

    # Every write is below this line -- including the mkdir. A refused
    # re-vendor must leave the filesystem exactly as it found it.
    print(f"vendoring {version} from {source}")
    if _refuse_on_change(out, source_digest, text, args.accept_new_upstream):
        return 1

    out.mkdir(parents=True, exist_ok=True)
    digest = _digest(text)
    (out / SCHEMA_NAME).write_text(text, encoding="utf-8")
    (out / f"{SCHEMA_NAME}.sha256").write_text(digest + "\n", encoding="utf-8")
    (out / SOURCE_DIGEST_NAME).write_text(source_digest + "\n", encoding="utf-8")
    print(f"wrote {len(seen)} schemas for {version}, "
          f"schema sha256={digest[:16]}..., upstream sha256={source_digest[:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
