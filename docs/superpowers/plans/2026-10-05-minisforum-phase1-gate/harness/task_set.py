"""Frozen worker-task set: load, hash, lay out a workspace, grade (spec §5.0.1).

Layout of a task root (committed under docs/superpowers/gate/worker-tasks/):
    <task-id>/TASK.md            the full contract -- every hidden check is stated here
    <task-id>/seed/*.py          the stub the worker edits
    <task-id>/hidden/test_*.py   grader tests; NEVER copied into a worker's workspace
    <task-id>/reference/*.py     known-good solution; used only to validate the set
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile


def load(root):
    """Sorted list of {"id", "dir"} for every task directory that has a TASK.md."""
    out = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isfile(os.path.join(d, "TASK.md")):
            out.append({"id": name, "dir": d})
    if not out:
        raise ValueError(f"no tasks under {root}")
    return out


def manifest(root):
    """sha256 over every file of every task (path + bytes), stable order. Frozen in the gate commit."""
    h = hashlib.sha256()
    for task in load(root):
        for dirpath, dirnames, files in os.walk(task["dir"]):
            dirnames.sort()
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for f in sorted(files):
                p = os.path.join(dirpath, f)
                h.update(os.path.relpath(p, root).replace(os.sep, "/").encode())
                with open(p, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


def prepare_workspace(root, dest):
    """dest/tasks/<id>/ gets TASK.md + seed files only. Returns the task list."""
    tasks = load(root)
    for t in tasks:
        tdir = os.path.join(dest, "tasks", t["id"])
        os.makedirs(tdir, exist_ok=True)
        shutil.copyfile(os.path.join(t["dir"], "TASK.md"), os.path.join(tdir, "TASK.md"))
        for f in os.listdir(os.path.join(t["dir"], "seed")):
            shutil.copyfile(os.path.join(t["dir"], "seed", f), os.path.join(tdir, f))
    return tasks


def _run_hidden(task_dir, code_dir, timeout=120):
    with tempfile.TemporaryDirectory() as tmp:
        for f in os.listdir(code_dir):
            p = os.path.join(code_dir, f)
            if f.endswith(".py") and os.path.isfile(p) and not f.startswith("test_"):
                shutil.copyfile(p, os.path.join(tmp, f))
        hidden = os.path.join(task_dir, "hidden")
        for f in os.listdir(hidden):
            shutil.copyfile(os.path.join(hidden, f), os.path.join(tmp, f))
        try:
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                               cwd=tmp, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        tail = (r.stdout.strip().splitlines() or [""])[-1]
        return r.returncode == 0, tail


def grade(root, workspace):
    """{task_id: {"pass": bool, "summary": str}} by running hidden tests on the worker's files.
    Only non-test .py files are taken from the workspace, so a worker cannot ship its own tests."""
    out = {}
    for t in load(root):
        ok, tail = _run_hidden(t["dir"], os.path.join(workspace, "tasks", t["id"]))
        out[t["id"]] = {"pass": ok, "summary": tail}
    return out


def validate(root):
    """Every task: hidden tests FAIL on the seed and PASS on the reference."""
    problems = []
    for t in load(root):
        seed_ok, _ = _run_hidden(t["dir"], os.path.join(t["dir"], "seed"))
        ref_ok, tail = _run_hidden(t["dir"], os.path.join(t["dir"], "reference"))
        if seed_ok:
            problems.append(f"{t['id']}: hidden tests pass on the seed (vacuous)")
        if not ref_ok:
            problems.append(f"{t['id']}: hidden tests fail on the reference ({tail})")
    return problems
