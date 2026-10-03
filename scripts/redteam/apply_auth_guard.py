"""Idempotently insert `import os` + an env-gated register guard into the
vendored orchestrator auth.py. Marker-guarded so re-applying is a no-op and
an upstream pull can be re-hardened by re-running (spec §4.2)."""
import re
import sys

MARKER = "local-gpu-cluster R1 hardening"

_GUARD = (
    "    # " + MARKER + " — self-service registration disabled by default\n"
    "    import os\n"
    "    from fastapi import HTTPException, status\n"
    "    if os.environ.get(\"REDTEAM_ALLOW_REGISTRATION\") != \"1\":\n"
    "        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,\n"
    "                            detail=\"registration disabled\")\n"
)


def apply_guard(src: str) -> str:
    if MARKER in src:
        return src  # already hardened
    lines = src.splitlines(keepends=True)
    # find the `def register(` line (single-line signature ending in ':')
    for i, line in enumerate(lines):
        if re.match(r"\s*def register\(", line) and line.rstrip().endswith(":"):
            insert_at = i + 1
            break
    else:
        raise ValueError("def register( not found — auth.py structure changed; refusing to guess")
    lines.insert(insert_at, _GUARD)
    out = "".join(lines)
    return out


def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: apply_auth_guard.py <path> [--check]"); return 2
    path = argv[0]
    check = "--check" in argv[1:]
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if MARKER in src:
        print("applied"); return 0
    if check:
        print("needs-apply"); return 1
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(apply_guard(src))
    print("applied"); return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
