"""Server-side checks: argv-only, allow-listed, run in a scrubbed environment."""
import dataclasses
import os
import subprocess
import tomllib


class CheckNotAllowed(Exception):
    """A requested check's argv prefix is not in the allow-list."""


@dataclasses.dataclass
class CheckResult:
    argv: list
    exit_code: int
    output: str


_SCRUB_PREFIXES = ("LOCAL_DELEGATE_",)
_SCRUB_EXACT = ("GH_TOKEN", "GITHUB_TOKEN")
_SCRUB_CONTAINS = ("SSH",)


def load_allowlist(path: str) -> list[list[str]]:
    """Read `allow = [...]` from a TOML file; split each entry into an argv prefix."""
    with open(path, "rb") as f:
        entries = tomllib.load(f).get("allow", [])
    return [entry.split() for entry in entries]


def _scrubbed_env() -> dict:
    out = {}
    for k, v in os.environ.items():
        if k.startswith(_SCRUB_PREFIXES) or k in _SCRUB_EXACT:
            continue
        if any(s in k.upper() for s in _SCRUB_CONTAINS):
            continue
        out[k] = v
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    return out


def _allowed(argv, allowlist) -> bool:
    return bool(argv) and any(argv[:len(p)] == p for p in allowlist)


def _isolate(argv) -> list:
    """Add interpreter isolation so planted conftest/sitecustomize cannot hijack the run."""
    base = os.path.basename(argv[0]).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    out = list(argv)
    if base.startswith("python"):
        if "-I" not in out:
            out.insert(1, "-I")
        if "pytest" in out and "no:cacheprovider" not in out:
            i = out.index("pytest")
            out[i + 1:i + 1] = ["-p", "no:cacheprovider"]
    elif base == "pytest" and "no:cacheprovider" not in out:
        out[1:1] = ["-p", "no:cacheprovider"]
    return out


def run_checks(dest, checks_list, *, allowlist, runner=subprocess.run):
    # Validate everything first so a disallowed entry runs nothing.
    for argv in checks_list:
        if not _allowed(argv, allowlist):
            raise CheckNotAllowed(argv)
    results = []
    for argv in checks_list:
        proc = runner(_isolate(argv), cwd=dest, env=_scrubbed_env(),
                      capture_output=True, text=True)
        results.append(CheckResult(argv=argv, exit_code=proc.returncode,
                                   output=(proc.stdout or "") + (proc.stderr or "")))
    return results
