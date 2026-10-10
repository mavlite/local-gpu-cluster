"""Offline reviewer replay (round 2 §5.1): only the reviewer (+ verdict turn) re-runs on a harvested run."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import grade  # noqa: E402
import replay  # noqa: E402
import workspace  # noqa: E402
from wf_fixtures import FakeOpencode, make_bundle, solution_src  # noqa: E402

BANNER = ("MAXIMUM STEPS REACHED (20). Respond with text ONLY. Do not call any tools.\n"
          "I verified the diff and ran the tests; summarising my findings so far.")


def _patch(tmp_path, bundle, tid, src):
    ws = workspace.Workspace.materialize(os.path.join(bundle, "snapshot.tar"), str(tmp_path / "p" / tid / "ws"),
                                         str(tmp_path / "p" / tid / "git"))
    with open(os.path.join(ws.root, "pkg", f"{tid}.py"), "w") as f:
        f.write(src)
    return ws.patch([f"pkg/{tid}.py"])


def _record(run, tid, verdicts, outcome):
    d = run / "tasks" / tid
    d.mkdir(parents=True)
    rec = {"id": tid, "arm": "T", "implementer": "impl-1", "outcome": outcome,
           "rounds": [{"round": k, "summary": f"SUMMARY: round {k} of {tid}"} for k in range(len(verdicts))],
           "reviews": [{"round": k, "verdict": v} for k, v in enumerate(verdicts)]}
    with open(d / "record.json", "w") as f:
        json.dump(rec, f)
    return d


def harvested_run(tmp_path):
    bundles = {t: make_bundle(str(tmp_path / "bundles"), t) for t in ("a", "b", "c")}
    run = tmp_path / "harvested"
    (run / "patches").mkdir(parents=True)
    # a: one NONE round whose state the round-2 harness stored per round
    d = _record(run, "a", ["NONE"], "lead-fixed")
    with open(d / "impl-r0.patch", "wb") as f:
        f.write(_patch(tmp_path, bundles["a"], "a", solution_src()))
    # b: accepted on review 0, so the final patch IS that round's state
    _record(run, "b", ["ACCEPT"], "implementer-accepted")
    with open(run / "patches" / "b.patch", "wb") as f:
        f.write(_patch(tmp_path, bundles["b"], "b", solution_src()))
    # c: lead-fixed with no per-round patch: the final patch is the fixer's, not the reviewed state
    _record(run, "c", ["REVISE"], "lead-fixed")
    with open(run / "patches" / "c.patch", "wb") as f:
        f.write(_patch(tmp_path, bundles["c"], "c", solution_src()))
    return run, str(tmp_path / "bundles")


def test_replay_reruns_only_the_reviewer_on_each_stored_round(tmp_path):
    run, bundles = harvested_run(tmp_path)
    fake = FakeOpencode({"a": {"review": [{"text": BANNER, "steps": 20}], "verdict": ["ACCEPT"]},
                         "b": {"review": ["ACCEPT"]}})
    out = replay.replay(str(run), bundles, fake, str(tmp_path / "out"), grade.LocalRunner())
    assert [c["role"] for c in fake.calls] == ["review", "verdict", "review"]
    rounds = {(r["task"], r["round"]): r for r in out["rounds"]}
    assert set(rounds) == {("a", 0), ("b", 0)} and out["skipped"] == [{"task": "c", "round": 0}]
    a, b = rounds[("a", 0)], rounds[("b", 0)]
    assert a["old_verdict"] == "NONE" and a["new_verdict"] == "ACCEPT" and a["capped"] and a["steps"] == 20
    assert a["verdict_turn"]["verdict"] == "ACCEPT" and a["prompt_tokens"] is None
    assert b["old_verdict"] == b["new_verdict"] == "ACCEPT" and not b["capped"] and b["verdict_turn"] is None
    t = out["totals"]
    assert t["old_none"] == 1 and t["new_none_after_turn"] == 0 and t["agreement"] == 1.0
    assert t["median_steps"] == 10.0 and t["median_prompt_tokens"] is None     # 20 and 0 (a plain fake review)
    with open(tmp_path / "out" / "replay.json") as f:
        assert json.load(f)["totals"] == t


def test_replay_packet_carries_the_stored_summary_and_the_patched_diff(tmp_path):
    run, bundles = harvested_run(tmp_path)
    fake = FakeOpencode({"a": {"review": ["ACCEPT"]}, "b": {"review": ["REVISE: no"]}})
    out = replay.replay(str(run), bundles, fake, str(tmp_path / "out"), grade.LocalRunner())
    pkt = open([c for c in fake.calls if c["task"] == "a"][0]["attach"]).read()
    assert "SUMMARY: round 0 of a" in pkt and "+    return x * 2" in pkt and "visible tests: 1 of 1" in pkt
    assert out["totals"]["agreement"] == 0.0            # b disagrees: ACCEPT before, REVISE now


def test_journal_gives_prompt_token_totals_not_per_round_attribution(tmp_path):
    run, bundles = harvested_run(tmp_path)
    fake = FakeOpencode({"a": {"review": ["ACCEPT"]}, "b": {"review": ["ACCEPT"]}})
    journal = ("35.18.961.553 I slot print_timing: id  1 | task 98 | prompt eval time =     244.42 ms /     4 tokens (x)\n"
               "35.19.961.553 I slot print_timing: id  1 | task 99 | prompt eval time =    9000.00 ms /  9000 tokens (x)\n"
               "35.20.961.553 I slot print_timing: id  1 | task 100 | prompt eval time =   3000.00 ms /  3000 tokens (x)\n")
    out = replay.replay(str(run), bundles, fake, str(tmp_path / "out"), grade.LocalRunner(), journal_text=journal)
    t = out["totals"]
    assert t["median_prompt_tokens"] == 6000 and t["prompt_tokens_total"] == 12004 and t["requests"] == 2
    assert all(r["prompt_tokens"] is None for r in out["rounds"])
