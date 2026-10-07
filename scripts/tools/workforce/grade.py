"""Grade one task (workforce spec §5.5).

The grading tree is rebuilt from the bundle's pristine snapshot, so nothing from the agent's
workspace is used except the exported (path-filtered) patch. The visible tests are restored from the
snapshot after the patch is applied, the hidden tests are copied in last (overwriting anything at
their destination), and pytest runs with a harness-owned ini, `-p no:cacheprovider`, and
`--noconftest` unless the task declares `needs_conftest` (conftest.py is a protected path, so the
conftest files in the tree are always the pristine ones).

A pass needs more than pytest's exit code: the code under test runs inside the pytest process, and
`os._exit(0)` at import ends it with code 0 before any test runs (security review CRITICAL-1). So
pytest writes a JUnit report and every expected test -- read from the test files with `ast` -- must
appear in it as passed, with no failure, error or skip anywhere.

Bundle layout (one directory per task):
    task.json      {"id", "commit", "parent", "request", "files", "tests", "hidden", "needs_conftest",
                    "timeout_s", optional "pytest_args"}
    request.md     the issue-style request given to the implementer
    snapshot.tar   the workspace snapshot (parent commit, stripped, visible tests included)
    hidden/        hidden tests; task.json "hidden" maps file name -> destination path in the tree
"""
import ast
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tarfile
import uuid
import xml.etree.ElementTree as ET

import workspace

INI = ".workforce_pytest.ini"
JUNIT = ".workforce_junit.xml"
_REQUIRED = {"id": str, "commit": str, "parent": str, "request": str, "files": list, "tests": list,
             "hidden": dict, "needs_conftest": bool, "timeout_s": int}


def _safe_rel(path):
    """A relative, normalised posix path inside the tree."""
    return (isinstance(path, str) and path and "\\" not in path and ":" not in path
            and not path.startswith("/") and posixpath.normpath(path) == path
            and path != ".." and not path.startswith("../"))


def load_task(bundle_dir):
    with open(os.path.join(bundle_dir, "task.json"), encoding="utf-8") as f:
        task = json.load(f)
    for key, typ in _REQUIRED.items():
        if not isinstance(task.get(key), typ):
            raise ValueError(f"{bundle_dir}: task.json '{key}' missing or not {typ.__name__}")
    paths = list(task["files"]) + list(task["tests"]) + list(task["hidden"].values()) +         [a.split("::")[0] for a in task.get("pytest_args") or []]
    bad = [p for p in paths if not _safe_rel(p)]
    bad += [n for n in task["hidden"] if not isinstance(n, str) or "/" in n or "\\" in n or n in ("", ".", "..")]
    if bad:
        raise ValueError(f"{task.get('id')}: unsafe paths in task.json: {bad}")
    overlap = set(task["files"]) & (set(task["tests"]) | set(task["hidden"].values()))
    if overlap:
        raise ValueError(f"{task['id']}: declared files overlap its tests: {sorted(overlap)}")
    if set(task["hidden"].values()) & set(task["tests"]):
        raise ValueError(f"{task['id']}: a hidden test destination is also a visible test")
    return task


def expected_tests(source):
    """{"test_x", "TestY::test_z"} defined in a test module's source."""
    out = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            out.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            out |= {f"{node.name}::{f.name}" for f in node.body
                    if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("test")}
    return out


def _targets(task):
    return list(task.get("pytest_args") or task["tests"]) + sorted(task["hidden"].values())


def expected_ids(task, tree):
    """{(module_dotted, "Class::name" | "name")} every test the grade must see pass."""
    out = set()
    for target in _targets(task):
        path, _, node = target.partition("::")
        module = path[:-3].replace("/", ".")
        if node:
            out.add((module, node))
        else:
            with open(os.path.join(tree, path), encoding="utf-8") as f:
                out |= {(module, name) for name in expected_tests(f.read())}
    return out


def junit_results(path):
    """{(module_dotted, "Class::name" | "name"): passed?} from a pytest JUnit report; parametrised
    cases fold into their function (all must pass)."""
    results = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        cls, name = case.get("classname", ""), case.get("name", "").split("[")[0]
        last = cls.rsplit(".", 1)[-1]
        module, key = (cls.rsplit(".", 1)[0], f"{last}::{name}") if last.startswith("Test") else (cls, name)
        ok = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
        results[(module, key)] = results.get((module, key), True) and ok
    return results


def pytest_args(task):
    args = ["-m", "pytest", "-c", INI, "-p", "no:cacheprovider", "-q", f"--junitxml={JUNIT}"]
    if not task["needs_conftest"]:
        args.append("--noconftest")
    return args + _targets(task)


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
    """A fresh, network-less, CPU- and memory-capped container per grade (spec §5.5); only the grading
    tree is mounted. On a timeout the container itself is killed: killing the docker client alone
    leaves it running."""

    def __init__(self, image, docker="docker", cpus=4):
        self.image, self.cpus = image, cpus
        self.docker = [docker] if isinstance(docker, str) else list(docker)

    def argv(self, tree, args, name="wf-grade"):
        return [*self.docker, "run", "--rm", "--name", name, "--network", "none", "--cpus", str(self.cpus),
                "--memory", "4g", "--pids-limit", "512", "-v", f"{tree}:/w", "-w", "/w",
                "-e", "PYTHONDONTWRITEBYTECODE=1", self.image, "python3", *args]

    def run(self, tree, args, timeout):
        name = f"wf-grade-{uuid.uuid4().hex[:12]}"
        try:
            r = subprocess.run(self.argv(os.path.abspath(tree), args, name), capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run([*self.docker, "kill", name], capture_output=True)
            return None, "", True
        return r.returncode, r.stdout + r.stderr, False


def _tail(text):
    lines = [x for x in text.strip().splitlines() if x.strip()]
    return lines[-1] if lines else ""


def check_visible(bundle_dir, patch, workdir, runner, timeout):
    """Output of the task's VISIBLE tests on pristine snapshot + patch, run by `runner` exactly like a
    grade (harness ini, --noconftest unless needed). Shown to the lead; never the hidden tests."""
    task = load_task(bundle_dir)
    tree = os.path.join(workdir, "tree")
    if os.path.exists(tree):
        shutil.rmtree(tree)
    workspace.unpack(os.path.join(bundle_dir, "snapshot.tar"), tree)
    try:
        workspace.apply_patch(tree, patch)
    except workspace.PatchError as e:
        return f"(the change does not apply to the task's snapshot: {str(e)[-300:]})"
    restore_visible_tests(bundle_dir, task, tree)
    with open(os.path.join(tree, INI), "w", encoding="utf-8") as f:
        f.write("[pytest]\n")
    args = ["-m", "pytest", "-c", INI, "-p", "no:cacheprovider", "-q"]
    args += [] if task["needs_conftest"] else ["--noconftest"]
    _rc, out, timed_out = runner.run(tree, args + list(task.get("pytest_args") or task["tests"]), timeout)
    if timed_out:
        return f"(tests did not finish within {timeout} s)"
    return "\n".join(out.strip().splitlines()[-200:])


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
    expected = expected_ids(task, tree)                    # read before any agent code runs
    junit = os.path.join(tree, JUNIT)
    if os.path.lexists(junit):
        os.remove(junit)
    rc, out, timed_out = runner.run(tree, pytest_args(task), task["timeout_s"])
    with open(os.path.join(workdir, "pytest.out"), "w", encoding="utf-8") as f:
        f.write(out)
    if timed_out:
        return {"pass": False, "stage": "timeout", "summary": f"over {task['timeout_s']} s", "rc": None}
    try:
        results = junit_results(junit)
    except (OSError, ET.ParseError):
        results = {}
    passed = sum(1 for k in expected if results.get(k))
    ok = rc == 0 and bool(expected) and passed == len(expected) and all(results.values())
    summary = _tail(out) or "no pytest summary (the test process ended early)"
    return {"pass": ok, "stage": "tests", "summary": summary, "rc": rc,
            "tests": {"expected": len(expected), "passed": passed}}
