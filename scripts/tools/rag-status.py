#!/usr/bin/env python3
"""rag-status.py — one screen showing the state of every RAG source.

The cluster-monitor dashboard (http://192.168.6.175:8888/) covers the
infrastructure — GPUs, LXC memory, upstreams, timers — but carries a single
RAG check: "rag_refresh: last run 4.7h ago". That says nothing about whether a
source is fully ingested, mid-backfill, halted awaiting review, or quietly
holding documents that were uploaded but never attached.

This fills that gap. Run it on the Proxmox host:

    python3 scripts/tools/rag-status.py
    python3 scripts/tools/rag-status.py --workspace vcf-reference
    python3 scripts/tools/rag-status.py --watch 60

Columns:
  TRACKED       URLs in the source's documents.json
  FETCHED       of those, with a real content hash (actually retrieved)
  PENDING       adopted by migrate_backfill but never fetched -- these are
                stale until the source runs
  LAST OK       last successful refresh
  DUE           whether refresh_interval has elapsed

A source showing PENDING > 0 and no recent run is the failure this exists to
surface: it looks healthy in every other view.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

STATE_DIR = "/tank/rag-state"
SOURCES = "/root/local-gpu-cluster/scripts/rag/sources.yaml"
PROPOSALS = os.path.join(STATE_DIR, "_proposals")

C = {"ok": "\033[32m", "warn": "\033[33m", "bad": "\033[31m",
     "dim": "\033[90m", "b": "\033[1m", "off": "\033[0m"}


def paint(s, k):
    return f"{C[k]}{s}{C['off']}" if sys.stdout.isatty() else s


def parse_interval(s: str) -> float:
    m = re.match(r"^(\d+)([dhm])$", str(s or "").strip())
    if not m:
        return 0.0
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 86400, "h": 3600, "m": 60}[unit]


def load_sources():
    import yaml
    with open(SOURCES, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cfg.get("sources", []), cfg.get("defaults", {})


def source_state(sid: str):
    """(tracked, fetched, pending, last_success)"""
    docs = os.path.join(STATE_DIR, sid, "documents.json")
    man = os.path.join(STATE_DIR, sid, "manifest.json")
    tracked = fetched = 0
    try:
        with open(docs, encoding="utf-8") as fh:
            d = json.load(fh)
        tracked = len(d)
        fetched = sum(1 for v in d.values()
                      if str(v.get("hash", "")).startswith("sha256:"))
    except Exception:
        pass
    last = None
    try:
        with open(man, encoding="utf-8") as fh:
            m = json.load(fh)
        # last_success sits at the TOP level, not under stats. Reading the
        # wrong one reports every source as "never", which it did once.
        last = m.get("last_success")
    except Exception:
        pass
    return tracked, fetched, tracked - fetched, last


def age(iso: str | None):
    if not iso:
        return None
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds()


def human(sec):
    if sec is None:
        return "never"
    if sec < 3600:
        return f"{sec/60:.0f}m"
    if sec < 86400:
        return f"{sec/3600:.1f}h"
    return f"{sec/86400:.1f}d"


def running():
    try:
        out = subprocess.run(["ps", "-eo", "etime,cmd"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    hits = []
    for line in out.splitlines():
        m = re.search(r"refresh\.py --source (\S+)", line)
        if m and "grep" not in line:
            hits.append((m.group(1), line.split()[0]))
    return hits


def render(only_ws=None):
    srcs, defaults = load_sources()
    active = {s: e for s, e in running()}
    props = {}
    for f in glob.glob(os.path.join(PROPOSALS, "*.json")):
        props.setdefault(os.path.basename(f).rsplit("-", 2)[0], []).append(f)

    print(paint(f"\n  RAG sources — {datetime.now().strftime('%Y-%m-%d %H:%M')}", "b"))
    hdr = ("  %-20s %-16s %-6s %8s %8s %8s  %-8s %s"
           % ("SOURCE", "HANDLER", "EVERY", "TRACKED", "FETCHED", "PENDING",
              "LAST OK", "NOTE"))
    print(paint(hdr, "dim"))

    for s in srcs:
        sid, ws = s["id"], s.get("workspace", "?")
        if only_ws and ws != only_ws:
            continue
        tracked, fetched, pending, last = source_state(sid)
        a = age(last)
        interval = parse_interval(s.get("refresh_interval"))

        notes = []
        if not s.get("enabled", True):
            notes.append(paint("disabled", "dim"))
        if sid in active:
            notes.append(paint(f"RUNNING {active[sid]}", "ok"))
        if sid in props:
            notes.append(paint(f"{len(props[sid])} proposal(s) awaiting review", "bad"))
        if pending:
            notes.append(paint(f"{pending} never fetched", "warn"))
        if a is not None and interval and a > interval and s.get("enabled", True):
            notes.append(paint("DUE", "warn"))

        pend_s = paint(f"{pending:8d}", "warn" if pending else "dim")
        print("  %-20s %-16s %-6s %8d %8d %s  %-8s %s"
              % (sid, s["handler"], s.get("refresh_interval", "-"),
                 tracked, fetched, pend_s, human(a), " ".join(notes)))

    stray = [f for k, v in props.items() for f in v
             if k not in {s["id"] for s in srcs}]
    if stray:
        print(paint(f"\n  {len(stray)} proposal(s) for unknown sources in "
                    f"{PROPOSALS}", "warn"))
    print(paint("\n  infra dashboard: http://192.168.6.175:8888/"
                "   (this view covers corpora only)\n", "dim"))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", help="limit to one workspace")
    ap.add_argument("--watch", type=int, metavar="SEC",
                    help="redraw every SEC seconds")
    args = ap.parse_args()
    if not args.watch:
        render(args.workspace)
        return 0
    try:
        while True:
            os.system("clear")
            render(args.workspace)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
