#!/usr/bin/env python3
"""MCP server exposing the AnythingLLM `sdg-documentation` workspace.

This is a thin bridge between OpenCode (or any MCP client) and AnythingLLM's
workspace-chat / vector-search REST endpoints. The workspace itself holds the
ingested corpus (OPNsense, Keycloak, TrueNAS Scale, OpenZFS, and tagged
community sources — see /tank/community-* dirs on the host for provenance).

Two tools are exposed:
  - query_sdg_documentation(question): full RAG synthesis (embed → retrieve →
    rerank → chat). Returns an answer + a compact source list. Use this when
    the calling LLM wants a finished answer.
  - search_sdg_documentation(query, top_n): direct vector search returning
    raw chunks. Use this when the calling LLM wants to read source excerpts
    itself (e.g., to extract a specific command or procedure).

Transport: SSE on $MCP_PORT (default 3004). Matches the OpenCode `type: remote`
MCP convention.

Env:
  ALLM_URL       Base URL for AnythingLLM API (default http://192.168.6.154:3001/api/v1)
  ALLM_API_KEY   AnythingLLM API key (required; from scripts/config.env)
  WORKSPACE      Workspace slug (default sdg-documentation)
  MCP_PORT       SSE listen port (default 3004)
  MCP_HOST       SSE bind address (default 0.0.0.0)
"""

import os
import sys
import logging

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s mcp-sdg %(message)s",
)
log = logging.getLogger("mcp-sdg")

ALLM_URL = os.environ.get("ALLM_URL", "http://192.168.6.154:3001/api/v1").rstrip("/")
ALLM_API_KEY = os.environ.get("ALLM_API_KEY")
WORKSPACE = os.environ.get("WORKSPACE", "sdg-documentation")
MCP_PORT = int(os.environ.get("MCP_PORT", "3004"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")

if not ALLM_API_KEY:
    log.error("ALLM_API_KEY env var is required")
    sys.exit(1)

mcp = FastMCP("sdg-docs", host=MCP_HOST, port=MCP_PORT)


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {ALLM_API_KEY}",
        "Content-Type": "application/json",
    }


def _normalize_source(src: dict) -> dict:
    """Flatten one source entry from AnythingLLM's chat response into a compact
    dict (docSource tag, clickable URL, title) — the calling LLM doesn't need
    the full chunk text in citations, just enough to identify provenance."""
    meta = src.get("metadata") or {}
    if isinstance(meta, str):
        try:
            import json as _json
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    return {
        "docSource": meta.get("docSource") or src.get("docSource") or "?",
        "url": meta.get("url")
        or (src.get("chunkSource", "") or "").replace("link://", "")
        or src.get("url", ""),
        "title": meta.get("title") or src.get("title") or "",
    }


@mcp.tool()
async def query_sdg_documentation(question: str) -> dict:
    """Query the SDG documentation workspace via full RAG synthesis.

    The workspace covers:
      - OPNsense (official docs + Zenarmor + homenetworkguy + Thomas-Krenn)
      - Keycloak (official docs + Phase Two + Inteca + Baeldung + n-k.de)
      - TrueNAS Scale + OpenZFS (official docs + ix-blog + 45drives + STH)

    Returns a synthesized answer with up to 8 citations. Use this for how-to,
    explain, and design questions about these technologies. For lookups where
    you want to read raw excerpts yourself, use `search_sdg_documentation`.

    Args:
        question: Natural-language question. RAG retrieval works best with
                  detailed questions that contain specific terms / proper nouns
                  (e.g., "How do I configure WireGuard in OPNsense with a
                  WAN-side peer?").

    Returns:
        {"answer": str, "sources": list[{docSource, url, title}], "source_count": int}
    """
    log.info("query workspace=%s len(question)=%d", WORKSPACE, len(question))
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{ALLM_URL}/workspace/{WORKSPACE}/chat",
                headers=_auth_headers(),
                json={"message": question, "mode": "query"},
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.error("query failed: %s", e)
        return {
            "answer": f"(MCP error fetching from AnythingLLM: {type(e).__name__})",
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


@mcp.tool()
async def search_sdg_documentation(query: str, top_n: int = 5) -> dict:
    """Vector-search the SDG documentation workspace and return raw chunks.

    Use when you want the source excerpts directly — for example to extract a
    specific CLI command, configuration snippet, or paragraph of vendor prose
    — rather than a synthesized answer.

    Args:
        query: Search terms. Embedding-based, so semantic similarity works
               (you don't need exact keyword matches).
        top_n: Number of chunks to return (1-20, default 5).

    Returns:
        {"chunks": list[{text, docSource, url, title, score}]}
    """
    top_n = max(1, min(20, int(top_n)))
    log.info("search workspace=%s top_n=%d len(query)=%d", WORKSPACE, top_n, len(query))
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{ALLM_URL}/workspace/{WORKSPACE}/vector-search",
                headers=_auth_headers(),
                json={"query": query, "topN": top_n, "scoreThreshold": 0.4},
            )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.error("search failed: %s", e)
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


if __name__ == "__main__":
    log.info(
        "starting sdg-docs MCP server on %s:%d (workspace=%s, allm=%s)",
        MCP_HOST, MCP_PORT, WORKSPACE, ALLM_URL,
    )
    mcp.run(transport="sse")
