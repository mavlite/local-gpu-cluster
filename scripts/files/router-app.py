"""
LLM cluster router (V620-only — Phase 7 of setup-runbook.md).

All upstreams live on LXC 151:
  POST /v1/chat/completions  -> V620 chat (port 8080, tensor-split, in-process spec decode)
  POST /v1/embeddings        -> V620 embedder (port 8082, --main-gpu 0, --pooling last)
  POST /v1/rerank            -> V620 reranker (port 8083, --main-gpu 1, --reranking)
  GET  /v1/models            -> aggregated list from chat + embed + rerank
  GET  /healthz              -> upstream availability probe (unauthed)
  GET  /metrics              -> Prometheus instrumentation (IP-allowlist gated)

Features (V620-only build):
  - Bearer auth on inbound (ROUTER_API_KEY) and outbound (LLAMACPP_API_KEY).
  - asyncio.Semaphore admission control (chat=1, embed=4).
  - Per-route token-budget admission via upstream /tokenize calls (chat input cap,
    embed input cap; reject 413 if over budget).
  - slowapi per-IP rate limiting.
  - prometheus-fastapi-instrumentator middleware for /metrics.
  - Fail-open SSE on upstream 5xx: emits a `service degraded` data frame so
    AnythingLLM can fall back without breaking the stream.
  - SSE keepalive (`: ping` every KEEPALIVE_INTERVAL seconds) during long generations.
  - Per-request <think>...</think> stripping (header > body field > system prompt > model alias).
  - Qwen3 Embedding compliance: appends <|endoftext|> if missing.
"""

import asyncio
import json
import os
import re
import secrets
import time
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

# Auth: ROUTER_API_KEY gates inbound; LLAMACPP_API_KEY is sent on outbound calls.
ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY", "")
LLAMACPP_API_KEY = os.environ.get("LLAMACPP_API_KEY", "")

# Admission control
CHAT_CONCURRENCY = int(os.environ.get("CHAT_CONCURRENCY", "1"))
EMBED_CONCURRENCY = int(os.environ.get("EMBED_CONCURRENCY", "4"))
MAX_CHAT_INPUT_TOKENS = int(os.environ.get("MAX_CHAT_INPUT_TOKENS", "100000"))
MAX_EMBED_INPUT_TOKENS = int(os.environ.get("MAX_EMBED_INPUT_TOKENS", "8192"))

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
    falling open with a `service_degraded` frame if the upstream errors."""
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
                        try:
                            chunk = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                        except asyncio.TimeoutError:
                            yield b": ping\n\n"
                            continue
                        if chunk is None:
                            break
                        out = THINK_RE.sub("", chunk) if strip_thinking else chunk
                        yield out.encode()
                finally:
                    task.cancel()
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
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

    # Qwen3.6 chat-template thinking-mode toggle: when the requested model alias
    # matches rag-*, disable reasoning at the template level. Qwen3.6 emits its
    # chain-of-thought as plain content (not wrapped in <think>...</think>) so
    # the regex stripper can't catch it — RAG UIs end up showing the analysis
    # AND blowing through max_tokens before the actual answer finishes.
    # Honor the client if they explicitly set chat_template_kwargs themselves.
    if RAG_MODEL_RE.search(body.get("model", "")):
        ctk = body.setdefault("chat_template_kwargs", {})
        ctk.setdefault("enable_thinking", False)

    # Token-budget admission control. Use /tokenize on the chat upstream.
    messages = body.get("messages", [])
    text = _approx_text_from_messages(messages)
    async with httpx.AsyncClient() as client:
        token_count = await count_tokens(client, V620_URL, text)
    if token_count > MAX_CHAT_INPUT_TOKENS:
        raise HTTPException(
            status_code=413,
            detail=f"input is {token_count} tokens, exceeds MAX_CHAT_INPUT_TOKENS={MAX_CHAT_INPUT_TOKENS}",
        )

    async with chat_sem:
        if stream:
            return StreamingResponse(
                sse_stream_with_keepalive(url, body, strip),
                media_type="text/event-stream",
            )

        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as c:
            try:
                r = await c.post(url, json=body, headers=upstream_headers())
            except (httpx.ConnectError, httpx.ReadError) as e:
                return JSONResponse(
                    {"error": "service_degraded", "detail": f"upstream: {type(e).__name__}"},
                    status_code=502,
                )
            try:
                data = r.json()
            except json.JSONDecodeError:
                return JSONResponse(
                    {"error": "upstream_invalid_json", "detail": r.text[:500]},
                    status_code=502,
                )
            if strip:
                for choice in data.get("choices", []):
                    msg = choice.get("message", {})
                    if "content" in msg:
                        msg["content"] = THINK_RE.sub("", msg["content"])
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

    # Token-budget admission control (best-effort)
    async with httpx.AsyncClient() as client:
        token_count = await count_tokens(client, EMBED_URL, text_for_count)
    if token_count > MAX_EMBED_INPUT_TOKENS:
        raise HTTPException(
            status_code=413,
            detail=f"input is {token_count} tokens, exceeds MAX_EMBED_INPUT_TOKENS={MAX_EMBED_INPUT_TOKENS}",
        )

    async with embed_sem:
        async with httpx.AsyncClient(timeout=120.0) as c:
            try:
                r = await c.post(f"{EMBED_URL}/v1/embeddings", json=body, headers=upstream_headers())
                return JSONResponse(r.json(), status_code=r.status_code)
            except (httpx.ConnectError, httpx.ReadError) as e:
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
            except (httpx.ConnectError, httpx.ReadError) as e:
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
