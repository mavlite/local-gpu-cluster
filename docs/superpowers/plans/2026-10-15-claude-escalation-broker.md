# Claude Escalation Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user's interactive opencode sessions on local qwen3.8 escalate to Claude only through a budgeted, brief-only broker in the router, with hard caps the client cannot bypass.

**Architecture:**
- A new router module `scripts/files/escalation.py` owns everything Claude-specific: the aliases, brief validation, message stripping, budget and ledger, the Anthropic call and the OpenAI-shaped answer with a footer.
- `router-app.py` gains only a short-circuit in the chat handler, the owner-only model listing and `GET /escalation/budget`.
- The workstation side is a small installer that adds the opencode subagents, commands, skill and a budget printer. It also edits RULES.md.

**Tech Stack:**
- Python 3.12 (router LXC, Ubuntu 24.04): FastAPI, slowapi, `httpx` (already installed), stdlib `zoneinfo`. No new pip dependency; the Anthropic REST API is called with httpx.
- Bash for `scripts/53-lxc-router.sh`.
- opencode 1.18.x config JSON.

**Spec:** `docs/superpowers/specs/2026-10-15-claude-escalation-broker-design.md`

## Global Constraints

**Aliases and caps:**

| Alias | Origin | Model tier | Brief cap | Answer cap | Daily calls |
|---|---|---|---|---|---|
| `claude-ask` | user | Sonnet (`claude-sonnet-5-5`) | 8192 tokens | 2048 tokens | shares 15/day |
| `claude-auto` | model | Sonnet | 8192 | 2048 | shares 15/day; at most 5/day |
| `claude-hard` | user | Opus (`claude-opus-5-5`) | 16384 | 4096 | 3/day (separate count) |

**Budget and pricing:**
- Defaults: `ESCALATION_MONTH_CAP_USD=150`, `ESCALATION_DAY_CAP_USD=7`.
- A call goes ahead only if its worst case fits: `((brief_tokens_est + 200) × input_price + max_answer × output_price) / 1e6`.
- `brief_tokens_est = ceil(len(utf8_bytes)/3)`.
- Prices come only from the `ESCALATION_PRICES` env, as JSON `{"<model id>": [usd_per_Mtok_in, usd_per_Mtok_out]}`. A model with no price fails closed with 503 `escalation: no price configured for <model>`. Prices are never hard-coded.

**Day and month boundaries:**
- `ESCALATION_TZ` (IANA name). Empty means the router host's local timezone.
- Calendar day and calendar month in that timezone.

**Brief rules:**
- These seven headings are required, in any order, each with content: `## Goal`, `## Context`, `## Reproduce`, `## Exact error`, `## Code`, `## What was tried`, `## Question`.
- `n/a: <reason>` is allowed except for Goal, Exact error and Question.
- Missing or empty: 422 `brief: missing or empty section <name>`.
- Over the cap: 413 `brief: <n> tokens, over the <cap> limit -- trim it`.

**Upstream request:** exactly `{"model", "max_tokens": <answer cap>, "system": SYSTEM_PROMPT, "messages": [{"role": "user", "content": <brief>}]}`.
- Client system messages, earlier turns, tools, `tool_choice` and sampling are all discarded.

**Fixed system prompt (verbatim):**

> You are a senior engineer helping a local coding model that is stuck. Answer the Question using only the brief. Be concise: diagnosis in a few sentences, then the minimal fix as a diff or a short snippet, plus how to verify it (the command or test to run). If the brief lacks information you need, say exactly what is missing and stop — do not guess.

**Anthropic call:**
- Endpoint: `ANTHROPIC_URL`, default `https://api.anthropic.com/v1/messages`.
- Headers: `x-api-key`, `anthropic-version: 2023-06-01`.
- Timeout: `ESCALATION_TIMEOUT_S` (default 120).
- One retry, after `ESCALATION_RETRY_DELAY_S` (default 2), on 429, ≥500 and transport errors. No retry on other 4xx.

**Key:**
- File: `ANTHROPIC_API_KEY_FILE`, default `/etc/router-anthropic.key`, `root:router` 0640.
- Read at each use and stripped.
- Missing or empty: 503 `escalation: no Anthropic key configured`.
- The key never appears in logs, errors, the ledger or responses.

**Ledger:**
- `ESCALATION_LEDGER`, default `/var/lib/llm-router/escalation-ledger.jsonl`. Mode 0600, append-only.
- One JSON line per attempt, with keys: `ts`, `alias`, `model`, `origin`, `input_tokens`, `output_tokens`, `cost_usd`, `brief_sha256`, `brief_tokens_est`, `outcome` (`ok`, `refused:<code>` or `error:<class>`).
- Refusals cost 0 and do not count as calls. Brief text is never written to the ledger.

**Optional brief audit:**
- `ESCALATION_KEEP_BRIEFS=N` (default 0) keeps the last N briefs in `ESCALATION_BRIEFS_DIR` (default `/var/lib/llm-router/escalation-briefs`).
- Directory 0700, files 0600.

**Footer** (appended to every successful answer):

```
\n\n---\nClaude <model>: $<call:.3f> this call · $<day:.2f> today of $<daycap:.2f> · $<month:.2f> this month of $<monthcap:.2f> · auto <left>/<autocap> left today
```

**Access and endpoints:**
- Only the owner key (`principal.is_owner`) may call `claude-*`. Scoped keys get 403 `escalation aliases need the owner key`, whatever their allowlist says.
- `GET /escalation/budget` is owner-only, through the existing `require_bearer` middleware.

**Repo conventions:**
- Conventional commits, no `Co-Authored-By` trailer.
- Commit by explicit path, as a standalone `git commit` command.
- Never print or commit a secret.
- Tests run with `python -m pytest scripts/files/tests -q` (router) and `python -m pytest scripts/tools/escalation/tests -q` (installer).
- Every edited `.sh` file passes `bash -n`.

## Review Focus

The five failure modes the spec implies but a straightforward test suite would miss. Each has a test in the task that owns it.

1. **opencode streams by default** (`"stream": true`, often with `stream_options`). A JSON body where SSE is expected breaks the session, so the reply must be valid SSE: one `chat.completion.chunk` with the whole answer plus footer, then `data: [DONE]`. Test in Task 4.
2. **The final user message may be a list of content parts** (`[{"type":"text","text":...}]`), not a string. The brief must be extracted from the text parts, not refused or stringified. Test in Task 1.
3. **Two escalations racing near a cap.** Both must not pass a check that only one fits: the worst-case cost is reserved under a lock before the call. Test in Task 3.
4. **A torn ledger line** (crash mid-write). It must not crash the broker or reset spend to zero: the bad line is skipped, and the next append starts on a fresh line. Test in Task 2.
5. **Anthropic returns 200 but the answer was cut** (`stop_reason: "max_tokens"`) **or has no text blocks.** The user still gets what came back plus the footer, with `finish_reason: "length"`, and the call is charged. Test in Task 3.

## File structure

| File | Responsibility |
|---|---|
| `scripts/files/escalation.py` (create) | All broker logic: aliases, config, brief extraction and validation, request building, ledger, reservations, budget, pricing, Anthropic call, response and SSE shaping, budget report |
| `scripts/files/router-app.py` (modify) | `import escalation`; the chat short-circuit and `_escalate` helper; `/v1/models` owner-only entries; `GET /escalation/budget` |
| `scripts/53-lxc-router.sh` (modify) | Ship `escalation.py`; create `/var/lib/llm-router`; seed default `ESCALATION_*` env values only when absent |
| `scripts/files/tests/anthropic_stub.py` (create) | Shared stub Anthropic server for the tests |
| `scripts/files/tests/test_escalation.py` (create) | Unit tests of `escalation.py` |
| `scripts/files/tests/test_router_escalation.py` (create) | Integration tests through the real router app |
| `scripts/tools/escalation/install_opencode.py` (create) | Pure `merge_config`, `ensure_rules`; `main` backs up and installs into `~/.config/opencode` |
| `scripts/tools/escalation/claude_budget.py` (create) | Prints the budget, reading the router URL and key from the opencode config (key never printed) |
| `scripts/tools/escalation/skill/SKILL.md` (create) | The `escalate` skill: template, sufficiency check, rules |
| `scripts/tools/escalation/tests/test_install_opencode.py` (create) | Tests for the installer and the budget printer |

---

### Task 1: Broker core — config, brief extraction and validation, request building

**Files:**
- Create: `scripts/files/escalation.py`
- Test: `scripts/files/tests/test_escalation.py`

**Interfaces:**
- Produces:
  - `ALIASES: dict[str, dict]` (keys `origin`, `tier`, `max_brief`, `max_answer`), `SECTIONS`, `NEVER_NA`, `SYSTEM_PROMPT`, `SYSTEM_TOKENS = 200`, `ANTHROPIC_VERSION`.
  - `handles(model) -> bool`
  - `Config` (frozen dataclass) and `load_config(env=None) -> Config`
  - `error_body(code: str, message: str) -> dict`
  - `estimate_tokens(text: str) -> int`
  - `brief_text(body: dict) -> Optional[str]`
  - `parse_sections(text: str) -> dict[str, str]`
  - `validate_brief(text: str, alias: str) -> Optional[tuple[int, str, str]]`, returning `(status, code, message)` or None
  - `build_request(model: str, brief: str, max_tokens: int) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `scripts/files/tests/test_escalation.py`:

```python
"""escalation.py -- budgeted, brief-only Claude escalation (spec 2026-10-15-claude-escalation-broker-design.md)."""
import os
import sys

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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest scripts/files/tests/test_escalation.py -q`
Expected: FAIL / ERROR, `ModuleNotFoundError: No module named 'escalation'`.

- [ ] **Step 3: Implement the core of `scripts/files/escalation.py`**

```python
"""escalation.py -- budgeted Claude escalation for the router.

Spec: docs/superpowers/specs/2026-10-15-claude-escalation-broker-design.md.

The model aliases claude-ask, claude-auto and claude-hard never reach llama.cpp; the router hands them here. The
broker:
  - strips the request to its own fixed system prompt plus the final user message (the brief);
  - refuses a malformed or oversized brief before any spend;
  - refuses a call whose worst-case cost would break a cap;
  - calls the Anthropic Messages API and prices the call from the usage it reports;
  - appends one ledger line per attempt;
  - returns an OpenAI-shaped answer with a budget footer.
The Anthropic key is read from a file at each use and never logged."""
import json
import math
import os
from dataclasses import dataclass
from typing import Optional

ALIASES = {
    "claude-ask": {"origin": "user", "tier": "sonnet", "max_brief": 8192, "max_answer": 2048},
    "claude-auto": {"origin": "model", "tier": "sonnet", "max_brief": 8192, "max_answer": 2048},
    "claude-hard": {"origin": "user", "tier": "opus", "max_brief": 16384, "max_answer": 4096},
}
SECTIONS = ("Goal", "Context", "Reproduce", "Exact error", "Code", "What was tried", "Question")
NEVER_NA = ("Goal", "Exact error", "Question")
SYSTEM_PROMPT = (
    "You are a senior engineer helping a local coding model that is stuck. Answer the Question using only the "
    "brief. Be concise: diagnosis in a few sentences, then the minimal fix as a diff or a short snippet, plus how "
    "to verify it (the command or test to run). If the brief lacks information you need, say exactly what is "
    "missing and stop — do not guess."
)
SYSTEM_TOKENS = 200                 # allowance for SYSTEM_PROMPT in the worst-case budget check
ANTHROPIC_VERSION = "2023-06-01"


def handles(model) -> bool:
    return isinstance(model, str) and model in ALIASES


@dataclass(frozen=True)
class Config:
    models: dict
    prices: dict
    month_cap: float
    day_cap: float
    shared_daily_calls: int
    auto_daily_calls: int
    hard_daily_calls: int
    tz: str
    ledger_path: str
    key_file: str
    url: str
    keep_briefs: int
    briefs_dir: str
    timeout_s: float
    retry_delay_s: float


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    prices = json.loads(env.get("ESCALATION_PRICES", "{}") or "{}")
    return Config(
        models=json.loads(env.get("ESCALATION_MODELS", "") or
                          '{"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}'),
        prices={m: (float(p[0]), float(p[1])) for m, p in prices.items()},
        month_cap=float(env.get("ESCALATION_MONTH_CAP_USD", "150")),
        day_cap=float(env.get("ESCALATION_DAY_CAP_USD", "7")),
        shared_daily_calls=int(env.get("ESCALATION_DAILY_CALLS", "15")),
        auto_daily_calls=int(env.get("ESCALATION_AUTO_DAILY_CALLS", "5")),
        hard_daily_calls=int(env.get("ESCALATION_HARD_DAILY_CALLS", "3")),
        tz=env.get("ESCALATION_TZ", ""),
        ledger_path=env.get("ESCALATION_LEDGER", "/var/lib/llm-router/escalation-ledger.jsonl"),
        key_file=env.get("ANTHROPIC_API_KEY_FILE", "/etc/router-anthropic.key"),
        url=env.get("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages"),
        keep_briefs=int(env.get("ESCALATION_KEEP_BRIEFS", "0")),
        briefs_dir=env.get("ESCALATION_BRIEFS_DIR", "/var/lib/llm-router/escalation-briefs"),
        timeout_s=float(env.get("ESCALATION_TIMEOUT_S", "120")),
        retry_delay_s=float(env.get("ESCALATION_RETRY_DELAY_S", "2")),
    )


def error_body(code: str, message: str) -> dict:
    return {"error": {"type": "escalation", "code": code, "message": message}}


def estimate_tokens(text: str) -> int:
    """Deliberate over-estimate (3 bytes per token) so a brief that passes the cap fits the real limit."""
    return math.ceil(len(text.encode("utf-8")) / 3)


def brief_text(body) -> Optional[str]:
    """The final user message: a string, or the text parts of a content list joined by newlines."""
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        return None
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            return None
    return None


def parse_sections(text: str) -> dict:
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out.setdefault(cur, [])
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def validate_brief(text: str, alias: str):
    """None when the brief may be sent, else (status, code, message). Structural only: sufficiency is judged
    locally by the escalate skill's checklist."""
    secs = parse_sections(text)
    for name in SECTIONS:
        content = secs.get(name, "")
        if not content or (name in NEVER_NA and content.lower().startswith("n/a")):
            return 422, "brief_section", f"brief: missing or empty section {name}"
    cap = ALIASES[alias]["max_brief"]
    n = estimate_tokens(text)
    if n > cap:
        return 413, "brief_too_large", f"brief: {n} tokens, over the {cap} limit -- trim it"
    return None


def build_request(model: str, brief: str, max_tokens: int) -> dict:
    return {"model": model, "max_tokens": max_tokens, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": brief}]}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/files/tests/test_escalation.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/files/escalation.py scripts/files/tests/test_escalation.py
git commit -m "feat(router): escalation broker core - aliases, brief validation, stripped request"
```

---

### Task 2: Ledger, periods, reservations and budget

**Files:**
- Modify: `scripts/files/escalation.py` (append)
- Test: `scripts/files/tests/test_escalation.py` (append)

**Interfaces:**
- Consumes (Task 1): `ALIASES`, `Config`, `load_config`, `SYSTEM_TOKENS`.
- Produces:
  - `period_bounds(now: float, tz: str) -> tuple[float, float, datetime, datetime]`, returning `(day_start_ts, month_start_ts, next_day_dt, next_month_dt)`
  - `class Ledger(path)` with `rows() -> list[dict]`, `append(row: dict) -> None`, `usage(cfg, now) -> dict`, `reserve(cfg, alias, worst, now) -> tuple[Optional[str], Optional[str]]` (token, refusal message) and `release(token) -> None`
  - `ledger_for(path) -> Ledger` (one instance per path)
  - `check_budget(cfg, usage, alias, worst) -> Optional[str]`
  - `worst_cost(cfg, model, brief_tokens, max_answer) -> float`
  - `call_cost(cfg, model, input_tokens, output_tokens) -> float`
  - `footer(cfg, model, call_cost, usage) -> str`
  - `budget_report(cfg, ledger, now) -> dict`

The `usage` dict: `{"day_cost": float, "month_cost": float, "calls": {"claude-ask": int, "claude-auto": int, "claude-hard": int}}`.

- [ ] **Step 1: Append the failing tests to `scripts/files/tests/test_escalation.py`**

```python
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
```

- [ ] **Step 2: Run them to verify the new ones fail**

Run: `python -m pytest scripts/files/tests/test_escalation.py -q`
Expected: the 7 new tests FAIL with `AttributeError: module 'escalation' has no attribute ...`; the 10 from Task 1 pass.

- [ ] **Step 3: Append the implementation to `scripts/files/escalation.py`**

Add `import threading`, `import uuid`, `from datetime import datetime, timedelta` and `from zoneinfo import ZoneInfo` to the imports at the top, then append:

```python
def _zone(tz: str):
    return ZoneInfo(tz) if tz else datetime.now().astimezone().tzinfo


def period_bounds(now: float, tz: str):
    """(day_start_ts, month_start_ts, next_day, next_month) for `now` in the configured timezone."""
    local = datetime.fromtimestamp(now, _zone(tz))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    next_day = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    next_month = (month + timedelta(days=32)).replace(day=1)
    return day.timestamp(), month.timestamp(), next_day, next_month


def _when(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def call_cost(cfg: Config, model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = cfg.prices[model]
    return (input_tokens * pin + output_tokens * pout) / 1e6


def worst_cost(cfg: Config, model: str, brief_tokens: int, max_answer: int) -> float:
    return call_cost(cfg, model, brief_tokens + SYSTEM_TOKENS, max_answer)


def check_budget(cfg: Config, usage: dict, alias: str, worst: float) -> Optional[str]:
    calls = usage["calls"]
    if usage["month_cost"] + worst > cfg.month_cap:
        return f"monthly cap reached (${cfg.month_cap:.2f})"
    if usage["day_cost"] + worst > cfg.day_cap:
        return f"daily cap reached (${cfg.day_cap:.2f})"
    if alias == "claude-hard":
        if calls["claude-hard"] >= cfg.hard_daily_calls:
            return f"Opus calls for today used ({cfg.hard_daily_calls})"
        return None
    if calls["claude-ask"] + calls["claude-auto"] >= cfg.shared_daily_calls:
        return f"daily calls used ({cfg.shared_daily_calls})"
    if alias == "claude-auto" and calls["claude-auto"] >= cfg.auto_daily_calls:
        return f"automatic calls for today used ({cfg.auto_daily_calls})"
    return None


class Ledger:
    """Append-only JSONL of every attempt. Budgets are recomputed from it (cached on size+mtime), so a router
    restart loses nothing. In-flight calls hold a reservation of their worst-case cost until they finish, so two
    concurrent escalations cannot both pass a check that only one fits."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._pending = {}
        self._cache = (None, [])

    def rows(self) -> list:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        sig = (st.st_size, st.st_mtime_ns)
        if self._cache[0] == sig:
            return self._cache[1]
        rows = []
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue                                # a torn line from a crash: skip it, keep the rest
                if isinstance(r, dict):
                    rows.append(r)
        self._cache = (sig, rows)
        return rows

    def append(self, row: dict) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "r+b") as f:
            f.seek(0, os.SEEK_END)
            lead = b""
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    lead = b"\n"                            # never glue a new line onto a torn one
            f.write(lead + (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))

    def usage(self, cfg: Config, now: float) -> dict:
        day0, month0, _, _ = period_bounds(now, cfg.tz)
        u = {"day_cost": 0.0, "month_cost": 0.0, "calls": {a: 0 for a in ALIASES}}
        for r in self.rows():
            t = float(r.get("ts") or 0)
            cost = float(r.get("cost_usd") or 0)
            if t >= month0:
                u["month_cost"] += cost
            if t >= day0:
                u["day_cost"] += cost
                if r.get("alias") in u["calls"] and not str(r.get("outcome", "")).startswith("refused"):
                    u["calls"][r["alias"]] += 1
        for alias, worst in list(self._pending.values()):
            u["day_cost"] += worst
            u["month_cost"] += worst
            u["calls"][alias] += 1
        u["day_cost"] = round(u["day_cost"], 6)
        u["month_cost"] = round(u["month_cost"], 6)
        return u

    def reserve(self, cfg: Config, alias: str, worst: float, now: float):
        with self._lock:
            reason = check_budget(cfg, self.usage(cfg, now), alias, worst)
            if reason:
                _, _, next_day, next_month = period_bounds(now, cfg.tz)
                when = next_month if reason.startswith("monthly") else next_day
                return None, f"budget: {reason}; resets {_when(when)}"
            token = uuid.uuid4().hex
            self._pending[token] = (alias, worst)
            return token, None

    def release(self, token: str) -> None:
        with self._lock:
            self._pending.pop(token, None)


_LEDGERS = {}


def ledger_for(path: str) -> Ledger:
    if path not in _LEDGERS:
        _LEDGERS[path] = Ledger(path)
    return _LEDGERS[path]


def _calls_left(cfg: Config, usage: dict) -> dict:
    c = usage["calls"]
    shared_left = max(0, cfg.shared_daily_calls - c["claude-ask"] - c["claude-auto"])
    return {"claude-ask": shared_left,
            "claude-auto": max(0, min(cfg.auto_daily_calls - c["claude-auto"], shared_left)),
            "claude-hard": max(0, cfg.hard_daily_calls - c["claude-hard"])}


def footer(cfg: Config, model: str, cost: float, usage: dict) -> str:
    auto_left = _calls_left(cfg, usage)["claude-auto"]
    return (f"\n\n---\nClaude {model}: ${cost:.3f} this call · ${usage['day_cost']:.2f} today of "
            f"${cfg.day_cap:.2f} · ${usage['month_cost']:.2f} this month of ${cfg.month_cap:.2f} · "
            f"auto {auto_left}/{cfg.auto_daily_calls} left today")


def budget_report(cfg: Config, ledger: Ledger, now: float) -> dict:
    u = ledger.usage(cfg, now)
    _, _, next_day, next_month = period_bounds(now, cfg.tz)
    keep = ("ts", "alias", "model", "origin", "input_tokens", "output_tokens", "cost_usd", "outcome")
    recent = [{k: r.get(k) for k in keep} for r in reversed(ledger.rows()[-10:])]
    return {"month": {"spent_usd": round(u["month_cost"], 4), "cap_usd": cfg.month_cap},
            "day": {"spent_usd": round(u["day_cost"], 4), "cap_usd": cfg.day_cap},
            "calls_left": _calls_left(cfg, u),
            "resets": {"day": _when(next_day), "month": _when(next_month)},
            "recent": recent}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest scripts/files/tests/test_escalation.py -q`
Expected: 17 passed. On Windows the 0600 assertion is skipped by its `os.name` guard; the test still passes.

- [ ] **Step 5: Commit**

```bash
git add scripts/files/escalation.py scripts/files/tests/test_escalation.py
git commit -m "feat(router): escalation ledger, reservations and budget caps"
```

---

### Task 3: The Anthropic call and `handle()`

**Files:**
- Modify: `scripts/files/escalation.py` (append)
- Create: `scripts/files/tests/anthropic_stub.py`
- Test: `scripts/files/tests/test_escalation.py` (append)

**Interfaces:**
- Consumes (Tasks 1-2): everything above.
- Produces:
  - `read_key(path) -> Optional[str]`
  - `async handle(body: dict, cfg: Config, now: Optional[float] = None) -> tuple[int, dict]`, returning `(status, payload)`. The payload is an OpenAI `chat.completion` dict on 200, else `error_body(...)`.
  - `completion(alias, text, input_tokens, output_tokens, stop_reason, now) -> dict`
  - `sse(completion: dict) -> str`
  - `model_entries(now: int) -> list[dict]`

The stub: `AnthropicStub()` with `.url`, `.requests` (list of `(headers_dict, body_dict)`), `.responses` (a list of `(status, body_dict)` consumed in order; when empty it returns a default 200), and `.close()`.

- [ ] **Step 1: Create the stub `scripts/files/tests/anthropic_stub.py`**

```python
"""A stand-in for the Anthropic Messages API: records every request (headers + JSON body) and replies from a
queue of (status, body) pairs, falling back to a short successful answer."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT = {"id": "msg_1", "type": "message", "role": "assistant", "stop_reason": "end_turn",
           "content": [{"type": "text", "text": "Root cause: X. Fix: Y. Verify: pytest -q"}],
           "usage": {"input_tokens": 1000, "output_tokens": 200}}


class AnthropicStub:
    def __init__(self):
        self.requests, self.responses = [], []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                stub.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
                status, obj = stub.responses.pop(0) if stub.responses else (200, DEFAULT)
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/messages"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
```

- [ ] **Step 2: Append the failing tests to `scripts/files/tests/test_escalation.py`**

```python
import asyncio as _asyncio
import logging as _logging

import pytest as _pytest

from anthropic_stub import AnthropicStub  # noqa: E402

SENTINEL_KEY = "sk-ant-SENTINEL-never-log-me"


@_pytest.fixture
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
```

- [ ] **Step 3: Run them to verify the new ones fail**

Run: `python -m pytest scripts/files/tests/test_escalation.py -q`
Expected: the 12 new tests FAIL (`AttributeError: ... 'handle'`, `'completion'`, `'sse'`, `'model_entries'`); the earlier 17 pass.

- [ ] **Step 4: Append the implementation to `scripts/files/escalation.py`**

Add `import asyncio`, `import hashlib`, `import time` and `import httpx` to the imports, then append:

```python
def read_key(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        return None
    return key or None


async def _post(cfg: Config, key: str, payload: dict):
    """(200, json, None) on success, else (status, None, error_class). One retry on 429, >=500 and transport
    errors. The key travels only in the request header; it is never put in an error, log or return value."""
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    err, status = "transport", 502
    for attempt in (0, 1):
        try:
            async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
                r = await client.post(cfg.url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            err, status = f"transport:{type(e).__name__}", 502
        else:
            if r.status_code == 200:
                return 200, r.json(), None
            err, status = f"http_{r.status_code}", r.status_code
            if not (r.status_code == 429 or r.status_code >= 500):
                return status, None, err
        if attempt == 0:
            await asyncio.sleep(cfg.retry_delay_s)
    return status, None, err


def completion(alias: str, text: str, input_tokens: int, output_tokens: int, stop_reason, now: float) -> dict:
    return {"id": f"esc-{int(now * 1000)}", "object": "chat.completion", "created": int(now), "model": alias,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "length" if stop_reason == "max_tokens" else "stop"}],
            "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                      "total_tokens": input_tokens + output_tokens}}


def sse(comp: dict) -> str:
    choice = comp["choices"][0]
    chunk = {"id": comp["id"], "object": "chat.completion.chunk", "created": comp["created"], "model": comp["model"],
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": choice["message"]["content"]},
                          "finish_reason": choice["finish_reason"]}],
             "usage": comp["usage"]}
    return f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"


def model_entries(now: int) -> list:
    return [{"id": a, "object": "model", "owned_by": "anthropic-via-router", "created": now} for a in ALIASES]


def _keep_brief(cfg: Config, brief: str, sha: str, now: float) -> None:
    if cfg.keep_briefs <= 0:
        return
    os.makedirs(cfg.briefs_dir, mode=0o700, exist_ok=True)
    path = os.path.join(cfg.briefs_dir, f"{int(now)}-{sha[:12]}.md")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(brief)
    for old in sorted(os.listdir(cfg.briefs_dir))[:-cfg.keep_briefs]:
        os.remove(os.path.join(cfg.briefs_dir, old))


async def handle(body: dict, cfg: Config, now: Optional[float] = None):
    """(status, payload) for one escalation request whose model is one of ALIASES. The caller has already
    checked that the principal is the owner."""
    now = time.time() if now is None else now
    alias = body["model"]
    spec = ALIASES[alias]
    model = cfg.models.get(spec["tier"])
    ledger = ledger_for(cfg.ledger_path)
    brief = brief_text(body)
    sha = hashlib.sha256((brief or "").encode("utf-8")).hexdigest()
    row = {"ts": now, "alias": alias, "model": model, "origin": spec["origin"], "input_tokens": 0,
           "output_tokens": 0, "cost_usd": 0.0, "brief_sha256": sha, "brief_tokens_est": estimate_tokens(brief or "")}

    def refuse(status, code, message):
        ledger.append(dict(row, outcome=f"refused:{code}"))
        return status, error_body(code, message)

    if brief is None:
        return refuse(422, "brief_missing", "brief: no user message")
    bad = validate_brief(brief, alias)
    if bad:
        return refuse(*bad)
    if not model or model not in cfg.prices:
        return refuse(503, "unpriced", f"escalation: no price configured for {model}")
    key = read_key(cfg.key_file)
    if not key:
        return refuse(503, "no_key", "escalation: no Anthropic key configured")
    token, reason = ledger.reserve(cfg, alias, worst_cost(cfg, model, row["brief_tokens_est"], spec["max_answer"]), now)
    if token is None:
        return refuse(429, "budget", reason)
    try:
        status, data, err = await _post(cfg, key, build_request(model, brief, spec["max_answer"]))
        if status != 200:
            ledger.append(dict(row, outcome=f"error:{err}"))
            failure = (502, error_body("upstream", f"escalation: Anthropic call failed ({err})"))
        else:
            used = data.get("usage") or {}
            it, ot = int(used.get("input_tokens") or 0), int(used.get("output_tokens") or 0)
            cost = call_cost(cfg, model, it, ot)
            ledger.append(dict(row, input_tokens=it, output_tokens=ot, cost_usd=round(cost, 6), outcome="ok"))
            failure = None
    finally:
        ledger.release(token)
    if failure:
        return failure
    _keep_brief(cfg, brief, sha, now)
    text = "".join(b.get("text", "") for b in data.get("content") or []
                   if isinstance(b, dict) and b.get("type") == "text")
    text += footer(cfg, model, cost, ledger.usage(cfg, now))
    return 200, completion(alias, text, it, ot, data.get("stop_reason"), now)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest scripts/files/tests/test_escalation.py -q`
Expected: 29 passed.

- [ ] **Step 6: Mutation-check the race guard**

Use a matched text replacement, never git checkout, restore or stash.
1. In `Ledger.usage`, replace `for alias, worst in list(self._pending.values()):` with `for alias, worst in []:`.
2. Run `python -m pytest scripts/files/tests/test_escalation.py -q -k "concurrent or reservation"`. Expected: both FAIL.
3. Restore the exact original line and re-run. Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/files/escalation.py scripts/files/tests/anthropic_stub.py scripts/files/tests/test_escalation.py
git commit -m "feat(router): escalation handle - Anthropic call, retries, pricing, footer, SSE"
```

---

### Task 4: Wire the broker into the router

**Files:**
- Modify: `scripts/files/router-app.py`:
  - the imports (line ~52-56);
  - the `chat()` handler right after `body = await _read_json_body(request)` (~line 1659);
  - `models()` (~line 2313-2368);
  - a new `GET /escalation/budget` route next to `models()`.
- Test: `scripts/files/tests/test_router_escalation.py`

**Interfaces:**
- Consumes (Task 3): `escalation.handles`, `load_config`, `handle`, `sse`, `error_body`, `model_entries`, `budget_report`, `ledger_for`.
- Existing router functions used: `log_access(route, model, input_tokens, output_tokens, duration_ms, status, client_ip, error=None)`; `request.state.principal` (with `.is_owner`); `JSONResponse` and `StreamingResponse` from `fastapi.responses` (already imported, line 45).

- [ ] **Step 1: Write the failing integration tests**

Create `scripts/files/tests/test_router_escalation.py`:

```python
"""Integration: the real router app sends claude-* aliases to the escalation broker (owner key only), lists them
for the owner, serves /escalation/budget, and never lets the request reach llama.cpp."""
import hashlib
import importlib.util
import json
import os
import sys
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("slowapi")
pytest.importorskip("prometheus_fastapi_instrumentator")
from fastapi.testclient import TestClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from anthropic_stub import AnthropicStub  # noqa: E402
from test_escalation import GOOD  # noqa: E402
from test_router_scoped_keys import OWNER, SCOPED, Upstream  # noqa: E402

APP = os.environ.get("ROUTER_APP_UNDER_TEST", os.path.join(HERE, "..", "router-app.py"))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("router-esc")
    up, ant = Upstream(), AnthropicStub()
    keys = tmp / "router-keys.json"
    keys.write_text(json.dumps({"keys": [{"name": "wf-test", "sha256": hashlib.sha256(SCOPED.encode()).hexdigest(),
                                          "aliases": ["qwen3.8-nothink", "claude-ask"],
                                          "expires": time.time() + 3600}]}))
    akey = tmp / "anthropic.key"
    akey.write_text("sk-ant-test\n")
    saved = dict(os.environ)
    os.environ.update({
        "ROUTER_API_KEY": OWNER, "LLAMACPP_API_KEY": "x", "V620_URL": up.url, "ROUTER_KEYS_FILE": str(keys),
        "ACCESS_LOG_PATH": str(tmp / "access.log"), "RATE_LIMIT_CHAT": "1000/minute",
        "SWAP_WEBHOOK_URL": up.url, "SWAP_WEBHOOK_KEY": "k", "SWAP_TIMEOUT": "5", "PROFILE_CACHE_TTL": "0",
        "CHAT_SLOT_REFRESH_S": "0.1", "TOOL_EXECUTION_DEFAULT": "server", "STRICT_PROFILE_MATCH": "1",
        "ANTHROPIC_URL": ant.url, "ANTHROPIC_API_KEY_FILE": str(akey), "ESCALATION_LEDGER": str(tmp / "ledger.jsonl"),
        "ESCALATION_PRICES": '{"claude-sonnet-5-5": [3, 15], "claude-opus-5-5": [15, 75]}',
        "ESCALATION_TZ": "UTC", "ESCALATION_RETRY_DELAY_S": "0",
    })
    sys.path.insert(0, os.path.dirname(APP))
    spec = importlib.util.spec_from_file_location("router_app_esc_under_test", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    client = TestClient(mod.app)
    yield {"client": client, "up": up, "ant": ant, "log": tmp / "access.log"}
    client.close()
    up.server.shutdown()
    ant.close()
    os.environ.clear()
    os.environ.update(saved)


def post(env, key, **body):
    return env["client"].post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})


def esc_body(**extra):
    return {"model": "claude-ask", "messages": [{"role": "system", "content": "big opencode prompt"},
                                                {"role": "user", "content": GOOD}], **extra}


def test_owner_escalation_is_answered_by_the_broker_not_llamacpp(env):
    before = len(env["up"].chat_bodies)
    r = post(env, OWNER, **esc_body())
    assert r.status_code == 200, r.text
    assert "Claude claude-sonnet-5-5:" in r.json()["choices"][0]["message"]["content"]
    assert len(env["up"].chat_bodies) == before                        # llama.cpp never saw it
    assert env["ant"].requests[-1][1]["messages"] == [{"role": "user", "content": GOOD}]


def test_streaming_request_gets_valid_sse(env):
    r = post(env, OWNER, **esc_body(stream=True, stream_options={"include_usage": True}))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [e for e in r.text.split("\n\n") if e]
    assert events[-1] == "data: [DONE]"
    chunk = json.loads(events[0][len("data: "):])
    assert chunk["object"] == "chat.completion.chunk" and "Claude claude-sonnet-5-5:" in chunk["choices"][0]["delta"]["content"]


def test_scoped_key_is_refused_even_with_the_alias_allowlisted(env):
    n = len(env["ant"].requests)
    r = post(env, SCOPED, **esc_body())
    assert r.status_code == 403 and r.json()["error"]["message"] == "escalation aliases need the owner key"
    assert len(env["ant"].requests) == n


def test_broker_refusal_passes_status_through(env):
    r = post(env, OWNER, model="claude-ask", messages=[{"role": "user", "content": "no template"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "brief_section"


def test_models_lists_claude_aliases_for_owner_only(env):
    owner_ids = [m["id"] for m in env["client"].get("/v1/models", headers={"Authorization": f"Bearer {OWNER}"}).json()["data"]]
    scoped_ids = [m["id"] for m in env["client"].get("/v1/models", headers={"Authorization": f"Bearer {SCOPED}"}).json()["data"]]
    assert {"claude-ask", "claude-auto", "claude-hard"} <= set(owner_ids)
    assert not {"claude-ask", "claude-auto", "claude-hard"} & set(scoped_ids)


def test_budget_endpoint_owner_only(env):
    r = env["client"].get("/escalation/budget", headers={"Authorization": f"Bearer {OWNER}"})
    assert r.status_code == 200 and r.json()["day"]["cap_usd"] == 7.0 and "calls_left" in r.json()
    assert env["client"].get("/escalation/budget", headers={"Authorization": f"Bearer {SCOPED}"}).status_code == 403


def test_access_log_records_escalations_without_the_brief(env):
    log = env["log"].read_text(encoding="utf-8")
    assert '"claude-ask"' in log and "Make the parser accept trailing commas" not in log
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest scripts/files/tests/test_router_escalation.py -q`
Expected: FAIL. Escalation requests reach the stub llama.cpp or come back 409/404, `/escalation/budget` returns 404, and the model list lacks `claude-*`.

- [ ] **Step 3: Wire the broker into `scripts/files/router-app.py`**

**(a)** After `import access_keys` (line 52), add:

```python
import escalation
```

**(b)** In `chat()`, immediately after the line `    body = await _read_json_body(request)`, insert:

```python
    if escalation.handles(body.get("model")):
        return await _escalate(request, principal, body)
```

**(c)** Directly above the `@app.post("/v1/chat/completions")` decorator, add:

```python
async def _escalate(request: Request, principal, body: dict):
    """claude-* aliases: owner key only, answered by the escalation broker, never forwarded to llama.cpp."""
    t0 = time.monotonic()
    ip = request.client.host if request.client else "?"
    model = str(body.get("model"))
    if not principal.is_owner:
        log_access("/v1/chat/completions", model, -1, 0, int((time.monotonic() - t0) * 1000), 403, ip,
                   error="escalation_owner_only")
        return JSONResponse(escalation.error_body("owner_only", "escalation aliases need the owner key"),
                            status_code=403)
    status, payload = await escalation.handle(body, escalation.load_config())
    usage = payload.get("usage") or {}
    log_access("/v1/chat/completions", model, int(usage.get("prompt_tokens", -1)),
               int(usage.get("completion_tokens", 0)), int((time.monotonic() - t0) * 1000), status, ip,
               error=None if status == 200 else payload.get("error", {}).get("code"))
    if status == 200 and body.get("stream"):
        return StreamingResponse(iter([escalation.sse(payload)]), media_type="text/event-stream")
    return JSONResponse(payload, status_code=status)
```

**(d)** Change `async def models():` to `async def models(request: Request):`. Immediately before its final `return {"object": "list", "data": data}`, insert:

```python
    principal = getattr(request.state, "principal", None)
    if principal is not None and principal.is_owner:
        data.extend(escalation.model_entries(now))
```

**(e)** Directly after the `models()` function, add:

```python
@app.get("/escalation/budget")
async def escalation_budget(request: Request):
    """Claude escalation spend and allowances (owner key only: require_bearer limits scoped keys to chat + models)."""
    cfg = escalation.load_config()
    return escalation.budget_report(cfg, escalation.ledger_for(cfg.ledger_path), time.time())
```

- [ ] **Step 4: Run the router tests**

Run: `python -m pytest scripts/files/tests/test_router_escalation.py scripts/files/tests/test_router_scoped_keys.py scripts/files/tests/test_router_vision.py -q`
Expected: all pass. The two existing router suites are unchanged.

- [ ] **Step 5: Run the full file suite**

Run: `python -m pytest scripts/files/tests -q`
Expected: all pass (the previous count plus 36 new tests).

- [ ] **Step 6: Commit**

```bash
git add scripts/files/router-app.py scripts/files/tests/test_router_escalation.py
git commit -m "feat(router): route claude-* aliases to the escalation broker (owner only), budget endpoint"
```

---

### Task 5: Ship the broker with the router deploy

**Files:**
- Modify: `scripts/53-lxc-router.sh`:
  - the source-variable block (~line 103-122);
  - phase 7.3 pushes (~line 224-235);
  - the env seeding next to `upsert_env` (~line 293-345).
- Test: `scripts/files/tests/test_escalation.py` (append)

**Interfaces:**
- Consumes: the existing `pct push … --perms 0644` plus `chown router:router` pattern, and the `upsert_env` Python-rewrite helper.
- Produces:
  - `/opt/llm-router/escalation.py` in LXC 153;
  - `/var/lib/llm-router` (`router:router`, 0700);
  - default `ESCALATION_*` lines in `/etc/router.env`, added only when absent. `ESCALATION_PRICES` is deliberately not seeded, so the broker fails closed until the operator sets verified prices.

- [ ] **Step 1: Append the failing test**

```python
def test_deploy_script_ships_escalation_and_seeds_env_only_when_absent():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    s = open(os.path.join(root, "53-lxc-router.sh"), encoding="utf-8").read()
    assert "files/escalation.py" in s and "/opt/llm-router/escalation.py" in s
    assert "install -d -o router -g router -m 0700 /var/lib/llm-router" in s
    for k in ("ESCALATION_MONTH_CAP_USD 150", "ESCALATION_DAY_CAP_USD 7", "ESCALATION_DAILY_CALLS 15",
              "ESCALATION_AUTO_DAILY_CALLS 5", "ESCALATION_HARD_DAILY_CALLS 3"):
        assert f"ensure_env {k}" in s
    assert "ensure_env ESCALATION_PRICES" not in s                    # prices are set by the operator, verified
    assert "ensure_env() {" in s
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest scripts/files/tests/test_escalation.py -q -k deploy`
Expected: FAIL on `files/escalation.py`.

- [ ] **Step 3: Edit `scripts/53-lxc-router.sh`**

**(a)** After the `ACCESS_KEYS_SRC` lines (117-118), add:

```bash
ESCALATION_SRC="$LGC_DIR/files/escalation.py"
[[ -r "$ESCALATION_SRC" ]] || die "escalation.py not found at $ESCALATION_SRC"
```

**(b)** After the two `access_keys.py` push and chown lines in phase 7.3, add:

```bash
  pct push "$ROUTER_VMID" "$ESCALATION_SRC" /opt/llm-router/escalation.py --perms 0644
  pct exec "$ROUTER_VMID" -- chown router:router /opt/llm-router/escalation.py
  # Claude escalation ledger + optional brief audit live here (spec 2026-10-15 sec 3.1); the router user only.
  pct exec "$ROUTER_VMID" -- install -d -o router -g router -m 0700 /var/lib/llm-router
```

**(c)** Directly after the closing `}` of the `upsert_env()` function definition, add a helper that never overwrites an operator's value:

```bash
    # Add KEY=VAL only when KEY is absent: operator-tuned escalation caps survive a re-run.
    ensure_env() {
      local key="$1" val="$2"
      grep -q "^${key}=" /etc/router.env 2>/dev/null || upsert_env "$key" "$val"
    }
```

Then, next to the existing `upsert_env METRICS_ALLOWED_IPS …` call, add:

```bash
    # Claude escalation broker caps (spec 2026-10-15). ESCALATION_PRICES is NOT seeded on purpose: the broker
    # refuses (503) until the operator sets prices verified against Anthropic's current price list.
    ensure_env ESCALATION_MONTH_CAP_USD 150
    ensure_env ESCALATION_DAY_CAP_USD 7
    ensure_env ESCALATION_DAILY_CALLS 15
    ensure_env ESCALATION_AUTO_DAILY_CALLS 5
    ensure_env ESCALATION_HARD_DAILY_CALLS 3
```

Read the surrounding block first: the `upsert_env` calls run in the context that owns `/etc/router.env`, so place `ensure_env` and its calls in that same context, whether that is inside the container (via a pushed script) or wherever the existing calls run. If they run inside a heredoc pushed into the container, add the lines inside that heredoc. Record where you put them in your report.

- [ ] **Step 4: Run the test and the syntax check**

Run: `python -m pytest scripts/files/tests/test_escalation.py scripts/files/tests/test_access_keys.py -q` then `bash -n scripts/53-lxc-router.sh`
Expected: all pass; `bash -n` exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/53-lxc-router.sh scripts/files/tests/test_escalation.py
git commit -m "feat(router): deploy ships the escalation broker; caps seeded only when absent"
```

---

### Task 6: opencode installer, skill and budget printer

**Files:**
- Create:
  - `scripts/tools/escalation/install_opencode.py`
  - `scripts/tools/escalation/claude_budget.py`
  - `scripts/tools/escalation/skill/SKILL.md`
- Test: `scripts/tools/escalation/tests/test_install_opencode.py`

**Interfaces:**
- Produces:
  - `install_opencode.merge_config(cfg: dict) -> dict`: a new dict; the input is not mutated.
  - `install_opencode.ensure_rules(text: str) -> str`: idempotent.
  - `install_opencode.main(home: str) -> int`: backs up, then installs into `<home>/.config/opencode`.
  - `claude_budget.format_report(report: dict) -> str`
  - `claude_budget.main() -> int`

- [ ] **Step 1: Verify the opencode config keys before writing code**

Fetch the schema `https://opencode.ai/config.json` and confirm the property names the code below uses:
- top-level `agent` and `command`;
- agent fields `mode` (`"subagent"`), `model`, `description`, `prompt`, `tools`;
- command fields `template`, `description`.

Also check how a command template receives arguments (`$ARGUMENTS`). If a name differs, use the schema's name and record it as a `Ruling:` in your report. Do not guess.

- [ ] **Step 2: Write the failing tests**

Create `scripts/tools/escalation/tests/test_install_opencode.py`:

```python
"""Installer for the opencode side of the Claude escalation broker, and the budget printer."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import claude_budget  # noqa: E402
import install_opencode as inst  # noqa: E402

BASE = {"model": "router/qwen3.8-think", "instructions": ["~/.config/opencode/RULES.md"],
        "provider": {"router": {"options": {"baseURL": "http://192.168.6.153:8000/v1", "apiKey": "OWNERKEY"},
                                "models": {"qwen3.8-think": {"name": "q"}}}}}


def test_merge_adds_models_agents_commands_without_mutating_input():
    before = copy.deepcopy(BASE)
    out = inst.merge_config(BASE)
    assert BASE == before
    models = out["provider"]["router"]["models"]
    for alias, cap in (("claude-ask", 2048), ("claude-auto", 2048), ("claude-hard", 4096)):
        assert models[alias]["tools"] is False and models[alias]["limit"] == {"context": 16384, "output": cap}
    assert models["qwen3.8-think"] == {"name": "q"}
    agents = out["agent"]
    assert agents["claude"]["model"] == "router/claude-ask" and agents["claude"]["mode"] == "subagent"
    assert agents["claude-hard"]["model"] == "router/claude-hard"
    assert agents["claude-auto"]["model"] == "router/claude-auto"
    assert all(v is False for a in agents.values() for v in a["tools"].values())
    assert "ask-claude" in out["command"] and "claude-budget" in out["command"]
    assert "$ARGUMENTS" in out["command"]["ask-claude"]["template"]
    assert out["model"] == "router/qwen3.8-think"


def test_merge_is_idempotent():
    once = inst.merge_config(BASE)
    assert inst.merge_config(once) == once


def test_rules_lines_added_once():
    t = inst.ensure_rules("# Operating Rules\n")
    assert t.count(inst.RULES_MARKER) == 1 and inst.ensure_rules(t) == t


def test_main_backs_up_and_installs(tmp_path):
    oc = tmp_path / ".config" / "opencode"
    oc.mkdir(parents=True)
    (oc / "config.json").write_text(json.dumps(BASE))
    (oc / "RULES.md").write_text("# Operating Rules\n")
    assert inst.main(str(tmp_path)) == 0
    assert any(p.name.startswith("config.json.bak-") for p in oc.iterdir())
    assert "claude-ask" in json.loads((oc / "config.json").read_text())["provider"]["router"]["models"]
    assert (oc / "skills" / "escalate" / "SKILL.md").exists()
    assert (oc / "bin" / "claude_budget.py").exists()
    assert inst.RULES_MARKER in (oc / "RULES.md").read_text()


def test_budget_report_format_has_no_secrets():
    rep = {"month": {"spent_usd": 23.1, "cap_usd": 150.0}, "day": {"spent_usd": 1.84, "cap_usd": 7.0},
           "calls_left": {"claude-ask": 11, "claude-auto": 3, "claude-hard": 3},
           "resets": {"day": "2026-10-16 00:00 UTC", "month": "2026-11-01 00:00 UTC"},
           "recent": [{"ts": 1792000000, "alias": "claude-ask", "model": "claude-sonnet-5-5", "cost_usd": 0.05,
                       "input_tokens": 900, "output_tokens": 120, "outcome": "ok"}]}
    s = claude_budget.format_report(rep)
    assert "$23.10 of $150.00 this month" in s and "$1.84 of $7.00 today" in s
    assert "ask 11 · auto 3 · hard 3 calls left today" in s and "claude-ask" in s
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m pytest scripts/tools/escalation/tests -q`
Expected: ERROR, `No module named 'claude_budget'`.

- [ ] **Step 4: Create `scripts/tools/escalation/skill/SKILL.md`**

```markdown
---
name: escalate
description: How and when to ask Claude for help through the budgeted escalation broker - the brief template, the sufficiency check, and the auto-escalation rules.
---

# Escalating to Claude

Claude is reached ONLY through the subagents `claude` (Sonnet, user-asked), `claude-hard` (Opus, user-asked) and
`claude-auto` (Sonnet, your own decision). Every call costs real money from a capped budget. The broker on the
router enforces the caps; never try to work around a refusal.

## The brief: sufficient first, small second

Claude sees ONLY the brief: no repo, no transcript, no tools. A brief that leaves Claude guessing wastes the call.
Leave out irrelevant material, never relevant context. The cap is ~8K tokens for Sonnet, ~16K for Opus.

Use exactly these headings. All are required; a section that truly does not apply says `n/a: <why>`. Goal, Exact
error and Question can never be n/a.

## Goal
1-3 sentences: what we are trying to achieve and why.
## Context
Repo purpose in one line; language, framework and the versions that matter; OS/runtime; constraints on the fix.
## Reproduce
The exact command(s) that show the problem; expected vs actual behaviour.
## Exact error
Verbatim: the full relevant traceback / compiler / test output, from the first cause to the failing line.
## Code
Every excerpt needed to reason about the failure, each headed `path:line-range`: the failing function, its
callers/callees on the failing path, the relevant types/config, and the failing test.
## What was tried
Bullets "attempt -> what happened", including hypotheses ruled out and why.
## Question
One specific question, and what a useful answer looks like (e.g. "root cause and a minimal diff").

Never paste the transcript or unrelated files. A whole file only when it is short and wholly relevant. Redact
anything that looks like a key, token or password.

## Sufficiency check (answer each; fix every "no" before sending)

1. Could a senior engineer who has never seen this repo reproduce or reason about the problem from this brief alone?
2. Is the full causal chain of the error present (the code on the failing path, not just the line that throws)?
3. Are versions and constraints stated wherever the fix could depend on them?
4. Is every attempted fix listed with its result?
5. Is the question answerable, and does it say what form of answer we need?

## When YOU may escalate on your own (subagent `claude-auto`)

Only if one of these holds:
- the same error survived 3 genuinely different fix attempts;
- you are looping (the same edit -> test cycle repeating);
- you need knowledge you could not find after trying the repo, context7 and searxng.

Never for something a search or reading the code would answer, never on a first failure, never for the same
problem twice, except ONE follow-up when Claude's answer named exactly what was missing, carrying exactly that.
If the sufficiency check fails and you cannot fix it within the cap, ask the user instead of sending.

## Sending and applying

Call the task tool with the subagent and the brief as the whole prompt. Read the answer, apply the fix yourself,
and run the verification it names. The answer ends with a budget footer; mention it to the user.

## When the broker refuses

422/413: fix the brief (missing section / too large) and resend; that costs nothing. 429 (budget or daily calls):
stop escalating. Continue without Claude where you can, otherwise tell the user you are stuck, what you tried,
and that the escalation budget is used for now.
```

- [ ] **Step 5: Create `scripts/tools/escalation/claude_budget.py`**

```python
"""claude_budget.py -- print the Claude escalation budget from the router (GET /escalation/budget). Reads the
router URL and owner key from the opencode config; the key is sent only in the request header and never printed."""
import json
import os
import sys
import urllib.request


def format_report(rep: dict) -> str:
    m, d, left = rep["month"], rep["day"], rep["calls_left"]
    lines = [f"Claude escalation: ${m['spent_usd']:.2f} of ${m['cap_usd']:.2f} this month "
             f"(resets {rep['resets']['month']}), ${d['spent_usd']:.2f} of ${d['cap_usd']:.2f} today "
             f"(resets {rep['resets']['day']})",
             f"ask {left['claude-ask']} · auto {left['claude-auto']} · hard {left['claude-hard']} calls left today"]
    if rep.get("recent"):
        lines.append("recent:")
        lines += [f"  {r['alias']:<11} {r.get('model') or '-':<20} ${float(r.get('cost_usd') or 0):.3f}  "
                  f"in {r.get('input_tokens')} out {r.get('output_tokens')}  {r.get('outcome')}" for r in rep["recent"]]
    return "\n".join(lines)


def main() -> int:
    path = os.path.join(os.path.expanduser("~"), ".config", "opencode", "config.json")
    with open(path, encoding="utf-8") as f:
        opts = json.load(f)["provider"]["router"]["options"]
    base = opts["baseURL"].rstrip("/")
    base = base[:-3] if base.endswith("/v1") else base
    req = urllib.request.Request(f"{base}/escalation/budget", headers={"Authorization": f"Bearer {opts['apiKey']}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            print(format_report(json.load(r)))
    except Exception as e:                                   # message only: never echo the request headers
        print(f"claude-budget: could not read the budget ({type(e).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Create `scripts/tools/escalation/install_opencode.py`**

```python
"""install_opencode.py -- add the Claude escalation pieces to the user's opencode setup (spec 2026-10-15 sec 3.2):
router models claude-ask/-auto/-hard, tool-less subagents claude/claude-hard/claude-auto, the /ask-claude and
/claude-budget commands, the `escalate` skill, the budget printer and two RULES.md lines. Backs up config.json
first; idempotent; never touches auth.json or any key."""
import copy
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_MARKER = "<!-- claude-escalation -->"
RULES_LINES = (
    f"\n{RULES_MARKER}\n"
    "- Stuck? Follow the `escalate` skill. Claude is reached only through the `claude`, `claude-hard` and "
    "`claude-auto` subagents, never any other way.\n"
    "- If the escalation broker refuses (budget or daily calls), do not retry or work around it: continue "
    "without Claude, or tell the user you are stuck and what you tried.\n"
)
RELAY_PROMPT = "Pass the brief through unchanged. Return the answer verbatim."
NO_TOOLS = {"*": False}
ASK_TEMPLATE = (
    "The user wants to ask Claude for help with what we are stuck on. Arguments: $ARGUMENTS\n"
    "1. Load the `escalate` skill and write the brief from this session using its template exactly. If the "
    "arguments contain a note, include it where it belongs.\n"
    "2. Run the sufficiency check and fix every 'no'.\n"
    "3. Show the user the brief, its estimated size (bytes/3 tokens) and your checklist answers, then STOP and "
    "wait. Send only after the user replies 'send' (they may add context first).\n"
    "4. On 'send': call the task tool with subagent `claude-hard` if the arguments start with 'hard', otherwise "
    "`claude`, with the brief as the whole prompt. Then apply the answer and run the verification it names."
)
BUDGET_TEMPLATE = ("Run this command with the bash tool and show its output verbatim, nothing else: "
                   "python ~/.config/opencode/bin/claude_budget.py")


def merge_config(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    models = out.setdefault("provider", {}).setdefault("router", {}).setdefault("models", {})
    for alias, cap in (("claude-ask", 2048), ("claude-auto", 2048), ("claude-hard", 4096)):
        models[alias] = {"name": f"Claude via escalation broker ({alias})", "tools": False,
                         "limit": {"context": 16384, "output": cap}}
    agents = out.setdefault("agent", {})
    for name, alias, desc in (("claude", "claude-ask", "Claude Sonnet (user-asked); send a brief from the escalate skill"),
                              ("claude-hard", "claude-hard", "Claude Opus (user-asked, hard problems only)"),
                              ("claude-auto", "claude-auto", "Claude Sonnet for self-escalation under the escalate skill's rules")):
        agents[name] = {"mode": "subagent", "model": f"router/{alias}", "description": desc,
                        "prompt": RELAY_PROMPT, "tools": dict(NO_TOOLS)}
    commands = out.setdefault("command", {})
    commands["ask-claude"] = {"description": "Ask Claude for help (Sonnet; 'hard' for Opus)", "template": ASK_TEMPLATE}
    commands["claude-budget"] = {"description": "Show the Claude escalation budget", "template": BUDGET_TEMPLATE}
    return out


def ensure_rules(text: str) -> str:
    return text if RULES_MARKER in text else text.rstrip("\n") + "\n" + RULES_LINES


def main(home: str) -> int:
    oc = os.path.join(home, ".config", "opencode")
    cfg_path = os.path.join(oc, "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    shutil.copy2(cfg_path, f"{cfg_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}-escalation")
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(merge_config(cfg), f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.makedirs(os.path.join(oc, "skills", "escalate"), exist_ok=True)
    shutil.copy2(os.path.join(HERE, "skill", "SKILL.md"), os.path.join(oc, "skills", "escalate", "SKILL.md"))
    os.makedirs(os.path.join(oc, "bin"), exist_ok=True)
    shutil.copy2(os.path.join(HERE, "claude_budget.py"), os.path.join(oc, "bin", "claude_budget.py"))
    rules = os.path.join(oc, "RULES.md")
    text = open(rules, encoding="utf-8").read() if os.path.exists(rules) else ""
    with open(rules, "w", encoding="utf-8", newline="\n") as f:
        f.write(ensure_rules(text))
    print(f"installed into {oc} (config backed up)")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.path.expanduser("~")))
```

If Step 1 found different schema names, adjust `merge_config` and the tests together, and record the ruling.

- [ ] **Step 7: Run the tests**

Run: `python -m pytest scripts/tools/escalation/tests -q`
Expected: 5 passed.

- [ ] **Step 8: Commit**

```bash
git add scripts/tools/escalation/install_opencode.py scripts/tools/escalation/claude_budget.py scripts/tools/escalation/skill/SKILL.md scripts/tools/escalation/tests/test_install_opencode.py
git commit -m "feat(escalation): opencode installer, escalate skill and budget printer"
```

---

### Task 7: Rollout (live; operator-run with the user's go-ahead at each marked step)

This task changes production. It is executed by the controller session, not an implementer subagent, after Tasks 1-6 are merged to main. Every step has a rollback, and secrets travel over stdin only, never argv, output or chat.

- [ ] **Step 1: Prices.** Look up the current Anthropic list prices for `claude-sonnet-5-5` and `claude-opus-5-5` (input and output, USD per million tokens) on Anthropic's pricing page. Record the source URL and the date. **Ask the user to confirm the numbers.**

- [ ] **Step 2: Router deploy.**
  1. On the Proxmox host, `git -C /root/local-gpu-cluster fetch origin && git -C /root/local-gpu-cluster worktree add --detach /root/wf/lgc-esc origin/main`. The host checkout lags main, so deploy from this worktree.
  2. Back up `/opt/llm-router` and `/etc/router.env` in LXC 153 (`cp -a` to `*.bak-<ts>`). Write `/root/router-rollback-esc.sh` to restore both and restart `llm-router`.
  3. Run the router deploy phase from `/root/wf/lgc-esc` (`bash scripts/53-lxc-router.sh`, or the router-deploy procedure in its runbook).
  4. Add `ESCALATION_PRICES='{"claude-sonnet-5-5": [<in>, <out>], "claude-opus-5-5": [<in>, <out>]}'` and `ESCALATION_TZ=<user's IANA tz>` to `/etc/router.env` with `upsert_env`, using a pushed script.
  5. Restart `llm-router`.
  6. Check: `/healthz` ok, chat unchanged (`qwen3.8-nothink` answers), and `GET /escalation/budget` with the owner key returns JSON. With no key file yet, a `claude-ask` call must give 503 `no Anthropic key configured`.

- [ ] **Step 3: Move the key.**
  1. On the workstation, read the Anthropic key from `~/.local/share/opencode/auth.json` (`anthropic.key`) in Python. Never print it.
  2. Pipe it over ssh stdin to the host. Use a pushed script that writes `/etc/router-anthropic.key` inside LXC 153 via `pct push` of a 0600 temp file, then `chown root:router` and `chmod 0640`, then shreds the host temp file.
  3. Back up `auth.json` to `auth.json.bak-<ts>-escalation` (0600). Then remove its `anthropic` entry.
  4. Verify by checking the length and sha256 prefix on both sides, never the value.

- [ ] **Step 4: opencode.**
  1. On the workstation: `python scripts/tools/escalation/install_opencode.py`.
  2. Restart opencode.
  3. Confirm `/ask-claude` and `/claude-budget` appear, and no direct `anthropic/*` model is selectable.
  4. Run `/claude-budget`. Expected: $0 spent and full allowances.

- [ ] **Step 5: Live smoke test.** **Ask the user for a go-ahead first:** one call, about $0.01.
  1. In opencode, `/ask-claude` on a small staged problem: a deliberately failing one-function test in a scratch repo.
  2. Check the brief follows the template and the checklist was shown.
  3. Say `send`.
  4. Check: the answer carries the footer; the ledger line's cost matches the Anthropic console's usage for that call; `/claude-budget` shows the spend.

- [ ] **Step 6: The user sets the console backstop.** Ask the user to set a monthly spend limit just under $200 in the Anthropic console. This is the cap the broker cannot provide.

- [ ] **Step 7: Record.**
  1. Write a memory note: the broker is live, where the key is, the caps, and the rollback.
  2. Remove the `/root/wf/lgc-esc` worktree once deployed.
  3. Rollback, if needed: run `/root/router-rollback-esc.sh` in LXC 153, restore the `config.json.bak-*-escalation` and `auth.json.bak-*-escalation` files.

---

## Out of scope for this plan

- **The cluster-monitor budget panel (spec §3.3).** It is deferred, as the spec allows. `/claude-budget` and the per-answer footer cover visibility. If added later, follow `check_tavily_proxy` in `scripts/files/cluster_monitor.py`.
- **Sub-project A** (speed and quality of the local qwen3.8 session).
- **Spec §1 "a typical call costs ~$0.05"** is an expectation, checked during the live smoke test (Task 7 Step 5), not a code requirement.
