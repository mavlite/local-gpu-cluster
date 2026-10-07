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


def test_a_failed_implementer_run_is_retried_without_burning_a_review_round(tmp_path):
    # Final review Important-3: opencode exits rc 1 within ~66 s when a worker is down; that turn went
    # to review, burned a round and left no trace.
    script = {"q": {"impl": [{}, {"pkg/q.py": SOL}], "fail_impl_turns": 1, "review": ["ACCEPT"]}}
    summary, fake = run_pipeline(tmp_path, "G", script, limits=pipeline.Limits(retry_wait_s=0))
    r = record(tmp_path, "q")
    assert [c["role"] for c in fake.calls] == ["impl", "impl", "review"]
    assert len(r["rounds"]) == 1 and r["rounds"][0]["rc"] == 0 and len(r["impl_failures"]) == 1
    assert r["outcome"] == "implementer-accepted" and r["accepted"] and summary["valid"]


def test_an_implementer_that_keeps_failing_is_an_infrastructure_error_that_invalidates_the_run(tmp_path):
    script = {"k": {"impl": [{}] * 5, "fail_impl_turns": 5, "review": []}}
    summary, fake = run_pipeline(tmp_path, "G", script, limits=pipeline.Limits(retry_wait_s=0, retries=2))
    r = record(tmp_path, "k")
    assert r["outcome"] == "infra-error" and not r["accepted"] and len(r["impl_failures"]) == 3
    assert [c["role"] for c in fake.calls] == ["impl"] * 3
    assert summary["valid"] is False and any("implementer runs failed" in x for x in summary["invalid_reasons"])


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
    pipeline.Pipeline("G", bundles, fake, str(tmp_path / "run"), grade.LocalRunner(),
                      own=lambda path, role: given.append(path)).run()
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


def test_a_failure_while_recording_an_error_still_ends_the_run(tmp_path):
    # Security review HIGH-3: an I/O error inside the error handler killed the thread and run() hung.
    import threading

    class Brittle(pipeline.Pipeline):
        def _write_record(self, t, outcome, *a, **kw):
            if outcome == "harness-error":
                raise OSError("disk full while recording the error")
            return super()._write_record(t, outcome, *a, **kw)

    script = {"x": {"impl": [{"pkg/x.py": SOL}], "review": ["ACCEPT"], "raise_on": "review"},
              "y": {"impl": [{"pkg/y.py": SOL}], "review": ["ACCEPT"]}}
    bundles = [make_bundle(str(tmp_path / "bundles"), tid) for tid in script]
    p = Brittle("T", bundles, FakeOpencode(script), str(tmp_path / "run"), grade.LocalRunner())
    out = {}
    th = threading.Thread(target=lambda: out.update(s=p.run()), daemon=True)
    th.start()
    th.join(60)
    assert not th.is_alive(), "run() deadlocked"
    assert out["s"]["valid"] is False and out["s"]["n_tasks"] == 2
    assert out["s"]["accepted_by_task"] == {"x": False, "y": True}


def test_each_task_gets_its_own_opencode_home_kept_across_its_rounds(tmp_path):
    # Security review HIGH-2: one shared, agent-writable opencode home for every task.
    given = []
    script = {"r": {"impl": [{"pkg/r.py": "x\n"}, {"pkg/r.py": SOL}], "review": ["REVISE: no", "ACCEPT"]},
              "s": {"impl": [{"pkg/s.py": SOL}], "review": ["ACCEPT"]}}
    bundles = [make_bundle(str(tmp_path / "bundles"), tid) for tid in script]
    fake = FakeOpencode(script)
    pipeline.Pipeline("T", bundles, fake, str(tmp_path / "run"), grade.LocalRunner(),
                      own=lambda path, role: given.append(path)).run()
    homes = {}
    for c in fake.calls:
        if c["role"] == "impl":
            homes.setdefault(c["task"], set()).add(c["home"])
    assert all(len(h) == 1 for h in homes.values()) and homes["r"] != homes["s"]      # per task, kept across rounds
    for tid, (home,) in homes.items():
        assert home.startswith(os.path.join(str(tmp_path / "run"), "agent", tid, "home-")) and home in given


class OwnRecorder:
    """Stand-in for chown: records who owns each path, and checks agents only touch their own."""

    def __init__(self, fake):
        self.owner = {}
        fake.owners = self.owner

    def __call__(self, path, role):
        self.owner[path] = role


def test_each_role_runs_as_its_own_user_and_owns_the_workspace_only_during_its_turn(tmp_path):
    # Final review Important-1: the reviewer's copy and the live workspace were siblings owned by one
    # agent user, so the lead could edit ../ws; every task's workspace was writable by every agent.
    script = {"u": {"impl": [{"pkg/u.py": "x\n"}, {"pkg/u.py": "x\n"}, {"pkg/u.py": "x\n"}],
                    "review": ["REVISE: a", "REVISE: b", "REVISE: c"], "fix": {"pkg/u.py": SOL}}}
    bundles = [make_bundle(str(tmp_path / "bundles"), "u")]
    fake = FakeOpencode(script)
    own = OwnRecorder(fake)
    pipeline.Pipeline("G", bundles, fake, str(tmp_path / "run"), grade.LocalRunner(), own=own).run()
    ws = os.path.join(str(tmp_path / "run"), "agent", "u", "ws")
    for c in fake.calls:
        expected = "lead" if c["role"] in ("review", "fix") else "impl-1"
        assert c["user"] == expected and c["owner"] == expected, c          # runs as, and owns, its role
        if c["role"] == "review":
            assert c["workdir"] != ws
    assert own.owner[ws] is None                                             # handed back to the harness
    homes = {c["role"]: c["home"] for c in fake.calls}
    assert homes["impl"] != homes["review"] == homes["fix"]


def test_review_time_tests_never_run_agent_code_outside_the_grader(tmp_path):
    # Final review CRITICAL: visible tests ran on the agent's copy as the harness user (root on the VM),
    # with the agent's own conftest.py. They now run through the grader on pristine snapshot + filtered patch.
    class Recording(grade.LocalRunner):
        def __init__(self):
            super().__init__()
            self.trees = []

        def run(self, tree, args, timeout):
            self.trees.append((tree, os.path.exists(os.path.join(tree, "conftest.py")), list(args)))
            return super().run(tree, args, timeout)

    script = {"v": {"impl": [{"pkg/v.py": SOL, "conftest.py": "raise SystemExit('agent code ran')\n"}],
                    "review": ["ACCEPT"]}}
    bundles = [make_bundle(str(tmp_path / "bundles"), "v")]
    runner = Recording()
    fake = FakeOpencode(script)
    pipeline.Pipeline("G", bundles, fake, str(tmp_path / "run"), runner).run()
    private = os.path.join(str(tmp_path / "run"), "tasks", "v")
    assert len(runner.trees) == 2                                            # review-time check + final grade
    for tree, has_conftest, args in runner.trees:
        assert tree.startswith(private) and not has_conftest and "--noconftest" in args
    rev = [c for c in fake.calls if c["role"] == "review"][0]
    assert "1 passed" in open(rev["attach"]).read()


def test_a_non_utf8_file_in_the_workspace_does_not_invalidate_the_run(tmp_path):
    # Final review Important-2: strict UTF-8 decoding of git's diff raised -> harness-error.
    script = {"b": {"impl": [{"pkg/b.py": SOL}], "review": ["ACCEPT"], "binary": {"notes.txt": "café, latin-1 text\n".encode("latin-1")}}}
    summary, _ = run_pipeline(tmp_path, "G", script)
    r = record(tmp_path, "b")
    assert r["outcome"] == "implementer-accepted" and r["accepted"] and summary["valid"]
    assert r["dropped"] == {"notes.txt": "undeclared"}


def test_duplicate_task_ids_are_refused_up_front(tmp_path):
    b1 = make_bundle(str(tmp_path / "one"), "d")
    b2 = make_bundle(str(tmp_path / "two"), "d")
    try:
        pipeline.Pipeline("G", [b1, b2], FakeOpencode({}), str(tmp_path / "run"), grade.LocalRunner())
    except ValueError:
        return
    raise AssertionError("duplicate task ids accepted")
