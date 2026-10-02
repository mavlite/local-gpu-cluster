"""local-delegate MCP service: localhost Streamable-HTTP server with bearer auth.

Wiring mirrors scripts/files/memory-vault-bridge.py (low-level Server +
StreamableHTTPSessionManager, stateless), served at exactly /mcp.
"""
import contextlib
import hmac
import json
import os
import time

import httpx
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from scripts.delegate.config import load_config
from scripts.delegate.gitstore import rmtree_force
from scripts.delegate.jobs import JobStore, real_deps
from scripts.delegate.ledger import Ledger
from scripts.delegate.lease import GpuLease
from scripts.delegate.router_client import InputTooLarge, PathNotAllowed, ProfileBusy, ask_local

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
    except PathNotAllowed:
        return _text("ERROR: path not allowed")
    except ProfileBusy as e:
        return _text(f"ERROR: {e}; try later")
    except (RuntimeError, OSError, ValueError, KeyError, httpx.HTTPError) as e:
        return _text(f"ERROR: local call failed ({type(e).__name__}): {str(e)[:300]}")
    finally:
        deps.lease.release()
    deps.ledger.append({"tool": "ask_local", "mode": mode, "local_tokens": out["usage"]})
    return _text(out["text"])


_TERMINAL = ("done", "failed", "abandoned")
_S = {"type": "string"}


def _tool(name: str, description: str, props: dict, required: list) -> Tool:
    return Tool(name=name, description=description,
                inputSchema={"type": "object", "properties": props, "required": required})


AGENTIC_TOOLS = [
    _tool("submit_task", "Queue a background coding job on the local model; returns job_id immediately.",
          {"task": _S, "repo": _S, "base_ref": _S,
           "checks": {"type": "array", "items": {"type": "array", "items": _S}},
           "task_type": _S, "allow_web": {"type": "boolean"}, "timeout_s": {"type": "integer"}},
          ["task", "repo"]),
    _tool("result", "Fetch a job's outcome (diff, checks, gate verdict, summary, tokens).",
          {"job_id": _S}, ["job_id"]),
    _tool("list_jobs", "List jobs, optionally filtered by status.", {"status": _S}, []),
    _tool("record_review",
          "Record a delegated job's review verdict + Claude's token cost (A/B ledger) and clean its work dir.",
          {"job_id": _S, "verdict": _S, "claude_tokens": {"type": "integer"},
           "fix_lines": {"type": "integer"}, "cause": _S},
          ["job_id", "verdict", "claude_tokens"]),
    _tool("record_direct",
          "Record a self-done (non-delegated) task's Claude token cost (A/B ledger, the coin-flip 'tails' arm).",
          {"task_type": _S, "claude_tokens": {"type": "integer"}, "note": _S},
          ["task_type", "claude_tokens"]),
]
_AGENTIC_NAMES = {t.name for t in AGENTIC_TOOLS}


class AgenticTools:
    """submit_task/result/list_jobs/record_review over a JobStore + ledger."""

    def __init__(self, cfg, store, ledger):
        self.cfg, self.store, self.ledger = cfg, store, ledger

    def submit_task(self, task, repo, base_ref="HEAD", checks=None, task_type="",
                    allow_web=False, timeout_s=1800) -> dict:
        spec = {"task": task, "repo": repo, "base_ref": base_ref, "checks": list(checks or []),
                "task_type": task_type, "allow_web": allow_web, "timeout_s": timeout_s}
        return {"job_id": self.store.submit(spec)}

    def result(self, job_id) -> dict:
        try:
            st = self.store.get(job_id)
        except (OSError, ValueError):
            return {"error": f"unknown job: {job_id}"}
        if st["status"] not in _TERMINAL:
            return {"status": st["status"], "note": "job not finished; wait for completion"}
        gate = st.get("gate") or {}
        dur = (st.get("finished") or 0) - (st.get("started") or 0)
        return {"status": st["status"], "diff": st.get("diff"), "checks": st.get("checks", []),
                "checks_skipped": st.get("checks_skipped"), "summary": st.get("text"),
                "tokens": st.get("tokens"), "duration_s": max(dur, 0),
                "flagged": gate.get("flagged", []), "gate_reasons": gate.get("reasons", []),
                "error": st.get("error")}

    def list_jobs(self, status=None) -> list:
        return [{"id": s["id"], "status": s["status"],
                 "task_type": (s.get("spec") or {}).get("task_type", ""),
                 "ts": s.get("submitted")} for s in self.store.list(status)]

    def record_review(self, job_id, verdict, claude_tokens, fix_lines=0, cause="") -> dict:
        """Delegated ('heads') arm: one consolidated measurement row joinable by job_id.

        Refuses (no rmtree) unless the job is terminal, so a running/queued job's work
        dir is never deleted out from under the worker.
        """
        st = self.store.get(job_id)  # ValueError on a traversal id; OSError if missing
        if st.get("status") not in _TERMINAL:
            return {"error": f"job {job_id} not terminal (status={st.get('status')}); not recorded"}
        dur = (st.get("finished") or 0) - (st.get("started") or 0)
        self.ledger.append({"ts": time.time(), "kind": "review", "job_id": job_id,
                            "delegated": True,
                            "task_type": (st.get("spec") or {}).get("task_type", ""),
                            "verdict": verdict, "fix_lines": fix_lines, "cause": cause,
                            "claude_tokens": claude_tokens, "local_tokens": st.get("tokens"),
                            "duration_s": max(dur, 0)})
        if os.path.basename(job_id) == job_id:  # never rmtree outside jobs_dir
            rmtree_force(os.path.join(self.cfg.jobs_dir, job_id))
        return {"ok": True}

    def record_direct(self, task_type, claude_tokens, note="") -> dict:
        """Self-done ('tails') arm: logs the task Claude did itself, for the same A/B ledger."""
        self.ledger.append({"ts": time.time(), "kind": "review", "job_id": None,
                            "delegated": False, "task_type": task_type,
                            "claude_tokens": claude_tokens, "note": note})
        return {"ok": True}


def _run_agentic(tools: AgenticTools, name: str, arguments: dict) -> list[TextContent]:
    try:
        out = getattr(tools, name)(**arguments)
    except (TypeError, OSError, ValueError, KeyError) as e:
        return _text(f"ERROR: {name} failed ({type(e).__name__}): {str(e)[:300]}")
    return _text(json.dumps(out))


def build_server(cfg, deps, store=None) -> Server:
    server = Server("local-delegate")
    tools = AgenticTools(cfg, store, ledger=deps.ledger) if store is not None else None

    @server.list_tools()
    async def _tools() -> list[Tool]:
        return [ASK_LOCAL_TOOL, *AGENTIC_TOOLS]

    @server.call_tool()
    async def _call(name: str, arguments: dict | None) -> list[TextContent]:
        if name == "ask_local":
            return await _run_ask_local(cfg, deps, arguments or {})
        if name in _AGENTIC_NAMES and tools is not None:
            return _run_agentic(tools, name, arguments or {})
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


class _McpEndpoint:
    """ASGI app for a Route. A class instance, not a function, so Starlette
    passes it the raw ASGI call instead of wrapping it as request/response."""

    def __init__(self, manager: StreamableHTTPSessionManager):
        self._manager = manager

    async def __call__(self, scope, receive, send):
        await self._manager.handle_request(scope, receive, send)


def build_app(cfg, deps, store=None) -> Starlette:
    manager = StreamableHTTPSessionManager(
        app=build_server(cfg, deps, store), event_store=None, json_response=True, stateless=True
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    # Route, not Mount: a Mount only matches /mcp/..., so a bare /mcp 307s to /mcp/.
    app = Starlette(routes=[Route("/mcp", endpoint=_McpEndpoint(manager))], lifespan=lifespan)
    app.add_middleware(BearerAuth, token=cfg.bearer_token)
    return app


class _Deps:
    def __init__(self, http, ledger, lease):
        self.http, self.ledger, self.lease = http, ledger, lease


if __name__ == "__main__":
    _cfg = load_config(os.environ)
    _ledger = Ledger(_cfg.ledger_path)  # one shared, lock-guarded writer
    _deps = _Deps(httpx.AsyncClient(timeout=_cfg.ask_timeout_s), _ledger, GpuLease(_cfg.lease_path))
    _store = JobStore(_cfg, real_deps(_cfg, ledger=_ledger))
    _store.reconcile()
    uvicorn.run(build_app(_cfg, _deps, _store), host=_cfg.host, port=_cfg.port)
