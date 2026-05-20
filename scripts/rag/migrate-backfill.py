#!/usr/bin/env python3
"""One-time migration: bootstrap state files from an existing AnythingLLM
workspace.

The existing workspace was populated by ad-hoc ingest runs without state
tracking. To stop re-uploading every document on the first refresh, we
need to populate scripts/rag/<state_dir>/<source-id>/documents.json with
the URL → docpath mapping for what's already there.

Strategy:
  1. List all documents in the workspace via AnythingLLM API.
  2. For each document, extract its citation URL from the docSource /
     sourceURL metadata or by scanning the text content for the
     'Source: <url>' / 'URL: <url>' header the ingest tools write.
  3. Match each URL to a source in sources.yaml via doc_prefix or
     domain heuristic.
  4. Write documents.json per source. Content hash field is left as
     "migrated" — the first real refresh will recompute hashes and only
     re-upload documents whose hash actually changed.

This script is idempotent — re-running won't duplicate or harm anything;
it'll just overwrite the documents.json files based on the current
workspace state.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from lib import allm, state as state_mod  # noqa: E402


URL_FROM_TEXT_RE = re.compile(
    r"^(?:Source|URL)\s*:\s*(https?://\S+)", re.MULTILINE
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backfill scripts/rag state from an existing workspace."
    )
    p.add_argument("--sources-file", default=str(SCRIPT_DIR / "sources.yaml"))
    p.add_argument(
        "--workspace",
        help="Limit to one workspace slug (default: all in sources.yaml)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written but don't touch state files",
    )
    return p.parse_args()


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def metadata_dict(doc: dict) -> dict[str, Any]:
    """Normalize a document's metadata to a dict.

    AnythingLLM returns metadata as a parsed object for some docs and as
    a JSON-serialized string for others (depends on ingest path and
    AnythingLLM version). Treat both as dicts; treat null/garbage as {}.
    """
    md = doc.get("metadata")
    if md is None:
        return {}
    if isinstance(md, dict):
        return md
    if isinstance(md, str):
        try:
            parsed = json.loads(md)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def extract_url(doc: dict) -> str | None:
    """Pull the citation URL from a workspace document record."""
    md = metadata_dict(doc)

    for key in ("sourceURL", "source_url", "url", "chunkSource", "docSource"):
        v = md.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v.strip()

    # Some docs carry the original URL in their docpath or pageContent.
    text_blob = doc.get("pageContent") or doc.get("textContent") or ""
    if isinstance(text_blob, str):
        m = URL_FROM_TEXT_RE.search(text_blob)
        if m:
            return m.group(1).strip()

    # AnythingLLM's URL-upload path encodes the source URL in the
    # 'chunkSource' field as 'link://<url>'.
    chunk_source = md.get("chunkSource") or ""
    if isinstance(chunk_source, str) and chunk_source.startswith("link://"):
        return chunk_source[len("link://") :].strip()

    # Some ingest paths put the URL on the top-level doc object.
    for key in ("sourceURL", "url"):
        v = doc.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v.strip()

    return None


def pick_source_for_url(
    url: str, doc_meta: dict, sources: list[dict]
) -> dict | None:
    """Match a workspace doc to one of the sources in sources.yaml.

    Strategy (in order):
      1. Exact docSource match against doc_prefix.
      2. docSource prefix-match (catches "[OFFICIAL] foo v27.0" against
         "[OFFICIAL] foo" or vice versa).
      3. URL domain heuristic against rendered_base / base_url / sitemap_url.
    """
    md_source = (doc_meta or {}).get("docSource") or ""

    # 1. exact match
    for src in sources:
        if md_source and md_source == src.get("doc_prefix"):
            return src

    # 2. prefix-of either direction (handles "v27.0" tail mismatches etc.)
    if md_source:
        for src in sources:
            sp = src.get("doc_prefix", "")
            if not sp:
                continue
            if md_source.startswith(sp) or sp.startswith(md_source):
                return src

    # 3. domain match
    url_domain = domain_of(url)
    for src in sources:
        cfg = src.get("config", {})
        for field in ("rendered_base", "base_url", "sitemap_url"):
            base = cfg.get(field)
            if base and domain_of(base) == url_domain:
                return src

    return None


def main() -> int:
    args = parse_args()

    sources_file = Path(args.sources_file)
    data = yaml.safe_load(sources_file.read_text(encoding="utf-8"))
    all_sources: list[dict] = data.get("sources", [])
    defaults: dict = data.get("defaults", {})

    if not all_sources:
        print("No sources defined in sources.yaml", file=sys.stderr)
        return 2

    api_key = allm.load_api_key()
    client = allm.AnythingLLMClient(
        base_url=defaults.get(
            "allm_base_url", "http://192.168.6.154:3001/api/v1"
        ),
        api_key=api_key,
    )

    state_dir = Path(defaults.get("state_dir", "/tank/rag-state"))

    # Group sources by workspace and process one workspace at a time.
    workspaces = sorted({s["workspace"] for s in all_sources})
    if args.workspace:
        workspaces = [args.workspace]

    for ws in workspaces:
        ws_sources = [s for s in all_sources if s["workspace"] == ws]
        print(f"[{ws}]  fetching workspace documents...")
        docs = client.list_workspace_documents(ws)
        print(f"  {len(docs)} documents in workspace")

        # Bucket per source id.
        per_source: dict[str, dict[str, dict[str, Any]]] = {
            s["id"]: {} for s in ws_sources
        }
        unmatched: list[str] = []

        for doc in docs:
            url = extract_url(doc)
            if not url:
                unmatched.append(doc.get("docpath") or doc.get("name") or "?")
                continue

            src = pick_source_for_url(url, doc.get("metadata") or {}, ws_sources)
            if src is None:
                unmatched.append(url)
                continue

            docpath = doc.get("docpath") or doc.get("location") or doc.get("name")
            per_source[src["id"]][url] = {
                # 'migrated' hash → real refresh will recompute and either
                # accept as unchanged or treat as an update.
                "hash": "migrated",
                "last_fetched": state_mod.utcnow_iso(),
                "allm_doc_path": docpath,
                "allm_doc_name": doc.get("title") or url,
                "metadata": doc.get("metadata") or {},
            }

        # Report + persist per source.
        for src in ws_sources:
            entries = per_source[src["id"]]
            print(f"    {src['id']}: {len(entries)} documents matched")
            if args.dry_run:
                continue
            ss = state_mod.SourceState(state_dir, src["id"])
            ss.save_documents(entries)
            manifest = ss.load_manifest()
            manifest["handler"] = src["handler"]
            manifest["workspace"] = src["workspace"]
            manifest["doc_prefix"] = src["doc_prefix"]
            manifest["stats"] = {
                "document_count": len(entries),
                "last_change": "backfilled from existing workspace",
            }
            ss.save_manifest(manifest)

        if unmatched:
            print(f"  WARNING: {len(unmatched)} documents could not be matched")
            for u in unmatched[:10]:
                print(f"    - {u}")
            if len(unmatched) > 10:
                print(f"    ... and {len(unmatched) - 10} more")
            print(
                "  Unmatched docs are probably from sources not yet in "
                "sources.yaml, or from ad-hoc URL uploads. Add the source "
                "and re-run, or leave them — they'll just live in the "
                "workspace without being tracked by refresh."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
