#!/usr/bin/env python3
"""One-off state-surgery: split blog URLs out of truenas-scale-docs into truenas-blog.

migrate_backfill.py's prefix-match was loose enough that ~595 blog posts
under https://www.truenas.com/blog/ landed in the truenas-scale-docs
state alongside the docs URLs (https://www.truenas.com/docs/). Without
this fix the next refresh run would compute REMOVE for every blog URL
(the github_repo handler doesn't yield blog URLs), and the safety
threshold halts a 595-doc deletion plan every time.

This script moves the blog URL entries from truenas-scale-docs into a
parked truenas-blog state directory, preserving allm_doc_path so the
existing AnythingLLM documents stay addressable when the rss handler
ships. AnythingLLM is NOT touched — the docs stay in the workspace,
they just move out of the docs-source state.

Idempotent: re-running after a successful split is a no-op (the source
state has no remaining blog URLs and the target state already exists).

Usage:
    --source-id <id>      Source to split blog URLs OUT of (default: truenas-scale-docs)
    --target-id <id>      Source to move them TO          (default: truenas-blog)
    --blog-prefix <url>   URL prefix that identifies blog entries
                          (default: https://www.truenas.com/blog/)
    --apply               Actually write the moved state (default is dry-run)
    --sources-file <p>    Override sources.yaml path
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

from lib import state as state_mod  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-id", default="truenas-scale-docs")
    p.add_argument("--target-id", default="truenas-blog")
    p.add_argument("--blog-prefix", default="https://www.truenas.com/blog/")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the moved state (default is dry-run)",
    )
    p.add_argument("--sources-file", default=str(SCRIPT_DIR / "sources.yaml"))
    return p.parse_args()


def main() -> int:
    args = parse_args()

    data = yaml.safe_load(Path(args.sources_file).read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    state_dir = Path(defaults.get("state_dir", "/tank/rag-state"))

    src = state_mod.SourceState(state_dir, args.source_id)
    tgt = state_mod.SourceState(state_dir, args.target_id)

    src_docs = src.load_documents()
    tgt_docs = tgt.load_documents()

    if not src_docs:
        print(f"[{args.source_id}] state is empty — nothing to split.")
        return 0

    # Partition the source state by blog-prefix match.
    blog_entries: dict[str, dict] = {}
    keep_entries: dict[str, dict] = {}
    for url, entry in src_docs.items():
        if url.startswith(args.blog_prefix):
            blog_entries[url] = entry
        else:
            keep_entries[url] = entry

    print(f"[{args.source_id}] {len(src_docs)} tracked URLs")
    print(f"  blog (move to {args.target_id}): {len(blog_entries)}")
    print(f"  keep (stay in {args.source_id}):  {len(keep_entries)}")
    print(f"[{args.target_id}] {len(tgt_docs)} tracked URLs already present")

    if not blog_entries:
        print("\nNo blog URLs in source state — nothing to do.")
        return 0

    # Detect collisions with anything already in the target state.
    collisions = sorted(set(blog_entries) & set(tgt_docs))
    if collisions:
        print(f"\nWARNING: {len(collisions)} URLs already exist in target state.")
        for url in collisions[:5]:
            print(f"  {url}")
        if len(collisions) > 5:
            print(f"  ... and {len(collisions) - 5} more")
        print("  Existing target entries will be PRESERVED (target wins on conflict).")

    print("\nFirst 5 URLs to move:")
    for url in list(blog_entries)[:5]:
        print(f"  {url}")
    if len(blog_entries) > 5:
        print(f"  ... and {len(blog_entries) - 5} more")

    if not args.apply:
        print(
            f"\nDRY-RUN. Re-run with --apply to write the split "
            f"(source: -{len(blog_entries)}, target: +{len(blog_entries) - len(collisions)})."
        )
        return 0

    # Apply: merge blog entries into target (target wins), then trim source.
    # Order matters — write target first so a failure between writes leaves
    # the entries findable in BOTH places rather than lost in neither.
    merged_target = dict(blog_entries)
    merged_target.update(tgt_docs)  # target wins on URL collision
    tgt.save_documents(merged_target)

    # Stamp a manifest on the target so refresh.py's interval check has
    # something to read. Mark this as a migration so future operators know
    # the entries weren't ingested by the (still-unimplemented) rss handler.
    tgt_manifest = tgt.load_manifest()
    tgt_manifest.setdefault("source_id", args.target_id)
    tgt_manifest["stats"] = {
        "document_count": len(merged_target),
        "last_note": f"populated by split_truenas_blog.py from {args.source_id}",
    }
    tgt.save_manifest(tgt_manifest)

    src.save_documents(keep_entries)

    print(
        f"\nApplied. {args.source_id}: {len(src_docs)} -> {len(keep_entries)}, "
        f"{args.target_id}: {len(tgt_docs)} -> {len(merged_target)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
