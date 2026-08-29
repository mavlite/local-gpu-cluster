#!/usr/bin/env python3
"""Un-freeze documents that migrate_backfill adopted but never re-fetched.

THE FREEZE. migrate_backfill.py adopts an existing workspace document for a URL
and records hash="migrated". On the next refresh, refresh.py takes a shortcut
(see its "Promote migrated-hash entries" block): it stamps the REAL content hash
and skips the upload, reasoning that the adopted content was vetted at migration
time.

That reasoning fails for anything adopted from the pre-refresh.py upload-link
ingest. Those documents are raw page scrapes -- they open "TechDocs / Login /
Register" and are mostly navigation chrome -- not the trafilatura extraction the
handler produces today. Promotion silently blesses them as current, and because
the stored hash now equals what the handler yields, NO future refresh will ever
replace them. Measured 2026-08-29 after a verified-full-coverage re-ingest:
1,346 of 10,482 VCF documents (12.8%) were still May-era content while their
state looked perfectly healthy.

This resets the state for those URLs so the next refresh re-uploads them:

  hash          -> a sentinel that cannot match any real content hash, so
                   plan.compute() classifies the URL as an UPDATE. Deliberately
                   NOT "migrated" -- that would re-trigger the same shortcut.
  last_fetched  -> epoch, so _prioritise() puts them at the FRONT of a budgeted
                   source's queue instead of leaving them to chance.

Identify the URLs first (a document whose stored content does not begin with the
handler's "Source: <url>" header is stale), then:

    reset-stale-adopted.py --state-dir /tank/rag-state --urls-file stale.json
    reset-stale-adopted.py ... --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

SENTINEL = "sha256:stale-adopted-reset"
EPOCH = "1970-01-01T00:00:00Z"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", default="/tank/rag-state")
    ap.add_argument("--urls-file", required=True,
                    help='JSON {source_id: [url, ...]}')
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with open(args.urls_file, encoding="utf-8") as fh:
        by_source = json.load(fh)

    total = 0
    for sid, urls in sorted(by_source.items()):
        path = os.path.join(args.state_dir, sid, "documents.json")
        if not os.path.isfile(path):
            print("  %s: no state file, skipped" % sid, file=sys.stderr)
            continue
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)

        hit = [u for u in urls if u in state]
        print("  %-22s %5d url(s) to reset (of %d in state)"
              % (sid, len(hit), len(state)))
        total += len(hit)
        if not args.apply:
            continue

        shutil.copy2(path, path + ".PRE-RESET-" +
                     time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        for u in hit:
            entry = dict(state[u])
            entry["hash"] = SENTINEL
            entry["last_fetched"] = EPOCH
            state[u] = entry
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
        print("    reset written (backup alongside)")

    if not args.apply:
        print("\n  would reset %d url(s); re-run with --apply" % total)
        return 1
    print("\n  reset %d url(s). Re-run each source with --force to re-upload."
          % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
