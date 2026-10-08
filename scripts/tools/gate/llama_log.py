"""Per-request token accounting from llama-server logs (spec §5.6: re-prefill count, cache_n).

Handles both servers the gate runs:
  mainline (LXC 151):  "slot print_timing: id  2 | task 45174 | prompt eval time = 2989.10 ms /   818 tokens (...)"
                       "slot      release: id  2 | task 45174 | stop processing: n_tokens = 72817, truncated = 0"
  ik_llama.cpp (CPU):  "slot print_timing: id  0 | task 7 | "  followed by an UNPREFIXED
                       "prompt eval time = ..." block, and no release line (cache_n is None).
"prompt eval ... N tokens" counts only tokens actually processed, so a large N means the cache missed.
"""
import re

_PREFIX = re.compile(r"\bid\s+(\d+) \| task (\d+) \|")
_PROMPT = re.compile(r"prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
_EVAL = re.compile(r"(?<!prompt )\beval time =\s*([\d.]+) ms /\s*(\d+) tokens")
_RELEASE = re.compile(r"stop processing: n_tokens = (\d+)")


def parse(text):
    """List of {"task", "slot", "prompt_n", "prompt_ms", "gen_n", "gen_ms", "cache_n"} in log order."""
    records, by_task, cur, last = [], {}, None, (None, None)
    for line in text.splitlines():
        m = _PREFIX.search(line)
        if m:
            last = (int(m.group(1)), int(m.group(2)))
        m = _PROMPT.search(line)
        if m:
            cur = {"slot": last[0], "task": last[1], "prompt_ms": float(m.group(1)),
                   "prompt_n": int(m.group(2)), "gen_ms": None, "gen_n": None, "cache_n": None}
            records.append(cur)
            if last[1] is not None:
                by_task[last[1]] = cur
            continue
        m = _EVAL.search(line)
        if m and cur is not None and cur["gen_n"] is None:
            cur["gen_ms"], cur["gen_n"] = float(m.group(1)), int(m.group(2))
            continue
        m = _RELEASE.search(line)
        if m and last[1] in by_task:
            r = by_task[last[1]]
            if r["gen_n"] is not None:
                r["cache_n"] = max(0, int(m.group(1)) - r["prompt_n"] - r["gen_n"])
    return records


def summarize(records, large_prefill_min=4096):
    """Totals for one run. large_prefills counts requests that processed >= large_prefill_min
    prompt tokens; the runbook subtracts the expected cold starts (one per fresh session)."""
    cached = [r["cache_n"] for r in records if r["cache_n"] is not None]
    return {
        "requests": len(records),
        "prompt_tokens": sum(r["prompt_n"] for r in records),
        "gen_tokens": sum(r["gen_n"] or 0 for r in records),
        "large_prefills": sum(1 for r in records if r["prompt_n"] >= large_prefill_min),
        "cache_n_total": sum(cached) if cached else None,
    }
