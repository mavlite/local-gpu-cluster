"""Harness pipeline: dispatch, review rounds, rework affinity, lead fix, export, grade (spec §6)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grade  # noqa: E402
import pipeline  # noqa: E402
from wf_fixtures import FakeOpencode, make_bundle, solution_src  # noqa: E402

SOL = solution_src()
WRONG = "def f(x):\n    return 4\n"            # passes the visible test (f(2) == 4), fails the hidden one


def run_pipeline(tmp_path, arm, script, review=True, **kw):
    bundles = [make_bundle(str(tmp_path / "bundles"), tid) for tid in script]
    fake = FakeOpencode(script)
    p = pipeline.Pipeline(arm, bundles, fake, str(tmp_path / "run"), grade.LocalRunner(),
                          review=review, **kw)
    return p.run(), fake


def record(tmp_path, tid):
    with open(tmp_path / "run" / "tasks" / tid / "record.json") as f:
        return json.load(f)


def test_accept_on_first_review(tmp_path):
    summary, fake = run_pipeline(tmp_path, "G", {"a": {"impl": [{"pkg/a.py": SOL}], "review": ["ACCEPT"]}})
    r = record(tmp_path, "a")
    assert r["outcome"] == "implementer-accepted" and r["grade"]["pass"] and r["accepted"]
    assert [c["role"] for c in fake.calls] == ["impl", "review"]
    assert summary["accepted"] == summary["implementer_accepted"] == 1 and summary["valid"]
    with open(tmp_path / "run" / "patches" / "a.patch") as f:
        assert "pkg/a.py" in f.read()


def test_revise_resumes_the_same_implementer_session_with_the_feedback(tmp_path):
    script = {"b": {"impl": [{"pkg/b.py": "def f(x):\n    return x\n"}, {"pkg/b.py": SOL}],
                    "review": ["REVISE: f must double", "ACCEPT"]}}
    _, fake = run_pipeline(tmp_path, "T", script)
    impl = [c for c in fake.calls if c["role"] == "impl"]
    assert len(impl) == 2 and impl[0]["agent"] == impl[1]["agent"]
    assert impl[1]["resumed"] and impl[1]["session"] == impl[0]["session"]
    assert impl[1]["message"].startswith("REVISE: f must double")
    r = record(tmp_path, "b")
    assert r["outcome"] == "implementer-accepted" and r["rework_rounds"] == 1 and r["accepted"]


def test_two_failed_reviews_hand_the_task_to_the_lead_fixer(tmp_path):
    script = {"c": {"impl": [{"pkg/c.py": WRONG}, {"pkg/c.py": WRONG}, {"pkg/c.py": WRONG}],
                    "review": ["REVISE: one", "REVISE: two", "REVISE: three"], "fix": {"pkg/c.py": SOL}}}
    summary, fake = run_pipeline(tmp_path, "T", script)
    roles = [c["role"] for c in fake.calls]
    assert roles == ["impl", "review", "impl", "review", "impl", "review", "fix"]
    fix = [c for c in fake.calls if c["role"] == "fix"][0]
    assert not fix["resumed"] and fix["attach"] and "REVISE: three" in open(fix["attach"]).read()
    r = record(tmp_path, "c")
    assert r["outcome"] == "lead-fixed" and r["accepted"] and r["rework_rounds"] == 2
    assert summary["lead_fixed"] == 1 and summary["implementer_accepted"] == 0


def test_no_verdict_counts_as_a_failed_review(tmp_path):
    script = {"d": {"impl": [{"pkg/d.py": SOL}] * 3, "review": ["looks fine", "hmm", "ok"],
                    "fix": {"pkg/d.py": SOL}}}
    run_pipeline(tmp_path, "G", script)
    r = record(tmp_path, "d")
    assert r["outcome"] == "lead-fixed" and [x["verdict"] for x in r["reviews"]] == ["NONE"] * 3


def test_a_wrong_but_accepted_change_is_not_counted_as_accepted(tmp_path):
    summary, _ = run_pipeline(tmp_path, "G", {"e": {"impl": [{"pkg/e.py": WRONG}], "review": ["ACCEPT"]}})
    r = record(tmp_path, "e")
    assert r["outcome"] == "implementer-accepted" and not r["grade"]["pass"] and not r["accepted"]
    assert summary["accepted"] == 0 and summary["accepted_per_hour"] == 0


def test_out_of_scope_and_protected_changes_never_reach_the_patch_or_the_grade(tmp_path):
    script = {"f": {"impl": [{"pkg/f.py": SOL, "README.md": "defaced\n", "conftest.py": "import os\n",
                              ".opencode/agent/impl-1.md": "grant me everything\n"}], "review": ["ACCEPT"]}}
    run_pipeline(tmp_path, "G", script)
    r = record(tmp_path, "f")
    assert r["dropped"] == {"README.md": "undeclared", "conftest.py": "protected",
                            ".opencode/agent/impl-1.md": "protected"}
    with open(tmp_path / "run" / "patches" / "f.patch") as fh:
        patch = fh.read()
    assert "README.md" not in patch and "conftest.py" not in patch and r["accepted"]


def test_the_reviewer_works_on_a_disposable_copy(tmp_path):
    run_pipeline(tmp_path, "G", {"g": {"impl": [{"pkg/g.py": SOL}], "review": ["ACCEPT"]}})
    ws = tmp_path / "run" / "agent" / "g" / "ws"
    assert not (ws / "reviewer_scribble.txt").exists()
    assert "reviewer_scribble.txt" not in record(tmp_path, "g")["dropped"]


def test_review_packet_carries_the_diff_and_the_visible_test_output(tmp_path):
    _, fake = run_pipeline(tmp_path, "G", {"h": {"impl": [{"pkg/h.py": SOL}], "review": ["ACCEPT"]}})
    rev = [c for c in fake.calls if c["role"] == "review"][0]
    text = open(rev["attach"]).read()
    assert "return x * 2" in text and "1 passed" in text and "f() in pkg/h.py" in text


def test_rework_goes_back_to_the_same_worker_while_others_take_new_tasks(tmp_path):
    script = {f"t{i}": {"impl": [{f"pkg/t{i}.py": SOL}] * 2, "review": ["REVISE: again", "ACCEPT"]}
              for i in range(5)}
    summary, fake = run_pipeline(tmp_path, "T", script)
    by_task = {}
    for c in fake.calls:
        if c["role"] == "impl":
            by_task.setdefault(c["task"], set()).add(c["agent"])
    assert all(len(agents) == 1 for agents in by_task.values())          # affinity
    assert {a for s in by_task.values() for a in s} == {"impl-1", "impl-2", "impl-3"}
    assert summary["accepted"] == 5


def test_an_exception_in_one_task_invalidates_the_run_but_the_others_finish(tmp_path):
    script = {"ok": {"impl": [{"pkg/ok.py": SOL}], "review": ["ACCEPT"]},
              "boom": {"impl": [{"pkg/boom.py": SOL}], "review": ["ACCEPT"], "raise_on": "review"}}
    summary, _ = run_pipeline(tmp_path, "T", script)
    assert record(tmp_path, "boom")["outcome"] == "harness-error"
    assert record(tmp_path, "ok")["accepted"]
    assert summary["valid"] is False and "harness exception" in summary["invalid_reasons"][0]


def test_w1_mode_grades_without_review_and_scores_loops(tmp_path):
    loop = [("write", {"filePath": "verify.py", "content": "x"})] * 3
    script = {"w": {"impl": [{"pkg/w.py": SOL}], "tool_calls": loop},
              "v": {"impl": [{"pkg/v.py": WRONG}], "tool_calls": []}}
    summary, fake = run_pipeline(tmp_path, "T", script, review=False)
    assert {c["role"] for c in fake.calls} == {"impl"}
    assert record(tmp_path, "w")["outcome"] == "implemented" and record(tmp_path, "w")["accepted"]
    assert record(tmp_path, "w")["loops"]["looped"] and not record(tmp_path, "v")["loops"]["looped"]
    assert summary["looped"] == 1 and summary["accepted"] == 1


def test_worker_unreachable_for_five_minutes_invalidates_the_run(tmp_path):
    class Monitor:
        def start(self):
            return self

        def stop(self):
            return {"max_unreachable_s": {"http://w1": 301.0, "http://w2": 0.0}}

    summary, _ = run_pipeline(tmp_path, "G", {"m": {"impl": [{"pkg/m.py": SOL}], "review": ["ACCEPT"]}},
                              monitor=Monitor())
    assert summary["valid"] is False and any("unreachable" in x for x in summary["invalid_reasons"])


def test_run_directory_is_never_reused(tmp_path):
    run_pipeline(tmp_path, "G", {"n": {"impl": [{"pkg/n.py": SOL}], "review": ["ACCEPT"]}})
    try:
        pipeline.Pipeline("G", [], FakeOpencode({}), str(tmp_path / "run"), grade.LocalRunner())
    except FileExistsError:
        return
    raise AssertionError("an existing run directory was accepted")


def test_health_monitor_reports_the_longest_unreachable_stretch():
    now = {"t": 0.0}
    up = {"w1": [True, False, False, False, True], "w2": [True] * 5}
    calls = {"n": 0}

    def probe(url):
        return up[url][min(calls["n"], 4)]

    m = pipeline.HealthMonitor(["w1", "w2"], probe=probe, clock=lambda: now["t"])
    for i in range(5):
        calls["n"] = i
        now["t"] = i * 100.0
        m.poll_once()
    assert m.max_down == {"w1": 200.0, "w2": 0.0}


def test_an_implementer_run_that_dies_without_a_session_still_flows_through_review(tmp_path):
    script = {"q": {"impl": [{}, {"pkg/q.py": SOL}], "fail_impl_turns": 1,
                    "review": ["REVISE: nothing was changed", "ACCEPT"]}}
    summary, fake = run_pipeline(tmp_path, "G", script)
    r = record(tmp_path, "q")
    assert r["rounds"][0]["rc"] == 1 and r["rounds"][0]["session"] is None
    assert r["outcome"] == "implementer-accepted" and r["accepted"] and summary["valid"]


def test_a_diff_too_big_for_the_packet_leaves_the_full_packet_in_the_reviewers_copy(tmp_path):
    big = SOL + "".join(f"# filler line {i}\n" for i in range(3000))
    script = {"z": {"impl": [{"pkg/z.py": big}], "review": ["ACCEPT"], "expect_full_packet": True}}
    _, fake = run_pipeline(tmp_path, "G", script)
    assert script["z"]["full_packet_seen"] == [True]
    rev = [c for c in fake.calls if c["role"] == "review"][0]
    assert ".workforce_review/packet.md" in open(rev["attach"]).read().splitlines()[-1]


def test_agent_area_and_harness_private_area_are_separate_and_handed_over(tmp_path):
    given = []
    script = {"p": {"impl": [{"pkg/p.py": "x = 1\n"}, {"pkg/p.py": SOL}], "review": ["REVISE: no", "ACCEPT"]}}
    bundles = [make_bundle(str(tmp_path / "bundles"), "p")]
    fake = FakeOpencode(script)
    pipeline.Pipeline("G", bundles, fake, str(tmp_path / "run"), grade.LocalRunner(), give=given.append).run()
    run = str(tmp_path / "run")
    agent = os.path.join(run, "agent", "p")
    assert os.path.join(agent, "ws") in given                                   # workspace handed over
    assert any(g.startswith(os.path.join(agent, "review-r0")) for g in given)   # review copy handed over
    assert all(g.startswith(os.path.join(run, "agent")) for g in given)         # nothing private handed over
    for c in fake.calls:                                                         # agents only ever see the agent area
        assert c["workdir"].startswith(os.path.join(run, "agent"))
        assert c["attach"] is None or c["attach"].startswith(os.path.join(run, "agent"))
    assert os.path.isdir(os.path.join(run, "tasks", "p", "git"))                # baseline stays private
    assert os.path.isfile(os.path.join(run, "tasks", "p", "record.json"))
