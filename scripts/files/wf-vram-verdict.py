#!/usr/bin/env python3
"""wf-vram-verdict.py FITS UNITS CONCURRENT BEFORE_FILE AFTER_FILE -- 77-wf-vram-check.sh's JSON verdict.

The rocm-smi readings come from LXC 151 and are untrusted data: they are read from files and parsed
with json.loads (kept as {"raw": text} if they are not JSON), never pasted into code (security review
HIGH: the first version interpolated them into a Python heredoc run as host root).
"""
import json
import sys


def reading(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read().strip()
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


def main(argv):
    fits, units, concurrent, before, after = argv[1:6]
    print(json.dumps({"fits": fits == "true", "units_with_rag": units.split(), "concurrent_probe": concurrent,
                      "vram_3slot_no_rag": reading(before), "vram_3slot_with_rag": reading(after)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
