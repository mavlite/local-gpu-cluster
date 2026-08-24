#!/usr/bin/env python3
"""reconcile-workspace.py — find documents a source thinks it attached but didn't.

refresh.py persists per-source state BEFORE calling /update-embeddings, so that
a failed embedding retries without re-uploading. The cost of that ordering is a
silent failure mode: if the embedding call dies partway, the documents are
uploaded to AnythingLLM storage and recorded in documents.json as complete, but
never attached to the workspace. They are then invisible twice over --
retrieval cannot see them, and no future refresh re-adds them because their
content hash still matches.

Seen for real on 2026-08-24: a 200-document embedding batch ran past the 1800s
client timeout against a ~7,600-document workspace. AnythingLLM finished it
server-side, but the client had already aborted, so batches 2-5 were never
sent. 698 documents ended up uploaded, tracked, and unattached. Nothing in the
logs said so; only comparing state against the workspace revealed it.

Run this after any refresh that failed at the embedding step.

    python3 scripts/tools/reconcile-workspace.py --workspace vcf-reference
    python3 scripts/tools/reconcile-workspace.py --workspace vcf-reference --fix

NOTE ON EXPECTED RESIDUE: a small non-zero count is normal. AnythingLLM
content-dedupes on embed, so a document whose extracted text matches one
already attached is accepted (HTTP 200) and silently not attached. The 9.0 and
9.1 copies of the same VCF page do this. Those cannot be "fixed" and should
not be chased -- --fix reports them as undeduplicable rather than looping.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time
import urllib.request


def api(base, key, method, path, body=None, timeout=1800):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def workspace_docpaths(base, key, slug):
    d = api(base, key, "GET", f"/workspace/{slug}", timeout=180)
    w = d.get("workspace")
    if isinstance(w, list):
        w = w[0] if w else {}
    return [x.get("docpath") for x in (w or d).get("documents", [])]


def tracked_for(state_dir, slug, sources_file):
    """docpath -> source_id, for every source targeting this workspace."""
    import yaml
    cfg = yaml.safe_load(open(sources_file, encoding="utf-8"))
    ids = [s["id"] for s in cfg["sources"] if s.get("workspace") == slug]
    out = {}
    for sid in ids:
        f = os.path.join(state_dir, sid, "documents.json")
        if not os.path.isfile(f):
            continue
        for rec in json.load(open(f, encoding="utf-8")).values():
            p = rec.get("allm_doc_path")
            if p:
                out[p] = sid
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--allm", default=os.environ.get(
        "ALLM", "http://192.168.6.154:3001/api/v1"))
    ap.add_argument("--api-key", default=os.environ.get("ALLM_API_KEY", ""))
    ap.add_argument("--state-dir", default="/tank/rag-state")
    ap.add_argument("--sources-file",
                    default="/root/local-gpu-cluster/scripts/rag/sources.yaml")
    ap.add_argument("--batch-size", type=int, default=50,
                    help="200 has been observed to exceed the client timeout")
    ap.add_argument("--fix", action="store_true",
                    help="re-attach the missing documents")
    args = ap.parse_args()

    if not args.api_key:
        print("ERROR: set ALLM_API_KEY or pass --api-key", file=sys.stderr)
        return 2

    tracked = tracked_for(args.state_dir, args.workspace, args.sources_file)
    present = set(workspace_docpaths(args.allm, args.api_key, args.workspace))
    missing = sorted(p for p in tracked if p not in present)

    print(f"  tracked by sources : {len(tracked)}")
    print(f"  present in workspace: {len(tracked) - len(missing)}")
    print(f"  MISSING             : {len(missing)}")
    if missing:
        by = collections.Counter(tracked[p] for p in missing)
        print(f"  by source           : {dict(by)}")
    if not missing:
        print("  workspace and state agree.")
        return 0
    if not args.fix:
        for p in missing[:5]:
            print(f"    {p[-88:]}")
        print(f"\n  re-run with --fix to re-attach in batches of "
              f"{args.batch_size}.")
        return 1

    for i in range(0, len(missing), args.batch_size):
        chunk = missing[i:i + args.batch_size]
        t0 = time.time()
        api(args.allm, args.api_key, "POST",
            f"/workspace/{args.workspace}/update-embeddings",
            {"adds": chunk, "deletes": []})
        print(f"    batch {i // args.batch_size + 1}: {len(chunk)} docs "
              f"({time.time() - t0:.0f}s)", flush=True)

    present2 = set(workspace_docpaths(args.allm, args.api_key, args.workspace))
    still = [p for p in tracked if p not in present2]
    print(f"\n  after: {len(still)} still unattached")
    if still:
        print("  These accepted HTTP 200 but did not attach — almost certainly "
              "AnythingLLM content-dedupe (their text matches a document "
              "already in the workspace, e.g. the 9.0 and 9.1 copies of one "
              "page). Not fixable, and not a coverage gap: the content is "
              "present under the other document.")
    return 0 if not still else 0


if __name__ == "__main__":
    sys.exit(main())
