#!/usr/bin/env python3
"""Remove workspace documents that no source owns, safely.

Unowned documents are ones no entry in /tank/rag-state/*/documents.json claims.
They are invisible to every refresh: never updated, never removed, never
re-validated. That makes them attractive to "clean up" -- and dangerous, because
some unowned documents are the ONLY surviving copy of content whose upstream is
gone. sdg-community had 364 of exactly that kind.

So this refuses to guess. It removes only the docpaths you hand it, records
everything about them first, and re-snapshots the workspace before every batch.

WHY RE-SNAPSHOT: an earlier cleanup computed its target list once and reused it
across batches. The workspace shifted underneath and it detached 75 documents it
should not have. Recovering them was luck, not design.

Removal is two steps, both required. update-embeddings detaches from the
workspace (retrieval stops seeing it); remove-documents deletes the stored file.
Detaching alone leaves orphaned storage; deleting alone can leave dangling
vectors.

    remove-unowned.py --workspace vcf-reference --paths-file list.json --limit 5
    remove-unowned.py --workspace vcf-reference --paths-file list.json --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def api(base, key, method, path, body=None, timeout=600):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def snapshot(base, key, slug):
    d = api(base, key, "GET", f"/workspace/{slug}", timeout=300)
    w = d.get("workspace")
    if isinstance(w, list):
        w = w[0] if w else {}
    return {x.get("docpath"): x for x in (w or d).get("documents", [])}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--paths-file", required=True,
                    help="JSON list of docpaths to remove")
    ap.add_argument("--allm", default=os.environ.get(
        "ALLM", "http://192.168.6.154:3001/api/v1"))
    ap.add_argument("--api-key", default=os.environ.get("ALLM_API_KEY", ""))
    ap.add_argument("--backup", default="/tank/rag-state/_removed",
                    help="where the pre-removal record is written")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0,
                    help="only act on the first N (trial run)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not args.api_key:
        print("ERROR: set ALLM_API_KEY", file=sys.stderr)
        return 2

    want = json.load(open(args.paths_file, encoding="utf-8"))
    if args.limit:
        want = want[:args.limit]

    live = snapshot(args.allm, args.api_key, args.workspace)
    present = [p for p in want if p in live]
    absent = len(want) - len(present)
    print(f"  requested {len(want)}  present in workspace {len(present)}  "
          f"already absent {absent}")
    if not present:
        print("  nothing to do.")
        return 0

    if not args.apply:
        for p in present[:5]:
            print(f"    {p[-96:]}")
        print(f"\n  re-run with --apply to remove {len(present)} document(s).")
        return 1

    os.makedirs(args.backup, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rec = os.path.join(args.backup, f"{args.workspace}-{stamp}.json")
    with open(rec, "w", encoding="utf-8") as fh:
        json.dump([live[p] for p in present], fh, indent=2)
    print(f"  recorded {len(present)} document(s) -> {rec}")

    done = 0
    for i in range(0, len(present), args.batch_size):
        # Re-snapshot every batch: the workspace can change underneath us.
        fresh = snapshot(args.allm, args.api_key, args.workspace)
        chunk = [p for p in present[i:i + args.batch_size] if p in fresh]
        if not chunk:
            continue
        api(args.allm, args.api_key, "POST",
            f"/workspace/{args.workspace}/update-embeddings",
            {"adds": [], "deletes": chunk})
        api(args.allm, args.api_key, "DELETE", "/system/remove-documents",
            {"names": chunk})
        done += len(chunk)
        print(f"    batch {i // args.batch_size + 1}: removed {len(chunk)} "
              f"(total {done})", flush=True)

    after = snapshot(args.allm, args.api_key, args.workspace)
    still = [p for p in present if p in after]
    print(f"\n  removed {done}; still present {len(still)}")
    print(f"  workspace now holds {len(after)} document(s)")
    return 0 if not still else 1


if __name__ == "__main__":
    sys.exit(main())
