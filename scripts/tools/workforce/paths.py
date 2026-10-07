"""Which changed paths may leave the sandbox in a patch (workforce spec §5.5).

A path is exported only if it is one of the task's declared files, is not protected, and is a
regular file (no symlinks, no submodules). Protected paths are refused even when declared: they
configure agents, tests or git itself, so an agent that edits them could change how its own work
is run or graded.
"""
import posixpath

# Refused wherever they appear (matched on the file name).
PROTECTED_NAMES = frozenset({
    ".mcp.json", "AGENTS.md", "CLAUDE.md", "opencode.json", "conftest.py", "pytest.ini",
    "setup.cfg", "pyproject.toml", ".gitattributes", ".lfsconfig", ".gitmodules",
    ".pre-commit-config.yaml",
})
# Refused when any directory component matches.
PROTECTED_DIRS = frozenset({".claude", ".opencode", ".git", ".githooks", ".husky"})

_MODE_REASON = {"120000": "symlink", "160000": "submodule"}


def is_protected(path: str) -> bool:
    parts = path.split("/")
    return parts[-1] in PROTECTED_NAMES or any(p in PROTECTED_DIRS for p in parts[:-1])


def classify(changes, declared):
    """changes: [(status, new_mode, path)] from `git diff --raw --no-renames`.
    Returns (allowed_paths, {dropped_path: reason}). Declared paths match exactly (posix)."""
    declared = {posixpath.normpath(p) for p in declared}
    allowed, dropped = [], {}
    for status, mode, path in changes:
        if is_protected(path):
            dropped[path] = "protected"
        elif status != "D" and mode in _MODE_REASON:
            dropped[path] = _MODE_REASON[mode]
        elif path not in declared:
            dropped[path] = "undeclared"
        else:
            allowed.append(path)
    return allowed, dropped
