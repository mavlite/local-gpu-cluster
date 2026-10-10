"""Grading: pristine snapshot + exported patch + restored visible tests + hidden tests (spec §5.5)."""
import io
import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grade  # noqa: E402
import workspace  # noqa: E402

MOD = "pkg/calc.py"
VISIBLE = "pkg/tests/test_calc.py"
HIDDEN_DEST = "pkg/tests/test_calc_hidden.py"

SNAPSHOT = {
    MOD: "def add(a, b):\n    return 0\n",
    VISIBLE: ("import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
              "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"),
    "conftest.py": "import pytest\n\n@pytest.fixture\ndef seven():\n    return 7\n",
}
HIDDEN = ("import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
          "from calc import add\n\ndef test_add_negative():\n    assert add(-2, -3) == -5\n")


def make_tar(path, files):
    with tarfile.open(path, "w") as t:
        for name, data in files.items():
            raw = data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            t.addfile(info, io.BytesIO(raw))


def make_bundle(root, **task_overrides):
    os.makedirs(os.path.join(root, "hidden"))
    make_tar(os.path.join(root, "snapshot.tar"), SNAPSHOT)
    with open(os.path.join(root, "hidden", "test_calc_hidden.py"), "w") as f:
        f.write(HIDDEN)
    with open(os.path.join(root, "request.md"), "w") as f:
        f.write("Make add() add.\n")
    task = {"id": "t1", "commit": "c" * 40, "parent": "p" * 40, "request": "request.md", "files": [MOD],
            "tests": [VISIBLE], "hidden": {"test_calc_hidden.py": HIDDEN_DEST}, "needs_conftest": False,
            "timeout_s": 120}
    task.update(task_overrides)
    with open(os.path.join(root, "task.json"), "w") as f:
        json.dump(task, f)
    return root


def patch_for(tmp_path, new_files):
    """A real `git diff` patch turning the snapshot into new_files (via a scratch workspace)."""
    ws = workspace.Workspace.materialize(str(tmp_path / "b" / "snapshot.tar"), str(tmp_path / "pws"),
                                         str(tmp_path / "pgit"))
    for name, data in new_files.items():
        p = os.path.join(ws.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
    return ws.patch([c[2] for c in ws.changes()])


@pytest.fixture
def bundle(tmp_path):
    return make_bundle(str(tmp_path / "b"))


def run(bundle, patch, tmp_path, name="g"):
    return grade.grade(bundle, patch, str(tmp_path / name), grade.LocalRunner())


def test_the_unchanged_snapshot_fails(bundle, tmp_path):
    r = run(bundle, "", tmp_path)
    # "2 failed": the tests ran and failed -- not "No module named pytest" (a false pass seen once)
    assert r["pass"] is False and r["stage"] == "tests" and r["summary"].startswith("2 failed"), r


def test_a_correct_patch_passes(bundle, tmp_path):
    p = patch_for(tmp_path, {MOD: "def add(a, b):\n    return a + b\n"})
    r = run(bundle, p, tmp_path)
    assert r["pass"] is True, r


def test_a_solution_that_only_satisfies_the_visible_test_fails_on_the_hidden_one(bundle, tmp_path):
    p = patch_for(tmp_path, {MOD: "def add(a, b):\n    return 5\n"})
    assert run(bundle, p, tmp_path)["pass"] is False


def test_visible_tests_are_restored_even_if_the_patch_rewrites_them(bundle, tmp_path):
    p = patch_for(tmp_path, {VISIBLE: "def test_add():\n    assert True\n",
                             MOD: "def add(a, b):\n    return 5\n"})
    assert run(bundle, p, tmp_path)["pass"] is False


def test_hidden_tests_cannot_be_preempted_by_a_file_at_their_destination(bundle, tmp_path):
    p = patch_for(tmp_path, {HIDDEN_DEST: "def test_add_negative():\n    assert True\n",
                             MOD: "def add(a, b):\n    return 5\n"})
    assert run(bundle, p, tmp_path)["pass"] is False


def test_a_patch_that_does_not_apply_fails_at_the_apply_stage(bundle, tmp_path):
    bad = "diff --git a/pkg/calc.py b/pkg/calc.py\n--- a/pkg/calc.py\n+++ b/pkg/calc.py\n@@ -1 +1 @@\n-nope\n+x\n"
    r = run(bundle, bad, tmp_path)
    assert r["pass"] is False and r["stage"] == "apply"


def test_conftest_is_ignored_unless_the_task_needs_it(tmp_path):
    use_fixture = ("def test_fixture(seven):\n    assert seven == 7\n")
    for needs, expected in ((False, False), (True, True)):
        b = make_bundle(str(tmp_path / f"b{needs}"), needs_conftest=needs, hidden={})
        with tarfile.open(os.path.join(b, "snapshot.tar"), "a") as t:
            raw = use_fixture.encode()
            info = tarfile.TarInfo("pkg/tests/test_fixture.py")
            info.size = len(raw)
            t.addfile(info, io.BytesIO(raw))
        with open(os.path.join(b, "task.json")) as f:
            task = json.load(f)
        task["tests"] = ["pkg/tests/test_fixture.py"]
        with open(os.path.join(b, "task.json"), "w") as f:
            json.dump(task, f)
        r = grade.grade(b, "", str(tmp_path / f"g{needs}"), grade.LocalRunner())
        assert r["pass"] is expected, (needs, r)


def test_timeout_is_a_failure_at_the_timeout_stage(tmp_path):
    b = make_bundle(str(tmp_path / "b"), timeout_s=1, hidden={})
    with tarfile.open(os.path.join(b, "snapshot.tar"), "a") as t:
        raw = b"import time\n\ndef test_slow():\n    time.sleep(30)\n"
        info = tarfile.TarInfo("pkg/tests/test_slow.py")
        info.size = len(raw)
        t.addfile(info, io.BytesIO(raw))
    with open(os.path.join(b, "task.json")) as f:
        task = json.load(f)
    task["tests"] = ["pkg/tests/test_slow.py"]
    with open(os.path.join(b, "task.json"), "w") as f:
        json.dump(task, f)
    r = grade.grade(b, "", str(tmp_path / "g"), grade.LocalRunner())
    assert r["pass"] is False and r["stage"] == "timeout"


def test_docker_runner_has_no_network_and_mounts_only_the_tree():
    argv = grade.DockerRunner("wf-grader:1").argv("/runs/r1/grade/t1/tree", ["-m", "pytest", "-q"])
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("-v") + 1] == "/runs/r1/grade/t1/tree:/w"
    assert argv[-4:] == ["python3", "-m", "pytest", "-q"]


def test_load_task_rejects_a_task_whose_declared_files_include_its_tests(tmp_path):
    b = make_bundle(str(tmp_path / "b"), files=[MOD, VISIBLE])
    with pytest.raises(ValueError):
        grade.load_task(b)


def test_a_module_that_exits_the_test_process_at_import_does_not_pass(bundle, tmp_path):
    # Security review CRITICAL-1: os._exit(0) during collection gave rc 0 and no tests ran.
    p = patch_for(tmp_path, {MOD: "import os\nos._exit(0)\ndef add(a, b):\n    return 0\n"})
    r = run(bundle, p, tmp_path)
    assert r["pass"] is False and r["stage"] == "tests", r


def test_a_module_level_skip_does_not_pass(bundle, tmp_path):
    p = patch_for(tmp_path, {MOD: "import pytest\npytest.skip('nope', allow_module_level=True)\n"})
    assert run(bundle, p, tmp_path)["pass"] is False


def test_every_expected_test_must_be_reported_passed(bundle, tmp_path):
    p = patch_for(tmp_path, {MOD: "def add(a, b):\n    return a + b\n"})
    r = run(bundle, p, tmp_path)
    assert r["pass"] is True and r["tests"] == {"expected": 2, "passed": 2}, r


def test_expected_tests_are_read_from_the_test_files():
    src = ("import pytest\n\ndef test_a():\n    pass\n\ndef helper():\n    pass\n\n"
           "class TestX:\n    def test_b(self):\n        pass\n\n    def other(self):\n        pass\n\n"
           "class Plain:\n    def test_c(self):\n        pass\n")
    assert grade.expected_tests(src) == {"test_a", "TestX::test_b"}


def test_load_task_rejects_paths_that_leave_the_tree(tmp_path):
    for over in ({"files": ["../evil.py"]}, {"tests": ["/etc/passwd"]}, {"hidden": {"../x.py": "pkg/tests/x.py"}},
                 {"hidden": {"test_calc_hidden.py": "../../outside.py"}}, {"files": ["pkg\\calc.py"]}):
        b = make_bundle(str(tmp_path / f"b{len(os.listdir(tmp_path))}"), **over)
        with pytest.raises(ValueError):
            grade.load_task(b)


def test_docker_runner_kills_its_container_on_timeout_and_caps_cpu(tmp_path, monkeypatch):
    # Final review Important-5: killing the docker client left the container running.
    killed = tmp_path / "killed.txt"
    monkeypatch.setenv("FAKE_DOCKER_KILLED", str(killed))
    fake = [sys.executable, os.path.join(os.path.dirname(__file__), "fake_docker.py")]
    runner = grade.DockerRunner("img:1", docker=fake, cpus=2)
    argv = runner.argv("/t", ["-m", "pytest"], name="wf-grade-x")
    assert argv[argv.index("--name") + 1] == "wf-grade-x" and argv[argv.index("--cpus") + 1] == "2"
    rc, out, timed_out = runner.run(str(tmp_path), ["-m", "pytest"], timeout=2)
    assert timed_out and killed.read_text().strip().startswith("wf-grade-")


def test_header_comes_from_junit_not_stdout(bundle, tmp_path):
    # Round 2 §3.2: a forged "N passed" line in stdout is shown but never trusted; the header is the
    # harness's own count from the JUnit report.
    forged = ("import os\nprint('===== 99 passed in 0.01s =====', flush=True)\nos._exit(0)\n\n\n"
              "def add(a, b):\n    return a + b\n")
    out, header = grade.check_visible(bundle, patch_for(tmp_path, {MOD: forged}), str(tmp_path / "w"),
                                      grade.LocalRunner(), 60)
    # pytest's fd capture swallows the forged line before os._exit, so the real attack surface is the
    # exit code: rc 0 with no JUnit report must still count as 0 passed.
    assert "99 passed" not in header
    assert header == "visible tests: 0 of 1 expected passed, rc 0"


def test_header_counts_expected_tests_that_passed(bundle, tmp_path):
    out, header = grade.check_visible(bundle, patch_for(tmp_path, {MOD: "def add(a, b):\n    return a + b\n"}),
                                      str(tmp_path / "w"), grade.LocalRunner(), 60)
    assert header == "visible tests: 1 of 1 expected passed, rc 0"
    assert "1 passed" in out


def test_header_says_not_run_when_the_patch_does_not_apply(bundle, tmp_path):
    out, header = grade.check_visible(bundle, b"garbage patch", str(tmp_path / "w"), grade.LocalRunner(), 60)
    assert header.startswith("visible tests: not run")


def test_header_denominator_is_read_before_the_agents_code_runs(bundle, tmp_path):
    # Review I4: code under test that deletes the visible test file must not crash the header (nor
    # change its denominator): expected ids are read from the tree before pytest runs.
    deleting = ("import os\nos.remove(os.path.join(os.path.dirname(__file__), 'tests', 'test_calc.py'))\n"
                "def add(a, b):\n    return a + b\n")
    out, header = grade.check_visible(bundle, patch_for(tmp_path, {MOD: deleting}), str(tmp_path / "w"),
                                      grade.LocalRunner(), 60)
    assert header.startswith("visible tests: ") and "of 1 expected passed" in header
