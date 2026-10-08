"""gate_tasks.py: turn a Phase-1 gate worker task (seed/, reference/, hidden/, TASK.md) plus authored
visible tests into a commit-based task definition, so `bundle-build` makes a W1 bundle from it
(workforce spec §7: W1 uses the gate's T1, T2 and T4)."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bundle  # noqa: E402
import gate_tasks  # noqa: E402
import grade  # noqa: E402

TASK_MD = "# T9 - doubler\n\nImplement `double(x)` in `dbl.py`: return `2 * x`.\n"


@pytest.fixture
def gate_task(tmp_path):
    d = tmp_path / "T9-doubler"
    for sub, name, text in (("seed", "dbl.py", "def double(x):\n    raise NotImplementedError\n"),
                            ("reference", "dbl.py", "def double(x):\n    return 2 * x\n"),
                            ("hidden", "test_hidden.py", "from dbl import double\n\n\ndef test_negative():\n    assert double(-4) == -8\n")):
        (d / sub).mkdir(parents=True, exist_ok=True)
        (d / sub / name).write_text(text, newline="\n")
    (d / "TASK.md").write_text(TASK_MD, newline="\n")
    vis = tmp_path / "visible"
    vis.mkdir()
    (vis / "test_dbl_visible.py").write_text("from dbl import double\n\n\ndef test_two():\n    assert double(2) == 4\n",
                                             newline="\n")
    return d, vis


def test_synth_makes_a_two_commit_repo_and_a_taskdef(tmp_path, gate_task):
    task, vis = gate_task
    td = gate_tasks.synth(str(task), str(vis), "w1-t9", str(tmp_path / "repo"), str(tmp_path / "def"))
    repo = tmp_path / "repo"
    log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.splitlines() == ["reference + visible tests", "seed"]
    parent = subprocess.run(["git", "-C", str(repo), "show", "HEAD~1:dbl.py"], capture_output=True, text=True).stdout
    assert "NotImplementedError" in parent
    assert td == json.loads((tmp_path / "def" / "taskdef.json").read_text())
    assert td["id"] == "w1-t9" and td["files"] == ["dbl.py"] and td["tests"] == ["test_dbl_visible.py"]
    assert td["hidden"] == {"test_hidden.py": "test_w1_t9_hidden.py"}
    assert (tmp_path / "def" / "request.md").read_text() == TASK_MD
    assert (tmp_path / "def" / "hidden" / "test_hidden.py").exists()


def test_the_synthesized_task_builds_and_validates_as_a_bundle(tmp_path, gate_task):
    task, vis = gate_task
    gate_tasks.synth(str(task), str(vis), "w1-t9", str(tmp_path / "repo"), str(tmp_path / "def"))
    b = bundle.build(str(tmp_path / "repo"), str(tmp_path / "def"), str(tmp_path / "bundles"), str(tmp_path / "refs"))
    problems = bundle.validate(b, str(tmp_path / "refs" / "w1-t9.patch"), str(tmp_path / "work"), grade.LocalRunner())
    assert problems == []


def test_synth_refuses_a_visible_test_that_shadows_the_hidden_one(tmp_path, gate_task):
    task, vis = gate_task
    (vis / "test_hidden.py").write_text("def test_x():\n    pass\n")
    with pytest.raises(ValueError, match="test_hidden.py"):
        gate_tasks.synth(str(task), str(vis), "w1-t9", str(tmp_path / "repo"), str(tmp_path / "def"))


def test_synth_refuses_to_overwrite(tmp_path, gate_task):
    task, vis = gate_task
    (tmp_path / "repo").mkdir()
    with pytest.raises(FileExistsError):
        gate_tasks.synth(str(task), str(vis), "w1-t9", str(tmp_path / "repo"), str(tmp_path / "def"))
