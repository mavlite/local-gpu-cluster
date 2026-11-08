#!/bin/python
"""Boot-time sampler: runs outside the healthd sandbox and writes the cache the plugin reads.

Each cycle runs collect() and replaces the cache atomically. The loop retries forever: early in
boot hostd answers 503 to esxcli, so the first cycles can carry errors and later ones recover.
"""
import argparse
import json
import os
import sys
import tempfile
import time

# Same guard as the plugin: the installed package lives next to this file on the host.
INSTALL_DIR = "/usr/lib/vmware/healthd/lab-hwhealth"
if os.path.isdir(INSTALL_DIR):
    # Shipped files carry mtime 0: a .pyc from any root run would look current after a
    # same-size update, so never write one on the host.
    sys.dont_write_bytecode = True
    if INSTALL_DIR not in sys.path:
        sys.path.insert(0, INSTALL_DIR)

from hwhealth import CACHE  # noqa: E402
from hwhealth.collect import collect  # noqa: E402
from hwhealth.runner import subprocess_runner  # noqa: E402

INTERVAL_S = 60


def run_once(run, path: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    report = collect(run)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cache-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv, run=subprocess_runner, sleep=time.sleep, max_cycles=None) -> int:
    parser = argparse.ArgumentParser(prog="lab_hwhealth_sampler")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--cache", default=CACHE)
    args = parser.parse_args(argv)
    if args.once:
        run_once(run, args.cache)
        return 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        try:
            run_once(run, args.cache)
        except Exception as exc:  # one bad cycle must not stop the sampler
            print(f"lab_hwhealth_sampler: cycle failed: {exc!r}", file=sys.stderr)
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            sleep(INTERVAL_S)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
