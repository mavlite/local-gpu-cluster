"""local-delegate MCP service: localhost Streamable-HTTP server with bearer auth.

Wiring mirrors scripts/files/memory-vault-bridge.py (low-level Server +
StreamableHTTPSessionManager, stateless, mounted at /mcp).
"""
import contextlib
import hmac
import os

import httpx
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from scripts.delegate.config import load_config
from scripts.delegate.ledger import Ledger
from scripts.delegate.lease import GpuLease
from scripts.delegate.router_client import InputTooLarge, ProfileBusy, ask_local

_ASK_KEYS = ("prompt", "content", "files", "mode")

ASK_LOCAL_TOOL = Tool(
    name="ask_local",
    description="Single-shot local-LLM call; server reads files so bulky input stays out of Claude's context.",
    inputSchema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "content": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["summarize", "code", "general"]},
        },
        "required": ["prompt"],
    },
)


def _text(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=msg)]


async def _run_ask_local(cfg, deps, arguments: dict) -> list[TextContent]:
    if "prompt" not in arguments:
        return _text("ERROR: 'prompt' is required")
    kwargs = {k: arguments[k] for k in _ASK_KEYS if k in arguments}
    mode = kwargs.get("mode", "summarize")
    if not deps.lease.acquire(timeout_s=0):
        return _text("ERROR: GPU busy (a job holds the lease). Try later.")
    try:
        out = await ask_local(deps.http, cfg, **kwargs)
    except InputTooLarge as e:
        return _text(f"ERROR: {e}")
    except ProfileBusy as e:
        return _text(f"ERROR: {e}; try later")
    except (RuntimeError, OSError, httpx.HTTPError) as e:
        return _text(f"ERROR: local call failed ({type(e).__name__}): {str(e)[:300]}")
    finally:
        deps.lease.release()
    deps.ledger.append({"tool": "ask_local", "mode": mode, "local_tokens": out["usage"]})
    return _text(out["text"])


def build_server(cfg, deps) -> Server:
    server = Server("local-delegate")

    @server.list_tools()
    async def _tools() -> list[Tool]:
        return [ASK_LOCAL_TOOL]

    @server.call_tool()
    async def _call(name: str, arguments: dict | None) -> list[TextContent]:
        if name == "ask_local":
            return await _run_ask_local(cfg, deps, arguments or {})
        return _text(f"unknown tool: {name}")

    return server


class BearerAuth:
    """Pure-ASGI bearer gate (no response buffering, safe for streaming)."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self._expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(cfg, deps) -> Starlette:
    manager = StreamableHTTPSessionManager(
        app=build_server(cfg, deps), event_store=None, json_response=True, stateless=True
    )

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
    app.add_middleware(BearerAuth, token=cfg.bearer_token)
    return app


class _Deps:
    def __init__(self, http, ledger, lease):
        self.http, self.ledger, self.lease = http, ledger, lease


if __name__ == "__main__":
    _cfg = load_config(os.environ)
    _deps = _Deps(httpx.AsyncClient(timeout=_cfg.ask_timeout_s), Ledger(_cfg.ledger_path), GpuLease(_cfg.lease_path))
    uvicorn.run(build_app(_cfg, _deps), host=_cfg.host, port=_cfg.port)
