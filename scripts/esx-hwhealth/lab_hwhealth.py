#!/bin/python
"""healthd plugin entrypoint for lab.hwHealth.

Default mode (how healthd launches it, with `-u <url>`): read the sampler's cache file, evaluate
it and send the report. It never execs a process: inside the healthd sandbox every exec fails.
`--stdout` instead does a live collect() and prints the report (manual runs as root only).
`vmware.*` is imported lazily, and only when a report is actually sent.
"""
import argparse
import json
import os
import sys
import time

# On the host the package sits outside healthd's scanned plugins/bin; on the workstation the
# directory does not exist and the normal import path is used.
INSTALL_DIR = "/usr/lib/vmware/healthd/lab-hwhealth"
if os.path.isdir(INSTALL_DIR):
    # Shipped files carry mtime 0: a .pyc from any root run would look current after a
    # same-size update, so never write one on the host.
    sys.dont_write_bytecode = True
    if INSTALL_DIR not in sys.path:
        sys.path.insert(0, INSTALL_DIR)

from hwhealth import CACHE  # noqa: E402  (re-exported: tests and the sampler share one path)
from hwhealth.collect import collect  # noqa: E402
from hwhealth.evaluate import ERROR, WARNING, evaluate  # noqa: E402
from hwhealth.runner import subprocess_runner  # noqa: E402

MAX_AGE_S = 180
PLUGIN_NAME = "lab_hwhealth"
REPORT_NAME = "lab.hwHealth"
TAGS = ["hardware"]
# HealthPlugin.Magnitude values, kept local so --stdout and the cache path never import vmware.*
MAGNITUDE_NONE, MAGNITUDE_LARGE = 0, 3


def magnitude_for(status: int) -> int:
    """Same rule as mobility_network.py: LARGE for WARNING/ERROR, NONE otherwise."""
    return MAGNITUDE_LARGE if status in (WARNING, ERROR) else MAGNITUDE_NONE


def _cache_problem(reason: str) -> dict:
    return {"schema": 1, "errors": [{"reading": "cache", "reason": reason}]}


def load_cache(path: str, clock=time.time, getmtime=os.path.getmtime):
    """Return (unstructured, floor_status). floor_status is WARNING for a bad cache, else None."""
    try:
        age = clock() - getmtime(path)
    except FileNotFoundError:
        return _cache_problem("cache missing"), WARNING
    except OSError:
        return _cache_problem("cache unreadable"), WARNING
    if age > MAX_AGE_S:
        return _cache_problem("cache stale"), WARNING
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return _cache_problem("cache missing"), WARNING
    except (OSError, ValueError):
        return _cache_problem("cache unreadable"), WARNING
    if not isinstance(data, dict):
        return _cache_problem("cache unreadable"), WARNING
    return data, None


def _default_plugin_cls():
    from vmware.healthd.healthPlugin import HealthPlugin  # exists only on the ESX host

    class LabHwHealth(HealthPlugin):
        def __init__(self, args=None):
            super().__init__(PLUGIN_NAME, args)

        def generateHealthReport(self):  # main() sends the report; nothing to generate here
            return True

    return LabHwHealth


def main(argv, run=subprocess_runner, plugin_cls=None, clock=time.time,
         getmtime=os.path.getmtime) -> int:
    parser = argparse.ArgumentParser(prog=PLUGIN_NAME)
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--from-cache", metavar="PATH", default=CACHE)
    args, healthd_args = parser.parse_known_args(argv)

    if args.stdout:
        u = collect(run)
        status, reasons = evaluate(u)
        print(json.dumps({"structured": {"status": status, "magnitude": magnitude_for(status),
                                         "tags": TAGS, "reasons": reasons},
                          "unstructured": u}))
        return 0

    u, floor = load_cache(args.from_cache, clock, getmtime)
    status, _ = evaluate(u)
    if floor is not None:
        status = max(status, floor)
    try:
        plugin = (plugin_cls or _default_plugin_cls())(args=healthd_args)
        sent = plugin.sendReport(REPORT_NAME, u, structuredData={
            "status": status, "magnitude": magnitude_for(status)})
    except Exception as exc:  # healthd only sees the exit code; keep the cause in its log
        print(f"{PLUGIN_NAME}: sendReport failed: {exc!r}", file=sys.stderr)
        return 1
    return 0 if sent is True else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
