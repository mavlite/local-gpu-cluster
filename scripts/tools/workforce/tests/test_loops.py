"""Loop metric over an `opencode export` session (spec §7)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import loops  # noqa: E402


def export_of(*calls):
    """A minimal `opencode export` document: one assistant message per tool call."""
    msgs = [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "go"}]}]
    for i, (tool, inp) in enumerate(calls):
        msgs.append({"info": {"role": "assistant"}, "parts": [
            {"type": "step-start"},
            {"type": "tool", "tool": tool, "state": {"status": "completed", "input": inp,
                                                    "time": {"start": i, "end": i}}},
            {"type": "step-finish"}]})
    return {"info": {"id": "ses_x"}, "messages": msgs}


W = ("write", {"filePath": "verify.py", "content": "print(1)\n"})
T = ("bash", {"command": "python -m pytest -q", "description": "tests"})


def E(path, old, new):
    return ("edit", {"filePath": path, "oldString": old, "newString": new})


def test_tool_calls_come_out_in_order_with_canonical_keys():
    calls = loops.tool_calls(export_of(W, T))
    assert [c["tool"] for c in calls] == ["write", "bash"]
    assert loops.key(calls[0]) == loops.key({"tool": "write", "input": {"content": "print(1)\n",
                                                                        "filePath": "verify.py"}})


def test_three_identical_consecutive_calls_are_a_repeat():
    eps = loops.episodes(loops.tool_calls(export_of(W, W, W, T)))
    assert eps == [{"kind": "repeat", "start": 0, "length": 3, "period": 1}]


def test_two_identical_calls_are_not_a_loop():
    assert loops.episodes(loops.tool_calls(export_of(W, W, T))) == []


def test_a_short_sequence_repeated_three_times_is_a_cycle():
    eps = loops.episodes(loops.tool_calls(export_of(W, T, W, T, W, T)))
    assert eps == [{"kind": "cycle", "start": 0, "length": 6, "period": 2}]


def test_test_runs_with_real_edits_between_them_are_not_a_loop():
    calls = [T, E("a.py", "x = 1", "x = 2"), T, E("a.py", "x = 2", "x = 3"), T, E("a.py", "x = 3", "x = 4"), T]
    assert loops.episodes(loops.tool_calls(export_of(*calls))) == []


def test_identical_test_runs_with_nothing_between_them_are_a_loop():
    assert loops.episodes(loops.tool_calls(export_of(T, T, T)))[0]["kind"] == "repeat"


def test_a_period_four_cycle_is_found_but_period_five_is_not():
    a, b, c, d, e = (("read", {"filePath": n}) for n in "abcde")
    assert loops.episodes(loops.tool_calls(export_of(*([a, b, c, d] * 3))))[0]["period"] == 4
    assert loops.episodes(loops.tool_calls(export_of(*([a, b, c, d, e] * 3)))) == []


def test_invalid_tool_calls_count_as_calls():
    bad = ("invalid", {"tool": "write", "error": "unavailable"})
    assert loops.episodes(loops.tool_calls(export_of(bad, bad, bad)))[0]["kind"] == "repeat"


def test_summary_counts_episodes_and_flags_the_session():
    s = loops.summarize(export_of(W, W, W, T, W, T, W, T, W, T))
    assert s["n_calls"] == 10 and s["looped"] is True and s["episodes"] == 2


def test_steps_per_turn_counts_assistant_messages_after_each_user_message():
    doc = {"messages": [{"info": {"role": "user"}}, {"info": {"role": "assistant"}}, {"info": {"role": "assistant"}},
                        {"info": {"role": "user"}}, {"info": {"role": "assistant"}}]}
    assert loops.steps_per_turn(doc) == [2, 1]
