#!/usr/bin/env python3
"""One-off cleanup helper for an interrupted refresh run.

When refresh.py is Ctrl-C'd mid-flight (before the resilience patch),
the workspace ends up with duplicate documents: each interrupted
upload left a new docpath in AnythingLLM, but state was never updated
to track it. Re-running refresh would create even more duplicates.

This script identifies the duplicates by:
  1. Filtering workspace documents by docSource (one source at a time)
  2. Comparing against the persisted state's allm_doc_path values
  3. Reporting (and optionally deleting) docpaths that are NOT in state
     — those are the orphan uploads from interrupted runs

Usage:
    --source <id>     The source to clean up (e.g. opnsense-docs)
    --dry-run         Show what would be deleted (default)
    --apply           Actually delete the orphan docpaths
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from lib import allm, state as state_mod  # noqa: E402
from migrate_backfill import metadata_dict  # noqa: E402


def _call_with_heartbeat(fn, label: str, interval_seconds: int = 30):
    """Same heartbeat helper as in refresh.py — prints elapsed time every
    `interval_seconds` from a daemon thread so the operator knows the
    request is in flight and doesn't Ctrl-C."""
    done = threading.Event()
    start = time.time()

    def beat():
        while not done.wait(interval_seconds):
            elapsed = int(time.time() - start)
            print(f"  ... {label}: still waiting on AnythingLLM ({elapsed}s elapsed)")

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        return fn()
    finally:
        done.set()
        t.join(timeout=1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True, help="Source id from sources.yaml")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete orphan docs (default is dry-run)",
    )
    p.add_argument("--sources-file", default=str(SCRIPT_DIR / "sources.yaml"))
    return p.parse_args()


def main() -> int:
    args = parse_args()

    data = yaml.safe_load(Path(args.sources_file).read_text(encoding="utf-8"))
    sources = data.get("sources", [])
    defaults = data.get("defaults", {})

    source = next((s for s in sources if s["id"] == args.source), None)
    if not source:
        print(f"No source with id={args.source!r}", file=sys.stderr)
        return 2

    state_dir = Path(defaults.get("state_dir", "/tank/rag-state"))
    src_state = state_mod.SourceState(state_dir, source["id"])
    persisted = src_state.load_documents()

    # Set of allm_doc_path values the state knows about. These are the
    # "official" documents — everything else with the same docSource is
    # an orphan from an interrupted refresh.
    tracked_paths = {
        entry.get("allm_doc_path")
        for entry in persisted.values()
        if entry.get("allm_doc_path")
    }
    print(f"  state has {len(tracked_paths)} tracked docpaths for source")

    client = allm.AnythingLLMClient(
        base_url=defaults.get(
            "allm_base_url", "http://192.168.6.154:3001/api/v1"
        ),
        api_key=allm.load_api_key(),
    )

    print(f"  fetching workspace documents from {source['workspace']}...")
    all_docs = client.list_workspace_documents(source["workspace"])
    print(f"  workspace has {len(all_docs)} total documents")

    # Filter to docs whose docSource matches this source's doc_prefix.
    # Use prefix-match (same loose matching as migrate_backfill) to handle
    # any past prefix drift.
    prefix = source["doc_prefix"]
    matching = []
    for d in all_docs:
        md = metadata_dict(d)
        ds = md.get("docSource") or ""
        if ds == prefix or ds.startswith(prefix) or prefix.startswith(ds):
            matching.append(d)

    print(f"  {len(matching)} workspace docs match docSource '{prefix}'")

    # Bucket each matching doc as TRACKED or ORPHAN.
    orphans = []
    tracked = 0
    for d in matching:
        docpath = d.get("docpath") or d.get("location") or d.get("name")
        if docpath in tracked_paths:
            tracked += 1
        else:
            orphans.append(d)

    print(f"  tracked (in state): {tracked}")
    print(f"  orphans (NOT in state): {len(orphans)}")

    if not orphans:
        print("\nNothing to clean up.")
        return 0

    print("\nFirst 10 orphan docpaths to delete:")
    for d in orphans[:10]:
        dp = d.get("docpath") or d.get("location") or d.get("name")
        ts = d.get("createdAt", "")
        print(f"  {dp}  (created {ts})")

    if not args.apply:
        print(
            f"\nDRY-RUN. Re-run with --apply to delete {len(orphans)} orphan "
            "docpaths from AnythingLLM."
        )
        return 0

    # Delete in batches via /workspace/{slug}/update-embeddings (removes
    # them from the workspace embeddings) AND /system/remove-documents
    # (removes the underlying files from AnythingLLM storage).
    orphan_paths = [
        d.get("docpath") or d.get("location") or d.get("name") for d in orphans
    ]
    orphan_paths = [p for p in orphan_paths if p]

    print(f"\nRemoving {len(orphan_paths)} orphans from workspace embeddings...")
    print(
        "  NOTE: AnythingLLM keeps processing server-side even if you Ctrl-C. "
        "Don't interrupt — heartbeats below confirm the request is in flight."
    )
    try:
        _call_with_heartbeat(
            lambda: client.update_embeddings(
                workspace=source["workspace"],
                adds=[],
                removes=orphan_paths,
                timeout=defaults.get("embed_timeout_seconds", 1800),
            ),
            label="orphan-removal embedding update",
        )
        print("  embeddings removed")
    except Exception as e:
        print(f"  WARNING: update_embeddings raised {e}")
        print("  Continuing to delete files anyway.")

    print(f"Deleting {len(orphan_paths)} orphan files from AnythingLLM storage...")
    try:
        _call_with_heartbeat(
            lambda: client.delete_documents(orphan_paths),
            label="orphan file deletion",
        )
        print("  files deleted")
    except Exception as e:
        print(f"  WARNING: delete_documents raised {e}")

    print("\nCleanup complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
