"""
LLM cluster router (Phase 7 of setup-runbook.md).

Routes per endpoint:
  POST /v1/chat/completions  -> V620 stack (port 8080), with spec decoding native
  POST /v1/embeddings        -> 3060 embedder (port 8082)
  POST /v1/rerank            -> 3060 reranker (port 8083)
  GET  /v1/models            -> aggregated list from both backends
  GET  /healthz              -> upstream availability probe

Features:
  - SSE keepalive (": ping" frames every KEEPALIVE_INTERVAL seconds during
    long generations so reverse proxies don't drop the connection).
  - Per-request <think>...</think> stripping decision, controlled by
    header > body field > system prompt hint > model alias > default.
  - Qwen3 Embedding compliance: appends <|endoftext|> if missing.
"""

import asyncio
import os
import re
import time

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

V620_URL = os.environ.get("V620_URL", "http://192.168.6.151:8080")
FAST_URL = os.environ.get("FAST_URL", "http://192.168.6.152:8081")
EMBED_URL = os.environ.get("EMBED_URL", "http://192.168.6.152:8082")
RERANK_URL = os.environ.get("RERANK_URL", "http://192.168.6.152:8083")
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "12"))

# Comma-separated model aliases that should be sent to the 3060 fast model.
# Anything else goes to the V620 stack. Default matches the alias from
# 52-lxc-nv.sh ($FAST_ALIAS).
FAST_ALIASES = {
    a.strip().lower()
    for a in os.environ.get("FAST_ALIASES", "qwen3-4b-fast").split(",")
    if a.strip()
}

# Finite timeouts for non-streaming paths. Streaming uses timeout=None
# because long generations are normal there.
CHAT_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=5.0)


def pick_chat_upstream(model: str) -> str:
    """Choose the upstream chat URL based on the requested model alias."""
    if model and model.lower() in FAST_ALIASES:
        return FAST_URL
    return V620_URL

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
NOTHINK_HINT = re.compile(r"/no_think|hide thinking|strip reasoning", re.IGNORECASE)
RAG_MODEL_RE = re.compile(r"(^rag-|-rag$)", re.IGNORECASE)

app = FastAPI(title="LLM Cluster Router")


def should_strip_thinking(body: dict, header_value: str | None) -> bool:
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


async def sse_stream_with_keepalive(upstream_url: str, payload: dict, strip_thinking: bool):
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", upstream_url, json=payload) as r:
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


@app.post("/v1/chat/completions")
async def chat(
    request: Request,
    x_strip_thinking: str | None = Header(default=None, alias="X-Strip-Thinking"),
):
    body = await request.json()
    strip = should_strip_thinking(body, x_strip_thinking)
    stream = body.get("stream", False)
    upstream = pick_chat_upstream(body.get("model", ""))
    url = f"{upstream}/v1/chat/completions"

    if stream:
        return StreamingResponse(
            sse_stream_with_keepalive(url, body, strip),
            media_type="text/event-stream",
        )

    async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as c:
        r = await c.post(url, json=body)
        data = r.json()
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
async def embeddings(request: Request):
    body = await request.json()
    inp = body.get("input")
    if isinstance(inp, str):
        body["input"] = _ensure_eot(inp)
    elif isinstance(inp, list):
        body["input"] = [_ensure_eot(x) if isinstance(x, str) else x for x in inp]

    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(f"{EMBED_URL}/v1/embeddings", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/rerank")
async def rerank(request: Request):
    body = await request.json()
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(f"{RERANK_URL}/v1/rerank", json=body)
        return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=10.0) as c:
        aggregated: list = []
        for url in (V620_URL, FAST_URL, EMBED_URL):
            try:
                r = await c.get(f"{url}/v1/models")
                aggregated.extend(r.json().get("data", []))
            except Exception:
                pass
    return {"object": "list", "data": aggregated}


@app.get("/healthz")
async def healthz():
    async with httpx.AsyncClient(timeout=3.0) as c:
        upstream_status = {}
        for name, url in [
            ("v620", V620_URL),
            ("fast", FAST_URL),
            ("embed", EMBED_URL),
            ("rerank", RERANK_URL),
        ]:
            try:
                r = await c.get(f"{url}/v1/models")
                upstream_status[name] = "ok" if r.status_code == 200 else f"http_{r.status_code}"
            except Exception as e:
                upstream_status[name] = f"unreachable: {type(e).__name__}"
    return {"ok": True, "ts": time.time(), "upstream": upstream_status}
