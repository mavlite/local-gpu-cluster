#!/usr/bin/env python3
"""dedupe-workspace.py — collapse double-attached documents in an AnythingLLM
workspace.

A document can end up attached to the same workspace twice: two rows in
workspace_documents with different ids pointing at one file. Both copies get
embedded, so the duplicate chunks compete for the same top-N retrieval slots.

Cause (fixed 2026-08-23 in lib/allm.py): `upload_raw_text` passed
`addToWorkspaces`, which attaches on upload, and refresh.py then attached the
same doc again via update-embeddings(adds=...). Measured in vcf-reference
before the fix: 934 of 6,637 documents double-attached, and a top-12
vector-search returned only 9 distinct chunks.

This script repairs corpora ingested before that fix.

Mechanics, established empirically against the live API -- do not "improve"
this without re-checking. update-embeddings `deletes` removes exactly ONE
attachment row for a docpath, not all of them. Traced on a 2-row document:

    before        : 2
    after delete  : 1
    after re-add  : 2

So the repair is a single delete per surplus attachment and nothing else. An
earlier version of this script detached then re-attached, which is a
2 -> 1 -> 2 no-op; it churned the vector store for ~30 minutes while the
duplicate count oscillated between 739 and 934, and was stopped. The surviving
attachment keeps its chunks, so no re-embedding is needed.

The underlying document files are never deleted -- only the surplus workspace
attachment is dropped.

RUN THIS SUPERVISED. It mutates a live corpus, and the first two versions of
it both misbehaved (see above). Watch the distinct-docpath count: it must stay
CONSTANT. If it falls, stop and re-attach -- every tracked path is recoverable
from the source state's allm_doc_path.

Usage:
    python3 scripts/tools/dedupe-workspace.py --workspace vcf-reference --dry-run
    python3 scripts/tools/dedupe-workspace.py --workspace vcf-reference --apply

Requires ALLM_API_KEY (or --api-key) and ALLM base URL (--allm).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request


def api(base: str, key: str, method: str, path: str, body=None, timeout=1800):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def workspace_docpaths(base: str, key: str, slug: str) -> list[str]:
    data = api(base, key, "GET", f"/workspace/{slug}", timeout=180)
    ws = data.get("workspace")
    if isinstance(ws, list):
        ws = ws[0] if ws else {}
    ws = ws or data
    return [d.get("docpath") or "" for d in ws.get("documents", [])]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--allm", default=os.environ.get(
        "ALLM", "http://192.168.6.154:3001/api/v1"))
    ap.add_argument("--api-key", default=os.environ.get("ALLM_API_KEY", ""))
    ap.add_argument("--batch-size", type=int, default=200,
                    help="update-embeddings degrades on large payloads")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not args.api_key:
        print("ERROR: set ALLM_API_KEY or pass --api-key", file=sys.stderr)
        return 2

    paths = workspace_docpaths(args.allm, args.api_key, args.workspace)
    counts = collections.Counter(p for p in paths if p)
    dupes = sorted(p for p, n in counts.items() if n > 1)

    print(f"  workspace rows   : {len(paths)}")
    print(f"  distinct docpaths: {len(counts)}")
    print(f"  double-attached  : {len(dupes)}")
    hist = collections.Counter(counts.values())
    print(f"  multiplicity     : {dict(sorted(hist.items()))}")

    if not dupes:
        print("  nothing to do.")
        return 0
    for p in dupes[:5]:
        print(f"    {counts[p]}x {p[:88]}")

    if args.dry_run:
        surplus = sum(n - 1 for n in counts.values() if n > 1)
        print(f"\n  DRY RUN: would drop {surplus} surplus attachment(s) across "
              f"{len(dupes)} docpaths, in batches of {args.batch_size}. "
              f"No re-embedding — the surviving attachment keeps its chunks.")
        return 0

    def batched(seq):
        for i in range(0, len(seq), args.batch_size):
            yield seq[i:i + args.batch_size]

    # One delete per surplus attachment, RE-SNAPSHOTTING between batches.
    #
    # The re-snapshot is not optional. A run using counts cached from the start
    # detached 75 documents entirely: paths that had already dropped to a
    # single row were still in the stale duplicate list, so deleting them took
    # them to zero. They were recoverable (the files stay on disk and the
    # source state keeps allm_doc_path, so re-adding restored them), but the
    # corpus was short 75 documents until it was noticed.
    #
    # Guard: before every batch, re-read the workspace and keep only paths that
    # STILL have more than one row. Never delete a path at count 1.
    remaining_rounds = 0
    while True:
        remaining_rounds += 1
        if remaining_rounds > 10:
            print("  giving up after 10 rounds; investigate before re-running")
            break
        live = collections.Counter(
            p for p in workspace_docpaths(args.allm, args.api_key, args.workspace) if p)
        targets = sorted(p for p, n in live.items() if n > 1)
        if not targets:
            break
        print(f"  round {remaining_rounds}: {len(targets)} paths still duplicated")
        for i, chunk in enumerate(batched(targets), start=1):
            t0 = time.time()
            # Re-verify this batch against a fresh read; the previous batch may
            # have changed things.
            live2 = collections.Counter(
                p for p in workspace_docpaths(args.allm, args.api_key, args.workspace) if p)
            safe = [p for p in chunk if live2.get(p, 0) > 1]
            skipped = len(chunk) - len(safe)
            if not safe:
                continue
            api(args.allm, args.api_key, "POST",
                f"/workspace/{args.workspace}/update-embeddings",
                {"adds": [], "deletes": safe})
            print(f"    batch {i}: dropped {len(safe)} surplus"
                  + (f", skipped {skipped} no longer duplicated" if skipped else "")
                  + f" ({time.time() - t0:.0f}s)", flush=True)

    after = workspace_docpaths(args.allm, args.api_key, args.workspace)
    ac = collections.Counter(p for p in after if p)
    still = [p for p, n in ac.items() if n > 1]
    print(f"\n  after: {len(after)} rows / {len(ac)} distinct / "
          f"{len(still)} still duplicated")
    return 0 if not still else 1


if __name__ == "__main__":
    sys.exit(main())
