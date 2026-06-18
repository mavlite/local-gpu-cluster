"""MCP-over-SSE bridge to a self-hosted Memory Vault REST API.

Exposes `remember`, `recall`, `forget`, `memory_status` as MCP tools over SSE so
remote clients (OpenCode, Claude Code) can use the cluster's existing remote-MCP
pattern. Each SSE connection is scoped to a memory space via `?space=<slug>` on
the connect URL; tools also accept an explicit `space` override.

Env (from /etc/memory-vault-bridge.env):
    MEMVAULT_API_URL        base URL of the Memory Vault REST API (e.g. http://127.0.0.1:8000)
    MEMVAULT_API_TOKEN      bearer token minted via `memory-vault token create`
    MEMVAULT_DEFAULT_SPACE  space used when a connection omits ?space=
    MCP_HOST / MCP_PORT     SSE listen address (default 0.0.0.0:3005)
"""
import contextvars
import os

import httpx
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.routing import Mount, Route

# ---- REST contract (confirm against docs/memory-vault-openapi.json, Task 3) ----
API_URL = os.environ.get("MEMVAULT_API_URL", "http://127.0.0.1:8000").rstrip("/")
API_TOKEN = os.environ.get("MEMVAULT_API_TOKEN", "")
DEFAULT_SPACE = os.environ.get("MEMVAULT_DEFAULT_SPACE", "default")

REMEMBER_PATH = "/api/ingest/text"
REMEMBER_FIELD_TEXT = "text"
REMEMBER_FIELD_SPACE = "space"

# Confirmed against the live memory-vault 1.0.1 /openapi.json (2026-06-18).
RECALL_PATH = "/api/search"
RECALL_FIELD_QUERY = "query"
RECALL_FIELD_SPACE = "spaces"           # /api/search takes a LIST of spaces, not a single string
RECALL_FIELD_LIMIT = "limit"
RECALL_RESULTS_KEY = "results"          # response: {results:[{chunk_id,content,similarity,space,...}], ...}
RECALL_ITEM_TEXT_KEYS = ("content", "text", "chunk")
RECALL_ITEM_SCORE_KEYS = ("similarity", "score", "rrf_score")

FORGET_PATH = "/api/chunks/{id}"        # DELETE /api/chunks/{chunk_id}
STATUS_PATH = "/api/health"             # falls back to /api/spaces on 404

# Per-connection memory space (set at SSE connect from ?space=).
_space_var: contextvars.ContextVar[str] = contextvars.ContextVar("space", default=DEFAULT_SPACE)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_TOKEN:
        h["Authorization"] = f"Bearer {API_TOKEN}"
    return h


def _space(override: str | None) -> str:
    return override or _space_var.get()


server = Server("memory-vault")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="remember",
            description=(
                "Persist a piece of context, decision, or learning to long-term "
                "memory for this project so it survives context compaction and new "
                "sessions. Use for durable facts, not transient chatter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The memory to store."},
                    "space": {"type": "string", "description": "Optional memory-space override."},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="recall",
            description=(
                "Retrieve relevant memories for this project via hybrid (vector + "
                "keyword) search. Call at the start of a task to rehydrate context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to recall."},
                    "top_n": {"type": "integer", "description": "Max results (default 5)."},
                    "space": {"type": "string", "description": "Optional memory-space override."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="forget",
            description="Soft-delete a memory by its chunk id.",
            inputSchema={
                "type": "object",
                "properties": {"chunk_id": {"type": "string"}},
                "required": ["chunk_id"],
            },
        ),
        Tool(
            name="memory_status",
            description="Report Memory Vault health and statistics.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    arguments = arguments or {}
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            if name == "remember":
                payload = {
                    REMEMBER_FIELD_TEXT: arguments["text"],
                    REMEMBER_FIELD_SPACE: _space(arguments.get("space")),
                }
                r = await c.post(f"{API_URL}{REMEMBER_PATH}", json=payload, headers=_headers())
                r.raise_for_status()
                return [TextContent(type="text", text=f"stored: {r.text[:500]}")]

            if name == "recall":
                payload = {
                    RECALL_FIELD_QUERY: arguments["query"],
                    RECALL_FIELD_SPACE: [_space(arguments.get("space"))],
                    RECALL_FIELD_LIMIT: int(arguments.get("top_n", 5)),
                }
                r = await c.post(f"{API_URL}{RECALL_PATH}", json=payload, headers=_headers())
                r.raise_for_status()
                data = r.json()
                hits = data.get(RECALL_RESULTS_KEY, data if isinstance(data, list) else [])
                if not hits:
                    return [TextContent(type="text", text="No memories found.")]
                lines = []
                for i, h in enumerate(hits, 1):
                    text = next((h[k] for k in RECALL_ITEM_TEXT_KEYS if k in h), str(h))
                    score = next((h[k] for k in RECALL_ITEM_SCORE_KEYS if k in h), None)
                    cid = h.get("chunk_id") if isinstance(h, dict) else None
                    score_tag = f" (score {score:.3f})" if isinstance(score, (int, float)) else ""
                    id_tag = f" [id={cid}]" if cid else ""
                    lines.append(f"{i}.{id_tag}{score_tag} {text}")
                return [TextContent(type="text", text="\n".join(lines))]

            if name == "forget":
                path = FORGET_PATH.format(id=arguments["chunk_id"])
                r = await c.delete(f"{API_URL}{path}", headers=_headers())
                r.raise_for_status()
                return [TextContent(type="text", text=f"forgotten: {arguments['chunk_id']}")]

            if name == "memory_status":
                r = await c.get(f"{API_URL}{STATUS_PATH}", headers=_headers())
                if r.status_code == 404:
                    r = await c.get(f"{API_URL}/api/spaces", headers=_headers())
                r.raise_for_status()
                return [TextContent(type="text", text=r.text[:1000])]

            return [TextContent(type="text", text=f"unknown tool: {name}")]
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text",
                            text=f"Memory Vault returned {e.response.status_code}: {e.response.text[:300]}")]
    except httpx.HTTPError as e:
        return [TextContent(type="text", text=f"Memory Vault unreachable: {type(e).__name__}")]


_sse = SseServerTransport("/messages/")


async def handle_sse(request):
    # Capture the per-connection memory space from ?space=; reset on disconnect
    # so the ContextVar can't leak across connections that reuse a task context.
    token = _space_var.set(request.query_params.get("space") or DEFAULT_SPACE)
    try:
        async with _sse.connect_sse(request.scope, request.receive, request._send) as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        _space_var.reset(token)


app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages/", app=_sse.handle_post_message),
])


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("MCP_PORT", "3005")))
