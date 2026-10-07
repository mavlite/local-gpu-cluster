"""CLI: patch import on the workstation, run preconditions, W1/W3 aggregation."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bundle  # noqa: E402
import cli  # noqa: E402
from test_bundle import SOLUTION, git, repo, taskdef  # noqa: E402,F401  (repo is a fixture)


def built(tmp_path, repo):
    return bundle.build(repo, taskdef(tmp_path, repo), str(tmp_path / "bundles"), str(tmp_path / "refs"))


def test_import_creates_a_branch_on_the_parent_without_touching_the_checkout(tmp_path, repo):
    b = built(tmp_path, repo)
    head_before = git(repo, "rev-parse", "HEAD")
    patch = tmp_path / "t1.patch"
    patch.write_bytes((tmp_path / "refs" / "t1.patch").read_bytes())      # byte-exact, no CRLF translation
    out = cli.import_patch(repo, str(patch), b, "run7")
    assert out == {"branch": "workforce/run7/t1", "paths": ["pkg/calc.py"]}
    assert git(repo, "rev-parse", "HEAD") == head_before and git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "workforce/run7/t1~1") == git(repo, "rev-parse", "HEAD~1")   # on the parent
    assert git(repo, "show", "workforce/run7/t1:pkg/calc.py") == SOLUTION.strip()
    assert "Co-Authored-By" not in git(repo, "log", "-1", "--format=%B", "workforce/run7/t1")


def test_import_refuses_a_patch_touching_undeclared_or_protected_paths(tmp_path, repo):
    b = built(tmp_path, repo)
    evil = tmp_path / "evil.patch"
    evil.write_text("diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n"
                    "+++ b/conftest.py\n@@ -0,0 +1 @@\n+import os\n")
    with pytest.raises(ValueError):
        cli.import_patch(repo, str(evil), b, "run7")
    assert "workforce/run7/t1" not in git(repo, "branch", "--list")


def test_run_requires_the_workforce_keys(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["run", "--arm", "T", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1"], env={"WF_ROUTER_KEY": "k"})


def write_run(d, **s):
    os.makedirs(d)
    base = {"n_tasks": 6, "looped": 0, "accepted": 4, "wall_s": 600.0, "valid": True,
            "accepted_by_task": {}, "accepted_per_hour": 1.0}
    base.update(s)
    with open(os.path.join(d, "run.json"), "w") as f:
        json.dump(base, f)
    return d


def test_w1_aggregates_run_directories_per_configuration(tmp_path, capsys):
    a = write_run(str(tmp_path / "c0a"))
    b = write_run(str(tmp_path / "c0b"), looped=1)
    c = write_run(str(tmp_path / "c2a"))
    stats = cli.w1_stats([a, b])
    assert stats == {"attempts": 12, "looped": 1, "passed": 8, "wall_s": 1200.0}
    cli.main(["w1", "--config", f"C0={a},{b}", "--config", f"C2={c}"])
    out = json.loads(capsys.readouterr().out)
    assert out["table"]["C0"]["loop_rate"] == pytest.approx(1 / 12) and "chosen" in out


def test_w3_reads_runs_and_marks_a_run_invalid_if_either_side_says_so(tmp_path):
    good = write_run(str(tmp_path / "g"), accepted_by_task={"t1": True}, accepted_per_hour=3.0)
    bad = write_run(str(tmp_path / "t"), accepted_by_task={"t1": True}, accepted_per_hour=5.0, valid=False)
    runs = cli.w3_runs([{"arm": "G", "run_dir": good, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0},
                        {"arm": "T", "run_dir": bad, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0}])
    assert [r["valid"] for r in runs] == [True, False]
    assert runs[0]["accepted"] == {"t1": True} and runs[1]["accepted_per_hour"] == 5.0


def test_patch_paths_reads_diff_headers():
    text = "diff --git a/x/y.py b/x/y.py\n--- a/x/y.py\n+++ b/x/y.py\ndiff --git a/z b/z\n"
    assert cli.patch_paths(text) == ["x/y.py", "z"]


def test_import_works_even_when_the_repo_converts_line_endings_on_checkout(tmp_path, repo):
    b = built(tmp_path, repo)
    git(repo, "config", "core.autocrlf", "true")
    patch = tmp_path / "t1.patch"
    patch.write_bytes((tmp_path / "refs" / "t1.patch").read_bytes())
    assert cli.import_patch(repo, str(patch), b, "run8")["branch"] == "workforce/run8/t1"
    assert git(repo, "show", "workforce/run8/t1:pkg/calc.py") == SOLUTION.strip()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_an_agent_user_run_refuses_bundles_readable_by_others(tmp_path):
    b = tmp_path / "bundles"
    b.mkdir()
    os.chmod(b, 0o755)
    with pytest.raises(SystemExit):
        cli.assert_private(str(b))
    os.chmod(b, 0o700)
    cli.assert_private(str(b))


def test_patch_paths_also_reads_body_paths_that_git_apply_honours():
    # Security review LOW: the header says one path, the body another; git apply follows the body.
    text = ("diff --git a/pkg/calc.py b/pkg/calc.py\n--- a/pkg/calc.py\n+++ b/conftest.py\n"
            "diff --git a/x b/y\nrename from x\nrename to y\ncopy from p\ncopy to q\n")
    assert cli.patch_paths(text) == ["conftest.py", "p", "pkg/calc.py", "q", "x", "y"]
