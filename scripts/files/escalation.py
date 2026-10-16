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
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

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
_KEY_RE = re.compile(r"[\x21-\x7e]+")
log = logging.getLogger("escalation")


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
    dedupe_s: float


def _money(name: str, raw) -> float:
    """A cap or price: a finite number >= 0. Anything else fails loudly at startup, never at spend time."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"escalation config: {name} must be a finite number >= 0")
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"escalation config: {name} must be a finite number >= 0")
    return v


def _count(name: str, raw) -> int:
    """A daily call count: an integer >= 0."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"escalation config: {name} must be an integer >= 0")
    if v < 0:
        raise ValueError(f"escalation config: {name} must be an integer >= 0")
    return v


def _seconds(name: str, raw, positive: bool) -> float:
    """A duration in seconds: finite, and > 0 (positive) or >= 0."""
    bound = "> 0" if positive else ">= 0"
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"escalation config: {name} must be a finite number {bound}")
    if not math.isfinite(v) or v < 0 or (positive and v == 0):
        raise ValueError(f"escalation config: {name} must be a finite number {bound}")
    return v


def _json_object(name: str, raw: str) -> dict:
    try:
        v = json.loads(raw)
    except ValueError:
        raise ValueError(f"escalation config: {name} is not valid JSON")
    if not isinstance(v, dict):
        raise ValueError(f"escalation config: {name} must be a JSON object")
    return v


def _prices(raw: str) -> dict:
    """ESCALATION_PRICES: {"model": [input_usd_per_mtok, output_usd_per_mtok], ...}."""
    out = {}
    for m, p in _json_object("ESCALATION_PRICES", raw).items():
        if not (isinstance(p, list) and len(p) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in p)):
            raise ValueError("escalation config: ESCALATION_PRICES values must be [input, output] number pairs")
        out[m] = (_money(f"ESCALATION_PRICES[{m}] input", p[0]), _money(f"ESCALATION_PRICES[{m}] output", p[1]))
    return out


def _models(raw: str) -> dict:
    models = _json_object("ESCALATION_MODELS", raw)
    for tier in ("sonnet", "opus"):
        if not isinstance(models.get(tier), str) or not models[tier]:
            raise ValueError("escalation config: ESCALATION_MODELS needs string values for sonnet and opus")
    return models


def _tz(raw: str) -> str:
    """Empty means the router host's local time; anything else must be a valid IANA zone."""
    if raw:
        try:
            ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("escalation config: ESCALATION_TZ is not a valid IANA time zone")
    return raw


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    return Config(
        models=_models(env.get("ESCALATION_MODELS", "") or '{"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}'),
        prices=_prices(env.get("ESCALATION_PRICES", "{}") or "{}"),
        month_cap=_money("ESCALATION_MONTH_CAP_USD", env.get("ESCALATION_MONTH_CAP_USD", "150")),
        day_cap=_money("ESCALATION_DAY_CAP_USD", env.get("ESCALATION_DAY_CAP_USD", "7")),
        shared_daily_calls=_count("ESCALATION_DAILY_CALLS", env.get("ESCALATION_DAILY_CALLS", "15")),
        auto_daily_calls=_count("ESCALATION_AUTO_DAILY_CALLS", env.get("ESCALATION_AUTO_DAILY_CALLS", "5")),
        hard_daily_calls=_count("ESCALATION_HARD_DAILY_CALLS", env.get("ESCALATION_HARD_DAILY_CALLS", "3")),
        tz=_tz(env.get("ESCALATION_TZ", "")),
        ledger_path=env.get("ESCALATION_LEDGER", "/var/lib/llm-router/escalation-ledger.jsonl"),
        key_file=env.get("ANTHROPIC_API_KEY_FILE", "/etc/router-anthropic.key"),
        url=env.get("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages"),
        keep_briefs=_count("ESCALATION_KEEP_BRIEFS", env.get("ESCALATION_KEEP_BRIEFS", "0")),
        briefs_dir=env.get("ESCALATION_BRIEFS_DIR", "/var/lib/llm-router/escalation-briefs"),
        timeout_s=_seconds("ESCALATION_TIMEOUT_S", env.get("ESCALATION_TIMEOUT_S", "120"), positive=True),
        retry_delay_s=_seconds("ESCALATION_RETRY_DELAY_S", env.get("ESCALATION_RETRY_DELAY_S", "2"), positive=False),
        dedupe_s=_seconds("ESCALATION_DEDUPE_S", env.get("ESCALATION_DEDUPE_S", "120"), positive=False),
    )


# opencode (AI SDK) retries 408/409/429/>=500 responses AND any error whose text matches these digit runs or
# phrases, up to five times. A retried charged failure is billed again, so no broker message may contain them.
_RETRY_DIGITS = re.compile(r"408|409|429|500|502|503|504|524")
_RETRY_PHRASES = re.compile(r"rate limit|service unavailable|try again", re.IGNORECASE)


def _safe_message(text: str) -> str:
    """text with every retry trigger broken: a forbidden digit run gets an apostrophe after its first digit
    ("$500.00" -> "$5'00.00", read like a digit-group mark), a forbidden phrase becomes "[...]". Fixed broker
    messages never contain either; this guards interpolated values (caps, model names, counts)."""
    text = _RETRY_PHRASES.sub("[...]", str(text))
    while True:
        m = _RETRY_DIGITS.search(text)
        if m is None:
            return text
        text = text[:m.start() + 1] + "'" + text[m.start() + 1:]


def error_body(code: str, message: str) -> dict:
    return {"error": {"type": "escalation", "code": code, "message": _safe_message(message)}}


def estimate_tokens(text: str) -> int:
    """An approximation, not a guarantee: UTF-8 bytes / 2.4, rounded up. 2.4 bytes/token was measured on the live
    smoke test (2026-10-15: a 3547-byte code-heavy brief was ~1460 Anthropic tokens; bytes/3 under-counted ~20%).
    Prose over-counts. It is not a true tokenizer; spend is always charged from the usage Anthropic reports.
    Lone surrogates encode as a replacement character instead of raising."""
    n = len(text.encode("utf-8", errors="replace"))
    return (n * 5 + 11) // 12                                   # ceil(n / 2.4) in integers


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
                return "\n".join(p["text"] for p in c
                                 if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
            return None
    return None


_HEADINGS = {f"## {name}" for name in SECTIONS}
_NA_EDGE = " \t\r\n*_`-()>"
_NA_RE = re.compile(r"^n/a(:|$)", re.IGNORECASE)


def parse_sections(text: str) -> dict:
    """A heading is a line that is exactly "## <name>" for a known section, outside a ``` fence. Every other line,
    including unknown "## x" lines and anything inside a fence, belongs to the current section's body."""
    out, cur, in_fence = {}, None, False
    for line in text.splitlines():
        if not in_fence and line.rstrip() in _HEADINGS:
            cur = line.strip()[3:]
            out.setdefault(cur, [])
            continue
        if cur is not None:
            out[cur].append(line)
        if line.strip().startswith("```"):
            in_fence = not in_fence
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _is_na(content: str) -> bool:
    return _NA_RE.match(content.strip(_NA_EDGE)) is not None


def validate_brief(text: str, alias: str):
    """None when the brief may be sent, else (status, code, message). Structural only: sufficiency is judged
    locally by the escalate skill's checklist."""
    secs = parse_sections(text)
    for name in SECTIONS:
        content = secs.get(name, "")
        if not content or (name in NEVER_NA and _is_na(content)):
            return 422, "brief_section", f"brief: missing or empty section {name}"
    cap = ALIASES[alias]["max_brief"]
    n = estimate_tokens(text)
    if n > cap:
        # Rounded to k (1024) so no raw count can look like a retryable status to the client.
        return 413, "brief_too_large", (f"brief: about {math.ceil(n / 1024)}k tokens, over the {cap // 1024}k "
                                        f"limit -- trim it")
    return None


def build_request(model: str, brief: str, max_tokens: int) -> dict:
    return {"model": model, "max_tokens": max_tokens, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": brief}]}


def period_bounds(now: float, tz: str):
    """(day_start_ts, month_start_ts, next_day, next_month) for `now`. A named tz uses ZoneInfo (aware datetimes).
    An empty tz uses the router host's local time: naive datetimes, whose .timestamp() goes through mktime and so
    stays DST-correct."""
    local = datetime.fromtimestamp(now, ZoneInfo(tz)) if tz else datetime.fromtimestamp(now)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    next_day = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    next_month = (month + timedelta(days=32)).replace(day=1)
    return day.timestamp(), month.timestamp(), next_day, next_month


def _when(dt: datetime) -> str:
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M") + " local"
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _finite(v) -> Optional[float]:
    """float(v) if it parses to a finite number, else None. Never raises."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def call_cost(cfg: Config, model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = cfg.prices[model]
    return (input_tokens * pin + output_tokens * pout) / 1e6


def worst_cost(cfg: Config, model: str, brief_tokens: int, max_answer: int) -> float:
    """Worst-case cost of one call. brief_tokens is the bytes/3 estimate (estimate_tokens), so this is approximate:
    a cap can be overshot by at most one call's estimation error."""
    return call_cost(cfg, model, brief_tokens + SYSTEM_TOKENS, max_answer)


def check_budget(cfg: Config, usage: dict, alias: str, worst: float) -> Optional[str]:
    calls = usage["calls"]
    # Written as "not (fits)" so that a NaN anywhere refuses rather than passing.
    if not (usage["month_cost"] + worst <= cfg.month_cap):
        return f"monthly cap reached (${cfg.month_cap:.2f})"
    if not (usage["day_cost"] + worst <= cfg.day_cap):
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
    """Append-only JSONL of every attempt. Budgets are recomputed from it (cached on size+mtime), so the ledger
    survives router restarts. A call in flight when the router is stopped can go unrecorded (the systemd stop
    timeout, 90 s, is shorter than the call timeout, 120 s): at most one call per restart. In-flight calls hold a reservation of their worst-case cost until they finish, so two
    concurrent escalations cannot both pass a check that only one fits. Reservation costs are worst-case estimates
    (bytes/3 brief tokens), so a cap can be overshot by at most one call's estimation error."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()                 # re-entrant: reserve() calls usage() under the lock
        self._pending = {}
        self._cache = (None, [])

    def rows(self) -> list:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        sig = (st.st_size, st.st_mtime_ns)
        if self._cache[0] == sig:
            return list(self._cache[1])
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
        return list(rows)

    def append(self, row: dict) -> None:
        line = json.dumps(row, sort_keys=True, allow_nan=False) + "\n"   # ValueError on NaN, before any write
        d = os.path.dirname(self.path)
        with self._lock:
            if d:
                os.makedirs(d, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "r+b") as f:
                f.seek(0, os.SEEK_END)
                lead = b""
                if f.tell() > 0:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        lead = b"\n"                        # never glue a new line onto a torn one
                f.write(lead + line.encode("utf-8"))

    def usage(self, cfg: Config, now: float) -> dict:
        """Never raises on a malformed row: an unreadable cost counts as cfg.day_cap (fail closed), an unreadable
        ts counts as `now`."""
        day0, month0, _, _ = period_bounds(now, cfg.tz)
        u = {"day_cost": 0.0, "month_cost": 0.0, "calls": {a: 0 for a in ALIASES}}
        for r in self.rows():
            t = _finite(r.get("ts"))
            t = now if t is None else t
            cost = _finite(r.get("cost_usd"))
            cost = cfg.day_cap if cost is None or cost < 0 else cost
            if t >= month0:
                u["month_cost"] += cost
            if t >= day0:
                u["day_cost"] += cost
                alias = r.get("alias")
                if isinstance(alias, str) and alias in u["calls"] and not str(r.get("outcome", "")).startswith("refused"):
                    u["calls"][alias] += 1
        with self._lock:
            pending = list(self._pending.values())
        for alias, worst in pending:
            u["day_cost"] += worst
            u["month_cost"] += worst
            u["calls"][alias] += 1
        u["day_cost"] = round(u["day_cost"], 6)
        u["month_cost"] = round(u["month_cost"], 6)
        return u

    def reserve(self, cfg: Config, alias: str, worst: float, now: float):
        if not (isinstance(alias, str) and alias in ALIASES):
            return None, "budget: unknown alias"
        if not (isinstance(worst, (int, float)) and math.isfinite(worst) and worst >= 0):
            return None, "budget: invalid cost estimate"
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
        """Drop a reservation for a call that writes no ledger row."""
        with self._lock:
            self._pending.pop(token, None)

    def settle(self, token: str, row: dict) -> None:
        """Write the call's ledger row, then drop its reservation, under one lock: the spend is never in neither
        place. If append raises (a bad row), the reservation stays held, which fails closed."""
        with self._lock:
            self.append(row)
            self._pending.pop(token, None)


_LEDGERS = {}
_LEDGERS_LOCK = threading.Lock()


def ledger_for(path: str) -> Ledger:
    """One Ledger per path, process-wide, so every caller shares one set of reservations."""
    with _LEDGERS_LOCK:
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


def read_key(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    except (OSError, ValueError):
        return None
    return key if _KEY_RE.fullmatch(key) else None       # visible ASCII only: nothing that can inject a header


# Failures that prove the request never reached Anthropic, so nothing can have been billed and a retry is safe.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_NEVER_SENT_CLASSES = tuple(f"transport:{c.__name__}" for c in _NEVER_SENT)


def _timeout(cfg: Config) -> httpx.Timeout:
    """Connect gets its own, shorter limit so a black-holed connect surfaces as ConnectTimeout (never sent: free,
    retried) before the outer total timeout in _post (cfg.timeout_s + 5) can pre-empt it as a possibly-sent call."""
    return httpx.Timeout(cfg.timeout_s, connect=min(10.0, cfg.timeout_s))


async def _send(cfg: Config, payload: dict, headers: dict):
    async with httpx.AsyncClient(timeout=_timeout(cfg)) as client:
        return await client.post(cfg.url, json=payload, headers=headers)


async def _post(cfg: Config, key: str, payload: dict):
    """(200, json, None) on success, else (status, None, error_class). One retry, only when the request provably
    never reached Anthropic (connect error/timeout, pool timeout) or Anthropic answered 429 or >=500 (error
    responses bill nothing). Any failure after the request may have been sent is final, because a retry could be
    billed twice. The key travels only in the request header; it is never put in an error, log or return value."""
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    err, status = "transport", 502
    for attempt in (0, 1):
        try:
            r = await asyncio.wait_for(_send(cfg, payload, headers), timeout=cfg.timeout_s + 5)
        except asyncio.TimeoutError:
            return 502, None, "transport:TotalTimeout"
        except httpx.HTTPError as e:
            err, status = f"transport:{type(e).__name__}", 502
            if not isinstance(e, _NEVER_SENT):
                return status, None, err
        else:
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    return 502, None, "transport:bad_response"      # billed but unreadable: charged as transport
                if not isinstance(data, dict):
                    return 502, None, "transport:bad_response"
                return 200, data, None
            err, status = f"http_{r.status_code}", r.status_code
            if not (r.status_code == 429 or r.status_code >= 500):
                return status, None, err
        if attempt == 0:
            await asyncio.sleep(cfg.retry_delay_s)
    return status, None, err


def completion(alias: str, text: str, input_tokens: int, output_tokens: int, stop_reason, now: float) -> dict:
    return {"id": f"esc-{uuid.uuid4().hex}", "object": "chat.completion", "created": int(now), "model": alias,
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


_BRIEF_NAME = re.compile(r"\d+-[0-9a-f]{12}\.md")


def _keep_brief(cfg: Config, brief: str, sha: str, now: float) -> None:
    if cfg.keep_briefs <= 0:
        return
    os.makedirs(cfg.briefs_dir, mode=0o700, exist_ok=True)
    os.chmod(cfg.briefs_dir, 0o700)                                  # an existing directory may be looser
    path = os.path.join(cfg.briefs_dir, f"{int(now)}-{sha[:12]}.md")
    tmp = os.path.join(cfg.briefs_dir, f".{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # fresh file: never inherits a looser mode
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(brief)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    ours = sorted(n for n in os.listdir(cfg.briefs_dir) if _BRIEF_NAME.fullmatch(n))
    for old in ours[:-cfg.keep_briefs]:
        os.remove(os.path.join(cfg.briefs_dir, old))                 # foreign files are never touched


def _upstream_message(err: str) -> str:
    """A word class for an Anthropic failure: never the raw status, which the client could take as retryable."""
    if err.startswith("http_"):
        try:
            code = int(err[5:])
        except ValueError:
            code = 0
        what = ("Anthropic was busy" if code == 429 else "Anthropic server error" if code >= 500
                else "Anthropic rejected the request")
    else:
        what = err
    return f"escalation: Anthropic call failed ({what})"


def _spent_or_budget_refused(r: dict) -> bool:
    outcome = str(r.get("outcome", ""))
    cost = _finite(r.get("cost_usd"))
    return (outcome == "ok" or outcome == "refused:budget"
            or (outcome.startswith("error:") and cost is not None and cost > 0))


def _duplicate_age(cfg: Config, ledger: Ledger, sha: str, now: float) -> Optional[float]:
    """Seconds since the latest identical brief that was answered, charged or budget-refused within
    cfg.dedupe_s, else None. The window runs from when that call FINISHED (done_ts, written at settle), so a resend
    right after a ~2-minute timeout is still caught; rows without done_ts fall back to their arrival ts. Rows with an
    unreadable ts are ignored here (the budget still counts them)."""
    if cfg.dedupe_s <= 0:
        return None
    ages = []
    for r in ledger.rows():
        if r.get("brief_sha256") != sha or not _spent_or_budget_refused(r):
            continue
        ts = _finite(r.get("ts"))
        if ts is None or ts > now:
            continue
        done = _finite(r.get("done_ts"))
        ref = max(ts, done) if done is not None else ts
        if ref >= now - cfg.dedupe_s:
            ages.append(max(0.0, now - ref))
    return min(ages) if ages else None


def _ago(age: float) -> str:
    return "under a minute ago" if age < 60 else f"under {int(age // 60) + 1} minutes ago"


async def handle(body: dict, cfg: Config, now: Optional[float] = None):
    """(status, payload) for one escalation request whose model is one of ALIASES. The caller has already
    checked that the principal is the owner."""
    now = time.time() if now is None else now
    started = time.monotonic()                          # done_ts = now + elapsed, so injected test clocks stay coherent
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
    # Statuses are never 408/409/429/>=500: opencode would resend those (see _safe_message).
    if not model or model not in cfg.prices:
        return refuse(424, "unpriced", f"escalation: no price configured for {model}")
    key = read_key(cfg.key_file)
    if not key:
        return refuse(424, "no_key", "escalation: no Anthropic key configured")
    age = _duplicate_age(cfg, ledger, sha, now)
    if age is not None:
        return refuse(400, "duplicate", f"brief: identical brief already sent {_ago(age)} -- change it or wait")
    worst = worst_cost(cfg, model, row["brief_tokens_est"], spec["max_answer"])
    token, reason = ledger.reserve(cfg, alias, worst, now)
    if token is None:
        return refuse(402, "budget", reason)

    def settle(final_row):
        """Record the call and drop its reservation together. If that fails the reservation stays held (fail
        closed) and the caller gets a 424; the log names the alias and outcome, never the key or the brief."""
        final_row = dict(final_row, done_ts=round(now + (time.monotonic() - started), 3))
        try:
            ledger.settle(token, final_row)
        except Exception:
            log.error("escalation: ledger write failed for %s (%s); reservation held until restart",
                      alias, final_row.get("outcome"), exc_info=True)
            return 424, error_body("ledger", "escalation: could not record the call; budget held until restart")
        return None

    try:
        status, data, err = await _post(cfg, key, build_request(model, brief, spec["max_answer"]))
    except asyncio.CancelledError:
        # The client went away mid-call; Anthropic may still bill it. Charge the worst case, then let it unwind.
        settle(dict(row, cost_usd=round(worst, 6), outcome="error:cancelled"))
        raise
    except Exception:
        log.error("escalation: unexpected failure calling Anthropic for %s", alias, exc_info=True)
        status, data, err = 502, None, "transport:unexpected"
    if status != 200:
        # A failure after the request may have been sent can still have been billed: charge the worst case.
        # A request that provably never left (connect error) and an HTTP error status (no usage) cost nothing.
        cost = worst if err.startswith("transport") and err not in _NEVER_SENT_CLASSES else 0.0
        failed = settle(dict(row, cost_usd=round(cost, 6), outcome=f"error:{err}"))
        return failed or (424, error_body("upstream", _upstream_message(err)))
    try:
        used = data.get("usage") or {}
        it, ot = int(used.get("input_tokens") or 0), int(used.get("output_tokens") or 0)
        cost = call_cost(cfg, model, it, ot)
        if not math.isfinite(cost) or it < 0 or ot < 0:
            raise ValueError("bad usage")
    except Exception:                                    # incl. OverflowError from a huge token count
        failed = settle(dict(row, cost_usd=round(worst, 6), outcome="error:transport:bad_usage"))
        return failed or (424, error_body("upstream", _upstream_message("transport:bad_usage")))
    failed = settle(dict(row, input_tokens=it, output_tokens=ot, cost_usd=round(cost, 6), outcome="ok"))
    if failed:
        return failed
    try:
        _keep_brief(cfg, brief, sha, now)
    except OSError:
        log.error("escalation: could not keep the brief for %s", alias, exc_info=True)
    blocks = data.get("content")
    text = "".join(b["text"] for b in (blocks if isinstance(blocks, list) else [])
                   if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    try:
        text += footer(cfg, model, cost, ledger.usage(cfg, now))
    except Exception:
        log.warning("escalation: budget footer failed for %s; answer returned without it", alias, exc_info=True)
    return 200, completion(alias, text, it, ot, data.get("stop_reason"), now)
