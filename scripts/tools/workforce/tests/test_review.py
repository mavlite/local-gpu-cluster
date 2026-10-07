"""Review protocol messages and verdict parsing (spec §6)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import review  # noqa: E402

TASK = {"id": "t1", "files": ["pkg/calc.py"], "tests": ["pkg/tests/test_calc.py"]}


def test_accept_and_revise_are_parsed_from_the_first_verdict_line():
    assert review.parse_verdict("ACCEPT\nLooks right.") == ("ACCEPT", "")
    assert review.parse_verdict("**ACCEPT**") == ("ACCEPT", "")
    assert review.parse_verdict("Checked.\nREVISE: handle negatives\nand zero") == \
        ("REVISE", "handle negatives\nand zero")


def test_no_verdict_is_reported_as_none():
    kind, text = review.parse_verdict("I think it is mostly fine")
    assert kind == "NONE" and "mostly fine" in text
    assert review.parse_verdict("") == ("NONE", "")


def test_a_word_that_merely_starts_with_accept_is_not_a_verdict():
    assert review.parse_verdict("ACCEPTABLE but REVISE nothing")[0] == "NONE"


def test_implement_message_names_scope_and_tests():
    m = review.implement_message(TASK, "Make add() add.")
    assert "Make add() add." in m and "pkg/calc.py" in m and "python3 -m pytest -q pkg/tests/test_calc.py" in m


def test_packet_is_truncated_below_opencodes_attachment_limit_with_a_pointer():
    diff = "".join(f"+line {i}\n" for i in range(5000))
    text, truncated = review.packet(TASK, "req", diff, "1 failed")
    assert truncated and len(text.splitlines()) <= review.MAX_ATTACH_LINES + 2
    assert review.FULL_PACKET in text.splitlines()[-1]


def test_small_packet_holds_every_section():
    text, truncated = review.packet(TASK, "the request", "+x\n", "1 passed", history=["REVISE: a"])
    assert not truncated
    for part in ("the request", "+x", "1 passed", "REVISE: a", "pkg/calc.py"):
        assert part in text


def test_revise_message_is_capped():
    m = review.revise_message("x" * 50000)
    assert m.startswith("REVISE:") and len(m) <= review.MAX_FEEDBACK + 200
