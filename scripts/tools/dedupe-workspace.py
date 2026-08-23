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

This script repairs corpora ingested before that fix. It detaches every
duplicated docpath and re-attaches it exactly once, which also rebuilds the
chunk set cleanly. The underlying document files are never deleted -- only the
workspace attachment is rewritten.

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
        print(f"\n  DRY RUN: would detach and re-attach {len(dupes)} docpaths "
              f"in batches of {args.batch_size}.")
        return 0

    def batched(seq):
        for i in range(0, len(seq), args.batch_size):
            yield seq[i:i + args.batch_size]

    # Detach every duplicated path (removes all of its rows), then re-attach
    # once. Done per batch so a failure leaves the rest of the corpus intact.
    for i, chunk in enumerate(batched(dupes), start=1):
        t0 = time.time()
        api(args.allm, args.api_key, "POST",
            f"/workspace/{args.workspace}/update-embeddings",
            {"adds": [], "deletes": chunk})
        api(args.allm, args.api_key, "POST",
            f"/workspace/{args.workspace}/update-embeddings",
            {"adds": chunk, "deletes": []})
        print(f"    batch {i}: {len(chunk)} paths re-attached "
              f"({time.time() - t0:.0f}s)", flush=True)

    after = workspace_docpaths(args.allm, args.api_key, args.workspace)
    ac = collections.Counter(p for p in after if p)
    still = [p for p, n in ac.items() if n > 1]
    print(f"\n  after: {len(after)} rows / {len(ac)} distinct / "
          f"{len(still)} still duplicated")
    return 0 if not still else 1


if __name__ == "__main__":
    sys.exit(main())
