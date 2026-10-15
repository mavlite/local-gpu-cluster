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


import json as _json
import stat as _stat
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI

UTC = "UTC"
PRICES = '{"claude-sonnet-5-5": [3, 15], "claude-opus-5-5": [15, 75]}'


def cfg(tmp_path, **env):
    base = {"ESCALATION_PRICES": PRICES, "ESCALATION_TZ": UTC,
            "ESCALATION_LEDGER": str(tmp_path / "ledger.jsonl")}
    base.update(env)
    return escalation.load_config(base)


def ts(y, mo, d, h=12, tz=UTC):
    return _dt(y, mo, d, h, tzinfo=_ZI(tz)).timestamp()


def test_period_bounds_day_and_month_in_the_configured_tz():
    d0, m0, nd, nm = escalation.period_bounds(ts(2026, 10, 15, 9), UTC)
    assert d0 == ts(2026, 10, 15, 0) and m0 == ts(2026, 10, 1, 0)
    assert nd.timestamp() == ts(2026, 10, 16, 0) and nm.timestamp() == ts(2026, 11, 1, 0)
    d0b, _, _, _ = escalation.period_bounds(ts(2026, 10, 15, 2), "America/New_York")
    assert d0b == ts(2026, 10, 14, 0, "America/New_York")             # 02:00 UTC is still the 14th in New York


def test_costs():
    c = escalation.load_config({"ESCALATION_PRICES": PRICES})
    assert abs(escalation.call_cost(c, "claude-sonnet-5-5", 1_000_000, 0) - 3.0) < 1e-9
    assert abs(escalation.worst_cost(c, "claude-sonnet-5-5", 800, 2048) - ((1000 * 3 + 2048 * 15) / 1e6)) < 1e-12


def test_ledger_appends_0600_and_sums_by_period(tmp_path):
    c = cfg(tmp_path)
    led = escalation.Ledger(c.ledger_path)
    led.append({"ts": ts(2026, 9, 30), "alias": "claude-ask", "cost_usd": 5.0, "outcome": "ok"})
    led.append({"ts": ts(2026, 10, 14), "alias": "claude-ask", "cost_usd": 1.0, "outcome": "ok"})
    led.append({"ts": ts(2026, 10, 15, 8), "alias": "claude-auto", "cost_usd": 0.25, "outcome": "ok"})
    led.append({"ts": ts(2026, 10, 15, 9), "alias": "claude-auto", "cost_usd": 0.0, "outcome": "refused:budget"})
    led.append({"ts": ts(2026, 10, 15, 10), "alias": "claude-hard", "cost_usd": 0.0, "outcome": "error:transport"})
    u = led.usage(c, ts(2026, 10, 15, 11))
    assert u["month_cost"] == 1.25 and u["day_cost"] == 0.25
    assert u["calls"] == {"claude-ask": 0, "claude-auto": 1, "claude-hard": 1}   # refusals do not count
    if os.name == "posix":
        assert _stat.S_IMODE(os.stat(c.ledger_path).st_mode) == 0o600


def test_torn_line_is_skipped_and_next_append_starts_fresh(tmp_path):
    c = cfg(tmp_path)
    led = escalation.Ledger(c.ledger_path)
    led.append({"ts": ts(2026, 10, 15), "alias": "claude-ask", "cost_usd": 0.5, "outcome": "ok"})
    with open(c.ledger_path, "a", encoding="utf-8") as f:
        f.write('{"ts": 1, "alias": "claude-a')                           # crash mid-write: no newline
    led.append({"ts": ts(2026, 10, 15, 13), "alias": "claude-ask", "cost_usd": 0.25, "outcome": "ok"})
    u = escalation.Ledger(c.ledger_path).usage(c, ts(2026, 10, 15, 14))   # a fresh instance = after a restart
    assert u["day_cost"] == 0.75 and u["calls"]["claude-ask"] == 2


def test_check_budget_caps_and_counts(tmp_path):
    c = cfg(tmp_path)
    u = {"day_cost": 6.9, "month_cost": 10.0, "calls": {"claude-ask": 0, "claude-auto": 0, "claude-hard": 0}}
    assert escalation.check_budget(c, u, "claude-ask", 0.2) == "daily cap reached ($7.00)"
    u = {"day_cost": 0.0, "month_cost": 149.95, "calls": {"claude-ask": 0, "claude-auto": 0, "claude-hard": 0}}
    assert escalation.check_budget(c, u, "claude-ask", 0.1) == "monthly cap reached ($150.00)"
    u = {"day_cost": 0.0, "month_cost": 0.0, "calls": {"claude-ask": 10, "claude-auto": 5, "claude-hard": 0}}
    assert escalation.check_budget(c, u, "claude-ask", 0.01) == "daily calls used (15)"
    u = {"day_cost": 0.0, "month_cost": 0.0, "calls": {"claude-ask": 2, "claude-auto": 5, "claude-hard": 0}}
    assert escalation.check_budget(c, u, "claude-auto", 0.01) == "automatic calls for today used (5)"
    assert escalation.check_budget(c, u, "claude-ask", 0.01) is None
    u = {"day_cost": 0.0, "month_cost": 0.0, "calls": {"claude-ask": 15, "claude-auto": 0, "claude-hard": 3}}
    assert escalation.check_budget(c, u, "claude-hard", 0.01) == "Opus calls for today used (3)"


def test_reservation_counts_until_released(tmp_path):
    c = cfg(tmp_path, ESCALATION_DAY_CAP_USD="1")
    led = escalation.Ledger(c.ledger_path)
    now = ts(2026, 10, 15)
    tok, why = led.reserve(c, "claude-ask", 0.6, now)
    assert tok and why is None
    tok2, why2 = led.reserve(c, "claude-ask", 0.6, now)                # the pending 0.6 counts
    assert tok2 is None and why2.startswith("budget: daily cap reached ($1.00); resets 2026-10-16 00:00")
    led.release(tok)
    tok3, _ = led.reserve(c, "claude-ask", 0.6, now)
    assert tok3


def test_footer_and_report(tmp_path):
    c = cfg(tmp_path)
    led = escalation.Ledger(c.ledger_path)
    led.append({"ts": ts(2026, 10, 15, 8), "alias": "claude-auto", "model": "claude-sonnet-5-5", "cost_usd": 0.05,
                "input_tokens": 900, "output_tokens": 300, "outcome": "ok", "brief_sha256": "ab" * 32})
    u = led.usage(c, ts(2026, 10, 15, 9))
    f = escalation.footer(c, "claude-sonnet-5-5", 0.05, u)
    assert f == ("\n\n---\nClaude claude-sonnet-5-5: $0.050 this call · $0.05 today of $7.00 · "
                 "$0.05 this month of $150.00 · auto 4/5 left today")
    rep = escalation.budget_report(c, led, ts(2026, 10, 15, 9))
    assert rep["month"]["spent_usd"] == 0.05 and rep["month"]["cap_usd"] == 150.0
    assert rep["day"]["spent_usd"] == 0.05 and rep["day"]["cap_usd"] == 7.0
    assert rep["calls_left"] == {"claude-ask": 14, "claude-auto": 4, "claude-hard": 3}
    assert rep["recent"][0]["alias"] == "claude-auto" and "brief_sha256" not in rep["recent"][0]
    assert rep["resets"]["day"].startswith("2026-10-16") and rep["resets"]["month"].startswith("2026-11-01")


# --- fix round 1 (review of 1a4eb21) ---

import time  # noqa: E402


def _raw(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _local(y, mo, d, h, mi=0):
    return _dt(y, mo, d, h, mi).timestamp()                      # naive -> host local time


def test_nan_cost_row_fails_the_day_closed(tmp_path):
    c = cfg(tmp_path, ESCALATION_DAY_CAP_USD="1")
    _raw(c.ledger_path, f'{{"ts": {ts(2026, 10, 15, 8)}, "alias": "claude-ask", "cost_usd": NaN, "outcome": "ok"}}')
    tok, why = escalation.Ledger(c.ledger_path).reserve(c, "claude-ask", 0.1, ts(2026, 10, 15, 9))
    assert tok is None and why.startswith("budget: ")


def test_string_nan_cost_fails_the_day_closed(tmp_path):
    c = cfg(tmp_path, ESCALATION_DAY_CAP_USD="1")
    _raw(c.ledger_path, f'{{"ts": {ts(2026, 10, 15, 8)}, "alias": "claude-ask", "cost_usd": "nan", "outcome": "ok"}}')
    tok, why = escalation.Ledger(c.ledger_path).reserve(c, "claude-ask", 0.1, ts(2026, 10, 15, 9))
    assert tok is None and why.startswith("budget: ")


def test_missing_ts_counts_as_now(tmp_path):
    c = cfg(tmp_path, ESCALATION_DAY_CAP_USD="1")
    _raw(c.ledger_path, '{"alias": "claude-ask", "cost_usd": 5, "outcome": "ok"}')
    led = escalation.Ledger(c.ledger_path)
    assert led.usage(c, ts(2026, 10, 15, 9))["day_cost"] == 5
    tok, why = led.reserve(c, "claude-ask", 0.1, ts(2026, 10, 15, 9))
    assert tok is None and why.startswith("budget: ")


def test_garbage_ts_is_counted_not_dropped_and_usage_never_raises(tmp_path):
    c = cfg(tmp_path)
    _raw(c.ledger_path, '{"ts": "2026-10-15T09:00", "alias": "claude-ask", "cost_usd": 0.5, "outcome": "ok"}')
    _raw(c.ledger_path, f'{{"ts": {ts(2026, 10, 15, 9)}, "alias": ["unhashable"], "cost_usd": 0.1, "outcome": "ok"}}')
    _raw(c.ledger_path, '{"ts": 1e400, "alias": "claude-ask", "cost_usd": 1' + "0" * 400 + ', "outcome": "ok"}')
    u = escalation.Ledger(c.ledger_path).usage(c, ts(2026, 10, 15, 10))
    assert abs(u["day_cost"] - (0.5 + 0.1 + c.day_cap)) < 1e-9       # bad cost fails closed at day_cap
    assert u["calls"]["claude-ask"] == 2                              # garbage ts row counts at now


def test_reserve_refuses_nan_negative_and_unknown_alias(tmp_path):
    c = cfg(tmp_path)
    led = escalation.Ledger(c.ledger_path)
    now = ts(2026, 10, 15)
    assert led.reserve(c, "claude-ask", float("nan"), now) == (None, "budget: invalid cost estimate")
    assert led.reserve(c, "claude-ask", -1.0, now) == (None, "budget: invalid cost estimate")
    assert led.reserve(c, "claude-x", 0.1, now) == (None, "budget: unknown alias")
    assert led._pending == {}


def test_check_budget_fails_closed_on_nan(tmp_path):
    c = cfg(tmp_path)
    zero = {"claude-ask": 0, "claude-auto": 0, "claude-hard": 0}
    assert escalation.check_budget(c, {"day_cost": float("nan"), "month_cost": 0.0, "calls": zero},
                                   "claude-ask", 0.1) == "daily cap reached ($7.00)"
    assert escalation.check_budget(c, {"day_cost": 0.0, "month_cost": 0.0, "calls": zero},
                                   "claude-ask", float("nan")) is not None


def test_append_rejects_nan_and_writes_nothing(tmp_path):
    c = cfg(tmp_path)
    led = escalation.Ledger(c.ledger_path)
    led.append({"ts": ts(2026, 10, 15), "alias": "claude-ask", "cost_usd": 0.5, "outcome": "ok"})
    before = open(c.ledger_path, "rb").read()
    with pytest.raises(ValueError):
        led.append({"ts": ts(2026, 10, 15), "alias": "claude-ask", "cost_usd": float("nan"), "outcome": "ok"})
    assert open(c.ledger_path, "rb").read() == before


def test_settle_moves_spend_from_pending_to_ledger_atomically(tmp_path):
    c = cfg(tmp_path, ESCALATION_DAY_CAP_USD="1")
    led = escalation.Ledger(c.ledger_path)
    now = ts(2026, 10, 15)
    tok, _ = led.reserve(c, "claude-ask", 0.6, now)
    led.settle(tok, {"ts": now, "alias": "claude-ask", "cost_usd": 0.6, "outcome": "ok"})
    assert led._pending == {}
    tok2, why2 = led.reserve(c, "claude-ask", 0.6, now)
    assert tok2 is None and why2.startswith("budget: daily cap reached ($1.00)")


def test_rows_returns_a_copy(tmp_path):
    c = cfg(tmp_path)
    led = escalation.Ledger(c.ledger_path)
    led.append({"ts": ts(2026, 10, 15), "alias": "claude-ask", "cost_usd": 0.5, "outcome": "ok"})
    led.rows().append({"x": 1})
    assert len(led.rows()) == 1


def test_zoneinfo_day_and_month_bounds_across_dst_and_year_end():
    ny, ldn = "America/New_York", "Europe/London"
    assert escalation.period_bounds(ts(2026, 11, 1, 12, ny), ny)[0] == ts(2026, 11, 1, 0, ny)
    assert escalation.period_bounds(ts(2026, 3, 8, 12, ny), ny)[0] == ts(2026, 3, 8, 0, ny)
    _, m0, _, nm = escalation.period_bounds(ts(2026, 10, 31, 23, ldn), ldn)
    assert m0 == ts(2026, 10, 1, 0, ldn) and nm.timestamp() == ts(2026, 11, 1, 0, ldn)
    _, _, _, nm2 = escalation.period_bounds(ts(2026, 12, 31, 23, ny), ny)
    assert nm2.timestamp() == ts(2027, 1, 1, 0, ny)


def test_empty_tz_is_host_local_time_across_dst(tmp_path, monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        c = escalation.load_config({"ESCALATION_PRICES": PRICES, "ESCALATION_TZ": "",
                                    "ESCALATION_LEDGER": str(tmp_path / "l.jsonl")})
        led = escalation.Ledger(c.ledger_path)
        led.append({"ts": _local(2026, 11, 1, 0, 30), "alias": "claude-ask", "cost_usd": 0.5, "outcome": "ok"})
        assert led.usage(c, _local(2026, 11, 1, 10))["day_cost"] == 0.5
    finally:
        monkeypatch.undo()
        time.tzset()


def test_naive_local_reset_label_has_no_zone_name():
    assert escalation._when(_dt(2026, 11, 1, 0, 0)) == "2026-11-01 00:00 local"


# ---------------------------------------------------------------- Task 3: the Anthropic call and handle()
import asyncio as _asyncio  # noqa: E402
import logging as _logging  # noqa: E402
import socket as _socket  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anthropic_stub import AnthropicStub  # noqa: E402

SENTINEL_KEY = "sk-ant-SENTINEL-never-log-me"


@pytest.fixture
def broker(tmp_path):
    stub = AnthropicStub()
    key = tmp_path / "anthropic.key"
    key.write_text(SENTINEL_KEY + "\n")
    c = escalation.load_config({"ESCALATION_PRICES": PRICES, "ESCALATION_TZ": UTC, "ANTHROPIC_URL": stub.url,
                                "ESCALATION_LEDGER": str(tmp_path / "ledger.jsonl"), "ANTHROPIC_API_KEY_FILE": str(key),
                                "ESCALATION_RETRY_DELAY_S": "0", "ESCALATION_TIMEOUT_S": "5"})
    yield stub, c, tmp_path
    stub.close()


def run(body, c, now=None):
    return _asyncio.run(escalation.handle(body, c, now))


def ask(alias="claude-ask", brief=GOOD, **extra):
    return {"model": alias, "messages": [{"role": "system", "content": "OPENCODE SYSTEM PROMPT " * 500},
                                         {"role": "user", "content": "earlier turn"},
                                         {"role": "user", "content": brief}],
            "tools": [{"type": "function", "function": {"name": "bash"}}], "temperature": 1.0, **extra}


def test_successful_call_strips_prices_ledgers_and_footers(broker, caplog):
    stub, c, tmp = broker
    caplog.set_level(_logging.DEBUG)
    status, out = run(ask(), c, ts(2026, 10, 15, 9))
    assert status == 200
    headers, req = stub.requests[0]
    assert req == escalation.build_request("claude-sonnet-5-5", GOOD, 2048)     # nothing but prompt + brief
    assert headers["x-api-key"] == SENTINEL_KEY and headers["anthropic-version"] == "2023-06-01"
    text = out["choices"][0]["message"]["content"]
    assert text.startswith("Root cause: X.") and "\n---\nClaude claude-sonnet-5-5: $0.006 this call" in text
    assert out["choices"][0]["finish_reason"] == "stop" and out["model"] == "claude-ask"
    assert out["usage"] == {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}
    rows = escalation.Ledger(c.ledger_path).rows()
    assert rows[-1]["outcome"] == "ok" and rows[-1]["origin"] == "user"
    assert abs(rows[-1]["cost_usd"] - (1000 * 3 + 200 * 15) / 1e6) < 1e-9
    assert "brief" not in rows[-1] and GOOD not in open(c.ledger_path, encoding="utf-8").read()
    assert SENTINEL_KEY not in caplog.text and SENTINEL_KEY not in open(c.ledger_path, encoding="utf-8").read()
    assert SENTINEL_KEY not in _json.dumps(out)


def test_opus_alias_uses_opus_and_bigger_answer_cap(broker):
    stub, c, _ = broker
    assert run(ask("claude-hard"), c)[0] == 200
    assert stub.requests[0][1]["model"] == "claude-opus-5-5" and stub.requests[0][1]["max_tokens"] == 4096


def test_refusals_spend_nothing_and_are_ledgered(broker, tmp_path):
    stub, c, _ = broker
    assert run(ask(brief="no headings"), c)[0] == 422
    assert run(ask(brief=GOOD + "x" * 30000), c)[0] == 413
    unpriced = escalation.load_config({"ESCALATION_PRICES": "{}", "ESCALATION_LEDGER": c.ledger_path,
                                       "ANTHROPIC_API_KEY_FILE": c.key_file})
    s, o = run(ask(), unpriced)
    assert s == 503 and o["error"]["message"] == "escalation: no price configured for claude-sonnet-5-5"
    nokey = escalation.load_config({"ESCALATION_PRICES": PRICES, "ESCALATION_LEDGER": c.ledger_path,
                                    "ANTHROPIC_API_KEY_FILE": str(tmp_path / "missing.key")})
    s, o = run(ask(), nokey)
    assert s == 503 and o["error"]["message"] == "escalation: no Anthropic key configured"
    assert stub.requests == []
    outcomes = [r["outcome"] for r in escalation.Ledger(c.ledger_path).rows()]
    assert outcomes == ["refused:brief_section", "refused:brief_too_large", "refused:unpriced", "refused:no_key"]


def test_budget_refusal_is_429_with_reset_time(broker):
    stub, c, tmp = broker
    tight = escalation.load_config({"ESCALATION_PRICES": PRICES, "ESCALATION_TZ": UTC, "ANTHROPIC_URL": stub.url,
                                    "ESCALATION_LEDGER": c.ledger_path, "ANTHROPIC_API_KEY_FILE": c.key_file,
                                    "ESCALATION_DAY_CAP_USD": "0.01"})
    s, o = run(ask(), tight, ts(2026, 10, 15, 9))
    assert s == 429 and o["error"]["code"] == "budget"
    assert o["error"]["message"].startswith("budget: daily cap reached ($0.01); resets 2026-10-16 00:00")
    assert stub.requests == []


def test_retry_once_on_5xx_then_succeed(broker):
    stub, c, _ = broker
    stub.responses = [(529, {"type": "error", "error": {"type": "overloaded_error"}})]
    assert run(ask(), c)[0] == 200 and len(stub.requests) == 2


def test_no_retry_on_400_and_error_is_ledgered_at_zero(broker):
    stub, c, _ = broker
    stub.responses = [(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})]
    s, o = run(ask(), c)
    assert s == 502 and o["error"]["message"] == "escalation: Anthropic call failed (http_400)"
    assert len(stub.requests) == 1
    last = escalation.Ledger(c.ledger_path).rows()[-1]
    assert last["outcome"] == "error:http_400" and last["cost_usd"] == 0.0


def test_connect_failure_is_retried_once_and_costs_nothing(broker):
    stub, c, tmp = broker
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                       # nothing listens here now
    dead = escalation.load_config({"ESCALATION_PRICES": PRICES, "ESCALATION_TZ": UTC,
                                   "ANTHROPIC_URL": f"http://127.0.0.1:{port}/v1/messages",
                                   "ESCALATION_LEDGER": c.ledger_path, "ANTHROPIC_API_KEY_FILE": c.key_file,
                                   "ESCALATION_RETRY_DELAY_S": "0", "ESCALATION_TIMEOUT_S": "5"})
    attempts = []
    real_post = _httpx.AsyncClient.post

    async def counting(self, *a, **k):
        attempts.append(1)
        return await real_post(self, *a, **k)

    _httpx.AsyncClient.post = counting
    try:
        st, o = run(ask(), dead)
    finally:
        _httpx.AsyncClient.post = real_post
    assert st == 502 and "transport:ConnectError" in o["error"]["message"] and len(attempts) == 2
    last = escalation.Ledger(c.ledger_path).rows()[-1]
    assert last["outcome"] == "error:transport:ConnectError" and last["cost_usd"] == 0.0   # never sent: free
    assert escalation.ledger_for(c.ledger_path)._pending == {}


def test_two_failures_give_502_transport_or_status(broker):
    stub, c, _ = broker
    stub.responses = [(500, {}), (503, {})]
    s, o = run(ask(), c)
    assert s == 502 and "http_503" in o["error"]["message"] and len(stub.requests) == 2


def test_cut_answer_is_returned_charged_and_marked_length(broker):
    stub, c, _ = broker
    stub.responses = [(200, {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}],
                             "usage": {"input_tokens": 100, "output_tokens": 2048}})]
    s, o = run(ask(), c)
    assert s == 200 and o["choices"][0]["finish_reason"] == "length"
    assert o["choices"][0]["message"]["content"].startswith("partial\n\n---\nClaude")
    stub.responses = [(200, {"stop_reason": "end_turn", "content": [], "usage": {"input_tokens": 5, "output_tokens": 0}})]
    s, o = run(ask(), c)
    assert s == 200 and o["choices"][0]["message"]["content"].startswith("\n\n---\nClaude")


def test_concurrent_calls_cannot_both_pass_a_cap_only_one_fits(broker):
    stub, c, _ = broker
    worst = escalation.worst_cost(c, "claude-sonnet-5-5", escalation.estimate_tokens(GOOD), 2048)
    tight = escalation.load_config({"ESCALATION_PRICES": PRICES, "ESCALATION_TZ": UTC, "ANTHROPIC_URL": stub.url,
                                    "ESCALATION_LEDGER": c.ledger_path, "ANTHROPIC_API_KEY_FILE": c.key_file,
                                    "ESCALATION_DAY_CAP_USD": str(worst * 1.5)})

    async def both():
        return await _asyncio.gather(escalation.handle(ask(), tight), escalation.handle(ask(), tight))

    statuses = sorted(s for s, _ in _asyncio.run(both()))
    assert statuses == [200, 429] and len(stub.requests) == 1


def test_keep_briefs_writes_0600_and_prunes(broker, tmp_path):
    stub, c, _ = broker
    keep = escalation.load_config({"ESCALATION_PRICES": PRICES, "ANTHROPIC_URL": stub.url,
                                   "ESCALATION_LEDGER": c.ledger_path, "ANTHROPIC_API_KEY_FILE": c.key_file,
                                   "ESCALATION_KEEP_BRIEFS": "2", "ESCALATION_BRIEFS_DIR": str(tmp_path / "briefs")})
    for i in range(3):
        assert run(ask(brief=GOOD + f"\n<!-- {i} -->"), keep, 1_800_000_000 + i)[0] == 200
    files = sorted(os.listdir(tmp_path / "briefs"))
    assert len(files) == 2 and open(tmp_path / "briefs" / files[-1], encoding="utf-8").read().endswith("<!-- 2 -->")


def test_sse_is_one_chunk_then_done():
    comp = escalation.completion("claude-ask", "hello", 1, 2, "end_turn", 1_800_000_000)
    s = escalation.sse(comp)
    assert s.endswith("data: [DONE]\n\n") and s.count("data: ") == 2
    chunk = _json.loads(s.split("\n\n")[0][len("data: "):])
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"][0]["delta"] == {"role": "assistant", "content": "hello"}
    assert chunk["choices"][0]["finish_reason"] == "stop"


def test_model_entries_list_the_three_aliases():
    ids = [m["id"] for m in escalation.model_entries(1)]
    assert ids == ["claude-ask", "claude-auto", "claude-hard"]


# ---------------------------------------------------------------- Task 3 fix round 1
import httpx as _httpx  # noqa: E402,F811


def _worst(c):
    return escalation.worst_cost(c, "claude-sonnet-5-5", escalation.estimate_tokens(GOOD), 2048)


def _cfg(c, stub, **env):
    base = {"ESCALATION_PRICES": PRICES, "ESCALATION_TZ": UTC, "ANTHROPIC_URL": stub.url,
            "ESCALATION_LEDGER": c.ledger_path, "ANTHROPIC_API_KEY_FILE": c.key_file,
            "ESCALATION_RETRY_DELAY_S": "0", "ESCALATION_TIMEOUT_S": "5"}
    base.update(env)
    return escalation.load_config(base)


def _last(c):
    return escalation.Ledger(c.ledger_path).rows()[-1]


def _pending(c):
    return escalation.ledger_for(c.ledger_path)._pending


def test_cancelled_call_is_charged_the_worst_case_and_reraises(broker):
    stub, c, _ = broker
    stub.delay = 3

    async def go():
        t = _asyncio.create_task(escalation.handle(ask(), c))
        await _asyncio.sleep(0.3)
        t.cancel()
        await t

    with pytest.raises(_asyncio.CancelledError):
        _asyncio.run(go())
    rows = escalation.Ledger(c.ledger_path).rows()
    assert len(rows) == 1 and rows[0]["outcome"] == "error:cancelled"
    assert abs(rows[0]["cost_usd"] - _worst(c)) < 1e-6 and _pending(c) == {}


def test_read_timeout_is_not_retried_and_is_charged_the_worst_case(broker):
    stub, c, _ = broker
    stub.delay = 1.0
    slow = _cfg(c, stub, ESCALATION_TIMEOUT_S="0.2")
    st, o = run(ask(), slow)
    assert st == 502 and "transport:" in o["error"]["message"]
    assert len(stub.requests) == 1                                   # sent once, never again
    last = _last(c)
    assert last["outcome"].startswith("error:transport:") and abs(last["cost_usd"] - _worst(c)) < 1e-6
    assert _pending(c) == {}


def test_total_timeout_is_not_retried_and_is_charged_the_worst_case(broker):
    stub, c, _ = broker
    calls = []

    async def hang(self, *a, **k):
        calls.append(1)
        await _asyncio.sleep(30)

    real = _httpx.AsyncClient.post
    _httpx.AsyncClient.post = hang
    try:
        st, o = run(ask(), _cfg(c, stub, ESCALATION_TIMEOUT_S="0.2"))
    finally:
        _httpx.AsyncClient.post = real
    assert st == 502 and len(calls) == 1
    last = _last(c)
    assert last["outcome"] == "error:transport:TotalTimeout" and abs(last["cost_usd"] - _worst(c)) < 1e-6


@pytest.mark.parametrize("raw", [b"<html>not json</html>", b"[1, 2, 3]"])
def test_unreadable_200_is_not_retried_and_is_charged_the_worst_case(broker, raw):
    stub, c, _ = broker
    stub.responses = [(200, raw)]
    st, _o = run(ask(), c)
    assert st == 502 and len(stub.requests) == 1
    last = _last(c)
    assert last["outcome"] == "error:transport:bad_response" and abs(last["cost_usd"] - _worst(c)) < 1e-6
    assert _pending(c) == {}


@pytest.mark.parametrize("usage", [{"input_tokens": 10 ** 400, "output_tokens": 1},
                                   {"input_tokens": -5, "output_tokens": 1},
                                   {"input_tokens": "lots", "output_tokens": 1},
                                   {"input_tokens": float("nan"), "output_tokens": 1},
                                   "oops"])
def test_bad_usage_is_charged_the_worst_case(broker, usage):
    stub, c, _ = broker
    stub.responses = [(200, _json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": "x"}],
                                         "usage": usage}).encode())]
    st, _o = run(ask(), c)
    assert st == 502
    last = _last(c)
    assert last["outcome"] == "error:transport:bad_usage" and abs(last["cost_usd"] - _worst(c)) < 1e-6
    assert _pending(c) == {}


def test_content_not_a_list_still_answers_with_the_footer(broker):
    stub, c, _ = broker
    stub.responses = [(200, {"stop_reason": "end_turn", "content": 7, "usage": {"input_tokens": 5, "output_tokens": 1}})]
    st, o = run(ask(), c)
    assert st == 200 and o["choices"][0]["message"]["content"].startswith("\n\n---\nClaude")
    stub.responses = [(200, {"stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 1},
                             "content": [{"type": "text", "text": 5}, "str", {"type": "text", "text": "ok"}]})]
    st, o = run(ask(), c)
    assert st == 200 and o["choices"][0]["message"]["content"].startswith("ok\n\n---\nClaude")


def test_footer_failure_returns_the_answer_without_footer(broker, monkeypatch, caplog):
    stub, c, _ = broker

    def boom(*a, **k):
        raise RuntimeError("footer broke")

    monkeypatch.setattr(escalation, "footer", boom)
    caplog.set_level(_logging.WARNING)
    st, o = run(ask(), c)
    assert st == 200 and o["choices"][0]["message"]["content"] == "Root cause: X. Fix: Y. Verify: pytest -q"
    assert "footer" in caplog.text and _last(c)["outcome"] == "ok"


def test_unexpected_post_exception_is_charged_worst_and_logged_without_the_key(broker, monkeypatch, caplog):
    stub, c, _ = broker

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(escalation, "_post", boom)
    caplog.set_level(_logging.DEBUG)
    st, _o = run(ask(), c)
    assert st == 502
    last = _last(c)
    assert last["outcome"].startswith("error:transport") and abs(last["cost_usd"] - _worst(c)) < 1e-6
    assert any(r.levelno == _logging.ERROR for r in caplog.records) and SENTINEL_KEY not in caplog.text
    assert _pending(c) == {}


def test_ledger_write_failure_on_the_ok_row_is_500_and_holds_the_reservation(broker, monkeypatch, caplog):
    stub, c, _ = broker
    real = escalation.Ledger.append

    def flaky(self, row):
        if row.get("outcome") == "ok":
            raise OSError("disk full")
        return real(self, row)

    monkeypatch.setattr(escalation.Ledger, "append", flaky)
    caplog.set_level(_logging.DEBUG)
    st, o = run(ask(), c)
    assert st == 500 and o["error"]["code"] == "ledger"
    assert o["error"]["message"] == "escalation: could not record the call; budget held until restart"
    assert len(_pending(c)) == 1
    assert any(r.levelno == _logging.ERROR for r in caplog.records) and SENTINEL_KEY not in caplog.text
    assert GOOD not in caplog.text


def test_key_with_embedded_header_injection_is_rejected(broker, tmp_path, caplog):
    stub, c, _ = broker
    evil = tmp_path / "evil.key"
    evil.write_text("sk-ant-SENTINEL\nX-Evil: 1")
    caplog.set_level(_logging.DEBUG)
    st, o = run(ask(), _cfg(c, stub, ANTHROPIC_API_KEY_FILE=str(evil)))
    assert st == 503 and o["error"]["message"] == "escalation: no Anthropic key configured"
    assert stub.requests == [] and "sk-ant-SENTINEL" not in caplog.text
    assert escalation.read_key(str(evil)) is None
    assert escalation.read_key(c.key_file) == SENTINEL_KEY


def test_kept_briefs_dir_and_files_are_private_and_foreign_files_survive(broker, tmp_path):
    stub, c, _ = broker
    bd = tmp_path / "briefs"
    bd.mkdir(mode=0o755)
    os.chmod(bd, 0o755)                                              # pre-existing, too open
    (bd / "notes.txt").write_text("mine")
    keep = _cfg(c, stub, ESCALATION_KEEP_BRIEFS="1", ESCALATION_BRIEFS_DIR=str(bd))
    for i in range(2):
        assert run(ask(brief=GOOD + f"\n<!-- {i} -->"), keep, 1_800_000_000 + i)[0] == 200
    names = sorted(os.listdir(bd))
    assert "notes.txt" in names and len([n for n in names if n.endswith(".md")]) == 1
    assert not [n for n in names if n.endswith(".tmp")]
    if os.name == "posix":
        assert _stat.S_IMODE(os.stat(bd).st_mode) == 0o700
        md = [n for n in names if n.endswith(".md")][0]
        assert _stat.S_IMODE(os.stat(bd / md).st_mode) == 0o600


def test_kept_brief_overwrites_a_looser_existing_file_privately(broker, tmp_path):
    stub, c, _ = broker
    bd = tmp_path / "briefs"
    keep = _cfg(c, stub, ESCALATION_KEEP_BRIEFS="3", ESCALATION_BRIEFS_DIR=str(bd))
    sha = escalation.hashlib.sha256(GOOD.encode()).hexdigest()
    bd.mkdir()
    victim = bd / f"1800000000-{sha[:12]}.md"
    victim.write_text("old")
    os.chmod(victim, 0o644)
    assert run(ask(), keep, 1_800_000_000)[0] == 200
    assert victim.read_text(encoding="utf-8") == GOOD
    if os.name == "posix":
        assert _stat.S_IMODE(os.stat(victim).st_mode) == 0o600


def test_completion_ids_are_unique():
    a = escalation.completion("claude-ask", "x", 1, 1, "end_turn", 1_800_000_000)
    b = escalation.completion("claude-ask", "x", 1, 1, "end_turn", 1_800_000_000)
    assert a["id"] != b["id"] and a["id"].startswith("esc-")
