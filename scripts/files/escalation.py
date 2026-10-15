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
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

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


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    prices = json.loads(env.get("ESCALATION_PRICES", "{}") or "{}")
    return Config(
        models=json.loads(env.get("ESCALATION_MODELS", "") or
                          '{"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}'),
        prices={m: (_money(f"ESCALATION_PRICES[{m}] input", p[0]), _money(f"ESCALATION_PRICES[{m}] output", p[1]))
                for m, p in prices.items()},
        month_cap=_money("ESCALATION_MONTH_CAP_USD", env.get("ESCALATION_MONTH_CAP_USD", "150")),
        day_cap=_money("ESCALATION_DAY_CAP_USD", env.get("ESCALATION_DAY_CAP_USD", "7")),
        shared_daily_calls=_count("ESCALATION_DAILY_CALLS", env.get("ESCALATION_DAILY_CALLS", "15")),
        auto_daily_calls=_count("ESCALATION_AUTO_DAILY_CALLS", env.get("ESCALATION_AUTO_DAILY_CALLS", "5")),
        hard_daily_calls=_count("ESCALATION_HARD_DAILY_CALLS", env.get("ESCALATION_HARD_DAILY_CALLS", "3")),
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
    """An approximation, not a guarantee: UTF-8 bytes / 3, rounded up. It over-counts ordinary text, but it is not
    a true tokenizer. Lone surrogates encode as a replacement character instead of raising."""
    return math.ceil(len(text.encode("utf-8", errors="replace")) / 3)


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
        return 413, "brief_too_large", f"brief: {n} tokens, over the {cap} limit -- trim it"
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
    """Append-only JSONL of every attempt. Budgets are recomputed from it (cached on size+mtime), so a router
    restart loses nothing. In-flight calls hold a reservation of their worst-case cost until they finish, so two
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
