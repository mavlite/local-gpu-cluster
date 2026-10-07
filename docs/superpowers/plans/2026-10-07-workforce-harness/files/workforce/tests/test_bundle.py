"""Task bundles built from repository history (spec §5.1, §8)."""
import json
import os
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bundle  # noqa: E402
import grade  # noqa: E402

STUB = "def add(a, b):\n    return 0\n"
SOLUTION = "def add(a, b):\n    return a + b\n"
TEST = ("import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
HIDDEN = ("import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
          "from calc import add\n\ndef test_add_negative():\n    assert add(-1, -1) == -2\n")


def git(repo, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false",
                           *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


def write(repo, path, text):
    p = os.path.join(repo, path)
    os.makedirs(os.path.dirname(p) or repo, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


@pytest.fixture
def repo(tmp_path):
    r = str(tmp_path / "repo")
    os.makedirs(r)
    git(r, "init", "-q")
    write(r, "pkg/calc.py", STUB)
    write(r, ".superpowers/ledger.md", "plan ledger\n")
    write(r, "docs/superpowers/plans/plan.md", "the plan\n")
    write(r, "docs/keep.md", "kept\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "parent")
    write(r, "pkg/calc.py", SOLUTION)
    write(r, "pkg/tests/test_calc.py", TEST)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "fix add")
    return r


def taskdef(tmp_path, repo, **over):
    d = str(tmp_path / "defs" / "t1")
    os.makedirs(os.path.join(d, "hidden"))
    td = {"id": "t1", "commit": git(repo, "rev-parse", "HEAD"), "files": ["pkg/calc.py"],
          "tests": ["pkg/tests/test_calc.py"], "hidden": {"test_calc_hidden.py": "pkg/tests/test_calc_hidden.py"},
          "needs_conftest": False, "timeout_s": 120}
    td.update(over)
    with open(os.path.join(d, "taskdef.json"), "w") as f:
        json.dump(td, f)
    with open(os.path.join(d, "request.md"), "w") as f:
        f.write("add() returns 0; make it add.\n")
    with open(os.path.join(d, "hidden", "test_calc_hidden.py"), "w") as f:
        f.write(HIDDEN)
    return d


def test_build_strips_planning_material_and_overlays_the_visible_tests(tmp_path, repo):
    out, ref = str(tmp_path / "bundles"), str(tmp_path / "refs")
    b = bundle.build(repo, taskdef(tmp_path, repo), out, ref)
    with tarfile.open(os.path.join(b, "snapshot.tar")) as t:
        names = set(t.getnames())
        calc = t.extractfile("pkg/calc.py").read().decode()
    assert "pkg/tests/test_calc.py" in names and "docs/keep.md" in names
    assert not any(n.startswith((".superpowers", "docs/superpowers")) for n in names)
    assert calc == STUB                                             # the parent's version, not the answer
    task = grade.load_task(b)
    assert task["parent"] == git(repo, "rev-parse", "HEAD~1")
    assert os.path.isfile(os.path.join(ref, "t1.patch")) and not os.path.exists(os.path.join(b, "t1.patch"))


def test_validate_requires_fail_on_snapshot_and_pass_with_the_reference(tmp_path, repo):
    out, ref = str(tmp_path / "bundles"), str(tmp_path / "refs")
    b = bundle.build(repo, taskdef(tmp_path, repo), out, ref)
    assert bundle.validate(b, os.path.join(ref, "t1.patch"), str(tmp_path / "v"), grade.LocalRunner()) == []


def test_validate_reports_a_vacuous_task(tmp_path, repo):
    out, ref = str(tmp_path / "bundles"), str(tmp_path / "refs")
    d = taskdef(tmp_path, repo, hidden={})
    with open(os.path.join(d, "request.md"), "w") as f:
        f.write("x\n")
    b = bundle.build(repo, d, out, ref)
    with open(os.path.join(ref, "t1.patch"), "w") as f:      # a reference that changes nothing
        f.write("")
    problems = bundle.validate(b, os.path.join(ref, "t1.patch"), str(tmp_path / "v"), grade.LocalRunner())
    assert any("reference" in p for p in problems)


def test_an_answer_blob_anywhere_in_the_snapshot_refuses_the_build(tmp_path):
    r = str(tmp_path / "leaky")
    os.makedirs(r)
    git(r, "init", "-q")
    write(r, "pkg/calc.py", STUB)
    write(r, "attic/answer_backup.py", SOLUTION)        # byte-identical copy of the answer in the parent
    git(r, "add", "-A")
    git(r, "commit", "-qm", "parent")
    write(r, "pkg/calc.py", SOLUTION)
    write(r, "pkg/tests/test_calc.py", TEST)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "fix add")
    with pytest.raises(bundle.LeakError) as e:
        bundle.build(r, taskdef(tmp_path, r), str(tmp_path / "bundles"), str(tmp_path / "refs"))
    assert "attic/answer_backup.py" in str(e.value)


def test_manifest_changes_when_any_bundle_byte_changes(tmp_path, repo):
    out, ref = str(tmp_path / "bundles"), str(tmp_path / "refs")
    b = bundle.build(repo, taskdef(tmp_path, repo), out, ref)
    h1 = bundle.manifest(out)
    with open(os.path.join(b, "request.md"), "a") as f:
        f.write(" ")
    assert bundle.manifest(out) != h1


def test_snapshot_tar_is_deterministic(tmp_path, repo):
    d = taskdef(tmp_path, repo)
    a = bundle.build(repo, d, str(tmp_path / "o1"), str(tmp_path / "r1"))
    b = bundle.build(repo, d, str(tmp_path / "o2"), str(tmp_path / "r2"))
    with open(os.path.join(a, "snapshot.tar"), "rb") as f1, open(os.path.join(b, "snapshot.tar"), "rb") as f2:
        assert f1.read() == f2.read()
