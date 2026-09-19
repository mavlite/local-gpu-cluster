# vcf-spec-tools/vcfspec/schema.py
"""Loading the vendored SddcSpec schema, with integrity enforcement.

`version` is caller-controlled: it arrives from an MCP tool argument
(`vcf_version`) or a CLI `--version` flag, i.e. from exactly the agent the
rest of this package is defending the operator against. It must therefore
never reach the filesystem as text.

Findings 1 and 2 of the 2026-09-19 security review were one bug with two
faces, both caused by `SCHEMA_DIR / version / ...` being built from that
text directly:

1. Integrity bypass. The SHA-256 sidecar is read from `path.parent` -- the
   *same* directory the caller chose. A `version` of
   "../../../../AppData/Local/Temp/.../evil" therefore had the attacker
   supplying both halves of the integrity check, and a junk document was
   certified `valid: true` with zero findings. The checksum was not weak;
   it was bound to the wrong thing. It binds a file to its own sibling,
   and only membership of a closed set can bind the *loader* to the
   vendored schema.
2. Existence oracle. The path was built and stat'd whatever the version
   said, so "this directory holds an sddc-spec.schema.json" and "it does
   not" came back as two reliably distinguishable envelopes -- a
   filesystem probe needing no write at all.

resolve_version() is the single gate that closes both. It membership-tests
against the set of versions actually vendored in this package (discovered
by listing SCHEMA_DIR, not by parsing the caller's string) and raises
before any path is built, so:

- nothing outside SCHEMA_DIR can ever be loaded, whatever the string says;
- an unknown version costs the same work, and produces the same finding,
  whether or not anything exists at the path it implied. There is no path.

_VERSION_RE is an *additional* filter applied to the directory listing, not
the gate. A regex alone would still permit "9.1.1.0" to name whatever a
symlink, junction or a future SCHEMA_DIR reshuffle put there; membership of
a listing of real vendored directories is what actually constrains it.
Do not replace the set test with the regex.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

DEFAULT_VERSION = "9.1.1.0"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
SCHEMA_FILENAME = "sddc-spec.schema.json"

# Shape of a vendored version directory: 1-4 dot-separated numeric parts.
# Applied to names read off disk, never to caller text (see module docstring).
_VERSION_RE = re.compile(r"\A[0-9]{1,5}(?:\.[0-9]{1,5}){0,3}\Z")


class SchemaIntegrityError(RuntimeError):
    """The vendored schema does not match its recorded checksum."""


class UnknownSchemaVersion(FileNotFoundError):
    """The requested version is not one this package vendors.

    Subclasses FileNotFoundError so every existing caller that already
    treats "nothing vendored for this version" as a structured finding
    keeps doing so -- there is no second error path to remember.
    """


@lru_cache(maxsize=4)
def _discover(schema_dir: Path) -> frozenset[str]:
    """The closed set of vendored versions, read off disk.

    Keyed on the directory rather than closed over the module global so a
    test that repoints SCHEMA_DIR gets its own entry instead of a stale
    one. maxsize is small and the key is never caller-controlled (it is a
    Path this module owns), so this cannot be grown by a caller.

    A directory only counts when it actually holds the schema file: that
    is what keeps the sibling `schemas/inventory/` tree -- which holds the
    inventory schema, not a VCF release -- out of the set, independently
    of the name filter.
    """
    try:
        entries = sorted(schema_dir.iterdir())
    except OSError:
        return frozenset()
    return frozenset(
        entry.name for entry in entries
        if _VERSION_RE.match(entry.name)
        and entry.is_dir()
        and (entry / SCHEMA_FILENAME).is_file())


def known_versions() -> frozenset[str]:
    """Every VCF version this installation has a vendored schema for."""
    return _discover(SCHEMA_DIR)


def resolve_version(version: str) -> str:
    """Return `version` iff it is a vendored one; raise otherwise.

    The ONLY sanctioned way to turn a caller-supplied version into a path
    segment. Call it before building any path, never after -- an
    after-the-fact `is_relative_to` check on an already-built path is a
    weaker control (it has already stat'd the caller's path by then) and
    is not what this package relies on.

    The raised message deliberately does not quote `version`. Two reasons:
    an unknown version must be indistinguishable from any other unknown
    version in the envelope that eventually carries it, and this package's
    envelopes are read by an AI agent, so reflecting an arbitrary
    caller-supplied string back into them is a surface worth not having.
    Naming the versions that *do* exist is strictly more useful to a
    legitimate caller than repeating what they just sent.
    """
    if type(version) is not str or version not in known_versions():
        raise UnknownSchemaVersion(
            "requested VCF version is not vendored in this installation; "
            f"vendored: {', '.join(sorted(known_versions())) or '(none)'}")
    return version


def schema_path(version: str = DEFAULT_VERSION) -> Path:
    # Read the module global at call time so tests can point it elsewhere.
    return SCHEMA_DIR / resolve_version(version) / SCHEMA_FILENAME


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
    caller for the rest of the process. Deep-copy on the way out instead --
    do not "optimise" this away.

    resolve_version() runs before _load_verified() so the lru_cache is
    keyed on a vendored version and never on arbitrary caller text.
    """
    return copy.deepcopy(_load_verified(resolve_version(version)))


def _cache_clear() -> None:
    """Clear both caches: the parsed schemas and the discovered version set.

    Tests that repoint SCHEMA_DIR need the discovery cache dropped too, or
    the closed set would still describe the previous directory.
    """
    _load_verified.cache_clear()
    _discover.cache_clear()


# Tests and callers clear the cache through the public name.
load_schema.cache_clear = _cache_clear
