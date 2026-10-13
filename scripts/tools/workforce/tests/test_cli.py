"""CLI: patch import on the workstation, run preconditions, W1/W3 aggregation."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bundle  # noqa: E402
import cli  # noqa: E402
from test_bundle import SOLUTION, git, repo, taskdef  # noqa: E402,F401  (repo is a fixture)


def built(tmp_path, repo):
    return bundle.build(repo, taskdef(tmp_path, repo), str(tmp_path / "bundles"), str(tmp_path / "refs"))


def test_import_creates_a_branch_on_the_parent_without_touching_the_checkout(tmp_path, repo):
    b = built(tmp_path, repo)
    head_before = git(repo, "rev-parse", "HEAD")
    patch = tmp_path / "t1.patch"
    patch.write_bytes((tmp_path / "refs" / "t1.patch").read_bytes())      # byte-exact, no CRLF translation
    out = cli.import_patch(repo, str(patch), b, "run7")
    assert out == {"branch": "workforce/run7/t1", "paths": ["pkg/calc.py"]}
    assert git(repo, "rev-parse", "HEAD") == head_before and git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "workforce/run7/t1~1") == git(repo, "rev-parse", "HEAD~1")   # on the parent
    assert git(repo, "show", "workforce/run7/t1:pkg/calc.py") == SOLUTION.strip()
    assert "Co-Authored-By" not in git(repo, "log", "-1", "--format=%B", "workforce/run7/t1")


def test_import_refuses_a_patch_touching_undeclared_or_protected_paths(tmp_path, repo):
    b = built(tmp_path, repo)
    evil = tmp_path / "evil.patch"
    evil.write_text("diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n"
                    "+++ b/conftest.py\n@@ -0,0 +1 @@\n+import os\n")
    with pytest.raises(ValueError):
        cli.import_patch(repo, str(evil), b, "run7")
    assert "workforce/run7/t1" not in git(repo, "branch", "--list")


def test_run_requires_the_workforce_keys(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["run", "--arm", "T", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1"], env={"WF_ROUTER_KEY": "k"})


def write_run(d, **s):
    os.makedirs(d)
    base = {"n_tasks": 6, "looped": 0, "accepted": 4, "wall_s": 600.0, "valid": True,
            "accepted_by_task": {}, "accepted_per_hour": 1.0, "outcomes": {"implemented": 6}}
    base.update(s)
    with open(os.path.join(d, "run.json"), "w") as f:
        json.dump(base, f)
    return d


def test_w1_aggregates_run_directories_per_configuration(tmp_path, capsys):
    a = write_run(str(tmp_path / "c0a"))
    b = write_run(str(tmp_path / "c0b"), looped=1)
    c = write_run(str(tmp_path / "c2a"))
    stats = cli.w1_stats([a, b])
    assert stats == {"attempts": 12, "looped": 1, "passed": 8, "wall_s": 1200.0, "invalid_runs": 0}
    cli.main(["w1", "--config", f"C0={a},{b}", "--config", f"C2={c}"])
    out = json.loads(capsys.readouterr().out)
    assert out["table"]["C0"]["loop_rate"] == pytest.approx(1 / 12) and "chosen" in out


def test_w3_reads_runs_and_marks_a_run_invalid_if_either_side_says_so(tmp_path):
    good = write_run(str(tmp_path / "g"), accepted_by_task={"t1": True}, accepted_per_hour=3.0)
    bad = write_run(str(tmp_path / "t"), accepted_by_task={"t1": True}, accepted_per_hour=5.0, valid=False)
    runs = cli.w3_runs([{"arm": "G", "run_dir": good, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0},
                        {"arm": "T", "run_dir": bad, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0}])
    assert [r["valid"] for r in runs] == [True, False]
    assert runs[0]["accepted"] == {"t1": True} and runs[1]["accepted_per_hour"] == 5.0


def test_patch_paths_reads_diff_headers():
    text = "diff --git a/x/y.py b/x/y.py\n--- a/x/y.py\n+++ b/x/y.py\ndiff --git a/z b/z\n"
    assert cli.patch_paths(text) == ["x/y.py", "z"]


def test_import_works_even_when_the_repo_converts_line_endings_on_checkout(tmp_path, repo):
    b = built(tmp_path, repo)
    git(repo, "config", "core.autocrlf", "true")
    patch = tmp_path / "t1.patch"
    patch.write_bytes((tmp_path / "refs" / "t1.patch").read_bytes())
    assert cli.import_patch(repo, str(patch), b, "run8")["branch"] == "workforce/run8/t1"
    assert git(repo, "show", "workforce/run8/t1:pkg/calc.py") == SOLUTION.strip()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_an_agent_user_run_refuses_bundles_readable_by_others(tmp_path):
    b = tmp_path / "bundles"
    b.mkdir()
    os.chmod(b, 0o755)
    with pytest.raises(SystemExit):
        cli.assert_private(str(b))
    os.chmod(b, 0o700)
    cli.assert_private(str(b))


def test_patch_paths_also_reads_body_paths_that_git_apply_honours():
    # Security review LOW: the header says one path, the body another; git apply follows the body.
    text = ("diff --git a/pkg/calc.py b/pkg/calc.py\n--- a/pkg/calc.py\n+++ b/conftest.py\n"
            "diff --git a/x b/y\nrename from x\nrename to y\ncopy from p\ncopy to q\n")
    assert cli.patch_paths(text) == ["conftest.py", "p", "pkg/calc.py", "q", "x", "y"]


def test_agent_user_runs_refuse_the_local_grader(tmp_path):
    # Final review CRITICAL: the local grader runs agent code as the harness (root on the VM).
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1", "--agent-user-prefix", "wfagent"],
                 env={"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"})
    assert "grader" in str(e.value)                           # refused for the grader, not by argparse


def test_role_users():
    assert cli.role_user("wfagent", "impl-2") == "wfagent-impl-2"
    assert cli.role_user("wfagent", "lead") == "wfagent-lead"
    assert cli.role_user("wfagent", None) == "root"


def test_w1_counts_only_valid_runs_and_real_attempts(tmp_path):
    # Final review Important-4: harness-error / infra-error attempts were counted as loop-free.
    a = write_run(str(tmp_path / "a"), outcomes={"implemented": 5, "harness-error": 1}, looped=0, accepted=3)
    b = write_run(str(tmp_path / "b"), outcomes={"implemented": 6}, looped=2, accepted=4, valid=False)
    assert cli.w1_stats([a, b]) == {"attempts": 5, "looped": 0, "passed": 3, "wall_s": 600.0, "invalid_runs": 1}


def test_w3_refuses_runs_whose_tasks_do_not_match_the_bundle_set(tmp_path):
    # Final review: a name mismatch made every difference zero and the quality clause passed silently.
    (tmp_path / "bundles" / "t1").mkdir(parents=True)
    g = write_run(str(tmp_path / "g"), accepted_by_task={"other": True})
    sched = tmp_path / "s.json"
    sched.write_text(json.dumps([{"arm": "G", "run_dir": g, "valid": True, "probe_p50": 1, "baseline_p50": 1}]))
    with pytest.raises(SystemExit):
        cli.main(["w3", "--schedule", str(sched), "--tasks", str(tmp_path / "bundles")])


def test_bundle_validate_can_use_the_docker_grader(tmp_path, repo, monkeypatch, capsys):
    # Plan B: Docker grading must reproduce local grading for every bundle (Plan C Review Focus 3).
    built(tmp_path, repo)
    seen = []
    real = cli.bundle.validate
    monkeypatch.setattr(cli.bundle, "validate", lambda b, ref, work, runner: (seen.append(runner), real(
        b, ref, work, cli.grade.LocalRunner()))[1])
    cli.main(["bundle-validate", "--bundles", str(tmp_path / "bundles"), "--refs", str(tmp_path / "refs"),
              "--work", str(tmp_path / "w"), "--grader", "docker:wf-grader:1"])
    assert isinstance(seen[0], cli.grade.DockerRunner) and seen[0].image == "wf-grader:1"
    assert json.loads(capsys.readouterr().out)["grader"] == "docker:wf-grader:1"


def test_replay_reviews_runs_only_the_reviewer_with_the_lead_config(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_build(arm, oc_dir, router, workers, cmd, env, launcher=None, reviewer_model=None, keys=None, **kw):
        seen["build"] = (arm, router, workers, launcher)
        return "OC"

    def fake_replay(run, bundles, opencode, out, grader, limits=None, journal_text=None, own=None, reviewer_model=None, **kw):
        seen.update(run=run, bundles=bundles, opencode=opencode, out=out, grader=type(grader).__name__,
                    journal=journal_text, own=own)
        return {"totals": {"old_none": 0}}

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.replay, "replay", fake_replay)
    (tmp_path / "j.log").write_text("journal line\n")
    assert cli.main(["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
                     "--router", "http://x/v1", "--grader", "local", "--journal", str(tmp_path / "j.log")],
                    env={"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"}) == 0
    assert seen["build"] == ("G", "http://x/v1", [], None) and seen["opencode"] == "OC"
    assert seen["run"] == "R" and seen["grader"] == "LocalRunner" and seen["journal"] == "journal line\n"
    assert seen["own"] is None and json.loads(capsys.readouterr().out)["totals"]["old_none"] == 0


def test_replay_reviews_needs_the_router_key(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
                  "--router", "http://x/v1"], env={"WF_OPENCODE": "opencode"})
    assert "WF_ROUTER_KEY" in str(e.value)


def test_run_arm_s_needs_the_router_key_and_never_reviews(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--arm", "S", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1"], env={"WF_OPENCODE": "opencode"})
    assert "WF_ROUTER_KEY" in str(e.value)
    seen = {}

    class FakePipeline:
        def __init__(self, arm, bundles, opencode, out, grader, review=True, monitor=None, own=None, **kw):
            seen.update(arm=arm, review=review, monitor=monitor)

        def run(self):
            return {"arm": seen["arm"]}

    monkeypatch.setattr(cli, "build_opencode", lambda arm, d, router, workers, cmd, env, launcher=None, **kw: "OC")
    monkeypatch.setattr(cli.pipeline, "Pipeline", FakePipeline)
    assert cli.main(["run", "--arm", "S", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                     "--router", "http://x/v1"], env={"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"}) == 0
    assert seen == {"arm": "S", "review": False, "monitor": None}


def _round2_run(tmp_path, name, arm, wall_s, capped, task_s, gpu_ms=3.6e6, accepted=None):
    accepted = accepted if accepted is not None else {t: True for t in task_s}
    d = write_run(str(tmp_path / name), arm=arm, wall_s=wall_s, accepted=sum(accepted.values()),
                  accepted_by_task=accepted, accepted_per_hour=sum(accepted.values()) / (wall_s / 3600),
                  review_capped=capped)
    for t, s in task_s.items():
        os.makedirs(os.path.join(d, "tasks", t))
        with open(os.path.join(d, "tasks", t, "record.json"), "w") as f:
            json.dump({"id": t, "accepted": accepted[t], "outcome": "implementer-accepted",
                       "t_start": 100.0, "t_end": 100.0 + s}, f)
    with open(os.path.join(d, "meta.json"), "w") as f:
        json.dump({"valid": True, "probe_p50": 1.0, "gpu_ms": gpu_ms}, f)
    return d


def test_w3_round2_reads_the_round_2_fields_from_the_runs(tmp_path, capsys):
    tasks = {"t1": 60.0, "t2": 120.0}
    (tmp_path / "bundles" / "t1").mkdir(parents=True)
    (tmp_path / "bundles" / "t2").mkdir()
    sched = [{"arm": a, "run_dir": _round2_run(tmp_path, f"{a}{i}", a, w, 0, tasks), "valid": True,
              "probe_p50": 1.0, "baseline_p50": 1.0}
             for i, (a, w) in enumerate([("S", 1200), ("G", 1200), ("T", 600), ("T", 600), ("G", 1200), ("S", 1200)])]
    runs = cli.round2_runs(sched, ["t1", "t2"])
    assert [r["arm"] for r in runs] == ["S", "G", "T", "T", "G", "S"]
    assert runs[2]["wall_s"] == 600 and runs[2]["gpu_ms"] == 3.6e6 and runs[2]["review_capped"] == 0
    assert runs[2]["task_s"] == tasks and runs[2]["worker_s"] is None and runs[2]["accepted_per_hour"] == 12.0
    (tmp_path / "sched.json").write_text(json.dumps(sched))
    assert cli.main(["w3", "--round2", "--schedule", str(tmp_path / "sched.json"), "--tasks", str(tmp_path / "bundles")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["band"] == "build" and out["ratio"] == 2.0


def test_w3_schedule_accepts_arm_s(tmp_path, capsys):
    d = _round2_run(tmp_path, "s1", "S", 1200, 0, {"t1": 60.0})
    (tmp_path / "base.jsonl").write_text(json.dumps({"ts": 1.0, "ok": True, "latency_s": 1.0}) + "\n")
    assert cli.main(["w3-schedule", "--baseline-probe", str(tmp_path / "base.jsonl"), "--run", f"S={d}",
                     "--out", str(tmp_path / "sched.json")]) == 0
    assert json.loads((tmp_path / "sched.json").read_text())[0]["arm"] == "S"


def test_voiding_keeps_arm_s_throughput_counting_all_accepted_tasks(tmp_path):
    # Review I5: arm S records outcome "implemented"; recomputing after a void must not zero it.
    d = write_run(str(tmp_path / "s"), arm="S", review=False, accepted_by_task={"t1": True, "t2": True},
                  accepted=2, accepted_per_hour=12.0, wall_s=600.0)
    for t in ("t1", "t2"):
        os.makedirs(os.path.join(d, "tasks", t))
        with open(os.path.join(d, "tasks", t, "record.json"), "w") as f:
            json.dump({"id": t, "accepted": True, "outcome": "implemented"}, f)
    runs = cli.w3_runs([{"arm": "S", "run_dir": d, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0}],
                       ["t1", "t2", "t3"] if False else ["t1", "t2"], void=["t2"])
    assert runs[0]["accepted"] == {"t1": True} and runs[0]["accepted_per_hour"] == 6.0


def test_round2_runs_carry_the_reviewer_health_fields(tmp_path):
    tasks = {"t1": 60.0}
    d = _round2_run(tmp_path, "g1", "G", 1200, 2, tasks)
    with open(os.path.join(d, "run.json")) as f:
        s = json.load(f)
    s.update(review_timed_out=1, review_none_after_turn=1)
    with open(os.path.join(d, "run.json"), "w") as f:
        json.dump(s, f)
    r = cli.round2_runs([{"arm": "G", "run_dir": d, "valid": True, "probe_p50": 1.0, "baseline_p50": 1.0}], ["t1"])[0]
    assert (r["review_capped"], r["review_timed_out"], r["review_none_after_turn"]) == (2, 1, 1)


def test_replay_reviews_with_a_worker_reviewer_needs_the_worker_key_and_passes_both(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_build(arm, oc_dir, router, workers, cmd, env, launcher=None, reviewer_model=None, keys=None, **kw):
        seen.update(arm=arm, workers=workers, reviewer_model=reviewer_model, keys=keys)
        return "OC"

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.replay, "replay", lambda *a, **k: {"totals": {}, "reviewer_model": k.get("reviewer_model")})
    args = ["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
            "--router", "http://x/v1", "--worker", "http://w/v1", "--reviewer-model", "w1/qwen3.6"]
    with pytest.raises(SystemExit) as e:
        cli.main(args, env={"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"})
    assert "WF_WORKER_KEY" in str(e.value)
    assert cli.main(args, env={"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w", "WF_OPENCODE": "opencode"}) == 0
    assert seen == {"arm": "G", "workers": ["http://w/v1"], "reviewer_model": "w1/qwen3.6", "keys": ("WF_ROUTER_KEY", "WF_WORKER_KEY")}
    assert json.loads(capsys.readouterr().out)["reviewer_model"] == "w1/qwen3.6"


def test_replay_reviews_read_first_flag_reaches_the_config_and_the_record(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_build(arm, oc_dir, router, workers, cmd, env, launcher=None, reviewer_model=None, keys=None, read_first=False, **kw):
        seen["read_first"] = read_first
        return "OC"

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.replay, "replay", lambda *a, **k: {"totals": {}, "read_first": k.get("read_first")})
    args = ["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
            "--router", "http://x/v1", "--read-first"]
    assert cli.main(args, env={"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"}) == 0
    assert seen["read_first"] is True and json.loads(capsys.readouterr().out)["read_first"] is True


def test_replay_reviews_worker_alias_flag_reaches_the_config(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_build(arm, oc_dir, router, workers, cmd, env, launcher=None, **kw):
        seen.update(kw)
        return "OC"

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.replay, "replay", lambda *a, **k: {"totals": {}})
    args = ["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
            "--router", "http://x/v1", "--worker", "http://w/v1", "--reviewer-model", "w1/gemma4", "--worker-alias", "gemma4"]
    assert cli.main(args, env={"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w", "WF_OPENCODE": "opencode"}) == 0
    assert seen["worker_alias"] == "gemma4" and seen["reviewer_model"] == "w1/gemma4"


def test_replay_reviews_review_steps_flag_reaches_config_and_pipeline(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_build(arm, oc_dir, router, workers, cmd, env, launcher=None, **kw):
        seen["build"] = kw
        return "OC"

    def fake_replay(*a, **k):
        seen["replay"] = k
        return {"totals": {}, "review_steps": k.get("review_steps")}

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.replay, "replay", fake_replay)
    args = ["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
            "--router", "http://x/v1", "--review-steps", "30"]
    assert cli.main(args, env={"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"}) == 0
    assert seen["build"]["review_steps"] == 30 and seen["replay"]["review_steps"] == 30
    assert json.loads(capsys.readouterr().out)["review_steps"] == 30


def test_run_lead_url_needs_the_worker_key_and_reaches_config_and_pipeline(tmp_path, monkeypatch):
    seen = {}

    class FakePipeline:
        def __init__(self, arm, bundles, opencode, out, grader, review=True, monitor=None, own=None, **kw):
            seen.update(arm=arm, review=review, **kw)

        def run(self):
            return {"arm": seen["arm"]}

    def fake_build(arm, oc_dir, router, workers, cmd, env, launcher=None, **kw):
        seen["build"] = kw
        return "OC"

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.pipeline, "Pipeline", FakePipeline)
    args = ["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"), "--router", "http://x/v1",
            "--lead-url", "http://192.168.6.151:8090/v1", "--read-first", "--review-steps", "30"]
    with pytest.raises(SystemExit) as e:
        cli.main(args, env={"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"})
    assert "WF_WORKER_KEY" in str(e.value)
    assert cli.main(args, env={"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w", "WF_OPENCODE": "opencode"}) == 0
    b = seen["build"]
    assert b["lead_url"] == "http://192.168.6.151:8090/v1" and b["lead_alias"] is None and b["read_first"] is True
    assert b["review_steps"] == 30 and seen["review_steps"] == 30


def test_quality_flags_reach_config_pipeline_and_replay(tmp_path, monkeypatch):
    seen = {}

    class FakePipeline:
        def __init__(self, arm, bundles, opencode, out, grader, review=True, monitor=None, own=None, **kw):
            seen["pipeline"] = kw

        def run(self):
            return {}

    def fake_build(arm, oc_dir, router, workers, cmd, env, launcher=None, **kw):
        seen["build"] = kw
        return "OC"

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.pipeline, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli.replay, "replay", lambda *a, **k: seen.update(replay=k) or {"totals": {}})
    env = {"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"}
    assert cli.main(["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"), "--router", "http://x/v1",
                     "--enforce-read", "--reviewer-temperature", "0.2"], env=env) == 0
    assert seen["build"]["reviewer_temperature"] == 0.2 and seen["pipeline"]["enforce_read"] is True
    assert cli.main(["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
                     "--router", "http://x/v1", "--enforce-read", "--reviewer-temperature", "0.2"], env=env) == 0
    assert seen["build"]["reviewer_temperature"] == 0.2 and seen["replay"]["enforce_read"] is True


def test_defects_only_flag_reaches_run_and_replay_configs(tmp_path, monkeypatch):
    seen = []

    class FakePipeline:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            return {}

    monkeypatch.setattr(cli, "build_opencode", lambda arm, d, router, workers, cmd, env, launcher=None, **kw: seen.append(kw) or "OC")
    monkeypatch.setattr(cli.pipeline, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli.replay, "replay", lambda *a, **k: {"totals": {}})
    env = {"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"}
    assert cli.main(["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"), "--router", "http://x/v1",
                     "--defects-only"], env=env) == 0
    assert cli.main(["replay-reviews", "--run", "R", "--bundles", str(tmp_path), "--out", str(tmp_path / "o"),
                     "--router", "http://x/v1", "--defects-only"], env=env) == 0
    assert [kw["defects_only"] for kw in seen] == [True, True]


def test_run_review_budget_reaches_the_pipeline(tmp_path, monkeypatch):
    seen = {}

    class FakePipeline:
        def __init__(self, arm, bundles, opencode, out, grader, review=True, monitor=None, own=None, **kw):
            seen.update(kw)

        def run(self):
            return {}

    monkeypatch.setattr(cli, "build_opencode", lambda *a, **k: "OC")
    monkeypatch.setattr(cli.pipeline, "Pipeline", FakePipeline)
    env = {"WF_ROUTER_KEY": "k", "WF_OPENCODE": "opencode"}
    assert cli.main(["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                     "--router", "http://x/v1", "--review-budget", "1"], env=env) == 0
    assert seen["review_budget"] == 1
    assert cli.main(["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r2"),
                     "--router", "http://x/v1"], env=env) == 0
    assert seen["review_budget"] is None


def test_run_review_pool_wires_lanes_monitor_and_both_keys(tmp_path, monkeypatch):
    seen = {}

    class FakePipeline:
        def __init__(self, arm, bundles, opencode, out, grader, review=True, monitor=None, own=None, **kw):
            seen.update(arm=arm, review=review, monitor=monitor, lanes=kw.get("review_lanes"),
                        budget=kw.get("review_budget"))

        def run(self):
            return {"arm": seen["arm"]}

    def fake_build(arm, d, router, workers, cmd, env, launcher=None, **kw):
        seen.update(build_pool=kw.get("review_pool"), build_workers=list(workers))
        return "OC"

    monkeypatch.setattr(cli, "build_opencode", fake_build)
    monkeypatch.setattr(cli.pipeline, "Pipeline", FakePipeline)
    w = ["http://172.16.10.205:8090/v1", "http://172.16.10.206:8090/v1"]
    argv = ["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"), "--router", "http://x/v1",
            "--review-pool", "--worker", w[0], "--worker", w[1], "--review-budget", "1"]
    assert cli.main(argv, env={"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w", "WF_OPENCODE": "opencode"}) == 0
    assert seen["build_pool"] is True and seen["build_workers"] == w
    assert seen["lanes"] == [("w1", w[0]), ("w2", w[1])] and seen["budget"] == 1
    assert isinstance(seen["monitor"], cli.pipeline.HealthMonitor) and seen["review"] is True


@pytest.mark.parametrize("extra,env,msg", [
    (["--review-pool", "--worker", "http://w/v1"], {"WF_ROUTER_KEY": "k"}, "WF_WORKER_KEY"),
    (["--review-pool"], {"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w"}, "--worker"),
])
def test_run_review_pool_refuses_missing_inputs(tmp_path, extra, env, msg):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--arm", "G", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1", *extra], env=dict(env, WF_OPENCODE="opencode"))
    assert msg in str(e.value)


def test_run_review_pool_is_arm_g_only(tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--arm", "S", "--bundles", str(tmp_path), "--out", str(tmp_path / "r"),
                  "--router", "http://x/v1", "--review-pool", "--worker", "http://w/v1"],
                 env={"WF_ROUTER_KEY": "k", "WF_WORKER_KEY": "w", "WF_OPENCODE": "opencode"})
    assert "arm G" in str(e.value)
