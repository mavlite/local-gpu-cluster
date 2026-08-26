#!/usr/bin/env python3
"""Mark tracked URLs whose upstream page is gone, without deleting their content.

THE TRAP THIS EXISTS TO CLOSE. sdg-community showed "388 never fetched" in
rag-status for weeks. That reads like a backlog, so the obvious cleanup is to
drop those URLs from the curated list. It would have been destructive: all 373
of them (after a User-Agent fix recovered 15) already own documents that are
ATTACHED AND SERVING in the workspace. They were adopted by migrate_backfill
from the original ad-hoc ingest, captured while the pages still existed. The
pages now 404, so the corpus holds the only remaining copy.

"Never fetched" conflates two states that need opposite handling:

  PENDING   not fetched yet; a future run will get it
  ARCHIVED  upstream is gone; content retained, no run will ever refetch it

Without the distinction the warning never clears, which trains you to ignore it,
and every reader is invited to "clean up" irreplaceable documents.

    mark-unfetchable.py --source sdg-community            # probe + report
    mark-unfetchable.py --source sdg-community --apply    # write the marks

Only 404/410 are marked. 403/406 usually mean blocked, not gone -- that is what
the missing User-Agent turned out to be -- and those stay PENDING so a later fix
can recover them.
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

GONE = (404, 410)
UA = "local-gpu-cluster-rag/1.0 (personal documentation mirror)"


def probe(url: str, timeout: int) -> object:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return type(e).__name__


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--state-dir", default="/tank/rag-state")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--apply", action="store_true",
                    help="write upstream_gone marks into documents.json")
    args = ap.parse_args()

    path = os.path.join(args.state_dir, args.source, "documents.json")
    if not os.path.isfile(path):
        print("no state for %s" % args.source, file=sys.stderr)
        return 2
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)

    todo = [u for u, v in state.items()
            if not str(v.get("hash", "")).startswith("sha256:")
            and not v.get("upstream_gone")]
    print("  %s: %d URL(s) tracked-but-unfetched to probe" % (args.source, len(todo)))
    if not todo:
        return 0

    codes = collections.Counter()
    gone, kept = [], []
    for i, u in enumerate(todo, 1):
        c = probe(u, args.timeout)
        codes[c] += 1
        (gone if c in GONE else kept).append(u)
        if i % 50 == 0 or i == len(todo):
            print("    probed %d/%d" % (i, len(todo)), flush=True)
        time.sleep(args.delay)

    print("  status codes: %s" % dict(codes))
    print("  upstream GONE (404/410): %d" % len(gone))
    print("  still PENDING (may recover): %d" % len(kept))
    withdoc = sum(1 for u in gone if state[u].get("allm_doc_path"))
    print("  of the gone, %d own a document that STAYS in the workspace" % withdoc)

    if not args.apply:
        print("\n  re-run with --apply to record the marks (no content is deleted)")
        return 1

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for u in gone:
        state[u]["upstream_gone"] = stamp
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    print("  marked %d URL(s) upstream_gone=%s" % (len(gone), stamp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
