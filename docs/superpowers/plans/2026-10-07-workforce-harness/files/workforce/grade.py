"""Grade one task (workforce spec §5.5).

The grading tree is rebuilt from the bundle's pristine snapshot, so nothing from the agent's
workspace is used except the exported (path-filtered) patch. The visible tests are restored from the
snapshot after the patch is applied, the hidden tests are copied in last (overwriting anything at
their destination), and pytest runs with a harness-owned ini, `-p no:cacheprovider`, and
`--noconftest` unless the task declares `needs_conftest` (conftest.py is a protected path, so the
conftest files in the tree are always the pristine ones).

Bundle layout (one directory per task):
    task.json      {"id", "commit", "parent", "request", "files", "tests", "hidden", "needs_conftest",
                    "timeout_s", optional "pytest_args"}
    request.md     the issue-style request given to the implementer
    snapshot.tar   the workspace snapshot (parent commit, stripped, visible tests included)
    hidden/        hidden tests; task.json "hidden" maps file name -> destination path in the tree
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile

import workspace

INI = ".workforce_pytest.ini"
_REQUIRED = {"id": str, "commit": str, "parent": str, "request": str, "files": list, "tests": list,
             "hidden": dict, "needs_conftest": bool, "timeout_s": int}


def load_task(bundle_dir):
    with open(os.path.join(bundle_dir, "task.json"), encoding="utf-8") as f:
        task = json.load(f)
    for key, typ in _REQUIRED.items():
        if not isinstance(task.get(key), typ):
            raise ValueError(f"{bundle_dir}: task.json '{key}' missing or not {typ.__name__}")
    overlap = set(task["files"]) & (set(task["tests"]) | set(task["hidden"].values()))
    if overlap:
        raise ValueError(f"{task['id']}: declared files overlap its tests: {sorted(overlap)}")
    if set(task["hidden"].values()) & set(task["tests"]):
        raise ValueError(f"{task['id']}: a hidden test destination is also a visible test")
    return task


def pytest_args(task):
    args = ["-m", "pytest", "-c", INI, "-p", "no:cacheprovider", "-q"]
    if not task["needs_conftest"]:
        args.append("--noconftest")
    return args + list(task.get("pytest_args") or task["tests"]) + sorted(task["hidden"].values())


def restore_visible_tests(bundle_dir, task, tree):
    """Overwrite the task's visible tests in `tree` with the snapshot's pristine copies."""
    with tarfile.open(os.path.join(bundle_dir, "snapshot.tar")) as t:
        members = [m for m in t.getmembers() if m.name in set(task["tests"])]
        for m in members:
            target = os.path.join(tree, m.name)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            elif os.path.lexists(target):
                os.remove(target)
        t.extractall(tree, members=members, filter="data")


def prepare_tree(bundle_dir, task, patch_text, tree):
    """Build the grading tree. Raises workspace.PatchError if the patch does not apply."""
    snapshot = os.path.join(bundle_dir, "snapshot.tar")
    workspace.unpack(snapshot, tree)
    workspace.apply_patch(tree, patch_text)
    restore_visible_tests(bundle_dir, task, tree)
    for name, dest in task["hidden"].items():
        target = os.path.join(tree, dest)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            os.remove(target)
        shutil.copyfile(os.path.join(bundle_dir, "hidden", name), target)
    with open(os.path.join(tree, INI), "w", encoding="utf-8") as f:
        f.write("[pytest]\n")


class LocalRunner:
    """Runs pytest with this interpreter (bundle validation, tests). Not network-isolated."""

    def __init__(self, python=sys.executable):
        self.python = python

    def run(self, tree, args, timeout):
        env = {k: v for k, v in os.environ.items() if k.upper() in
               {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "HOME",
                "APPDATA", "LOCALAPPDATA", "USERPROFILE"}}     # user site-packages live under these
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            r = subprocess.run([self.python, *args], cwd=tree, capture_output=True, text=True,
                               timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return None, "", True
        return r.returncode, r.stdout + r.stderr, False


class DockerRunner:
    """A fresh, network-less container per grade (spec §5.5); only the grading tree is mounted."""

    def __init__(self, image, docker="docker"):
        self.image, self.docker = image, docker

    def argv(self, tree, args):
        return [self.docker, "run", "--rm", "--network", "none", "--memory", "4g", "--pids-limit", "512",
                "-v", f"{tree}:/w", "-w", "/w", "-e", "PYTHONDONTWRITEBYTECODE=1", self.image,
                "python3", *args]

    def run(self, tree, args, timeout):
        try:
            r = subprocess.run(self.argv(os.path.abspath(tree), args), capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, "", True
        return r.returncode, r.stdout + r.stderr, False


def _tail(text):
    lines = [x for x in text.strip().splitlines() if x.strip()]
    return lines[-1] if lines else ""


def grade(bundle_dir, patch_text, workdir, runner):
    """{"pass": bool, "stage": "apply"|"timeout"|"tests", "summary": str, "rc": int|None}"""
    task = load_task(bundle_dir)
    tree = os.path.join(workdir, "tree")
    if os.path.exists(tree):
        shutil.rmtree(tree)
    try:
        prepare_tree(bundle_dir, task, patch_text, tree)
    except workspace.PatchError as e:
        return {"pass": False, "stage": "apply", "summary": str(e)[-300:], "rc": None}
    rc, out, timed_out = runner.run(tree, pytest_args(task), task["timeout_s"])
    with open(os.path.join(workdir, "pytest.out"), "w", encoding="utf-8") as f:
        f.write(out)
    if timed_out:
        return {"pass": False, "stage": "timeout", "summary": f"over {task['timeout_s']} s", "rc": None}
    return {"pass": rc == 0, "stage": "tests", "summary": _tail(out), "rc": rc}
