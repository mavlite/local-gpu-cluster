"""Result gate: inspect an agent's diff before it is shown, rejecting dangerous content."""
import dataclasses
import re

from scripts.delegate.gitstore import run_git


@dataclasses.dataclass
class GateReport:
    rejected: bool
    reasons: list
    flagged: list


_REJECT_PATH = re.compile(r"(^|/)(\.git|\.github|\.husky)(/|$)|(^|/)conftest\.py$")
_FLAG_PATH = re.compile(
    r"(^|/)(package\.json|requirements[^/]*\.txt|[^/]*\.lock|[^/]*lock\.json)$|\.ya?ml$")
_PATHSPEC = ["--", ".", ":(exclude).opencode", ":(exclude).opencode/**", ":(exclude)opencode.json"]
_EXEC = "100755"


def _raw_entries(raw: str):
    """Parse `diff --raw -z` output into (src_mode, dst_mode, [paths])."""
    toks = raw.split("\0")
    i = 0
    while i < len(toks) and toks[i].startswith(":"):
        meta = toks[i][1:].split()
        n = 2 if meta[4][0] in "RC" else 1
        yield meta[0], meta[1], toks[i + 1:i + 1 + n]
        i += 1 + n


def _check_entry(src, dst, paths, reasons, flagged):
    path = paths[-1]
    if dst == "120000":
        reasons.append(f"symlink: {path}")
    if "160000" in (src, dst):
        reasons.append(f"gitlink: {path}")
    if dst == _EXEC and src != _EXEC:
        reasons.append(f"mode-change +x: {path}")
    for p in paths:
        if _REJECT_PATH.search(p):
            reasons.append(f"protected path: {p}")
    if _FLAG_PATH.search(path):
        flagged.append(path)


def inspect_diff(git_dir: str, work_dir: str, base: str) -> GateReport:
    rng = f"{base}..HEAD"
    raw = run_git(git_dir, work_dir, "diff", "--raw", "-M", "-z", "--no-ext-diff",
                  "--no-textconv", rng, *_PATHSPEC)
    reasons, flagged = [], []
    for src, dst, paths in _raw_entries(raw):
        _check_entry(src, dst, paths, reasons, flagged)
    numstat = run_git(git_dir, work_dir, "diff", "--numstat", "--no-renames", "-z",
                      "--no-ext-diff", "--no-textconv", rng, *_PATHSPEC)
    for rec in filter(None, numstat.split("\0")):
        if rec.startswith("-\t-\t"):
            reasons.append(f"binary: {rec.split(chr(9), 2)[-1]}")
    return GateReport(rejected=bool(reasons), reasons=reasons, flagged=flagged)
