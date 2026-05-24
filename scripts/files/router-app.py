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
# (e.g. the Weekly Customer Adoption Review HTML artifact) can do real web
# search without ever seeing the key. Set TAVILY_API_KEY in /etc/router.env
# to enable; if empty, /v1/tavily/search returns 503.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = os.environ.get("TAVILY_URL", "https://api.tavily.com/search")
# Whitelist of body fields we forward to Tavily. Anything else in the client
# request is dropped. We omit raw_content / images / favicon by default to
# keep response sizes small (the model only needs title + url + content).
TAVILY_ALLOWED_FIELDS = {
    "query", "search_depth", "chunks_per_source", "max_results",
    "topic", "time_range", "start_date", "end_date",
    "include_answer", "include_domains", "exclude_domains",
    "country", "auto_parameters", "exact_match",
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


# ---------- Routes ----------

@app.post("/v1/chat/completions")
@limiter.limit(RATE_LIMIT_CHAT)
async def chat(
    request: Request,
    x_strip_thinking: Optional[str] = Header(default=None, alias="X-Strip-Thinking"),
):
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

    async with chat_sem:
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

    Browser-side clients (e.g. the Weekly Customer Adoption Review HTML
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

    Chat aliases come from ALIAS_MAP (multiple virtual aliases → one llama-
    server backend). We list them client-side rather than aggregating from
    upstream because llama-server only knows one --alias per process, but we
    expose several names that resolve to it (rag-qwen3.6 for thinking-off
    RAG, qwen3.6-think for thinking-on agent work).

    Embed + rerank still come from upstream — those have a 1:1 mapping
    between client-facing name and backend alias, so the upstream's
    /v1/models is the source of truth.
    """
    now = int(time.time())
    data: list = [
        {"id": alias, "object": "model", "owned_by": "v620-cluster", "created": now}
        for alias in ALIAS_MAP.keys()
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
        for name, url in [
            ("chat", V620_URL),
            ("embed", EMBED_URL),
            ("rerank", RERANK_URL),
        ]:
            try:
                r = await c.get(f"{url}/v1/models", headers=upstream_headers())
                upstream_status[name] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
            except Exception as e:
                upstream_status[name] = f"unreachable: {type(e).__name__}"
    return {"ok": True, "ts": time.time(), "upstream": upstream_status}
