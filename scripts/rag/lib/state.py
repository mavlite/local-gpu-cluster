"""Per-source state directory I/O.

Layout under {state_dir}/{source_id}/:
    manifest.json   — last refresh time, schema version, summary stats
    documents.json  — url -> {hash, last_fetched, allm_doc_id, allm_doc_name}
    errors.log      — append-only error trail (newest at bottom)

The two JSON files together are the system's view of "what we put in
AnythingLLM and when." They are the source of truth for diff computation —
not AnythingLLM itself, since AnythingLLM doesn't surface chunk-level
provenance through its API.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_interval(spec: str) -> int:
    """Parse '30d', '12h', '90m' into seconds. Raises ValueError on bad input."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", spec or "")
    if not m:
        raise ValueError(f"Bad interval: {spec!r} (expect e.g. '30d', '12h')")
    n = int(m.group(1))
    unit = m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[unit]
    return n * mult


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    # Accept Z-suffixed and offset-bearing forms.
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class SourceState:
    """Read/write helpers for one source's state files."""

    def __init__(self, root: Path, source_id: str):
        self.dir = root / source_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self.documents_path = self.dir / "documents.json"
        self.errors_path = self.dir / "errors.log"

    # ─── manifest ─────────────────────────────────────────────────────────
    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "source_id": self.dir.name,
                "last_refresh": None,
                "last_success": None,
                "stats": {},
            }
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["schema_version"] = SCHEMA_VERSION
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # ─── documents ────────────────────────────────────────────────────────
    def load_documents(self) -> dict[str, dict[str, Any]]:
        if not self.documents_path.exists():
            return {}
        return json.loads(self.documents_path.read_text(encoding="utf-8"))

    def save_documents(self, docs: dict[str, dict[str, Any]]) -> None:
        # sort_keys for deterministic diffs in version control if state_dir
        # is ever git-tracked (it shouldn't be, but stable output is cheap).
        self.documents_path.write_text(
            json.dumps(docs, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # ─── errors ───────────────────────────────────────────────────────────
    def append_error(self, message: str) -> None:
        line = f"{utcnow_iso()} {message}\n"
        with self.errors_path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def is_due(manifest: dict[str, Any], interval_spec: str) -> bool:
    """True if no successful refresh has happened, or interval has elapsed."""
    last = parse_iso(manifest.get("last_success"))
    if last is None:
        return True
    seconds = parse_interval(interval_spec)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age >= seconds
