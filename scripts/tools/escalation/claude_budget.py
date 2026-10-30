"""claude_budget.py -- print the Claude escalation budget from the router (GET /escalation/budget). Reads the
router URL and owner key from the opencode config (config.json, opencode.json or opencode.jsonc under
~/.config/opencode, first found wins; apiKey may be "{env:NAME}" or "{file:PATH}"). The key is sent only in the
request header and never printed."""
import json
import os
import re
import sys
import urllib.request

CONFIG_NAMES = ("config.json", "opencode.json", "opencode.jsonc")
# A JSON string (kept as is), a // line comment or a /* */ block comment (dropped).
_JSONC_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
_TRAILING_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])', re.DOTALL)
_REF = re.compile(r"\{(env|file):([^}]+)\}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the Authorization header must go only to the configured router."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(req):
    return urllib.request.build_opener(NoRedirect).open(req, timeout=10)


def _calls_line(left: dict) -> str:
    """None = no call-count limit for that alias (owner decision 2026-10-30); the dollar caps still apply."""
    if all(v is not None for v in left.values()):
        return f"ask {left['claude-ask']} · auto {left['claude-auto']} · hard {left['claude-hard']} calls left today"
    show = {k: "no limit" if v is None else v for k, v in left.items()}
    return (f"calls left today: ask {show['claude-ask']} · auto {show['claude-auto']} · hard {show['claude-hard']} "
            f"(the dollar caps still apply)")


def format_report(rep: dict) -> str:
    m, d, left = rep["month"], rep["day"], rep["calls_left"]
    lines = [f"Claude escalation: ${m['spent_usd']:.2f} of ${m['cap_usd']:.2f} this month "
             f"(resets {rep['resets']['month']}), ${d['spent_usd']:.2f} of ${d['cap_usd']:.2f} today "
             f"(resets {rep['resets']['day']})",
             _calls_line(left)]
    if rep.get("recent"):
        lines.append("recent:")
        lines += [f"  {r['alias']:<11} {r.get('model') or '-':<20} ${float(r.get('cost_usd') or 0):.3f}  "
                  f"in {r.get('input_tokens')} out {r.get('output_tokens')}  {r.get('outcome')}" for r in rep["recent"]]
    return "\n".join(lines)


def strip_jsonc(text: str) -> str:
    """JSONC -> JSON: drop // and /* */ comments and trailing commas, never touching string contents."""
    text = _JSONC_TOKEN.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    return _TRAILING_COMMA.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(0), text)


def load_opencode_config(home: str) -> dict:
    oc = os.path.join(home, ".config", "opencode")
    for name in CONFIG_NAMES:
        path = os.path.join(oc, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            return json.loads(strip_jsonc(text) if name.endswith(".jsonc") else text)
    raise FileNotFoundError("no opencode config")


def resolve_secret(value: str) -> str:
    """opencode's variable forms: "{env:NAME}" -> os.environ[NAME], "{file:PATH}" -> the file's stripped text
    (~ expanded). Raises KeyError/OSError when the source is missing; the caller prints only the class name."""
    m = _REF.fullmatch(value.strip())
    if m is None:
        return value
    kind, ref = m.group(1), m.group(2).strip()
    if kind == "env":
        return os.environ[ref]
    with open(os.path.expanduser(ref), encoding="utf-8") as f:
        return f.read().strip()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        opts = load_opencode_config(os.path.expanduser("~"))["provider"]["router"]["options"]
        base = opts["baseURL"].rstrip("/")
        base = (base[:-3] if base.endswith("/v1") else base).rstrip("/")
        key = resolve_secret(opts["apiKey"])
        if not key:
            raise ValueError("empty key")
        req = urllib.request.Request(f"{base}/escalation/budget", headers={"Authorization": f"Bearer {key}"})
        with _open(req) as r:
            print(format_report(json.load(r)))
    except Exception as e:                                   # class name only: never echo headers or values
        print(f"claude-budget: could not read the budget ({type(e).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
