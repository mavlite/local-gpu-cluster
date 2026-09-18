# vcf-spec-tools/vcfspec/schema.py
"""Loading the vendored SddcSpec schema, with integrity enforcement."""
from __future__ import annotations

import copy
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
def _load_verified(version: str) -> dict:
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


def load_schema(version: str = DEFAULT_VERSION) -> dict:
    """Return a private copy: callers must not share schema state.

    The parsed schema is cached (re-reading and re-hashing a ~70 KB file on
    every validation call would be wasteful), but handing out the cached
    dict directly would let one caller's in-place edit corrupt every other
    caller for the rest of the process. Deep-copy on the way out instead —
    do not "optimise" this away.
    """
    return copy.deepcopy(_load_verified(version))


# Tests and callers clear the cache through the public name.
load_schema.cache_clear = _load_verified.cache_clear
