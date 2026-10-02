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


# Allow-list, not deny-list: only these names reach agent-authored conftest.py/checks.
# A denylist leaks anything it did not think to name (e.g. AWS_ACCESS_KEY_ID,
# GOOGLE_APPLICATION_CREDENTIALS, which carry no TOKEN/SECRET/KEY substring).
_ENV_ALLOW = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
    "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS",
})
_DEFAULT_TIMEOUT = 300


def load_allowlist(path: str) -> list[list[str]]:
    """Read `allow = [...]` from a TOML file; split each entry into an argv prefix."""
    with open(path, "rb") as f:
        entries = tomllib.load(f).get("allow", [])
    return [entry.split() for entry in entries]


def _scrubbed_env() -> dict:
    out = {k: v for k, v in os.environ.items() if k.upper() in _ENV_ALLOW}
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
        # -E (ignore PYTHON* env) + -P (don't prepend cwd/script dir to sys.path,
        # so a work-dir sitecustomize.py the agent planted is never imported).
        # NOT -I: -I also implies -s, which disables user-site packages and hides a
        # pip --user pytest, breaking checks. usercustomize lives in user-site, which
        # the agent cannot write, so keeping user-site is safe. Unconditional insert:
        # a trailing script arg "-E"/"-P" must not suppress interpreter isolation.
        out[1:1] = ["-E", "-P"]
        if "pytest" in out and "no:cacheprovider" not in out:
            i = out.index("pytest")
            out[i + 1:i + 1] = ["-p", "no:cacheprovider"]
    # Bare pytest: true interpreter isolation needs `python -m pytest` (the only allow-listed form).
    elif base == "pytest" and "no:cacheprovider" not in out:
        out[1:1] = ["-p", "no:cacheprovider"]
    return out


def _run_one(argv, dest, runner, timeout) -> CheckResult:
    try:
        proc = runner(_isolate(argv), cwd=dest, env=_scrubbed_env(),
                      capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(argv=argv, exit_code=127, output=f"{type(exc).__name__}: {exc}")
    return CheckResult(argv=argv, exit_code=proc.returncode,
                       output=(proc.stdout or "") + (proc.stderr or ""))


def run_checks(dest, checks_list, *, allowlist, runner=subprocess.run, timeout=_DEFAULT_TIMEOUT):
    # Validate everything first so a disallowed entry runs nothing.
    for argv in checks_list:
        if not _allowed(argv, allowlist):
            raise CheckNotAllowed(argv)
    return [_run_one(argv, dest, runner, timeout) for argv in checks_list]
