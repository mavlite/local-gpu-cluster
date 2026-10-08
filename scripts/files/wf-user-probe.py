#!/usr/bin/env python3
"""wf-user-probe.py run --out FILE [--interval S] [--count N | --until EPOCH] [--timeout S]

The user-lane latency probe of a workforce window (docs/superpowers/specs/2026-10-07-distributed-
workforce-design.md §9, clause 3; Plan D). Runs inside LXC 153 (the router) as root and sends one
small, fixed chat request every --interval seconds with the OWNER key, read from /etc/router.env and
never from argv, so it takes the user's reserved lane exactly as the user would.

Each request appends one JSON line: ts, ok, status, latency_s, and the chat server's own prompt_ms /
predicted_ms (so its share can be subtracted from the lead's GPU time). A refused, timed-out or
unreachable request is recorded with ok=false -- never dropped: lane starvation must show up in the
p50, not vanish from it. Stdlib only.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("WF_PROBE_URL", "http://127.0.0.1:8000")
ENV_FILE = os.environ.get("WF_PROBE_ENV_FILE", "/etc/router.env")
ALIAS = "qwen3.8-nothink"
# Fixed, so every probe in every run asks the same thing: comparable latency, a warm prompt cache
# like a user's follow-up turn.
MESSAGES = [{"role": "user", "content": "Reply with the single word: ready."}]


def owner_key(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("ROUTER_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    raise SystemExit(f"ROUTER_API_KEY not found in {path}")


def probe_once(key, timeout):
    body = json.dumps({"model": ALIAS, "messages": MESSAGES, "max_tokens": 16, "stream": False}).encode()
    req = urllib.request.Request(f"{URL}/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    rec = {"ts": time.time(), "ok": False, "status": None, "latency_s": None,
           "prompt_ms": None, "predicted_ms": None}
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
            rec["status"] = r.status
            rec["ok"] = r.status == 200
            timings = data.get("timings") or {}
            rec["prompt_ms"], rec["predicted_ms"] = timings.get("prompt_ms"), timings.get("predicted_ms")
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        rec["error"] = type(e).__name__
    rec["latency_s"] = round(time.monotonic() - t0, 3)
    return rec


def main(argv):
    ap = argparse.ArgumentParser(prog="wf-user-probe")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--out", required=True)
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--count", type=int)
    p.add_argument("--until", type=float, help="stop at this epoch second")
    p.add_argument("--timeout", type=float, default=120.0)
    a = ap.parse_args(argv)
    key = owner_key(ENV_FILE)
    n = 0
    while (a.count is None or n < a.count) and (a.until is None or time.time() < a.until):
        rec = probe_once(key, a.timeout)
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        n += 1
        if a.count is not None and n >= a.count:
            break
        time.sleep(a.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
