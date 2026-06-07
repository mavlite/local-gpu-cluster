"""
LLM cluster router (V620-only — Phase 7 of setup-runbook.md).

All upstreams live on LXC 151:
  POST /v1/chat/completions  -> V620 chat (port 8080, tensor-split, in-process spec decode)
  POST /v1/completions       -> V620 chat (legacy completions / FIM passthrough)
  POST /v1/embeddings        -> V620 embedder (port 8082, --main-gpu 0, --pooling last)
  POST /v1/rerank            -> V620 reranker (port 8083, --main-gpu 1, --reranking)
  GET  /v1/models            -> aggregated list from chat + embed + rerank
  GET  /healthz              -> upstream availability probe (unauthed)
  GET  /metrics              -> Prometheus instrumentation (IP-allowlist gated)

Features (V620-only build):
  - Bearer auth on inbound (ROUTER_API_KEY) and outbound (LLAMACPP_API_KEY).
  - asyncio.Semaphore admission control (chat=N, embed=4).
  - Per-route token-budget admission via upstream /tokenize calls (chat input cap,
    embed input cap; reject 413 if over budget).
  - slowapi per-IP rate limiting.
  - prometheus-fastapi-instrumentator middleware for /metrics.
  - Fail-open SSE on upstream 5xx: emits a `service degraded` data frame so
    AnythingLLM can fall back without breaking the stream.
  - SSE keepalive (`: ping` every KEEPALIVE_INTERVAL seconds) during long generations.
  - Per-request <think>...</think> stripping (header > body field > system prompt > model alias).
  - Per-request [CONTEXT N] / (Context 0, 1) marker stripping (Qwen3.6 prior).
  - Qwen3 Embedding compliance: appends <|endoftext|> if missing.
  - Structured access logging (model, tokens_in, tokens_out, duration, client_ip).
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from logging.handlers import RotatingFileHandler
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from prometheus_fastapi_instrumentator import Instrumentator

# ---------- Configuration (loaded from EnvironmentFile=/etc/router.env) ----------

V620_URL = os.environ.get("V620_URL", "http://192.168.6.151:8080")
EMBED_URL = os.environ.get("EMBED_URL", "http://192.168.6.151:8082")
RERANK_URL = os.environ.get("RERANK_URL", "http://192.168.6.151:8083")
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "12"))

# Wall-clock cap on a single streaming generation. Without this, a wedged
# llama-server (OOM, ROCm driver hang) would hold its chat_sem slot forever
# and deadlock the router once CHAT_CONCURRENCY slots are stuck. Set high
# enough that legitimate long generations finish (e.g., 1500-token essay at
# ~30 t/s = ~50s; agent loops with thinking can run minutes).
MAX_STREAM_SECONDS = int(os.environ.get("MAX_STREAM_SECONDS", "900"))

# Auth: ROUTER_API_KEY gates inbound; LLAMACPP_API_KEY is sent on outbound calls.
ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY", "")
LLAMACPP_API_KEY = os.environ.get("LLAMACPP_API_KEY", "")

# Admission control. CHAT_CONCURRENCY bumped 1→2 in 2026-05 for coding-agent
# workflows: tools like Cline / OpenCode fire parallel chat calls (main agent +
# sub-agent context). Two concurrent chats split the 4 llama.cpp slots evenly
# (~33 t/s each) — small hit per request but agent UX is much smoother.
CHAT_CONCURRENCY = int(os.environ.get("CHAT_CONCURRENCY", "2"))
EMBED_CONCURRENCY = int(os.environ.get("EMBED_CONCURRENCY", "4"))
MAX_CHAT_INPUT_TOKENS = int(os.environ.get("MAX_CHAT_INPUT_TOKENS", "120000"))
MAX_EMBED_INPUT_TOKENS = int(os.environ.get("MAX_EMBED_INPUT_TOKENS", "16384"))

# Rate limit (slowapi syntax: "60/minute")
RATE_LIMIT_CHAT = os.environ.get("RATE_LIMIT_CHAT", "60/minute")
RATE_LIMIT_EMBED = os.environ.get("RATE_LIMIT_EMBED", "200/minute")
RATE_LIMIT_TAVILY = os.environ.get("RATE_LIMIT_TAVILY", "30/minute")

# Tavily proxy. Holds the Tavily key server-side so browser-side clients
# (e.g. the external HTML reporting artifact) can do real web
# search without ever seeing the key. Set TAVILY_API_KEY in /etc/router.env
# to enable; if empty, /v1/tavily/search returns 503.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = os.environ.get("TAVILY_URL", "https://api.tavily.com/search")
TAVILY_EXTRACT_URL = os.environ.get("TAVILY_EXTRACT_URL", "https://api.tavily.com/extract")
TAVILY_CRAWL_URL = os.environ.get("TAVILY_CRAWL_URL", "https://api.tavily.com/crawl")
TAVILY_MAP_URL = os.environ.get("TAVILY_MAP_URL", "https://api.tavily.com/map")
# Whitelist of body fields we forward to Tavily Search. Anything else in the
# client request is dropped. We forward include_raw_content because clients
# (e.g. the external HTML reporting artifact) need full advisory text
# to mine identifiers like CVE-YYYY-NNNNN that don't fit in the 400-char
# `content` snippet. images / favicon stay omitted to keep response sizes
# bounded — we don't currently expose any client that needs them.
TAVILY_ALLOWED_FIELDS = {
    "query", "search_depth", "chunks_per_source", "max_results",
    "topic", "time_range", "start_date", "end_date", "days",
    "include_answer", "include_raw_content",
    "include_domains", "exclude_domains",
    "country", "auto_parameters", "exact_match",
}

# ---------- Server-side tool execution ----------
# When a chat completion request includes `"tool_execution": "server"`, the
# router runs the OpenAI tools/tool_calls multi-turn loop internally instead
# of returning tool_calls to the client. Each iteration: send messages +
# tools upstream → upstream emits tool_calls → router executes each tool via
# the registry → results appended as role:"tool" messages → repeat. Loop
# terminates when the model returns a non-tool-call response or MAX_TOOL_ITERATIONS
# is reached. Default is "client" (legacy behavior — tool_calls pass through
# to the caller for client-side execution, as OpenCode / Cline / Continue
# expect).
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "10"))
TOOL_EXECUTION_DEFAULT = os.environ.get("TOOL_EXECUTION_DEFAULT", "client")

# web_fetch tool safety. Direct HTTPS (or HTTP) GET with explicit denylist
# of metadata-service endpoints and private/loopback ranges to prevent SSRF.
# Max response size cap and timeout cap prevent runaway downloads.
WEB_FETCH_MAX_SIZE_BYTES = int(os.environ.get("WEB_FETCH_MAX_SIZE_KB", "1024")) * 1024
WEB_FETCH_TIMEOUT_SECONDS = int(os.environ.get("WEB_FETCH_TIMEOUT_SECONDS", "15"))
WEB_FETCH_DENY_HOSTS = {
    # Cloud metadata endpoints — block to prevent SSRF to instance creds.
    "169.254.169.254", "metadata.google.internal", "metadata", "169.254.170.2",
    # Loopback / link-local. The cluster is on 192.168.6.0/24 but a tool
    # call to one of our own LXCs (e.g., the router itself) would be a
    # weird recursion vector. Block by default.
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
}

# /metrics IP allowlist (comma-separated)
METRICS_ALLOWED_IPS = {
    ip.strip() for ip in os.environ.get("METRICS_ALLOWED_IPS", "127.0.0.1").split(",") if ip.strip()
}

# CORS: needed when an HTML page loaded from file:// (Origin: null) or a
# different LAN origin calls this router directly via fetch(). Without this,
# browsers refuse to expose the response to JS even when the server accepts
# the request. Default "*" is OK because this router lives on a LAN-only
# IP behind a firewall AND requires Bearer auth on every protected endpoint
# (header-based auth doesn't trigger the credentials/wildcard conflict).
# Tighten to a specific origin list (e.g., "https://app.lan,http://192.168.6.150")
# once you know the legitimate caller set.
CORS_ALLOW_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()
]

# Finite timeouts. Streaming uses timeout=None because long generations are normal.
CHAT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=5.0)
SMALL_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

# ---------- Structured access logging ----------
# One JSON line per request to /var/log/llm-router/access.log (rotated at 50MB,
# keep 5 backups). Logs: timestamp, route, model, input_tokens, output_tokens,
# duration_ms, http_status, client_ip. Useful for debugging slow completions
# and tracking per-model usage over time.
ACCESS_LOG_PATH = os.environ.get("ACCESS_LOG_PATH", "/var/log/llm-router/access.log")
access_logger = logging.getLogger("llm-router.access")
access_logger.setLevel(logging.INFO)
access_logger.propagate = False
try:
    os.makedirs(os.path.dirname(ACCESS_LOG_PATH), exist_ok=True)
    _h = RotatingFileHandler(ACCESS_LOG_PATH, maxBytes=50 * 1024 * 1024, backupCount=5)
    _h.setFormatter(logging.Formatter("%(message)s"))
    access_logger.addHandler(_h)
except (OSError, PermissionError):
    # If we can't write the log file (e.g., perms during dev), fall back to stderr
    access_logger.addHandler(logging.StreamHandler())


def log_access(route: str, model: str, input_tokens: int, output_tokens: int,
               duration_ms: int, status: int, client_ip: str, error: Optional[str] = None) -> None:
    entry = {
        "ts": time.time(),
        "route": route,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "status": status,
        "client_ip": client_ip,
    }
    if error:
        entry["error"] = error
    try:
        access_logger.info(json.dumps(entry))
    except Exception:
        pass  # never let logging break a request

# ---------- App + middleware ----------

limiter = Limiter(key_func=get_remote_address, default_limits=[])
app = FastAPI(title="LLM Cluster Router (V620-only)")
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
# CORS middleware registered AFTER SlowAPI so it sits OUTERMOST. FastAPI executes
# middleware in reverse-registration order on requests, so OPTIONS preflight is
# answered by CORS before reaching the rate-limit counter (preflights should not
# consume rate-limit budget).
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    # NOTE: _error_body is defined later in the file but referenced here at
    # request time, not import time, so the forward reference is fine.
    return JSONResponse(
        _error_body("rate_limit_exceeded", str(exc.detail)),
        status_code=429,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Wrap FastAPI's default {"detail": "..."} shape into the OpenAI-style
    # {"error": {"type": "...", "message": "...", "code": null}} envelope.
    # Without this, raises like `raise HTTPException(413, detail="...")` go
    # out as a flat detail field that breaks OpenAI-compatible clients (Zod
    # validators in AI-SDK / OpenCode reject responses lacking `choices` AND
    # an `error` object).
    type_map = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        413: "request_too_large",
        429: "rate_limit_exceeded",
        500: "internal_error",
        502: "bad_gateway",
        503: "service_unavailable",
        504: "gateway_timeout",
    }
    return JSONResponse(
        _error_body(type_map.get(exc.status_code, "error"), str(exc.detail)),
        status_code=exc.status_code,
    )


# Bearer auth middleware. /healthz and /metrics are exempt (the latter has its own
# IP-allowlist check). CORS preflight OPTIONS requests are also exempt because
# they cannot carry credentials per the CORS spec — auth must skip them so the
# CORSMiddleware can answer with the access-control-allow-* headers. Without
# this skip, browsers loaded from file:// (or any cross-origin page) get 403 on
# the preflight and the actual request never fires.
@app.middleware("http")
async def require_bearer(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.url.path in ("/healthz", "/metrics"):
        return await call_next(request)
    if not ROUTER_API_KEY:
        return JSONResponse(
            _error_body("router_misconfigured", "ROUTER_API_KEY not set in /etc/router.env"),
            status_code=503,
        )
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse(_error_body("unauthorized", "missing Bearer token"), status_code=403)
    presented = auth[7:].strip()
    if not secrets.compare_digest(presented, ROUTER_API_KEY):
        return JSONResponse(_error_body("unauthorized", "invalid Bearer token"), status_code=403)
    return await call_next(request)


# Prometheus instrumentation. The middleware exposes /metrics automatically.
Instrumentator(should_group_status_codes=False).instrument(app).expose(
    app, endpoint="/metrics", include_in_schema=False
)


# /metrics IP allowlist — applied AFTER the instrumentator registers the route.
@app.middleware("http")
async def metrics_ip_allowlist(request: Request, call_next):
    if request.url.path == "/metrics":
        client_ip = request.client.host if request.client else ""
        if client_ip not in METRICS_ALLOWED_IPS:
            return PlainTextResponse("metrics endpoint restricted", status_code=403)
    return await call_next(request)


# ---------- Admission control semaphores ----------

chat_sem = asyncio.Semaphore(CHAT_CONCURRENCY)
embed_sem = asyncio.Semaphore(EMBED_CONCURRENCY)


# ---------- Auth headers for upstream calls ----------

def upstream_headers(extra: Optional[dict] = None) -> dict:
    headers = dict(extra) if extra else {}
    if LLAMACPP_API_KEY:
        headers["Authorization"] = f"Bearer {LLAMACPP_API_KEY}"
    headers.setdefault("Content-Type", "application/json")
    return headers


# ---------- Strip-thinking heuristic ----------

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
NOTHINK_HINT = re.compile(r"/no_think|hide thinking|strip reasoning", re.IGNORECASE)
RAG_MODEL_RE = re.compile(r"(^rag-|-rag$)", re.IGNORECASE)

# AnythingLLM-style RAG chunk markers leak into model output even after explicit
# prompt-level instructions to suppress them. Qwen3.6 has a strong training prior
# to write "[CONTEXT 1]", "[Context 0]", "(Context 0, 1)", etc. as citation
# references to the numbered chunks AnythingLLM passes in the system prompt.
# These are visible noise to end users. Strip them server-side.
#
# Handled patterns:
#   [CONTEXT 1]              -> ""
#   [Context 0]              -> ""
#   (Context 0)              -> ""
#   (Context 0, 1)           -> ""
#   (Context 0, Context 1)   -> ""
#   [CONTEXT 1] [CONTEXT 9]  -> ""   (consecutive, eats interior whitespace)
# Leading whitespace is consumed so "claim [CTX 1]." -> "claim."
CONTEXT_MARKER_RE = re.compile(
    r"\s*[\[\(]\s*(?:CONTEXT|Context|context)\s+\d+(?:\s*,\s*(?:(?:CONTEXT|Context|context)\s+)?\d+)*\s*[\]\)]"
)
STRIP_CONTEXT_MARKERS = os.environ.get("STRIP_CONTEXT_MARKERS", "true").lower() in ("true", "1", "yes")


# ---------- Client-facing chat aliases ----------
#
# Qwen3.6 was post-trained with a thinking-first reasoning protocol baked in:
# the model emits a <think>...</think> block, THEN the answer. Benchmarks
# (HumanEval, SWE-bench, MMLU-Pro) are measured with thinking enabled.
# Disabling thinking doesn't give "the same model, faster" — it gives a
# meaningfully weaker variant. So we expose two virtual aliases for the same
# underlying chat upstream:
#
#   rag-qwen3.6     thinking OFF — used by AnythingLLM. RAG synthesis is
#                   chunk-formatting, not from-scratch reasoning; the thinking
#                   block is pure latency overhead and can blow max_tokens
#                   before the actual answer finishes. Thinking-off also
#                   prevents Qwen3.6's plain-text reasoning bleed into
#                   user-facing content for RAG UIs that don't render the
#                   reasoning_content field separately.
#   qwen3.6-think   thinking ON — used by OpenCode / Cline / coding agents.
#                   The wall-clock cost (10-30s of pre-token reasoning) is
#                   recouped many times over by correct-on-first-try output.
#                   The router's SSE keepalive (KEEPALIVE_INTERVAL=12-15s)
#                   prevents the client's read timer from firing during the
#                   pre-token window.
#   qwen3.6         convenience alias for qwen3.6-think.
#
# Both chat aliases resolve to the SAME backend (llama-server's --alias
# rag-qwen3.6) — no extra VRAM, no second model. The only thing that changes
# per-alias is the chat_template_kwargs.enable_thinking value the router
# injects before forwarding.
#
# Schema per entry:
#   backend          : str  — the alias llama-server actually knows.
#                             Router rewrites body["model"] to this before
#                             forwarding upstream.
#   enable_thinking  : bool — value to inject into chat_template_kwargs.
#                             Client-supplied chat_template_kwargs takes
#                             precedence (we setdefault, not overwrite).
#   strip_thinking   : bool — whether to regex-strip <think>...</think> from
#                             the response. Belt-and-suspenders for
#                             enable_thinking=False cases where the model
#                             ignores the template kwarg.
ALIAS_MAP: dict[str, dict] = {
    "rag-qwen3.6":   {"backend": "rag-qwen3.6", "enable_thinking": False, "strip_thinking": True},
    "qwen3.6-think": {"backend": "rag-qwen3.6", "enable_thinking": True,  "strip_thinking": False},
    "qwen3.6":       {"backend": "rag-qwen3.6", "enable_thinking": True,  "strip_thinking": False},
    # Throughput-prioritized UD-Q4_K_M variant of qwen3.6 (swap-chat-model.sh
    # profile "qwen3.6-fast"; LLAMA_ALIAS=rag-qwen3.6-fast). Same template and
    # thinking semantics as the default Q6_K profile, just smaller weights for
    # higher t/s. Renamed from "qwen3.6-hi" on 2026-05-27 when UD-Q6_K became
    # the default — Q4 is now the *fast* alternative, not a "high-precision"
    # variant. Aliases only resolve when the chat unit is actually serving
    # this backend.
    "rag-qwen3.6-fast":   {"backend": "rag-qwen3.6-fast", "enable_thinking": False, "strip_thinking": True},
    "qwen3.6-fast-think": {"backend": "rag-qwen3.6-fast", "enable_thinking": True,  "strip_thinking": False},
    "qwen3.6-fast":       {"backend": "rag-qwen3.6-fast", "enable_thinking": True,  "strip_thinking": False},
    # Qwen3-Coder-Next aliases. These resolve only when the chat unit is
    # actually serving the Coder-Next backend (config.env LLAMA_ALIAS=qwen3-coder).
    # If Qwen3.6 is still loaded, requests to these aliases get rewritten to
    # backend="qwen3-coder" but llama-server serves whatever model is loaded
    # regardless of the requested name — useful for one-line A/B testing but
    # something to be aware of.
    #
    # Coder-Next doesn't use Qwen3.6's thinking-mode template, so leave
    # enable_thinking=None (don't inject) and strip_thinking=False (don't
    # regex-strip — Coder typically doesn't emit <think> blocks).
    "qwen3-coder":      {"backend": "qwen3-coder", "enable_thinking": None, "strip_thinking": False},
    "qwen3-coder-next": {"backend": "qwen3-coder", "enable_thinking": None, "strip_thinking": False},
}


def resolve_alias(model: str) -> dict:
    """Resolve a client-facing chat alias to backend behavior.

    Returns a dict with keys:
        backend          : str  — alias name to send upstream
        enable_thinking  : bool|None — value for chat_template_kwargs
                                       (None = don't inject, let Qwen3.6 default)
        strip_thinking   : bool — whether to regex-strip <think> tags from response

    Unknown models fall back to the legacy RAG_MODEL_RE heuristic for
    backward compatibility, then to passthrough (no rewrite, no injection).
    """
    if model in ALIAS_MAP:
        return ALIAS_MAP[model]
    # Legacy fallback: any rag-* / -rag model name gets thinking-off behavior.
    if RAG_MODEL_RE.search(model):
        return {"backend": model, "enable_thinking": False, "strip_thinking": True}
    # Unknown model: pass through unchanged. llama-server will warn but serve
    # the loaded model regardless of the requested name.
    return {"backend": model, "enable_thinking": None, "strip_thinking": False}


def should_strip_thinking(body: dict, header_value: Optional[str]) -> bool:
    # Explicit client overrides win in all cases.
    if header_value is not None:
        return header_value.lower() in ("true", "1", "yes")
    if "strip_thinking" in body:
        return bool(body["strip_thinking"])
    # System-prompt hint (legacy AnythingLLM pattern).
    msgs = body.get("messages", [])
    if msgs and msgs[0].get("role") == "system":
        if NOTHINK_HINT.search(msgs[0].get("content", "")):
            return True
    # Alias-based default. resolve_alias handles both ALIAS_MAP entries and
    # the legacy RAG_MODEL_RE regex fallback for unknown rag-* names.
    return resolve_alias(body.get("model", "")).get("strip_thinking", False)


# ---------- Token-budget admission via upstream /tokenize ----------

async def count_tokens(client: httpx.AsyncClient, upstream: str, text: str) -> int:
    """Return token count from llama.cpp's /tokenize endpoint. Returns -1 on failure
    (caller should treat as 'unknown — allow') to avoid hard-failing if /tokenize is
    unreachable; the per-IP rate limit is a backstop."""
    try:
        r = await client.post(f"{upstream}/tokenize", json={"content": text}, headers=upstream_headers(), timeout=SMALL_TIMEOUT)
        if r.status_code == 200:
            return len(r.json().get("tokens", []))
    except Exception:
        return -1
    return -1


def _approx_text_from_messages(messages: list) -> str:
    """Concatenate message contents for a /tokenize call. Doesn't reproduce the chat
    template exactly but is a close upper bound for admission control."""
    parts = []
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for piece in c:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(piece.get("text", ""))
    return "\n".join(parts)


# ---------- Fail-open SSE on upstream 5xx ----------

def _error_body(error_type: str, message: str) -> dict:
    """OpenAI-compatible error envelope.

    OpenAI SDKs (and AI-SDK / OpenCode's zod schema) expect `error` to be an
    OBJECT with at least `type` and `message`. Returning {"error": "string"}
    breaks the client-side validator (it tries to parse the response as
    either a chat-completion or an error, fails both, and surfaces a
    cryptic Zod union error to the user). Always emit this envelope shape.
    """
    return {"error": {"type": error_type, "message": message, "code": None}}


DEGRADED_FRAME = (
    "data: " + json.dumps(_error_body("service_degraded", "upstream returned an error; retry shortly"))
    + "\n\n"
).encode()
DONE_FRAME = b"data: [DONE]\n\n"


async def sse_stream_with_keepalive(upstream_url: str, payload: dict, strip_thinking: bool):
    """Stream upstream SSE with proactive keepalive frames.

    The previous structure was:
        async with client.stream("POST", ...) as r:
            async for chunk in r.aiter_text():
                ... keepalive loop ...

    That had a fatal bug: `async with client.stream(...) as r:` blocks until
    the upstream returns response headers. For long prompts (15-30K+ tokens)
    Qwen3.6 prompt-processing can take 60-120 seconds before llama-server
    flushes the SSE response headers. During that window, the keepalive
    loop hasn't started running yet — NO `: ping` frames reach the client.
    OpenCode's hardcoded ~2-minute SSE read timer fires, the connection
    drops, and the user sees "SSE read timed out" with zero data received.

    The fix:
      1. Yield a `: ping` frame IMMEDIATELY, before any upstream work, so
         the client sees data on its socket the instant the streaming
         response begins.
      2. Run the upstream connection inside a background asyncio task so
         the main generator can keep yielding keepalives while httpx is
         blocked waiting for upstream response headers.

    The queue carries (msg_type, payload) tuples: ("data", str chunk),
    ("degraded", None), or ("eof", None). The main loop drains the queue
    with KEEPALIVE_INTERVAL timeout and yields `: ping` on timeout.

    Also caps the total stream wall-clock at MAX_STREAM_SECONDS so a
    wedged upstream can't hold its chat_sem slot forever.
    """
    stream_started = time.monotonic()

    # (1) Immediate keepalive — flushes a byte to the client right away
    # so its SSE read timer starts in a known state. Without this, the
    # client could time out during the upstream-connect-and-headers
    # window even if our keepalive loop is otherwise correct.
    yield b": ping\n\n"

    # (2) Background task for the upstream call. Everything that could
    # block — opening the connection, sending the POST, waiting for
    # response headers, draining the body — happens here, off the main
    # generator's path.
    queue: asyncio.Queue = asyncio.Queue()

    async def upstream_reader():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", upstream_url, json=payload, headers=upstream_headers()
                ) as r:
                    if r.status_code >= 500:
                        await queue.put(("degraded", None))
                        return
                    async for chunk in r.aiter_text():
                        await queue.put(("data", chunk))
                    await queue.put(("eof", None))
        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ):
            await queue.put(("degraded", None))
        except Exception:
            # Any other unexpected error from httpx — degrade rather than
            # leak a 500 from the generator into the SSE stream.
            await queue.put(("degraded", None))

    task = asyncio.create_task(upstream_reader())

    try:
        while True:
            if time.monotonic() - stream_started > MAX_STREAM_SECONDS:
                yield DEGRADED_FRAME
                yield DONE_FRAME
                return
            try:
                msg_type, payload_chunk = await asyncio.wait_for(
                    queue.get(), timeout=KEEPALIVE_INTERVAL
                )
            except asyncio.TimeoutError:
                # No upstream data within KEEPALIVE_INTERVAL — could mean
                # we're still in prompt-processing on the upstream, or
                # the upstream paused between thinking chunks. Either
                # way, keep the client connection alive.
                yield b": ping\n\n"
                continue
            if msg_type == "degraded":
                yield DEGRADED_FRAME
                yield DONE_FRAME
                return
            if msg_type == "eof":
                break
            # msg_type == "data"
            out = THINK_RE.sub("", payload_chunk) if strip_thinking else payload_chunk
            if STRIP_CONTEXT_MARKERS:
                out = CONTEXT_MARKER_RE.sub("", out)
            yield out.encode()
    finally:
        task.cancel()


# ---------- Tool registry + handlers ----------
#
# Server-side tool execution. When a chat completion request includes
# `"tool_execution": "server"`, the router runs the OpenAI tools/tool_calls
# multi-turn loop internally instead of returning tool_calls to the client.
# This lets browser-side clients (e.g. external HTML reporting
# artifact) get tool-augmented answers without implementing the dispatcher
# loop themselves.
#
# Clients that want to keep doing their own tool execution (OpenCode, Cline,
# Continue) get unchanged behavior — the default tool_execution is "client".


TAVILY_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tavily_search",
        "description": (
            "Search the web. Returns ranked results with titles, URLs, and "
            "content snippets. Use for current events, product releases, or "
            "facts beyond the model's training cutoff."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "search_depth": {"type": "string", "enum": ["basic", "advanced"],
                                 "description": "basic = fast / shallow; advanced = slower / deeper"},
                "max_results": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                "topic": {"type": "string", "enum": ["general", "news", "finance"],
                          "description": "Default 'general'; use 'news' for recent events"},
                "time_range": {"type": "string", "enum": ["day", "week", "month", "year"],
                               "description": "Recency filter"},
                "include_domains": {"type": "array", "items": {"type": "string"},
                                    "description": "Only return results from these domains"},
                "exclude_domains": {"type": "array", "items": {"type": "string"},
                                    "description": "Exclude these domains from results"},
            },
            "required": ["query"],
        },
    },
}

TAVILY_EXTRACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tavily_extract",
        "description": (
            "Extract full content from one or more URLs. Use when tavily_search "
            "results don't give enough detail and you need the actual page "
            "contents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"},
                         "description": "List of URLs to extract content from"},
                "extract_depth": {"type": "string", "enum": ["basic", "advanced"],
                                  "description": "advanced extracts more thoroughly"},
                "include_images": {"type": "boolean", "default": False},
                "format": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            },
            "required": ["urls"],
        },
    },
}

TAVILY_CRAWL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tavily_crawl",
        "description": (
            "Crawl a website following links to gather content. Use when you "
            "need broad coverage of a site, not just one page. Expensive — "
            "set max_depth and limit conservatively."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Starting URL"},
                "max_depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5},
                "max_breadth": {"type": "integer", "default": 10,
                                "description": "Max links followed per page"},
                "limit": {"type": "integer", "default": 20, "description": "Max total pages"},
                "instructions": {"type": "string",
                                 "description": "Natural-language guidance for what to focus on"},
                "select_paths": {"type": "array", "items": {"type": "string"},
                                 "description": "Regex paths to include"},
                "exclude_paths": {"type": "array", "items": {"type": "string"},
                                  "description": "Regex paths to exclude"},
            },
            "required": ["url"],
        },
    },
}

TAVILY_MAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tavily_map",
        "description": (
            "Map a website's URL structure (URLs only, no content). Use to "
            "scope a site before doing deeper extraction with tavily_extract "
            "or tavily_crawl."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Starting URL"},
                "max_depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5},
                "limit": {"type": "integer", "default": 50},
                "instructions": {"type": "string", "description": "Natural-language guidance"},
            },
            "required": ["url"],
        },
    },
}

WEB_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch a single public HTTP(S) URL and return its body (capped at "
            "1 MB). Use for known pages — prefer tavily_search for discovery. "
            "Cannot access private networks, loopback, or cloud metadata endpoints."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public HTTP or HTTPS URL"},
            },
            "required": ["url"],
        },
    },
}


async def _tavily_call(endpoint: str, payload: dict) -> dict:
    """POST to a Tavily endpoint with auth, return parsed JSON or an error envelope."""
    if not TAVILY_API_KEY:
        return {"error": "tavily_unconfigured",
                "message": "TAVILY_API_KEY not set in /etc/router.env"}
    async with httpx.AsyncClient(timeout=SMALL_TIMEOUT) as c:
        try:
            r = await c.post(endpoint, json=payload, headers={
                "Authorization": f"Bearer {TAVILY_API_KEY}",
                "Content-Type": "application/json",
            })
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
            return {"error": "tavily_unreachable", "message": type(e).__name__}
    if r.status_code >= 400:
        return {"error": f"tavily_http_{r.status_code}", "body": (r.text or "")[:500]}
    try:
        return r.json()
    except ValueError:
        return {"error": "tavily_invalid_json", "body": (r.text or "")[:500]}


async def _tool_tavily_search(args: dict) -> dict:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "missing 'query' field"}
    forwarded = {k: v for k, v in args.items() if k in TAVILY_ALLOWED_FIELDS}
    return await _tavily_call(TAVILY_URL, forwarded)


async def _tool_tavily_extract(args: dict) -> dict:
    urls = args.get("urls") or args.get("url")
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls:
        return {"error": "missing 'urls' (list of strings)"}
    allowed = {"urls", "extract_depth", "include_images", "include_favicon", "format"}
    forwarded = {k: v for k, v in args.items() if k in allowed}
    forwarded["urls"] = urls
    return await _tavily_call(TAVILY_EXTRACT_URL, forwarded)


async def _tool_tavily_crawl(args: dict) -> dict:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"error": "missing 'url' field"}
    allowed = {
        "url", "max_depth", "max_breadth", "limit", "instructions",
        "select_paths", "select_domains", "exclude_paths", "exclude_domains",
        "allow_external", "categories", "extract_depth", "format",
    }
    forwarded = {k: v for k, v in args.items() if k in allowed}
    return await _tavily_call(TAVILY_CRAWL_URL, forwarded)


async def _tool_tavily_map(args: dict) -> dict:
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"error": "missing 'url' field"}
    allowed = {
        "url", "max_depth", "max_breadth", "limit", "instructions",
        "select_paths", "select_domains", "exclude_paths", "exclude_domains",
        "allow_external", "categories",
    }
    forwarded = {k: v for k, v in args.items() if k in allowed}
    return await _tavily_call(TAVILY_MAP_URL, forwarded)


async def _tool_web_fetch(args: dict) -> dict:
    """HTTPS (or HTTP) GET with SSRF guards and size cap.

    SSRF strategy: resolve the hostname via getaddrinfo and reject if ANY
    returned address is private/loopback/link-local/reserved, then connect
    normally. This covers IPv4, IPv6 (including IPv4-mapped IPv6 like
    ::ffff:192.168.x.x and ULA fc00::/7), and avoids the brittle prefix-string
    matching the earlier guard used. A narrow DNS-rebinding window still
    exists between resolution and connect, but for a single-shot GET with no
    keepalive the gap is small enough not to be a usable primitive without
    sub-millisecond TTLs the kernel resolver caches over anyway.
    """
    import urllib.parse
    import ipaddress
    import socket

    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"error": "missing 'url' field"}

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return {"error": "invalid_url", "message": str(e)}

    if parsed.scheme not in ("http", "https"):
        return {"error": "scheme_not_allowed", "scheme": parsed.scheme}

    host = (parsed.hostname or "").lower()
    if not host:
        return {"error": "missing_host"}
    if host in WEB_FETCH_DENY_HOSTS:
        return {"error": "host_denied", "host": host}

    # Resolve and inspect every returned address. Reject if any is non-global.
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        return {"error": "dns_resolution_failed", "message": str(e)}

    for info in infos:
        sockaddr = info[4]
        addr_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            return {"error": "host_denied_unparseable_ip", "host": host, "addr": addr_str}
        # `is_global` is True only for routable public addresses. Excludes
        # private, loopback, link-local, multicast, reserved, unspecified,
        # and (for IPv6) site-local + ULA. Also blocks IPv4-mapped IPv6
        # forms like ::ffff:192.168.x.x because ipaddress unwraps them.
        if not ip.is_global:
            return {
                "error": "host_denied_private_range",
                "host": host,
                "resolved": addr_str,
            }

    headers = {
        "User-Agent": "local-gpu-cluster-router/1.0 (web_fetch)",
        "Accept": "text/html, text/plain, application/json, application/xml;q=0.9, */*;q=0.1",
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(WEB_FETCH_TIMEOUT_SECONDS),
        follow_redirects=True,
    ) as c:
        try:
            r = await c.get(url, headers=headers)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
            return {"error": "unreachable", "message": type(e).__name__}

    body_bytes = (r.content or b"")[:WEB_FETCH_MAX_SIZE_BYTES]
    truncated = bool(r.content and len(r.content) > WEB_FETCH_MAX_SIZE_BYTES)
    try:
        body_text = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        body_text = repr(body_bytes)

    return {
        "status": r.status_code,
        "url": str(r.url),
        "content_type": r.headers.get("content-type", ""),
        "content_length": len(r.content) if r.content else 0,
        "truncated": truncated,
        "body": body_text,
    }


TOOLS: dict[str, dict] = {
    "tavily_search": {"handler": _tool_tavily_search, "schema": TAVILY_SEARCH_SCHEMA},
    "tavily_extract": {"handler": _tool_tavily_extract, "schema": TAVILY_EXTRACT_SCHEMA},
    "tavily_crawl": {"handler": _tool_tavily_crawl, "schema": TAVILY_CRAWL_SCHEMA},
    "tavily_map": {"handler": _tool_tavily_map, "schema": TAVILY_MAP_SCHEMA},
    "web_fetch": {"handler": _tool_web_fetch, "schema": WEB_FETCH_SCHEMA},
}


def registered_tool_schemas() -> list:
    """Return all registered tool schemas as a list (for injection into upstream
    chat completion requests when client didn't supply tools explicitly)."""
    return [t["schema"] for t in TOOLS.values()]


def _format_tavily_search(result: dict) -> str:
    """Flatten a Tavily Search response into model-friendly markdown so the
    model doesn't have to traverse nested JSON to extract results."""
    lines = []
    if result.get("answer"):
        lines.append(f"**Tavily answer:** {result['answer']}\n")
    results = result.get("results") or []
    if not results:
        return "No results found." if not lines else "\n".join(lines) + "\nNo results."
    lines.append(f"{len(results)} results:\n")
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(no title)"
        url = r.get("url") or ""
        content = (r.get("content") or "").strip()
        score = r.get("score")
        score_str = f" (score: {score:.2f})" if isinstance(score, (int, float)) else ""
        lines.append(f"### {i}. {title}{score_str}")
        lines.append(f"URL: {url}")
        if content:
            lines.append(f"\n{content}\n")
    return "\n".join(lines)


def _format_tavily_extract(result: dict) -> str:
    """Flatten Tavily Extract response into markdown."""
    lines = []
    results = result.get("results") or []
    if not results:
        return "No content extracted."
    for r in results:
        url = r.get("url") or ""
        content = (r.get("raw_content") or r.get("content") or "").strip()
        lines.append(f"## {url}\n\n{content}\n")
    failed = result.get("failed_results") or []
    if failed:
        lines.append("\n**Failed URLs:**")
        for f in failed:
            lines.append(f"- {f.get('url', '')}: {f.get('error', '')}")
    return "\n".join(lines)


def _format_tavily_crawl_map(result: dict) -> str:
    """Flatten Tavily Crawl / Map response into markdown."""
    base_url = result.get("base_url") or "(unknown)"
    results = result.get("results") or []
    if not results:
        return f"No pages found under {base_url}."
    lines = [f"Crawled/mapped {len(results)} pages under {base_url}:\n"]
    for r in results:
        url = r.get("url") or ""
        content = (r.get("raw_content") or r.get("content") or "").strip()
        if content:
            lines.append(f"### {url}\n{content[:1500]}\n")
        else:
            lines.append(f"- {url}")
    return "\n".join(lines)


def _format_tool_result(name: str, result) -> str:
    """Convert a tool handler's return value into a string suitable for the
    role:"tool" message content field. Tavily endpoints get flattened to
    markdown (much easier for the model to parse than nested JSON); others
    fall back to JSON.

    Errors always stay as JSON so they're structurally distinct and the
    model can detect failure mode programmatically.
    """
    if not isinstance(result, dict):
        return json.dumps(result, default=str)
    if "error" in result:
        return json.dumps(result, default=str)
    if name == "tavily_search":
        return _format_tavily_search(result)
    if name == "tavily_extract":
        return _format_tavily_extract(result)
    if name in ("tavily_crawl", "tavily_map"):
        return _format_tavily_crawl_map(result)
    # web_fetch / unknown — JSON pass-through
    return json.dumps(result, default=str)


def _summarize_tool_result(result_str: str) -> str:
    """Return a metadata-only summary of a tool result for access logs.

    The full text is intentionally NOT included — tool results frequently
    contain fetched page bodies (web_fetch), Tavily raw_content, or other
    user-data that we don't want persisted in /var/log/llm-router/access.log
    where it can outlive the request lifecycle. The summary returns size +
    any error class found in the JSON envelope, which is enough for ops
    debugging without retaining PII.
    """
    size = len(result_str)
    # Most tool errors come back as `{"error": "<class>", ...}` JSON. Sniff
    # the leading bytes cheaply rather than parsing the whole thing.
    head = result_str.lstrip()[:80]
    if head.startswith('{"error"'):
        try:
            obj = json.loads(result_str)
            if isinstance(obj, dict) and "error" in obj:
                return f"size={size} error={obj.get('error', 'unknown')!r}"
        except (ValueError, json.JSONDecodeError):
            pass
    return f"size={size} ok"


async def dispatch_tool(name: str, args_json: str) -> str:
    """Execute a tool by name. args_json is a JSON-encoded arguments string
    (as produced by the model). Returns a result string suitable for embedding
    in a role:"tool" message — markdown for Tavily endpoints, JSON for others
    and for errors."""
    if name not in TOOLS:
        return json.dumps({"error": "unknown_tool", "tool": name})
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": "invalid_json_arguments", "message": str(e)})
    if not isinstance(args, dict):
        return json.dumps({"error": "arguments_must_be_object"})
    try:
        result = await TOOLS[name]["handler"](args)
        return _format_tool_result(name, result)
    except Exception as e:
        return json.dumps({"error": "tool_execution_failed",
                           "type": type(e).__name__, "message": str(e)})


def _ensure_tool_call_ids(tool_calls: list) -> list:
    """Synthesize an ID for any tool_call missing one. Qwen3.6's jinja chat
    template uses tool_call_id to pair role:"tool" responses back to the
    assistant's tool_calls; if the model emitted calls without IDs, the
    pairing fails and the model effectively never sees the responses,
    causing it to call the tool again on the next turn (until max_iterations).
    """
    for tc in tool_calls:
        if not tc.get("id"):
            tc["id"] = f"call_{uuid.uuid4().hex[:24]}"
    return tool_calls


# ---------- Server-side tool execution: multi-turn loops ----------

class ToolCallAccumulator:
    """Aggregates tool_call deltas from upstream SSE chunks into complete
    tool_call objects. llama.cpp streams tool_calls as deltas keyed by index;
    each chunk may contain partial id / name / arguments fragments that
    must be concatenated in order."""

    def __init__(self):
        # Preserve insertion order so finalize() can return tool_calls in
        # the order the model emitted them.
        self.calls: dict = {}

    def feed(self, delta_tool_calls: list) -> None:
        for tc in delta_tool_calls or []:
            idx = tc.get("index", 0)
            cur = self.calls.setdefault(
                idx, {"id": None, "name": None, "arguments": ""}
            )
            if tc.get("id"):
                cur["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["name"] = fn["name"]
            if fn.get("arguments"):
                cur["arguments"] += fn["arguments"]

    def has_any(self) -> bool:
        return bool(self.calls)

    def finalize(self) -> list:
        return [
            {
                "id": v["id"],
                "type": "function",
                "function": {"name": v["name"], "arguments": v["arguments"]},
            }
            for v in self.calls.values()
        ]


async def tool_loop_stream(initial_body: dict, strip_thinking: bool, client_ip: str):
    """Multi-turn server-side tool-execution in streaming mode.

    Each iteration: send messages + tools to llama.cpp → accumulate tool_calls
    during the SSE stream → on finish_reason=tool_calls, run the tools via
    dispatch_tool, append results as role:"tool" messages, loop. On any other
    finish_reason, pass the final content delta + [DONE] through to the client.

    Hides tool_call deltas from the client by default — only content deltas
    flow downstream. Clients see what looks like a single streamed assistant
    response, possibly with a pause between iterations while tools execute.
    """
    yield b": ping\n\n"

    body = dict(initial_body)
    if "tools" not in body:
        body["tools"] = registered_tool_schemas()
    body["stream"] = True
    # tool_execution and the per-iteration messages list are router-internal;
    # don't forward to llama.cpp where they'd be ignored or warned about.
    body.pop("tool_execution", None)
    messages = list(body["messages"])
    started = time.monotonic()
    model_label = body.get("model", "?")

    for iteration in range(MAX_TOOL_ITERATIONS):
        if time.monotonic() - started > MAX_STREAM_SECONDS:
            yield DEGRADED_FRAME
            yield DONE_FRAME
            return

        per_iter = dict(body)
        per_iter["messages"] = messages
        accumulator = ToolCallAccumulator()
        assistant_content = ""
        finish_reason = None

        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream(
                    "POST", f"{V620_URL}/v1/chat/completions",
                    json=per_iter, headers=upstream_headers(),
                ) as r:
                    if r.status_code >= 500:
                        yield DEGRADED_FRAME
                        yield DONE_FRAME
                        return

                    buf = ""
                    async for raw in r.aiter_text():
                        buf += raw
                        # SSE events are terminated by blank lines
                        while "\n\n" in buf:
                            event, buf = buf.split("\n\n", 1)
                            for line in event.split("\n"):
                                if not line.startswith("data: "):
                                    continue
                                data = line[6:].strip()
                                if data == "[DONE]":
                                    finish_reason = finish_reason or "stop"
                                    continue
                                try:
                                    chunk = json.loads(data)
                                except json.JSONDecodeError:
                                    continue
                                choices = chunk.get("choices") or []
                                if not choices:
                                    continue
                                delta = choices[0].get("delta") or {}
                                fr = choices[0].get("finish_reason")
                                if fr:
                                    finish_reason = fr
                                if delta.get("tool_calls"):
                                    accumulator.feed(delta["tool_calls"])
                                    # do NOT forward tool_call deltas to client
                                if delta.get("content") is not None:
                                    assistant_content += delta["content"]
                                    out = delta["content"]
                                    if strip_thinking:
                                        out = THINK_RE.sub("", out)
                                    if STRIP_CONTEXT_MARKERS:
                                        out = CONTEXT_MARKER_RE.sub("", out)
                                    # Rebuild client chunk (skip any tool_call deltas)
                                    client_chunk = {
                                        "id": chunk.get("id"),
                                        "object": "chat.completion.chunk",
                                        "created": chunk.get("created"),
                                        "model": chunk.get("model"),
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": out},
                                            "finish_reason": None,
                                        }],
                                    }
                                    yield f"data: {json.dumps(client_chunk)}\n\n".encode()
        except (
            httpx.ConnectError, httpx.ReadError,
            httpx.TimeoutException, httpx.RemoteProtocolError,
        ):
            yield DEGRADED_FRAME
            yield DONE_FRAME
            return

        if finish_reason == "tool_calls" and accumulator.has_any():
            tool_calls = _ensure_tool_call_ids(accumulator.finalize())
            messages.append({
                "role": "assistant",
                "content": assistant_content or None,
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = tc["function"]
                result_str = await dispatch_tool(fn["name"], fn["arguments"])
                log_access(
                    "/v1/chat/completions:tool", fn["name"],
                    len(fn.get("arguments") or ""), len(result_str),
                    int((time.monotonic() - started) * 1000),
                    200, client_ip,
                    error=(f"iter={iteration+1}/{MAX_TOOL_ITERATIONS}"
                           f" id={tc.get('id') or 'NULL'}"
                           f" args_size={len(fn.get('arguments') or '')}"
                           f" result_summary={_summarize_tool_result(result_str)}"),
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })
            continue

        # Final response — emit terminator and exit
        if finish_reason:
            final = {
                "object": "chat.completion.chunk",
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }],
            }
            yield f"data: {json.dumps(final)}\n\n".encode()
        yield b"data: [DONE]\n\n"
        return

    # MAX_TOOL_ITERATIONS exceeded
    final = {
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {"content": "\n\n[router: max_tool_iterations reached]"},
            "finish_reason": "stop",
        }],
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"


async def tool_loop_nonstream(initial_body: dict, strip_thinking: bool, client_ip: str) -> dict:
    """Multi-turn server-side tool-execution in non-streaming mode.

    Returns the final OpenAI chat-completion response dict, or an error
    envelope. Subject to the same MAX_TOOL_ITERATIONS and MAX_STREAM_SECONDS
    bounds as the streaming variant.
    """
    body = dict(initial_body)
    if "tools" not in body:
        body["tools"] = registered_tool_schemas()
    body["stream"] = False
    body.pop("tool_execution", None)
    messages = list(body["messages"])
    started = time.monotonic()
    model_label = body.get("model", "?")
    last_response: dict = {}

    for iteration in range(MAX_TOOL_ITERATIONS):
        if time.monotonic() - started > MAX_STREAM_SECONDS:
            return _error_body("service_degraded", "max stream time exceeded during tool loop")

        per_iter = dict(body)
        per_iter["messages"] = messages

        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as c:
            try:
                r = await c.post(
                    f"{V620_URL}/v1/chat/completions",
                    json=per_iter, headers=upstream_headers(),
                )
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                return _error_body("service_degraded", f"upstream: {type(e).__name__}")
            try:
                data = r.json()
            except json.JSONDecodeError:
                return _error_body("upstream_invalid_json", r.text[:500])
        last_response = data

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason")

        if finish == "tool_calls" and msg.get("tool_calls"):
            _ensure_tool_call_ids(msg["tool_calls"])
            messages.append(msg)  # assistant message with tool_calls
            for tc in msg["tool_calls"]:
                fn = tc.get("function") or {}
                result_str = await dispatch_tool(
                    fn.get("name", ""), fn.get("arguments", ""),
                )
                log_access(
                    "/v1/chat/completions:tool", fn.get("name", "?"),
                    len(fn.get("arguments") or ""), len(result_str),
                    int((time.monotonic() - started) * 1000),
                    200, client_ip,
                    error=(f"iter={iteration+1}/{MAX_TOOL_ITERATIONS}"
                           f" id={tc.get('id') or 'NULL'}"
                           f" args_size={len(fn.get('arguments') or '')}"
                           f" result_summary={_summarize_tool_result(result_str)}"),
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": result_str,
                })
            continue

        # Terminal — apply strip rules and return
        if msg.get("content"):
            if strip_thinking:
                msg["content"] = THINK_RE.sub("", msg["content"])
            if STRIP_CONTEXT_MARKERS:
                msg["content"] = CONTEXT_MARKER_RE.sub("", msg["content"])
        return data

    # MAX_TOOL_ITERATIONS exceeded — return last response with an annotation
    if last_response.get("choices"):
        last_response["choices"][0].setdefault("message", {})
        existing = last_response["choices"][0]["message"].get("content") or ""
        last_response["choices"][0]["message"]["content"] = (
            existing + "\n\n[router: max_tool_iterations reached]"
        )
        last_response["choices"][0]["finish_reason"] = "stop"
        return last_response
    return _error_body(
        "max_tool_iterations_exceeded",
        f"reached MAX_TOOL_ITERATIONS={MAX_TOOL_ITERATIONS} without final response",
    )


# ---------- Routes ----------

# Monotonic timestamp of the most recent chat-request arrival. Surfaced in
# /healthz as seconds_since_chat so the host fan bridge can pre-ramp the GPU
# fans the instant a query lands — before any GPU compute heats the die.
_last_chat_ts: Optional[float] = None


@app.post("/v1/chat/completions")
@limiter.limit(RATE_LIMIT_CHAT)
async def chat(
    request: Request,
    x_strip_thinking: Optional[str] = Header(default=None, alias="X-Strip-Thinking"),
):
    global _last_chat_ts
    _last_chat_ts = time.monotonic()      # feed-forward signal for the host fan bridge
    body = await request.json()
    strip = should_strip_thinking(body, x_strip_thinking)
    stream = body.get("stream", False)
    url = f"{V620_URL}/v1/chat/completions"
    started = time.monotonic()
    client_ip = request.client.host if request.client else "?"
    # The model name the CLIENT requested — used for logging + alias lookup.
    # body["model"] gets rewritten to the backend alias below before forwarding.
    model = body.get("model", "?")

    # Resolve the client-facing alias to backend behavior. Two things happen:
    #   1. body["model"] gets rewritten to the backend alias llama-server knows.
    #      (Without this, requesting model="qwen3.6-think" would forward that
    #      literal string upstream; llama-server doesn't recognize it and
    #      would either warn or 404 depending on its strict-model mode.)
    #   2. chat_template_kwargs.enable_thinking gets set per the alias —
    #      OFF for rag-qwen3.6 (RAG synthesis), ON for qwen3.6-think (agent
    #      reasoning). Client-supplied chat_template_kwargs takes precedence
    #      via setdefault so an explicit override always wins.
    alias_info = resolve_alias(model)
    if alias_info["backend"] != model:
        body["model"] = alias_info["backend"]
    if alias_info["enable_thinking"] is not None:
        ctk = body.setdefault("chat_template_kwargs", {})
        ctk.setdefault("enable_thinking", alias_info["enable_thinking"])

    # Token-budget admission control. Use /tokenize on the chat upstream.
    # count_tokens returns -1 when /tokenize is unreachable (fail-open).
    messages = body.get("messages", [])
    text = _approx_text_from_messages(messages)
    async with httpx.AsyncClient() as client:
        token_count = await count_tokens(client, V620_URL, text)
    if token_count != -1 and token_count > MAX_CHAT_INPUT_TOKENS:
        log_access("/v1/chat/completions", model, token_count, 0,
                   int((time.monotonic() - started) * 1000), 413, client_ip,
                   error="exceeds_max_chat_input_tokens")
        raise HTTPException(
            status_code=413,
            detail=f"input is {token_count} tokens, exceeds MAX_CHAT_INPUT_TOKENS={MAX_CHAT_INPUT_TOKENS}",
        )

    # Server-side tool-execution dispatch — if the client opted in via
    # body["tool_execution"] == "server", run the multi-turn tool loop
    # internally rather than passing tool_calls through to the client. The
    # loop hides tool_call deltas from the client (only content streams
    # downstream); clients see what looks like a single streamed assistant
    # response, possibly with a pause between iterations while tools execute.
    tool_exec = body.get("tool_execution", TOOL_EXECUTION_DEFAULT)

    async with chat_sem:
        if tool_exec == "server":
            log_access(
                "/v1/chat/completions", model, token_count,
                -1 if stream else 0,
                int((time.monotonic() - started) * 1000), 200, client_ip,
                error=f"tool_execution=server,stream={stream}",
            )
            if stream:
                return StreamingResponse(
                    tool_loop_stream(body, strip, client_ip),
                    media_type="text/event-stream",
                )
            data = await tool_loop_nonstream(body, strip, client_ip)
            return JSONResponse(data, status_code=200)

        if stream:
            # Streaming responses log only on connection close (we don't get tokens-out
            # here easily). Log a "stream-started" entry now; rely on access patterns for the rest.
            log_access("/v1/chat/completions", model, token_count, -1,
                       int((time.monotonic() - started) * 1000), 200, client_ip,
                       error="stream-started")
            return StreamingResponse(
                sse_stream_with_keepalive(url, body, strip),
                media_type="text/event-stream",
            )

        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as c:
            try:
                r = await c.post(url, json=body, headers=upstream_headers())
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                log_access("/v1/chat/completions", model, token_count, 0,
                           int((time.monotonic() - started) * 1000), 502, client_ip,
                           error=type(e).__name__)
                return JSONResponse(
                    _error_body("service_degraded", f"upstream: {type(e).__name__}"),
                    status_code=502,
                )
            try:
                data = r.json()
            except json.JSONDecodeError:
                log_access("/v1/chat/completions", model, token_count, 0,
                           int((time.monotonic() - started) * 1000), 502, client_ip,
                           error="upstream_invalid_json")
                return JSONResponse(
                    _error_body("upstream_invalid_json", r.text[:500]),
                    status_code=502,
                )
            if strip or STRIP_CONTEXT_MARKERS:
                for choice in data.get("choices", []):
                    msg = choice.get("message", {})
                    if "content" in msg:
                        if strip:
                            msg["content"] = THINK_RE.sub("", msg["content"])
                        if STRIP_CONTEXT_MARKERS:
                            msg["content"] = CONTEXT_MARKER_RE.sub("", msg["content"])
            usage = data.get("usage", {}) or {}
            log_access("/v1/chat/completions", model,
                       usage.get("prompt_tokens", token_count),
                       usage.get("completion_tokens", 0),
                       int((time.monotonic() - started) * 1000),
                       r.status_code, client_ip)
            return JSONResponse(data, status_code=r.status_code)


@app.post("/v1/completions")
@limiter.limit(RATE_LIMIT_CHAT)
async def completions(request: Request):
    """Legacy completions / FIM passthrough.

    Used by Continue.dev autocomplete, Cody, and other code completion plugins
    that send `prompt` + optional `suffix` for fill-in-the-middle. We proxy to
    the same chat upstream — llama-server handles both /v1/chat/completions and
    /v1/completions on the same port. Lighter pipeline than chat: no message-
    template assembly, no tool calls, no [CONTEXT N] stripping (FIM responses
    are pure code so the regex would never match anyway).
    """
    body = await request.json()
    stream = body.get("stream", False)
    url = f"{V620_URL}/v1/completions"
    started = time.monotonic()
    client_ip = request.client.host if request.client else "?"
    model = body.get("model", "?")

    # Token-budget admission control on the prompt + suffix.
    # OpenAI spec allows `prompt` to be str | list[str] | list[int]:
    #   - str: pass through
    #   - list[str]: concat with newline
    #   - list[int]: token IDs — already tokenized, don't call /tokenize (which counts
    #     by tokenizing text). Just use the length as the token count directly.
    prompt = body.get("prompt", "")
    suffix = body.get("suffix", "") or ""
    if isinstance(prompt, list) and prompt and all(isinstance(p, int) for p in prompt):
        # Pre-tokenized prompt: just count the IDs. Add a rough estimate for suffix.
        token_count = len(prompt)
        if isinstance(suffix, str) and suffix:
            async with httpx.AsyncClient() as client:
                suf = await count_tokens(client, V620_URL, suffix)
            if suf != -1:
                token_count += suf
    else:
        if isinstance(prompt, list):
            prompt = "\n".join(str(p) for p in prompt if isinstance(p, str))
        text = f"{prompt}\n{suffix}"
        async with httpx.AsyncClient() as client:
            token_count = await count_tokens(client, V620_URL, text)
    if token_count != -1 and token_count > MAX_CHAT_INPUT_TOKENS:
        log_access("/v1/completions", model, token_count, 0,
                   int((time.monotonic() - started) * 1000), 413, client_ip,
                   error="exceeds_max_chat_input_tokens")
        raise HTTPException(
            status_code=413,
            detail=f"input is {token_count} tokens, exceeds MAX_CHAT_INPUT_TOKENS={MAX_CHAT_INPUT_TOKENS}",
        )

    async with chat_sem:
        if stream:
            log_access("/v1/completions", model, token_count, -1,
                       int((time.monotonic() - started) * 1000), 200, client_ip,
                       error="stream-started")
            return StreamingResponse(
                sse_stream_with_keepalive(url, body, strip_thinking=False),
                media_type="text/event-stream",
            )

        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as c:
            try:
                r = await c.post(url, json=body, headers=upstream_headers())
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                log_access("/v1/completions", model, token_count, 0,
                           int((time.monotonic() - started) * 1000), 502, client_ip,
                           error=type(e).__name__)
                return JSONResponse(
                    _error_body("service_degraded", f"upstream: {type(e).__name__}"),
                    status_code=502,
                )
            try:
                data = r.json()
            except json.JSONDecodeError:
                log_access("/v1/completions", model, token_count, 0,
                           int((time.monotonic() - started) * 1000), 502, client_ip,
                           error="upstream_invalid_json")
                return JSONResponse(
                    _error_body("upstream_invalid_json", r.text[:500]),
                    status_code=502,
                )
            usage = data.get("usage", {}) or {}
            log_access("/v1/completions", model,
                       usage.get("prompt_tokens", token_count),
                       usage.get("completion_tokens", 0),
                       int((time.monotonic() - started) * 1000),
                       r.status_code, client_ip)
            return JSONResponse(data, status_code=r.status_code)


# Qwen3 Embedding requires <|endoftext|> appended to each input.
QWEN3_EOT = "<|endoftext|>"


def _ensure_eot(text: str) -> str:
    if not text:
        return text
    return text if text.endswith(QWEN3_EOT) else text.rstrip() + QWEN3_EOT


@app.post("/v1/embeddings")
@limiter.limit(RATE_LIMIT_EMBED)
async def embeddings(request: Request):
    body = await request.json()
    inp = body.get("input")
    if isinstance(inp, str):
        body["input"] = _ensure_eot(inp)
        text_for_count = inp
    elif isinstance(inp, list):
        body["input"] = [_ensure_eot(x) if isinstance(x, str) else x for x in inp]
        text_for_count = "\n".join(x for x in inp if isinstance(x, str))
    else:
        text_for_count = ""

    # Token-budget admission control (best-effort). count_tokens returns -1
    # when /tokenize is unreachable (fail-open).
    async with httpx.AsyncClient() as client:
        token_count = await count_tokens(client, EMBED_URL, text_for_count)
    if token_count != -1 and token_count > MAX_EMBED_INPUT_TOKENS:
        raise HTTPException(
            status_code=413,
            detail=f"input is {token_count} tokens, exceeds MAX_EMBED_INPUT_TOKENS={MAX_EMBED_INPUT_TOKENS}",
        )

    async with embed_sem:
        async with httpx.AsyncClient(timeout=120.0) as c:
            try:
                r = await c.post(f"{EMBED_URL}/v1/embeddings", json=body, headers=upstream_headers())
                return JSONResponse(r.json(), status_code=r.status_code)
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                return JSONResponse(
                    _error_body("service_degraded", f"upstream: {type(e).__name__}"),
                    status_code=502,
                )


@app.post("/v1/rerank")
@limiter.limit(RATE_LIMIT_EMBED)
async def rerank(request: Request):
    body = await request.json()
    async with embed_sem:  # share semaphore with embed (both are bulk-RAG)
        async with httpx.AsyncClient(timeout=60.0) as c:
            try:
                r = await c.post(f"{RERANK_URL}/v1/rerank", json=body, headers=upstream_headers())
                return JSONResponse(r.json(), status_code=r.status_code)
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                return JSONResponse(
                    _error_body("service_degraded", f"upstream: {type(e).__name__}"),
                    status_code=502,
                )


@app.post("/v1/tavily/search")
@limiter.limit(RATE_LIMIT_TAVILY)
async def tavily_search(request: Request):
    """Proxy to Tavily Search API.

    Browser-side clients (e.g. the external HTML reporting
    artifact) call this instead of Tavily directly, so the Tavily key never
    leaves the server. ROUTER_API_KEY still gates the request; rate-limited
    separately so search bursts can't starve chat/embed budgets.

    Body whitelist (see TAVILY_ALLOWED_FIELDS) keeps the client surface
    narrow and drops large-response options (raw_content, images, favicon).
    """
    if not TAVILY_API_KEY:
        return JSONResponse(
            _error_body(
                "tavily_unconfigured",
                "TAVILY_API_KEY not set in /etc/router.env — add it and restart llm-router",
            ),
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _error_body("bad_request", "request body must be JSON"),
            status_code=400,
        )
    if not isinstance(body, dict) or not body.get("query"):
        return JSONResponse(
            _error_body("bad_request", "missing required field: query"),
            status_code=400,
        )

    forwarded = {k: v for k, v in body.items() if k in TAVILY_ALLOWED_FIELDS}

    async with httpx.AsyncClient(timeout=SMALL_TIMEOUT) as c:
        try:
            r = await c.post(
                TAVILY_URL,
                json=forwarded,
                headers={
                    "Authorization": f"Bearer {TAVILY_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
            return JSONResponse(
                _error_body("tavily_unreachable", f"upstream: {type(e).__name__}"),
                status_code=502,
            )

    if r.status_code >= 400:
        # Pass Tavily's error body through but normalize to our error envelope
        # so clients get a consistent shape. Truncate to avoid log spam from
        # accidentally massive HTML error pages.
        upstream_text = (r.text or "")[:500]
        return JSONResponse(
            _error_body(
                "tavily_error",
                f"Tavily returned {r.status_code}: {upstream_text}",
            ),
            status_code=r.status_code if r.status_code in (401, 403, 429) else 502,
        )

    try:
        return JSONResponse(r.json(), status_code=200)
    except ValueError:
        return JSONResponse(
            _error_body("tavily_invalid_json", r.text[:500]),
            status_code=502,
        )


@app.get("/v1/models")
async def models():
    """Enumerate client-facing model aliases.

    Chat aliases come from ALIAS_MAP filtered by which backend is ACTUALLY
    loaded right now. The chat unit serves only one model at a time (one
    --alias per llama-server process), so advertising aliases for the
    *other* profile would let a client request something the chat unit
    can't service correctly. We query the chat unit's /v1/models, take its
    reported model id as the active backend, and only advertise ALIAS_MAP
    entries whose 'backend' field matches.

    If the chat unit is unreachable we fall back to advertising ALL
    ALIAS_MAP keys (degraded mode — better than an empty list).

    Embed + rerank still come from upstream — those have a 1:1 mapping
    between client-facing name and backend alias, so the upstream's
    /v1/models is the source of truth.
    """
    now = int(time.time())
    active_backend: str | None = None
    async with httpx.AsyncClient(timeout=5.0) as c:
        try:
            r = await c.get(f"{V620_URL}/v1/models", headers=upstream_headers())
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
            # llama-server returns one id per loaded model — the chat unit serves
            # exactly one, so just take the first.
            if ids:
                active_backend = ids[0]
        except Exception:
            pass

    if active_backend is not None:
        chat_aliases = [
            alias for alias, entry in ALIAS_MAP.items()
            if entry.get("backend") == active_backend
        ]
    else:
        # Fallback: chat unit unreachable. Advertise everything so clients
        # can still discover aliases; mismatched ones will fail at request time.
        chat_aliases = list(ALIAS_MAP.keys())

    data: list = [
        {"id": alias, "object": "model", "owned_by": "v620-cluster", "created": now}
        for alias in chat_aliases
    ]

    async with httpx.AsyncClient(timeout=10.0) as c:
        for url in (EMBED_URL, RERANK_URL):
            try:
                r = await c.get(f"{url}/v1/models", headers=upstream_headers())
                data.extend(r.json().get("data", []))
            except Exception:
                pass
    return {"object": "list", "data": data}


@app.get("/healthz")
async def healthz():
    async with httpx.AsyncClient(timeout=3.0) as c:
        upstream_status = {}
        chat_models_payload = None
        for name, url in [
            ("chat", V620_URL),
            ("embed", EMBED_URL),
            ("rerank", RERANK_URL),
        ]:
            try:
                r = await c.get(f"{url}/v1/models", headers=upstream_headers())
                upstream_status[name] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
                if name == "chat" and r.status_code == 200:
                    chat_models_payload = r.json()
            except Exception as e:
                upstream_status[name] = f"unreachable: {type(e).__name__}"
    # Surface the active chat profile (matches what /v1/models filters against)
    # so monitoring tools can alert on unexpected swaps.
    active_chat_profile: str | None = None
    if chat_models_payload:
        ids = [m.get("id") for m in chat_models_payload.get("data", []) if m.get("id")]
        if ids:
            active_chat_profile = ids[0]
    return {
        "ok": True,
        "ts": time.time(),
        "upstream": upstream_status,
        "active_chat_profile": active_chat_profile,
        "seconds_since_chat": (time.monotonic() - _last_chat_ts) if _last_chat_ts is not None else None,
    }
