#!/usr/bin/env python3
"""MCP server exposing one or more AnythingLLM workspaces.

This is a thin bridge between OpenCode (or any MCP client) and AnythingLLM's
workspace-chat / vector-search REST endpoints. Each configured workspace gets
two tools registered with the MCP server:

  query_<slug>(question)   — full RAG synthesis (embed → retrieve → rerank
                             → chat). Returns an answer + compact source list.
                             Use this when you want a finished answer.
  search_<slug>(query, top_n) — direct vector search returning raw chunks.
                             Use this when you want to read source excerpts
                             yourself (extract a command, paragraph, etc.).

Tool slugs are dash-to-underscore-sanitized from the workspace slug so they're
valid Python identifiers. Example: workspace `sdg-documentation` exposes
`query_sdg_documentation` + `search_sdg_documentation`.

The file is still named mcp-sdg-server.py for historical reasons (started as a
single-workspace SDG bridge); it now handles N workspaces dynamically.

Transport: SSE on $MCP_PORT (default 3004), matching the OpenCode
`type: remote` MCP convention.

Env:
  ALLM_URL                       Base URL for AnythingLLM API
                                 (default http://192.168.6.154:3001/api/v1)
  ALLM_API_KEY                   AnythingLLM API key (required)
  MCP_WORKSPACES                 Comma-separated workspace slug list
                                 (default: sdg-documentation)
                                 e.g. "sdg-documentation,vcf-reference"
  WORKSPACE                      Legacy single-workspace fallback. If
                                 MCP_WORKSPACES is unset, this is used as
                                 the only workspace.
  MCP_WORKSPACE_DESC_<SLUG>      Human-readable description for a workspace,
                                 surfaced in the tool's docstring so the
                                 calling LLM can pick the right one. SLUG is
                                 the workspace slug uppercased with dashes
                                 replaced by underscores. E.g. for slug
                                 "sdg-documentation", set
                                 MCP_WORKSPACE_DESC_SDG_DOCUMENTATION.
  MCP_PORT                       SSE listen port (default 3004)
  MCP_HOST                       SSE bind address (default 0.0.0.0)
"""

import os
import sys
import json
import logging
import re

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s mcp-sdg %(message)s",
)
log = logging.getLogger("mcp-sdg")

ALLM_URL = os.environ.get("ALLM_URL", "http://192.168.6.154:3001/api/v1").rstrip("/")
ALLM_API_KEY = os.environ.get("ALLM_API_KEY")
MCP_PORT = int(os.environ.get("MCP_PORT", "3004"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")

if not ALLM_API_KEY:
    log.error("ALLM_API_KEY env var is required")
    sys.exit(1)

# Workspace list: MCP_WORKSPACES (new) takes precedence over WORKSPACE (legacy).
_raw_workspaces = os.environ.get("MCP_WORKSPACES") or os.environ.get("WORKSPACE", "sdg-documentation")
WORKSPACES = [w.strip() for w in _raw_workspaces.split(",") if w.strip()]
if not WORKSPACES:
    log.error("No workspaces configured. Set MCP_WORKSPACES or WORKSPACE env var.")
    sys.exit(1)

_VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
for ws in WORKSPACES:
    if not _VALID_SLUG.match(ws):
        log.error("Workspace slug %r is invalid (must match %s)", ws, _VALID_SLUG.pattern)
        sys.exit(1)


def _tool_suffix(workspace_slug: str) -> str:
    """Convert workspace slug to a valid tool-name suffix.
    `sdg-documentation` → `sdg_documentation`, `vcf-reference` → `vcf_reference`.
    """
    return workspace_slug.replace("-", "_")


def _workspace_description(workspace_slug: str) -> str:
    """Look up an optional human description from env, or fall back to slug."""
    env_key = f"MCP_WORKSPACE_DESC_{workspace_slug.upper().replace('-', '_')}"
    return os.environ.get(env_key, "").strip()


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {ALLM_API_KEY}",
        "Content-Type": "application/json",
    }


def _normalize_source(src: dict) -> dict:
    """Flatten one source entry from AnythingLLM into a compact dict
    (docSource tag, clickable URL, title)."""
    meta = src.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return {
        "docSource": meta.get("docSource") or src.get("docSource") or "?",
        "url": meta.get("url")
        or (src.get("chunkSource", "") or "").replace("link://", "")
        or src.get("url", ""),
        "title": meta.get("title") or src.get("title") or "",
    }


mcp = FastMCP("anythingllm-bridge", host=MCP_HOST, port=MCP_PORT)


# ---------------------------------------------------------------------------
# Inner helpers — the per-workspace `query` and `search` tool functions call
# these, parameterized by the workspace slug their factory captured.
# ---------------------------------------------------------------------------

async def _query_workspace(workspace: str, question: str) -> dict:
    log.info("query workspace=%s len(question)=%d", workspace, len(question))
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{ALLM_URL}/workspace/{workspace}/chat",
                headers=_auth_headers(),
                json={"message": question, "mode": "query"},
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.error("query failed (%s): %s", workspace, e)
        return {
            "answer": f"(MCP error fetching from AnythingLLM workspace '{workspace}': {type(e).__name__})",
            "sources": [],
            "source_count": 0,
        }

    data = resp.json()
    raw_sources = data.get("sources") or []
    return {
        "answer": data.get("textResponse", "") or "",
        "sources": [_normalize_source(s) for s in raw_sources[:8]],
        "source_count": len(raw_sources),
    }


async def _search_workspace(workspace: str, query: str, top_n: int) -> dict:
    top_n = max(1, min(20, int(top_n)))
    log.info("search workspace=%s top_n=%d len(query)=%d", workspace, top_n, len(query))
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{ALLM_URL}/workspace/{workspace}/vector-search",
                headers=_auth_headers(),
                json={"query": query, "topN": top_n, "scoreThreshold": 0.4},
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.error("search failed (%s): %s", workspace, e)
        return {"chunks": [], "error": f"{type(e).__name__}: {e}"}

    data = resp.json()
    results = data.get("results") or []
    out = []
    for r in results:
        n = _normalize_source(r)
        out.append({
            "text": (r.get("text") or r.get("pageContent") or "")[:4000],
            "docSource": n["docSource"],
            "url": n["url"],
            "title": n["title"],
            "score": r.get("score"),
        })
    return {"chunks": out}


# ---------------------------------------------------------------------------
# Tool factories — produce closures that capture a specific workspace slug
# and forward to the inner helpers. We use closures (not partials) so the
# generated functions have clean signatures that FastMCP can introspect.
# ---------------------------------------------------------------------------

def _make_query_tool(workspace: str, description: str):
    async def fn(question: str) -> dict:
        return await _query_workspace(workspace, question)

    fn.__name__ = f"query_{_tool_suffix(workspace)}"
    fn.__doc__ = (
        f"Query the '{workspace}' workspace via full RAG synthesis (embed → "
        f"retrieve → rerank → chat). Returns a synthesized answer with up to "
        f"8 citations.\n\n"
        f"{description or '(No description configured for this workspace.)'}"
        f"\n\nUse this when you want a finished answer. For raw excerpts, use "
        f"search_{_tool_suffix(workspace)} instead.\n\n"
        f"Args:\n"
        f"    question: Natural-language question. Detailed questions with "
        f"specific terms / proper nouns retrieve best.\n\n"
        f"Returns:\n"
        f'    {{"answer": str, "sources": list[{{docSource, url, title}}], "source_count": int}}'
    )
    return fn


def _make_search_tool(workspace: str, description: str):
    async def fn(query: str, top_n: int = 5) -> dict:
        return await _search_workspace(workspace, query, top_n)

    fn.__name__ = f"search_{_tool_suffix(workspace)}"
    fn.__doc__ = (
        f"Vector-search the '{workspace}' workspace and return raw chunks.\n\n"
        f"{description or '(No description configured for this workspace.)'}"
        f"\n\nUse this when you want source excerpts directly (extract a CLI "
        f"command, configuration snippet, paragraph) rather than a synthesized "
        f"answer.\n\n"
        f"Args:\n"
        f"    query: Search terms. Embedding-based — semantic similarity works.\n"
        f"    top_n: Number of chunks to return (1-20, default 5).\n\n"
        f"Returns:\n"
        f'    {{"chunks": list[{{text, docSource, url, title, score}}]}}'
    )
    return fn


# ---------------------------------------------------------------------------
# Register one tool pair per configured workspace.
# ---------------------------------------------------------------------------

for ws in WORKSPACES:
    desc = _workspace_description(ws)
    qfn = _make_query_tool(ws, desc)
    sfn = _make_search_tool(ws, desc)
    mcp.tool(name=qfn.__name__, description=qfn.__doc__)(qfn)
    mcp.tool(name=sfn.__name__, description=sfn.__doc__)(sfn)
    log.info("registered tools for workspace=%s: %s, %s", ws, qfn.__name__, sfn.__name__)


if __name__ == "__main__":
    log.info(
        "starting MCP bridge on %s:%d (workspaces=%s, allm=%s)",
        MCP_HOST, MCP_PORT, ",".join(WORKSPACES), ALLM_URL,
    )
    mcp.run(transport="sse")
