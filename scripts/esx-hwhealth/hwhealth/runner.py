"""Command runner: one place that shells out, so everything else is testable."""
import re
import subprocess
from typing import Callable

Runner = Callable[[list[str], float], str]

_ERROR_PATTERN = re.compile(r"Get failed|Not supported|Not implemented|mal-formed")


class CommandError(Exception):
    def __init__(self, argv, message: str):
        super().__init__(f"{' '.join(argv)}: {message}")
        self.argv = list(argv)
        self.message = message


def subprocess_runner(argv: list[str], timeout: float) -> str:
    """Run argv, return stdout. Raise CommandError on failure, timeout or error text."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CommandError(argv, "timeout") from exc
    except OSError as exc:
        raise CommandError(argv, str(exc)) from exc
    out, err = proc.stdout or "", proc.stderr or ""
    if proc.returncode != 0:
        raise CommandError(argv, (err.strip() or out.strip() or f"exit {proc.returncode}"))
    if _ERROR_PATTERN.search(out) or _ERROR_PATTERN.search(err):
        raise CommandError(argv, (err.strip() or out.strip()))
    return out
