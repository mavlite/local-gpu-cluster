import os
import shutil
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

import task_set  # noqa: E402

ROOT = os.environ.get("GATE_TASK_ROOT",
                      os.path.join(HERE, "..", "..", "..", "..", "docs", "superpowers", "gate", "worker-tasks"))


def test_the_committed_set_is_valid():
    assert len(task_set.load(ROOT)) >= 6
    assert task_set.validate(ROOT) == []


def test_workspace_never_contains_hidden_or_reference(tmp_path):
    task_set.prepare_workspace(ROOT, str(tmp_path))
    for dirpath, _, files in os.walk(tmp_path):
        assert "hidden" not in dirpath and "reference" not in dirpath
        assert not any(f.startswith("test_") for f in files)


def test_grade_seed_fails_reference_passes(tmp_path):
    tasks = task_set.prepare_workspace(ROOT, str(tmp_path))
    assert not any(v["pass"] for v in task_set.grade(ROOT, str(tmp_path)).values())
    for t in tasks:                                   # "worker" submits the reference
        ref = os.path.join(t["dir"], "reference")
        for f in os.listdir(ref):
            shutil.copyfile(os.path.join(ref, f), os.path.join(tmp_path, "tasks", t["id"], f))
    assert all(v["pass"] for v in task_set.grade(ROOT, str(tmp_path)).values())


def test_worker_supplied_tests_are_ignored(tmp_path):
    task_set.prepare_workspace(ROOT, str(tmp_path))
    first = task_set.load(ROOT)[0]["id"]
    with open(os.path.join(tmp_path, "tasks", first, "test_cheat.py"), "w") as f:
        f.write("def test_ok():\n    assert True\n")
    assert task_set.grade(ROOT, str(tmp_path))[first]["pass"] is False


def test_manifest_changes_when_any_file_changes(tmp_path):
    copy = tmp_path / "set"
    shutil.copytree(ROOT, copy)
    before = task_set.manifest(str(copy))
    first = task_set.load(str(copy))[0]["dir"]
    with open(os.path.join(first, "TASK.md"), "a") as f:
        f.write("\n")
    assert task_set.manifest(str(copy)) != before


def test_empty_root_rejected(tmp_path):
    with pytest.raises(ValueError):
        task_set.load(str(tmp_path))
