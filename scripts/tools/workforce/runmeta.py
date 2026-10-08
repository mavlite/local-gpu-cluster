"""Per-run metadata for W3 (spec §9; Plan D). Runs on the workstation, after a run's files are harvested.

- validity: the host stamps taken just before and just after a run (`wf-window.sh stamp`) must agree:
  same boot, same router and chat-server invocations, the open 3-slot window at both ends. Any
  difference is one of spec §9's mechanical infrastructure failures, and the run is invalid.
- probe_p50: the user-lane probe's median latency over the run. A failed probe counts as `fail_s`
  (the probe's own timeout), so a starved lane raises the median instead of leaving the sample.
- gpu_ms: chat-server prompt + generation time over the run (llama-server's own timings), minus the
  probe's share. The lead and arm G's implementer share an alias, so this is the GPU's workforce time,
  reported per accepted task -- spec §9 reports it; it does not decide.
"""
import calendar
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gate"))
import llama_log  # noqa: E402  (scripts/tools/gate: parses mainline and ik llama-server timings)


def iso_epoch(iso):
    return float(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def validity(before, after):
    """Reasons the run is invalid; empty means valid."""
    reasons = []
    for s in (before, after):
        if not s.get("router_invocation") or not s.get("chat_invocation") or not s.get("boot_id"):
            reasons.append(f"stamp {s.get('label')} is incomplete")
    if before.get("boot_id") != after.get("boot_id"):
        reasons.append("host rebooted during the run")
    if before.get("router_invocation") != after.get("router_invocation"):
        reasons.append("router restarted during the run")
    if before.get("chat_invocation") != after.get("chat_invocation"):
        reasons.append("chat server restarted during the run")
    if before.get("capacity") != 3 or after.get("capacity") != 3:
        reasons.append("chat was not in the 3-slot layout at both ends")
    if not (before.get("window_open") and after.get("window_open")):
        reasons.append("the window was not open at both ends")
    return reasons


def _in_window(records, since, until):
    return [r for r in records if since <= r["ts"] <= until]


def probe_p50(records, since, until, fail_s):
    lat = [r["latency_s"] if r.get("ok") else fail_s for r in _in_window(records, since, until)]
    return statistics.median(lat) if lat else None


def gpu_ms(journal_text, probe_records):
    total = sum((r.get("prompt_ms") or 0) + (r.get("gen_ms") or 0) for r in llama_log.parse(journal_text))
    probe = sum((r.get("prompt_ms") or 0) + (r.get("predicted_ms") or 0) for r in probe_records)
    return total - probe


def _stamp(stamps, label):
    found = [s for s in stamps if s.get("label") == label]
    if len(found) != 1:
        raise SystemExit(f"need exactly one stamp labelled {label!r}, found {len(found)}")
    return found[0]


def run_meta(run_dir, stamps_file, label, probe_file, journal_file, fail_s):
    """Judge one run and write <run_dir>/meta.json. `journal_file`: the chat server's journal for the
    run's window (journalctl -u llamacpp-chat --since/--until, captured on the host)."""
    stamps = load_jsonl(stamps_file)
    before, after = _stamp(stamps, f"before-{label}"), _stamp(stamps, f"after-{label}")
    since, until = iso_epoch(before["ts"]), iso_epoch(after["ts"])
    probes = load_jsonl(probe_file)
    reasons = validity(before, after)
    p50 = probe_p50(probes, since, until, fail_s)
    if p50 is None:
        reasons.append("no user-lane probe samples during the run (the probe died)")
    with open(journal_file, encoding="utf-8", errors="replace") as f:
        ms = gpu_ms(f.read(), _in_window(probes, since, until))
    with open(os.path.join(run_dir, "run.json"), encoding="utf-8") as f:
        accepted = json.load(f)["accepted"]
    meta = {"label": label, "valid": not reasons, "reasons": reasons, "since": before["ts"],
            "until": after["ts"], "probe_p50": p50, "gpu_ms": ms,
            "gpu_ms_per_accepted": ms / accepted if accepted else None}
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return meta


def w3_schedule(baseline_probe_file, runs, fail_s):
    """runs: [(arm, run_dir)] in schedule order -> the schedule `cli.py w3` reads."""
    base = load_jsonl(baseline_probe_file)
    baseline = probe_p50(base, float("-inf"), float("inf"), fail_s)
    if baseline is None:
        raise SystemExit(f"{baseline_probe_file}: no idle-baseline probe samples")
    out = []
    for arm, d in runs:
        path = os.path.join(d, "meta.json")
        if not os.path.isfile(path):
            raise SystemExit(f"{d}: no meta.json -- run `cli.py run-meta` for it first")
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        out.append({"arm": arm, "run_dir": d, "valid": bool(m["valid"]), "probe_p50": m["probe_p50"],
                    "baseline_p50": baseline})
    return out
