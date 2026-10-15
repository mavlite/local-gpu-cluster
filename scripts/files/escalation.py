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
