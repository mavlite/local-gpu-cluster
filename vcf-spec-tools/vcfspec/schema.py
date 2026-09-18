# vcf-spec-tools/vcfspec/schema.py
"""Loading the vendored SddcSpec schema, with integrity enforcement."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

DEFAULT_VERSION = "9.1.1.0"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


class SchemaIntegrityError(RuntimeError):
    """The vendored schema does not match its recorded checksum."""


def schema_path(version: str = DEFAULT_VERSION) -> Path:
    # Read the module global at call time so tests can point it elsewhere.
    return SCHEMA_DIR / version / "sddc-spec.schema.json"


@lru_cache(maxsize=8)
def load_schema(version: str = DEFAULT_VERSION) -> dict:
    path = schema_path(version)
    if not path.exists():
        raise FileNotFoundError(f"no vendored schema for VCF {version}: {path}")
    text = path.read_text(encoding="utf-8")
    expected = (path.parent / f"{path.name}.sha256").read_text(encoding="utf-8").strip()
    actual = hashlib.sha256(text.encode()).hexdigest()
    if actual != expected:
        raise SchemaIntegrityError(
            f"schema for {version} failed integrity check: "
            f"expected {expected[:16]}..., got {actual[:16]}...")
    return json.loads(text)
