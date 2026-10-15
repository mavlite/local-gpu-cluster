"""claude_budget.py -- print the Claude escalation budget from the router (GET /escalation/budget). Reads the
router URL and owner key from the opencode config; the key is sent only in the request header and never printed."""
import json
import os
import sys
import urllib.request


def format_report(rep: dict) -> str:
    m, d, left = rep["month"], rep["day"], rep["calls_left"]
    lines = [f"Claude escalation: ${m['spent_usd']:.2f} of ${m['cap_usd']:.2f} this month "
             f"(resets {rep['resets']['month']}), ${d['spent_usd']:.2f} of ${d['cap_usd']:.2f} today "
             f"(resets {rep['resets']['day']})",
             f"ask {left['claude-ask']} · auto {left['claude-auto']} · hard {left['claude-hard']} calls left today"]
    if rep.get("recent"):
        lines.append("recent:")
        lines += [f"  {r['alias']:<11} {r.get('model') or '-':<20} ${float(r.get('cost_usd') or 0):.3f}  "
                  f"in {r.get('input_tokens')} out {r.get('output_tokens')}  {r.get('outcome')}" for r in rep["recent"]]
    return "\n".join(lines)


def main() -> int:
    path = os.path.join(os.path.expanduser("~"), ".config", "opencode", "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            opts = json.load(f)["provider"]["router"]["options"]
        base = opts["baseURL"].rstrip("/")
        base = base[:-3] if base.endswith("/v1") else base
        req = urllib.request.Request(f"{base}/escalation/budget",
                                     headers={"Authorization": f"Bearer {opts['apiKey']}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            print(format_report(json.load(r)))
    except Exception as e:                                   # class name only: never echo headers or values
        print(f"claude-budget: could not read the budget ({type(e).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
