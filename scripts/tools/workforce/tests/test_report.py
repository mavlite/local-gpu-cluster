"""Per-ticket report: one page a human or Claude reads to decide on a background task's branch (spec §3.3 B)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import report  # noqa: E402


def make_run(root, tasks):
    os.makedirs(os.path.join(root, "patches"))
    for tid, rec in tasks.items():
        d = os.path.join(root, "tasks", tid)
        os.makedirs(d)
        with open(os.path.join(d, "record.json"), "w") as f:
            json.dump(dict({"id": tid, "dropped": [], "rework_rounds": 0, "review_infra": [], "reviews": [],
                            "t_start": 0.0, "t_end": 90.0, "grade": {"pass": True, "summary": "3 passed"}}, **rec), f)
        with open(os.path.join(root, "patches", f"{tid}.patch"), "w") as f:
            f.write(f"diff --git a/pkg/{tid}.py b/pkg/{tid}.py\n")
    with open(os.path.join(root, "run.json"), "w") as f:
        json.dump({"arm": "G", "wall_s": 600.0, "accepted": sum(1 for r in tasks.values() if r["accepted"]),
                   "n_tasks": len(tasks), "valid": True, "review_lanes": {"w1": 1, "lead": 1}}, f)
    return root


def test_one_page_per_task_with_outcome_review_lane_and_tests(tmp_path):
    run = make_run(str(tmp_path / "run"), {
        "a": {"outcome": "implementer-accepted", "accepted": True,
              "reviews": [{"lane": "w1", "verdict": "ACCEPT", "feedback": ""}]},
        "b": {"outcome": "reworked-unreviewed", "accepted": False, "rework_rounds": 1,
              "grade": {"pass": False, "summary": "1 failed, 2 passed"},
              "reviews": [{"lane": "lead", "verdict": "REVISE", "feedback": "f(-3) must be -6"}],
              "review_infra": [{"lane": "w2", "rc": 1, "t": 1.0}]}})
    paths = report.write(run, str(tmp_path / "out"))
    assert sorted(os.path.basename(p) for p in paths) == ["a.md", "b.md", "index.md"]
    a = open(tmp_path / "out" / "a.md", encoding="utf-8").read()
    assert "ACCEPTED" in a and "w1" in a and "ACCEPT" in a and "3 passed" in a and "a.patch" in a
    b = open(tmp_path / "out" / "b.md", encoding="utf-8").read()
    assert "NOT ACCEPTED" in b and "f(-3) must be -6" in b and "1 failed, 2 passed" in b
    assert "worker failure" in b.lower() and "w2" in b
    idx = open(tmp_path / "out" / "index.md", encoding="utf-8").read()
    assert "1 of 2 accepted" in idx and "[a](a.md)" in idx and "[b](b.md)" in idx


def test_a_task_with_no_review_and_no_grade_still_gets_a_page(tmp_path):
    run = make_run(str(tmp_path / "run"), {"c": {"outcome": "infra-error", "accepted": False, "grade": None}})
    report.write(run, str(tmp_path / "out"))
    c = open(tmp_path / "out" / "c.md", encoding="utf-8").read()
    assert "infra-error" in c and "not graded" in c.lower() and "no review" in c.lower()


def test_feedback_cannot_break_out_of_its_block(tmp_path):
    run = make_run(str(tmp_path / "run"), {"d": {"outcome": "reworked-unreviewed", "accepted": False,
        "reviews": [{"lane": "w1", "verdict": "REVISE", "feedback": "```\n# Injected heading\n```"}]}})
    report.write(run, str(tmp_path / "out"))
    d = open(tmp_path / "out" / "d.md", encoding="utf-8").read()
    assert "\n# Injected heading" not in d                                     # quoted, not rendered


def test_cli_report_writes_the_index(tmp_path, capsys):
    import cli
    run = make_run(str(tmp_path / "run"), {"e": {"outcome": "implementer-accepted", "accepted": True}})
    cli.main(["report", "--run", run, "--out", str(tmp_path / "out")])
    assert os.path.isfile(tmp_path / "out" / "index.md")
    assert len(json.loads(capsys.readouterr().out)["written"]) == 2
