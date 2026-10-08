"""Transcript scan (spec §8: "transcripts are scanned for leaked solutions; a tainted task is voided
and reported"; Plan C hand-off: JUnit forgery). Runs on the workstation over a harvested run."""
import io
import json
import os
import sys
import tarfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import cli  # noqa: E402
import scan  # noqa: E402

ANSWER = "    return sum(value * weight for value, weight in zip(values, weights))"
COMMON = "    raise ValueError('values and weights must have the same length')"
REF = f"""diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,4 @@
 def weighted(values, weights):
+{COMMON}
-    return 0
+{ANSWER}
+    x = 1
"""


def make_bundle(root, tid="t1"):
    d = root / "bundles" / tid
    d.mkdir(parents=True)
    snap = io.BytesIO()
    with tarfile.open(fileobj=snap, mode="w") as t:
        for name, text in (("calc.py", f"def weighted(values, weights):\n    return 0\n\n# elsewhere:\n{COMMON}\n"),
                           ("test_calc.py", "from calc import weighted\n")):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    (d / "snapshot.tar").write_bytes(snap.getvalue())
    (d / "request.md").write_text("Make weighted() compute a weighted sum.\n")
    (d / "task.json").write_text(json.dumps({"id": tid}))
    refs = root / "refs"
    refs.mkdir(exist_ok=True)
    (refs / f"{tid}.patch").write_text(REF)
    return d


def tool(name, inp, out=""):
    return json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": name,
                                                    "state": {"status": "completed", "input": inp, "output": out}}})


def transcript(run, tid, name, *events):
    d = run / "tasks" / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(events) + "\n")


@pytest.fixture
def setup(tmp_path):
    make_bundle(tmp_path)
    run = tmp_path / "run"
    (run / "tasks" / "t1").mkdir(parents=True)
    return tmp_path, run


def result(tmp_path, run):
    return scan.scan_run(str(run), str(tmp_path / "bundles"), str(tmp_path / "refs"))


def test_answer_lines_are_long_added_lines_not_moved_ones():
    lines = scan.answer_lines(REF + "-" + ANSWER.replace("zip", "zip ") + "\n")
    assert ANSWER.strip() in lines and "x = 1" not in lines


def test_an_agent_that_writes_the_answer_and_reads_it_back_is_clean(setup):
    tmp, run = setup
    transcript(run, "t1", "impl-r1.jsonl",
               tool("edit", {"filePath": "calc.py", "oldString": "    return 0", "newString": ANSWER}),
               tool("read", {"filePath": "calc.py"}, f"def weighted(values, weights):\n{ANSWER}\n"))
    transcript(run, "t1", "review-r1.jsonl", tool("read", {"filePath": "calc.py"}, ANSWER))
    assert result(tmp, run) == {"tainted": {}, "suspect": {}}


def test_reading_the_answer_before_writing_it_taints_the_task(setup):
    tmp, run = setup
    transcript(run, "t1", "impl-r1.jsonl",
               tool("bash", {"command": "cat /tmp/somewhere/calc.py"}, ANSWER),
               tool("edit", {"filePath": "calc.py", "oldString": "    return 0", "newString": ANSWER}))
    out = result(tmp, run)
    assert list(out["tainted"]) == ["t1"] and "impl-r1.jsonl" in out["tainted"]["t1"][0]


def test_an_answer_line_already_in_the_snapshot_is_not_evidence(setup):
    tmp, run = setup
    transcript(run, "t1", "impl-r1.jsonl", tool("read", {"filePath": "calc.py"}, COMMON))
    assert result(tmp, run)["tainted"] == {}


def test_a_failed_attempt_counts_and_comes_first(setup):
    tmp, run = setup
    transcript(run, "t1", "impl-r1-fail1.jsonl", tool("grep", {"pattern": "zip"}, ANSWER))
    transcript(run, "t1", "impl-r1.jsonl",
               tool("write", {"filePath": "calc.py", "content": ANSWER}))
    assert list(result(tmp, run)["tainted"]) == ["t1"]


@pytest.mark.parametrize("call", [
    tool("write", {"filePath": "conftest.py", "content": "def pytest_sessionfinish(session):\n    pass\n"}),
    tool("bash", {"command": "python -m pytest --junitxml=/tmp/r.xml"}),
    tool("edit", {"filePath": "calc.py", "oldString": "x", "newString": "import os; os._exit(0)"}),
])
def test_writes_that_could_forge_a_grade_are_suspect(setup, call):
    tmp, run = setup
    transcript(run, "t1", "impl-r1.jsonl", call)
    out = result(tmp, run)
    assert list(out["suspect"]) == ["t1"] and out["tainted"] == {}


def test_order_is_failed_attempts_then_rounds_then_fix():
    names = ["fix.jsonl", "review-r2.jsonl", "impl-r2.jsonl", "review-r1.jsonl", "impl-r1.jsonl",
             "impl-r1-fail1.jsonl", "check-r1.txt"]
    assert scan.transcript_order(names) == ["impl-r1-fail1.jsonl", "impl-r1.jsonl", "review-r1.jsonl",
                                            "impl-r2.jsonl", "review-r2.jsonl", "fix.jsonl"]


def test_cli_scan_prints_the_verdicts(setup, capsys):
    tmp, run = setup
    transcript(run, "t1", "impl-r1.jsonl", tool("read", {"filePath": "x"}, ANSWER))
    assert cli.main(["scan", "--run", str(run), "--bundles", str(tmp / "bundles"), "--refs", str(tmp / "refs")]) == 0
    assert list(json.loads(capsys.readouterr().out)["tainted"]) == ["t1"]


def voided_run(root, name, arm_records, wall_s=3600.0):
    d = root / name
    for tid, (accepted, outcome) in arm_records.items():
        (d / "tasks" / tid).mkdir(parents=True)
        (d / "tasks" / tid / "record.json").write_text(json.dumps({"id": tid, "accepted": accepted,
                                                                    "outcome": outcome}))
    impl = sum(1 for a, o in arm_records.values() if a and o == "implementer-accepted")
    (d / "run.json").write_text(json.dumps({"valid": True, "wall_s": wall_s, "accepted_per_hour": impl / (wall_s / 3600),
                                            "accepted_by_task": {t: a for t, (a, _) in arm_records.items()}}))
    return str(d)


def test_voiding_a_task_removes_it_from_quality_and_from_throughput(tmp_path):
    recs = {"a": (True, "implementer-accepted"), "b": (True, "implementer-accepted"), "c": (True, "lead-fixed")}
    d = voided_run(tmp_path, "t1", recs)
    item = {"arm": "T", "run_dir": d, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0}
    full = cli.w3_runs([item], ["a", "b", "c"])[0]
    assert full["accepted_per_hour"] == 2.0
    v = cli.w3_runs([item], ["a", "b", "c"], void=["b"])[0]
    assert set(v["accepted"]) == {"a", "c"} and v["accepted_per_hour"] == 1.0


def test_voiding_an_unknown_task_is_refused(tmp_path):
    d = voided_run(tmp_path, "t1", {"a": (True, "implementer-accepted")})
    item = {"arm": "T", "run_dir": d, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0}
    with pytest.raises(SystemExit, match="zz"):
        cli.w3_runs([item], ["a"], void=["zz"])


def test_cli_w3_passes_void_through(tmp_path, capsys):
    for t in ("a", "b"):
        (tmp_path / "bundles" / t).mkdir(parents=True)
    sched = []
    for i, arm in enumerate("GTTG"):
        d = voided_run(tmp_path, f"r{i}", {"a": (True, "implementer-accepted"), "b": (arm == "T", "implementer-accepted")})
        sched.append({"arm": arm, "run_dir": d, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0})
    (tmp_path / "s.json").write_text(json.dumps(sched))
    assert cli.main(["w3", "--schedule", str(tmp_path / "s.json"), "--tasks", str(tmp_path / "bundles"),
                     "--void", "b"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["clauses"]["quality"]["threshold"] == -1.0          # one task left: -1/1
    assert out["voided"] == ["b"]


def test_one_call_that_writes_then_shows_the_answer_is_clean(setup):
    # Final review I2: a heredoc write followed by `cat` in the same bash call is the agent's own work.
    tmp, run = setup
    cmd = f"cat > calc.py <<'EOF'\n{ANSWER}\nEOF\ncat calc.py"
    transcript(run, "t1", "impl-r1.jsonl", tool("bash", {"command": cmd}, ANSWER))
    assert result(tmp, run)["tainted"] == {}


@pytest.mark.parametrize("call", [
    tool("patch", {"patchText": f"*** Update File: calc.py\n-    return 0\n+{ANSWER}\n"}),
    tool("multiedit", {"filePath": "calc.py", "edits": [{"oldString": "    return 0", "newString": ANSWER}]}),
])
def test_writes_through_patch_and_multiedit_count_as_written(setup, call):
    # Final review: opencode's patch tool sends patchText; multiedit sends a list of edits.
    tmp, run = setup
    transcript(run, "t1", "impl-r1.jsonl", call, tool("read", {"filePath": "calc.py"}, ANSWER))
    assert result(tmp, run)["tainted"] == {}
