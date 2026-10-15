"""escalation.py -- budgeted, brief-only Claude escalation (spec 2026-10-15-claude-escalation-broker-design.md)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import escalation  # noqa: E402

GOOD = """## Goal
Make the parser accept trailing commas.
## Context
Python 3.12, no third-party deps.
## Reproduce
python -m pytest tests/test_parse.py -q -> 1 failed
## Exact error
Traceback (most recent call last):
  File "parse.py", line 12, in parse
ValueError: unexpected ','
## Code
parse.py:1-20
def parse(s): ...
## What was tried
- strip trailing comma before split -> breaks quoted commas
## Question
What is the root cause and the minimal diff?
"""


def test_handles_only_the_three_aliases():
    assert escalation.handles("claude-ask") and escalation.handles("claude-auto") and escalation.handles("claude-hard")
    assert not escalation.handles("qwen3.8-nothink") and not escalation.handles(None) and not escalation.handles(3)


def test_config_defaults_and_overrides():
    c = escalation.load_config({})
    assert c.month_cap == 150.0 and c.day_cap == 7.0
    assert c.shared_daily_calls == 15 and c.auto_daily_calls == 5 and c.hard_daily_calls == 3
    assert c.models == {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
    assert c.prices == {} and c.key_file == "/etc/router-anthropic.key"
    c2 = escalation.load_config({"ESCALATION_PRICES": '{"m": [3, 15]}', "ESCALATION_DAY_CAP_USD": "2.5"})
    assert c2.prices == {"m": (3.0, 15.0)} and c2.day_cap == 2.5


def test_estimate_overcounts_bytes_over_three():
    assert escalation.estimate_tokens("abcdef") == 2
    assert escalation.estimate_tokens("abcdefg") == 3
    assert escalation.estimate_tokens("é") == 1                       # 2 utf-8 bytes


def test_brief_is_the_last_user_message_string_or_text_parts():
    body = {"messages": [{"role": "system", "content": "huge opencode prompt"},
                         {"role": "user", "content": "old turn"},
                         {"role": "assistant", "content": "x"},
                         {"role": "user", "content": [{"type": "text", "text": "part one"},
                                                      {"type": "image_url", "image_url": {"url": "data:"}},
                                                      {"type": "text", "text": "part two"}]}]}
    assert escalation.brief_text(body) == "part one\npart two"
    assert escalation.brief_text({"messages": [{"role": "user", "content": "s"}]}) == "s"
    assert escalation.brief_text({"messages": [{"role": "system", "content": "s"}]}) is None
    assert escalation.brief_text({}) is None


def test_valid_brief_passes():
    assert escalation.validate_brief(GOOD, "claude-ask") is None


def test_missing_or_empty_section_is_422():
    r = escalation.validate_brief(GOOD.replace("## Reproduce", "## Repro"), "claude-ask")
    assert r == (422, "brief_section", "brief: missing or empty section Reproduce")
    empty_code = GOOD.replace("parse.py:1-20\ndef parse(s): ...\n", "")
    assert escalation.validate_brief(empty_code, "claude-ask")[2] == "brief: missing or empty section Code"


def test_na_allowed_except_goal_error_question():
    assert escalation.validate_brief(GOOD.replace("Python 3.12, no third-party deps.", "n/a: trivial"), "claude-ask") is None
    na_err = GOOD.replace("Traceback (most recent call last):\n  File \"parse.py\", line 12, in parse\nValueError: unexpected ','",
                          "n/a: no error")
    assert escalation.validate_brief(na_err, "claude-ask")[2] == "brief: missing or empty section Exact error"


def test_oversized_brief_is_413_with_numbers():
    big = GOOD + "x" * (8192 * 3)
    status, code, msg = escalation.validate_brief(big, "claude-ask")
    assert status == 413 and code == "brief_too_large"
    assert msg.startswith("brief: ") and "over the 8192 limit -- trim it" in msg
    assert escalation.validate_brief(big, "claude-hard") is None       # Opus allows 16384


def test_request_is_only_system_prompt_plus_brief():
    req = escalation.build_request("claude-sonnet-5-5", GOOD, 2048)
    assert req == {"model": "claude-sonnet-5-5", "max_tokens": 2048, "system": escalation.SYSTEM_PROMPT,
                   "messages": [{"role": "user", "content": GOOD}]}


def test_error_body_shape():
    assert escalation.error_body("budget", "m") == {"error": {"type": "escalation", "code": "budget", "message": "m"}}


# --- fix round 1 ---

def test_helper_heading_inside_code_is_content():
    text = GOOD.replace("## Code\nparse.py:1-20", "## Code\n## helper\nparse.py:1-20")
    assert escalation.parse_sections(text)["Code"].startswith("## helper")
    assert escalation.validate_brief(text, "claude-ask") is None


def test_reproduce_heading_inside_fence_does_not_count():
    no_repro = GOOD.replace("## Reproduce\npython -m pytest tests/test_parse.py -q -> 1 failed\n", "")
    fenced = no_repro.replace("parse.py:1-20\n", "parse.py:1-20\n```\n## Reproduce\nrun it\n```\n")
    assert escalation.validate_brief(fenced, "claude-ask") == (422, "brief_section",
                                                                "brief: missing or empty section Reproduce")


def test_unknown_heading_mid_code_stays_in_code():
    text = GOOD.replace("parse.py:1-20\n", "parse.py:1-20\n## foo\n")
    secs = escalation.parse_sections(text)
    assert "foo" not in secs
    assert secs["Code"] == "parse.py:1-20\n## foo\ndef parse(s): ..."
    assert escalation.validate_brief(text, "claude-ask") is None


def test_na_is_strict_about_its_form():
    exact_error = "Traceback (most recent call last):\n  File \"parse.py\", line 12, in parse\nValueError: unexpected ','"
    assert escalation.validate_brief(GOOD.replace(exact_error, "N/A: none"), "claude-ask") == (
        422, "brief_section", "brief: missing or empty section Exact error")
    assert escalation.validate_brief(GOOD.replace("Make the parser accept trailing commas.", "**n/a**"),
                                     "claude-ask") == (422, "brief_section", "brief: missing or empty section Goal")
    assert escalation.validate_brief(GOOD.replace("Make the parser accept trailing commas.",
                                                  "N/A values crash the parser"), "claude-ask") is None


def test_non_string_text_part_is_skipped():
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": None},
                                                      {"type": "text", "text": "ok"}]}]}
    assert escalation.brief_text(body) == "ok"


def test_lone_surrogate_does_not_raise():
    assert escalation.estimate_tokens("\ud800") == 1


@pytest.mark.parametrize("env", [
    {"ESCALATION_MONTH_CAP_USD": "nan"},
    {"ESCALATION_DAY_CAP_USD": "-1"},
    {"ESCALATION_DAY_CAP_USD": "inf"},
    {"ESCALATION_PRICES": '{"m": [NaN, 1]}'},
    {"ESCALATION_PRICES": '{"m": [3, -1]}'},
    {"ESCALATION_DAILY_CALLS": "-1"},
])
def test_config_rejects_non_finite_or_negative(env):
    with pytest.raises(ValueError):
        escalation.load_config(env)


def _padded(total_bytes):
    pad = total_bytes - len(GOOD.encode("utf-8"))
    return GOOD.replace("What is the root cause and the minimal diff?",
                        "What is the root cause and the minimal diff?" + "x" * pad)


def test_413_boundary_is_exact():
    assert escalation.validate_brief(_padded(8192 * 3), "claude-ask") is None
    assert escalation.validate_brief(_padded(8192 * 3 + 1), "claude-ask")[1] == "brief_too_large"
