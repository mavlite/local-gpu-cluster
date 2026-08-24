#!/usr/bin/env python3
"""verify-removals.py — check whether a halted refresh's removals are real.

refresh.py halts when a plan would delete more than `max_delete_pct` of a
source's documents and writes the plan to _proposals/ for review. The whole
point of that halt is that a human looks before deleting, so "approve it and
see" is not review. This does the looking: it samples the proposed removals
and asks the origin server whether those URLs still exist.

    404 / 410  -> genuinely gone; removal is correct
    200        -> STILL LIVE; approving would delete content that exists,
                  which usually means the source's include/exclude patterns
                  no longer match the URL shape rather than upstream deleting
    3xx        -> moved; the content lives at a new URL. Removal may be fine
                  IF the same plan adds the new location, but check.

Precedent for why the distinction matters: an openzfs-docs halt on 2026-08-22
looked like an 8-document deletion but was really an upstream path move
(Basic Concepts/X -> Basic Concepts/Data Storage/X), with the new paths
already in the plan's adds. A vcf-remaining-docs halt on 2026-08-24 sampled
30/30 at 404 -- a genuine Broadcom restructure that dropped old GUID-style
URLs -- and was safe to approve.

Usage:
    python3 scripts/tools/verify-removals.py <proposal.json> [--sample 30]
    python3 scripts/tools/verify-removals.py <proposal.json> --all

Exit 0 if every sampled URL is gone (safe to approve), 1 if any is still live.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; lgc-verify-removals/1.0)"


def head(url: str, timeout: int) -> int | str:
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    try:
        return urllib.request.urlopen(req, timeout=timeout).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:                       # DNS, TLS, timeout, ...
        return f"ERR:{type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("proposal")
    ap.add_argument("--sample", type=int, default=30,
                    help="how many removals to probe (default 30)")
    ap.add_argument("--all", action="store_true", help="probe every removal")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (politeness)")
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--seed", type=int, default=42,
                    help="fixed so a re-run probes the same sample")
    args = ap.parse_args()

    plan = json.load(open(args.proposal, encoding="utf-8"))
    removes = [u if isinstance(u, str) else str(u)
               for u in (plan.get("removes") or [])]
    adds = plan.get("adds") or []
    print(f"  source : {plan.get('source_id')}")
    print(f"  reason : {plan.get('reason')}")
    print(f"  removes: {len(removes)}   adds: {len(adds)}")
    if not removes:
        print("  nothing to verify.")
        return 0

    if args.all:
        sample = removes
    else:
        random.seed(args.seed)
        sample = random.sample(removes, min(args.sample, len(removes)))
    print(f"  probing {len(sample)} of {len(removes)} upstream...\n")

    codes: collections.Counter = collections.Counter()
    live: list[str] = []
    moved: list[str] = []
    for u in sample:
        c = head(u, args.timeout)
        codes[c] += 1
        if c == 200:
            live.append(u)
        elif isinstance(c, int) and 300 <= c < 400:
            moved.append(u)
        time.sleep(args.delay)

    print(f"  status distribution: {dict(codes)}")
    if moved:
        print(f"\n  {len(moved)} MOVED (3xx) — confirm the plan adds the new "
              f"location before approving:")
        for u in moved[:8]:
            print(f"     {u[-92:]}")
    if live:
        print(f"\n  !! {len(live)} STILL LIVE (HTTP 200). Approving would "
              f"delete content that still exists.")
        print("     Check the source's include/exclude patterns before "
              "approving — a pattern that no longer matches the current URL "
              "shape looks exactly like upstream deletion.")
        for u in live[:8]:
            print(f"     {u[-92:]}")
        return 1

    gone = sum(v for k, v in codes.items() if isinstance(k, int) and k in (404, 410))
    print(f"\n  {gone}/{len(sample)} confirmed gone (404/410). "
          f"Removal looks correct.")
    if gone < len(sample):
        print("  NOTE: the rest were errors, not confirmations — re-run "
              "before treating this as a clean result.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
