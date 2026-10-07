"""Which changed paths may leave the sandbox in a patch (spec §5.5)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import paths  # noqa: E402

DECLARED = ["scripts/files/access_keys.py", "scripts/53-lxc-router.sh"]


def test_declared_files_are_allowed():
    allowed, dropped = paths.classify([("M", "100644", "scripts/files/access_keys.py")], DECLARED)
    assert allowed == ["scripts/files/access_keys.py"] and dropped == {}


def test_undeclared_files_are_dropped_even_if_harmless():
    allowed, dropped = paths.classify([("A", "100644", "notes.txt"),
                                       ("M", "100644", "scripts/52-lxc-v620.sh")], DECLARED)
    assert allowed == [] and dropped == {"notes.txt": "undeclared", "scripts/52-lxc-v620.sh": "undeclared"}


def test_protected_paths_are_rejected_even_when_declared():
    protected = [".mcp.json", ".claude/settings.json", "AGENTS.md", "CLAUDE.md", "opencode.json",
                 ".opencode/agent/x.md", "conftest.py", "vcf-spec-tools/tests/conftest.py", "pytest.ini",
                 "setup.cfg", "pyproject.toml", ".gitattributes", ".lfsconfig", ".gitmodules",
                 ".githooks/pre-commit", ".husky/pre-push", ".pre-commit-config.yaml", "sub/.git/config"]
    allowed, dropped = paths.classify([("M", "100644", p) for p in protected], protected)
    assert allowed == [] and set(dropped.values()) == {"protected"} and len(dropped) == len(protected)


def test_declared_deployed_script_is_allowed_but_only_by_exact_path():
    allowed, dropped = paths.classify([("M", "100755", "scripts/53-lxc-router.sh"),
                                       ("M", "100755", "./scripts/53-lxc-router.sh")], DECLARED)
    assert allowed == ["scripts/53-lxc-router.sh"]
    assert dropped == {"./scripts/53-lxc-router.sh": "undeclared"}


def test_symlinks_and_submodules_are_rejected():
    allowed, dropped = paths.classify([("A", "120000", "scripts/files/access_keys.py"),
                                       ("A", "160000", "scripts/53-lxc-router.sh")], DECLARED)
    assert allowed == [] and dropped == {"scripts/files/access_keys.py": "symlink",
                                         "scripts/53-lxc-router.sh": "submodule"}


def test_deleting_a_declared_file_is_allowed():
    allowed, _ = paths.classify([("D", "000000", "scripts/files/access_keys.py")], DECLARED)
    assert allowed == ["scripts/files/access_keys.py"]
