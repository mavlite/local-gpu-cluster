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

# /metrics IP allowlist (comma-separated)
METRICS_ALLOWED_IPS = {
    ip.strip() for ip in os.environ.get("METRICS_ALLOWED_IPS", "127.0.0.1").split(",") if ip.strip()
}

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


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        {"error": "rate_limit_exceeded", "detail": str(exc.detail)},
        status_code=429,
    )


# Bearer auth middleware. /healthz and /metrics are exempt (the latter has its own
# IP-allowlist check).
@app.middleware("http")
async def require_bearer(request: Request, call_next):
    if request.url.path in ("/healthz", "/metrics"):
        return await call_next(request)
    if not ROUTER_API_KEY:
        return JSONResponse(
            {"error": "router_misconfigured", "detail": "ROUTER_API_KEY not set in /etc/router.env"},
            status_code=503,
        )
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return JSONResponse({"error": "unauthorized", "detail": "missing Bearer token"}, status_code=403)
    presented = auth[7:].strip()
    if not secrets.compare_digest(presented, ROUTER_API_KEY):
        return JSONResponse({"error": "unauthorized", "detail": "invalid Bearer token"}, status_code=403)
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


def should_strip_thinking(body: dict, header_value: Optional[str]) -> bool:
    if header_value is not None:
        return header_value.lower() in ("true", "1", "yes")
    if "strip_thinking" in body:
        return bool(body["strip_thinking"])
    msgs = body.get("messages", [])
    if msgs and msgs[0].get("role") == "system":
        if NOTHINK_HINT.search(msgs[0].get("content", "")):
            return True
    if RAG_MODEL_RE.search(body.get("model", "")):
        return True
    return False


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

DEGRADED_FRAME = (
    "data: " + json.dumps({"error": "service_degraded", "detail": "upstream returned an error; retry shortly"})
    + "\n\n"
).encode()
DONE_FRAME = b"data: [DONE]\n\n"


async def sse_stream_with_keepalive(upstream_url: str, payload: dict, strip_thinking: bool):
    """Stream upstream SSE, injecting keepalives every KEEPALIVE_INTERVAL seconds and
    falling open with a `service_degraded` frame if the upstream errors. Caps the
    total stream wall-clock at MAX_STREAM_SECONDS so a wedged upstream can't hold
    its chat_sem slot forever."""
    stream_started = time.monotonic()
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("POST", upstream_url, json=payload, headers=upstream_headers()) as r:
                if r.status_code >= 500:
                    yield DEGRADED_FRAME
                    yield DONE_FRAME
                    return
                queue: asyncio.Queue = asyncio.Queue()

                async def reader():
                    async for chunk in r.aiter_text():
                        await queue.put(chunk)
                    await queue.put(None)

                task = asyncio.create_task(reader())
                try:
                    while True:
                        if time.monotonic() - stream_started > MAX_STREAM_SECONDS:
                            yield DEGRADED_FRAME
                            yield DONE_FRAME
                            return
                        try:
                            chunk = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                        except asyncio.TimeoutError:
                            yield b": ping\n\n"
                            continue
                        if chunk is None:
                            break
                        out = THINK_RE.sub("", chunk) if strip_thinking else chunk
                        if STRIP_CONTEXT_MARKERS:
                            out = CONTEXT_MARKER_RE.sub("", out)
                        yield out.encode()
                finally:
                    task.cancel()
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException, httpx.RemoteProtocolError):
            yield DEGRADED_FRAME
            yield DONE_FRAME


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
    model = body.get("model", "?")

    # Qwen3.6 chat-template thinking-mode toggle: when the requested model alias
    # matches rag-*, disable reasoning at the template level. Qwen3.6 emits its
    # chain-of-thought as plain content (not wrapped in <think>...</think>) so
    # the regex stripper can't catch it — RAG UIs end up showing the analysis
    # AND blowing through max_tokens before the actual answer finishes.
    # Honor the client if they explicitly set chat_template_kwargs themselves.
    if RAG_MODEL_RE.search(model):
        ctk = body.setdefault("chat_template_kwargs", {})
        ctk.setdefault("enable_thinking", False)

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
                    {"error": "service_degraded", "detail": f"upstream: {type(e).__name__}"},
                    status_code=502,
                )
            try:
                data = r.json()
            except json.JSONDecodeError:
                log_access("/v1/chat/completions", model, token_count, 0,
                           int((time.monotonic() - started) * 1000), 502, client_ip,
                           error="upstream_invalid_json")
                return JSONResponse(
                    {"error": "upstream_invalid_json", "detail": r.text[:500]},
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
                    {"error": "service_degraded", "detail": f"upstream: {type(e).__name__}"},
                    status_code=502,
                )
            try:
                data = r.json()
            except json.JSONDecodeError:
                log_access("/v1/completions", model, token_count, 0,
                           int((time.monotonic() - started) * 1000), 502, client_ip,
                           error="upstream_invalid_json")
                return JSONResponse(
                    {"error": "upstream_invalid_json", "detail": r.text[:500]},
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
                    {"error": "service_degraded", "detail": f"upstream: {type(e).__name__}"},
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
                    {"error": "service_degraded", "detail": f"upstream: {type(e).__name__}"},
                    status_code=502,
                )


@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=10.0) as c:
        aggregated: list = []
        for url in (V620_URL, EMBED_URL, RERANK_URL):
            try:
                r = await c.get(f"{url}/v1/models", headers=upstream_headers())
                aggregated.extend(r.json().get("data", []))
            except Exception:
                pass
    return {"object": "list", "data": aggregated}


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
