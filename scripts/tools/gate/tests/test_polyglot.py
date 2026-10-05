import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import polyglot  # noqa: E402


def put(run_dir, name, outcomes, **extra):
    d = run_dir / "python" / "exercises" / "practice" / name
    d.mkdir(parents=True)
    # shape copied from LXC 158 2026-09-06 py34-q38nothink/forth
    rec = {"testcase": name, "model": "openai/qwen3.8-nothink", "edit_format": "whole",
           "tests_outcomes": outcomes, "duration": 10.0, "num_malformed_responses": 0,
           "num_exhausted_context_windows": 0}
    rec.update(extra)
    (d / ".aider.results.json").write_text(json.dumps(rec))


def test_pass_at_1_and_2(tmp_path):
    put(tmp_path, "a", [True])
    put(tmp_path, "b", [False, True])
    put(tmp_path, "c", [False, False])
    s = polyglot.summarize(str(tmp_path), ["a", "b", "c", "d"])
    assert s["pass1"] == 0.25 and s["pass2"] == 0.5 and s["passed2"] == 2
    assert s["missing"] == ["d"]                      # crashed exercise counts as a fail


def test_empty_outcomes_is_fail(tmp_path):
    put(tmp_path, "a", [])
    assert polyglot.summarize(str(tmp_path), ["a"])["pass2"] == 0


def test_counts_malformed_and_exhausted(tmp_path):
    put(tmp_path, "a", [False, False], num_malformed_responses=2, num_exhausted_context_windows=1)
    s = polyglot.summarize(str(tmp_path), ["a"])
    assert s["malformed"] == 2 and s["exhausted_ctx"] == 1


def test_rejects_results_outside_frozen_set_and_empty_list(tmp_path):
    put(tmp_path, "zzz", [True])
    with pytest.raises(ValueError):
        polyglot.summarize(str(tmp_path), ["a"])
    with pytest.raises(ValueError):
        polyglot.summarize(str(tmp_path), [])
