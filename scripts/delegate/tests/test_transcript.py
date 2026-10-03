"""Tests for the transcript token-meter (pure, fixture-driven)."""
import json

import pytest

from scripts.delegate import transcript as tx


# --- fixture builders: lines in the Claude Code transcript JSONL shape ---

def _usage(n):
    # Distinct per-component so a wrong window is visible in every field.
    return {"input_tokens": n, "output_tokens": n * 2,
            "cache_read_input_tokens": n * 3, "cache_creation_input_tokens": n * 4}


def _assistant(n, tool_uses=None):
    content = [{"type": "text", "text": "x"}]
    for name, tid, inp in tool_uses or []:
        content.append({"type": "tool_use", "name": name, "id": tid, "input": inp})
    return json.dumps({"type": "assistant", "message": {"usage": _usage(n), "content": content}})


def _tool_result(tid, payload):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": json.dumps(payload)}]}})


def _write(tmp_path, *lines):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


MCP = "mcp__local-delegate__"


def test_delegated_counts_submit_and_review_skips_background(tmp_path):
    path = _write(
        tmp_path,
        _assistant(10, [(MCP + "submit_task", "t1", {"task": "x", "repo": "r"})]),
        _tool_result("t1", {"job_id": "A"}),
        _assistant(100),  # background work while the job runs — must NOT count
        _assistant(200),  # more background — must NOT count
        _assistant(5, [(MCP + "result", "t2", {"job_id": "A"})]),
        _assistant(7),    # reviewing the diff — counts
        _assistant(3, [(MCP + "record_review", "t3", {"job_id": "A"})]),
    )
    got = tx.measure_delegated(path, "A")
    # counted turns: 10 (submit) + 5 (result) + 7 (review) + 3 (record) = 25
    assert got["input"] == 25
    assert got["output"] == 50
    assert got["cache_read"] == 75
    assert got["cache_creation"] == 100
    assert got["total"] == 25 + 50 + 75 + 100


def test_delegated_two_concurrent_jobs_scoped_separately(tmp_path):
    path = _write(
        tmp_path,
        _assistant(10, [(MCP + "submit_task", "s1", {"task": "a", "repo": "r"})]),
        _tool_result("s1", {"job_id": "A"}),
        _assistant(20, [(MCP + "submit_task", "s2", {"task": "b", "repo": "r"})]),
        _tool_result("s2", {"job_id": "B"}),
        _assistant(5, [(MCP + "result", "r1", {"job_id": "A"})]),
        _assistant(3, [(MCP + "record_review", "rr1", {"job_id": "A"})]),
        _assistant(9, [(MCP + "result", "r2", {"job_id": "B"})]),
        _assistant(4, [(MCP + "record_review", "rr2", {"job_id": "B"})]),
    )
    # Job A: submit(10) + result(5) + record(3) = 18 input. B's submit(20) is between
    # A's submit and A's result but is a different job — must be excluded.
    assert tx.measure_delegated(path, "A")["input"] == 18
    # Job B: submit(20) + result(9) + record(4) = 33 input.
    assert tx.measure_delegated(path, "B")["input"] == 33


def test_delegated_review_not_yet_flushed_uses_last_turn(tmp_path):
    # Service computes at record_review time; that turn may be the last line present.
    path = _write(
        tmp_path,
        _assistant(10, [(MCP + "submit_task", "t1", {"task": "x", "repo": "r"})]),
        _tool_result("t1", {"job_id": "A"}),
        _assistant(100),  # background
        _assistant(5, [(MCP + "result", "t2", {"job_id": "A"})]),
        _assistant(7),    # review turn, record_review tool_use not yet on disk
    )
    # submit(10) + result(5) + trailing review(7) = 22; background(100) excluded.
    assert tx.measure_delegated(path, "A")["input"] == 22


def test_selfdone_contiguous_window_by_marker(tmp_path):
    path = _write(
        tmp_path,
        _assistant(99),   # unrelated earlier work — must NOT count
        _assistant(10, [(MCP + "mark_start", "m1", {"task_type": "refactor"})]),
        _tool_result("m1", {"marker_id": "M"}),
        _assistant(20),   # doing the task — counts
        _assistant(30),   # doing the task — counts
        _assistant(5, [(MCP + "record_direct", "d1", {"task_type": "refactor", "marker_id": "M"})]),
    )
    # 10 + 20 + 30 + 5 = 65 input; the earlier 99 is before mark_start.
    assert tx.measure_selfdone(path, "M")["input"] == 65


def test_selfdone_no_marker_uses_last_mark_start_to_end(tmp_path):
    path = _write(
        tmp_path,
        _assistant(10, [(MCP + "mark_start", "m1", {"task_type": "x"})]),
        _tool_result("m1", {"marker_id": "M"}),
        _assistant(40),
        _assistant(5, [(MCP + "record_direct", "d1", {"task_type": "x"})]),
    )
    assert tx.measure_selfdone(path)["input"] == 55


def test_missing_anchor_raises(tmp_path):
    path = _write(tmp_path, _assistant(10), _assistant(20))
    with pytest.raises(ValueError):
        tx.measure_delegated(path, "A")
    with pytest.raises(ValueError):
        tx.measure_selfdone(path, "M")


def test_find_transcript_by_session_id(tmp_path):
    proj = tmp_path / "projects" / "c--some-repo"
    proj.mkdir(parents=True)
    (proj / "sess-123.jsonl").write_text("{}\n", encoding="utf-8")
    (proj / "other.jsonl").write_text("{}\n", encoding="utf-8")
    got = tx.find_transcript(session_id="sess-123", projects_dir=str(tmp_path / "projects"))
    assert got.endswith("sess-123.jsonl")


def test_find_transcript_newest_fallback(tmp_path):
    import os
    import time
    proj = tmp_path / "projects" / "c--some-repo"
    proj.mkdir(parents=True)
    old = proj / "old.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    new = proj / "new.jsonl"
    new.write_text("{}\n", encoding="utf-8")
    os.utime(str(old), (time.time() - 100, time.time() - 100))
    got = tx.find_transcript(projects_dir=str(tmp_path / "projects"))
    assert got.endswith("new.jsonl")


def test_malformed_lines_are_skipped(tmp_path):
    path = _write(
        tmp_path,
        "not json at all",
        _assistant(10, [(MCP + "mark_start", "m1", {"task_type": "x"})]),
        _tool_result("m1", {"marker_id": "M"}),
        _assistant(5, [(MCP + "record_direct", "d1", {"marker_id": "M"})]),
    )
    assert tx.measure_selfdone(path, "M")["input"] == 15
