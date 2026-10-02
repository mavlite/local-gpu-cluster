# local-delegate MCP (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A persistent localhost MCP service that lets Claude Code offload single-shot (`ask_local`) and agentic (`submit_task`) work to the local Qwen3.8 cluster, instrumented to measure whether delegation actually saves Claude tokens.

**Architecture:** One long-lived Python service bound to `127.0.0.1`, started at logon, speaking Streamable-HTTP MCP (same SDK pattern as `scripts/files/memory-vault-bridge.py`). It is the sole holder of a cross-process GPU lease and the sole writer of a JSONL ledger. Agentic jobs run `opencode run` against an **isolated git export** (never a shared worktree) with an isolated opencode config, confined by capability-neutering (Phase-1 stage 1 of the spec's staged isolation). Claude reviews every result; nothing merges automatically.

**Tech Stack:** Python 3.14 (`C:\Python314\python.exe`), `mcp>=1.2,<2`, `httpx`, `uvicorn`, `starlette` (the proven MCP-bridge set), git, opencode 1.18.34. Windows 11 workstation. Tests: `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-02-local-delegate-mcp-design.md` (read it alongside this plan — the plan argues from it).

## Global Constraints

- **MCP SDK pin (load-bearing):** install `mcp>=1.2,<2` exactly — mcp 2.0.0 removed modules and caused a 26-hour outage (`scripts/README.md:40`). Use the SDK low-level `Server` + `StreamableHTTPSessionManager`, **not** FastMCP.
- **New runtime deps limited to:** `mcp>=1.2,<2`, `httpx`, `uvicorn`, `starlette`. No other third-party runtime deps without asking (per repo AGENTS.md). ULID, Windows file-lock, and process-tree kill are stdlib (`secrets`/`time`, `msvcrt`, `subprocess`+`taskkill`).
- **Do not add ruff/black/mypy or change build config** (repo AGENTS.md).
- **Checks before done:** `python3 -m pytest scripts/delegate/tests -q`; `bash -n` every edited `.sh`; show real output, never weaken a test to pass it (repo AGENTS.md).
- **Commits:** conventional commits, **no `Co-Authored-By` trailer** (repo AGENTS.md + user memory). Branch: `feat/local-delegate-mcp` (already created).
- **The allow-list is not a write boundary** (proven: opencode does not check redirect targets or path args). The boundary is the isolated git export + neutered capabilities + scrubbed-env server-side checks + result gate. No `git`-write or interpreter commands in the agent's bash allow-list.
- **Router facts:** `http://192.168.6.153:8000/v1`, bearer from env `LOCAL_DELEGATE_ROUTER_TOKEN`; `CHAT_CONCURRENCY=1` and streaming is **not** serialized by the router, so the GPU lease is load-bearing. Aliases: `qwen3.8-think` (code), `rag-qwen3.8` (summaries — avoids any redteam-mode alias keying). `/healthz` is unauthenticated and reports `active_chat_profile`.
- **opencode spawn facts:** spawn `node_modules\opencode-ai\bin\opencode.exe` directly (PATH `opencode` is a `.CMD` shim); `stdin=DEVNULL` (opencode appends stdin to the prompt and hangs if attached); every permission explicit `allow`/`deny`, never `ask` (headless auto-rejects `ask` and exits 0 silently).

## Review Focus

Inputs/failure modes the spec implies but that no single task's happy-path tests exercise (each gets a test added to the owning task):

- **Two local jobs/ask_local at once must not both touch the GPU** — the lease must block the second across *processes*, not just threads (Task 3 test).
- **A job whose owner process is alive must never be marked `abandoned` by another starting session** (Task 9 test: liveness keys on PID + process-start-time, not presence of a `running` record).
- **`base_ref="WORKTREE"` on a clean tree** (`git stash create` prints `''`) must still produce a valid export, not crash (Task 6 test).
- **An opencode run that silently no-ops** (an `ask`/`rejected` permission line on stderr, exit 0) must be classified `permission-blocked`/failed, not reported as an empty success (Task 7 test).
- **A result diff containing a symlink / gitlink / mode-change / `.git*` / binary** must be rejected before Claude sees it (Task 10 tests — the exact escapes the feasibility probe performed).

---

### Task 1: Package skeleton, config, and dependency manifest

**Files:**
- Create: `scripts/delegate/__init__.py`
- Create: `scripts/delegate/config.py`
- Create: `scripts/delegate/requirements.txt`
- Create: `scripts/delegate/tests/__init__.py`
- Create: `scripts/delegate/tests/test_config.py`

**Interfaces:**
- Produces: `Config` dataclass with fields `host:str="127.0.0.1"`, `port:int=3006`, `bearer_token:str`, `router_url:str`, `router_token:str`, `allowed_repo_roots:tuple[str,...]`, `jobs_dir:str`, `ledger_path:str`, `opencode_exe:str`, `overlay_dir:str`, `ask_timeout_s:int=600`, `job_timeout_s:int=1800`, `ask_max_input_bytes:int=400_000`. Function `load_config(env:Mapping[str,str]) -> Config` reading `LOCAL_DELEGATE_*` env vars with the defaults above; raises `ConfigError` if `LOCAL_DELEGATE_BEARER_TOKEN` or `LOCAL_DELEGATE_ROUTER_TOKEN` is missing/empty.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_config.py
import pytest
from scripts.delegate.config import load_config, Config, ConfigError

BASE = {"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"}

def test_defaults_applied():
    c = load_config(BASE)
    assert isinstance(c, Config)
    assert c.host == "127.0.0.1" and c.port == 3006
    assert c.router_url.endswith("/v1")
    assert c.ask_max_input_bytes == 400_000

def test_missing_required_tokens_raise():
    with pytest.raises(ConfigError):
        load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b"})  # router token absent

def test_allowed_roots_parsed_from_pathsep_list():
    c = load_config({**BASE, "LOCAL_DELEGATE_ALLOWED_ROOTS": r"C:\a;C:\b"})
    assert c.allowed_repo_roots == (r"C:\a", r"C:\b")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.delegate.config`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/config.py
import os
from dataclasses import dataclass, field

class ConfigError(Exception): ...

@dataclass(frozen=True)
class Config:
    bearer_token: str
    router_token: str
    host: str = "127.0.0.1"
    port: int = 3006
    router_url: str = "http://192.168.6.153:8000/v1"
    allowed_repo_roots: tuple = ()
    jobs_dir: str = ""
    ledger_path: str = ""
    opencode_exe: str = ""
    overlay_dir: str = ""
    ask_timeout_s: int = 600
    job_timeout_s: int = 1800
    ask_max_input_bytes: int = 400_000

def _default_local_appdata(sub: str) -> str:
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    return os.path.join(base, "local-delegate", sub)

def load_config(env) -> Config:
    bearer = env.get("LOCAL_DELEGATE_BEARER_TOKEN", "")
    router = env.get("LOCAL_DELEGATE_ROUTER_TOKEN", "")
    if not bearer or not router:
        raise ConfigError("LOCAL_DELEGATE_BEARER_TOKEN and LOCAL_DELEGATE_ROUTER_TOKEN are required")
    roots = env.get("LOCAL_DELEGATE_ALLOWED_ROOTS", "")
    return Config(
        bearer_token=bearer,
        router_token=router,
        host=env.get("LOCAL_DELEGATE_HOST", "127.0.0.1"),
        port=int(env.get("LOCAL_DELEGATE_PORT", "3006")),
        router_url=env.get("LOCAL_DELEGATE_ROUTER_URL", "http://192.168.6.153:8000/v1"),
        allowed_repo_roots=tuple(p for p in roots.split(os.pathsep) if p),
        jobs_dir=env.get("LOCAL_DELEGATE_JOBS_DIR", _default_local_appdata("jobs")),
        ledger_path=env.get("LOCAL_DELEGATE_LEDGER", _default_local_appdata("ledger.jsonl")),
        opencode_exe=env.get("LOCAL_DELEGATE_OPENCODE_EXE", ""),
        overlay_dir=env.get("LOCAL_DELEGATE_OVERLAY", ""),
    )
```

Also create empty `scripts/delegate/__init__.py` and `scripts/delegate/tests/__init__.py`, and `requirements.txt`:

```
mcp>=1.2,<2
httpx
uvicorn
starlette
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_config.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/__init__.py scripts/delegate/config.py scripts/delegate/requirements.txt scripts/delegate/tests/
git commit -m "feat(delegate): config loader and package skeleton"
```

---

### Task 2: Ledger (single-writer append + read)

**Files:**
- Create: `scripts/delegate/ledger.py`
- Create: `scripts/delegate/tests/test_ledger.py`

**Interfaces:**
- Produces: `class Ledger(path:str)` with `append(record:dict) -> None` (atomic line append, adds `ts` ISO-8601 if absent) and `read_all() -> list[dict]`. The service holds the only `Ledger` instance (single writer).

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_ledger.py
import json
from scripts.delegate.ledger import Ledger

def test_append_then_read_roundtrip(tmp_path):
    led = Ledger(str(tmp_path / "l.jsonl"))
    led.append({"ulid": "01", "delegated": True, "claude_tokens": 12})
    led.append({"ulid": "02", "delegated": False, "claude_tokens": 40})
    rows = led.read_all()
    assert [r["ulid"] for r in rows] == ["01", "02"]
    assert all("ts" in r for r in rows)

def test_append_is_one_line_per_record(tmp_path):
    p = tmp_path / "l.jsonl"
    led = Ledger(str(p))
    led.append({"ulid": "01"})
    led.append({"ulid": "02"})
    assert p.read_text(encoding="utf-8").count("\n") == 2
    # each line is valid json
    for line in p.read_text(encoding="utf-8").splitlines():
        json.loads(line)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_ledger.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/ledger.py
import json, os, datetime

class Ledger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def append(self, record: dict) -> None:
        rec = dict(record)
        rec.setdefault("ts", datetime.datetime.now(datetime.timezone.utc).isoformat())
        line = json.dumps(rec, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def read_all(self) -> list:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_ledger.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/ledger.py scripts/delegate/tests/test_ledger.py
git commit -m "feat(delegate): single-writer JSONL ledger"
```

---

### Task 3: Cross-process GPU lease

**Files:**
- Create: `scripts/delegate/lease.py`
- Create: `scripts/delegate/tests/test_lease.py`

**Interfaces:**
- Produces: `class GpuLease(path:str)` — a context manager `acquire(timeout_s:float=0) -> bool` returning whether the lock was taken, and `release()`. Uses `msvcrt.locking` on Windows (an exclusive byte-range lock on a lock file) so the exclusion holds **across processes**. `held_by() -> int|None` returns the PID recorded in the lock file, or None.

**Review-focus test included:** the second holder must be blocked across processes.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_lease.py
import subprocess, sys, textwrap, time
from scripts.delegate.lease import GpuLease

def test_same_process_second_acquire_fails_fast(tmp_path):
    p = str(tmp_path / "gpu.lock")
    a = GpuLease(p)
    assert a.acquire(timeout_s=0) is True
    b = GpuLease(p)
    assert b.acquire(timeout_s=0) is False
    a.release()
    assert b.acquire(timeout_s=0) is True
    b.release()

def test_cross_process_exclusion(tmp_path):
    p = str(tmp_path / "gpu.lock")
    holder = textwrap.dedent(f"""
        import time, sys
        sys.path.insert(0, r"{tmp_path.parents[100] if False else ''}")
        from scripts.delegate.lease import GpuLease
        g = GpuLease(r"{p}")
        assert g.acquire(timeout_s=0) is True
        print("HELD", flush=True)
        time.sleep(3)
        g.release()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, cwd=".")
    assert proc.stdout.readline().strip() == b"HELD"   # holder has the lock
    local = GpuLease(p)
    assert local.acquire(timeout_s=0) is False          # we cannot take it
    proc.wait(timeout=10)
    assert local.acquire(timeout_s=1) is True            # free after holder exits
    local.release()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_lease.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/lease.py
import msvcrt, os, time

class GpuLease:
    def __init__(self, path: str):
        self.path = path
        self._fh = None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def acquire(self, timeout_s: float = 0) -> bool:
        deadline = time.monotonic() + timeout_s
        fh = open(self.path, "a+")
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                fh.seek(0); fh.truncate(); fh.write(str(os.getpid())); fh.flush()
                self._fh = fh
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    return False
                time.sleep(0.1)

    def held_by(self):
        try:
            with open(self.path) as f:
                v = f.read().strip()
                return int(v) if v else None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._fh.close()
                self._fh = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_lease.py -v`
Expected: PASS (cross-process test spawns a real second interpreter)

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/lease.py scripts/delegate/tests/test_lease.py
git commit -m "feat(delegate): cross-process GPU lease via msvcrt lock"
```

---

### Task 4: Router client (`ask_local` core)

**Files:**
- Create: `scripts/delegate/router_client.py`
- Create: `scripts/delegate/tests/test_router_client.py`

**Interfaces:**
- Consumes: `Config` (Task 1).
- Produces: `async def ask_local(http, cfg, *, prompt, content=None, files=None, mode="summarize", read_file=None) -> dict` returning `{"text": str, "usage": {"input": int, "output": int}}`. `http` is an injected async client with `.post(url, json=, headers=)` and `.get(url)`. `mode` → alias: `code`→`qwen3.8-think`, else `rag-qwen3.8`. Reads `files` via injected `read_file(path)->bytes`; raises `InputTooLarge` if total content exceeds `cfg.ask_max_input_bytes` (never silently truncates). Raises `ProfileBusy` if `/healthz` `active_chat_profile` is not `qwen3.8`. One retry on a 502/`service_degraded`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_router_client.py
import pytest, asyncio
from scripts.delegate.config import load_config
from scripts.delegate.router_client import ask_local, InputTooLarge, ProfileBusy

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "b", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"})

class FakeResp:
    def __init__(self, status, payload): self.status_code = status; self._p = payload
    def json(self): return self._p

class FakeHTTP:
    def __init__(self, healthz_profile="qwen3.8", post_results=None):
        self.healthz_profile = healthz_profile
        self.post_results = list(post_results or [])
        self.posts = []
    async def get(self, url): return FakeResp(200, {"active_chat_profile": self.healthz_profile})
    async def post(self, url, json=None, headers=None):
        self.posts.append(json)
        return self.post_results.pop(0)

def run(coro): return asyncio.get_event_loop().run_until_complete(coro)

def test_code_mode_uses_think_alias():
    http = FakeHTTP(post_results=[FakeResp(200, {"choices":[{"message":{"content":"ok"}}], "usage":{"prompt_tokens":5,"completion_tokens":2}})])
    out = run(ask_local(http, CFG, prompt="hi", mode="code"))
    assert http.posts[0]["model"] == "qwen3.8-think"
    assert out["text"] == "ok" and out["usage"] == {"input":5,"output":2}

def test_summarize_mode_uses_rag_alias():
    http = FakeHTTP(post_results=[FakeResp(200, {"choices":[{"message":{"content":"s"}}],"usage":{}})])
    run(ask_local(http, CFG, prompt="sum", mode="summarize"))
    assert http.posts[0]["model"] == "rag-qwen3.8"

def test_oversize_input_raises_not_truncates():
    http = FakeHTTP()
    big = "x" * (CFG.ask_max_input_bytes + 1)
    with pytest.raises(InputTooLarge):
        run(ask_local(http, CFG, prompt="p", content=big))

def test_wrong_profile_raises_profilebusy():
    http = FakeHTTP(healthz_profile="qwen3-coder")
    with pytest.raises(ProfileBusy):
        run(ask_local(http, CFG, prompt="p"))

def test_retries_once_on_degraded_then_succeeds():
    http = FakeHTTP(post_results=[FakeResp(502, {"error":"service_degraded"}),
                                  FakeResp(200, {"choices":[{"message":{"content":"ok"}}],"usage":{}})])
    out = run(ask_local(http, CFG, prompt="p"))
    assert out["text"] == "ok" and len(http.posts) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_router_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/router_client.py
class InputTooLarge(Exception): ...
class ProfileBusy(Exception): ...

_ALIAS = {"code": "qwen3.8-think"}

def _alias(mode: str) -> str:
    return _ALIAS.get(mode, "rag-qwen3.8")

def _healthz_url(cfg) -> str:
    return cfg.router_url.rsplit("/v1", 1)[0] + "/healthz"

async def ask_local(http, cfg, *, prompt, content=None, files=None, mode="summarize", read_file=None):
    parts = []
    if content:
        parts.append(content)
    for path in (files or []):
        data = (read_file or _default_read)(path)
        parts.append(data.decode("utf-8", "replace"))
    joined = "".join(parts)
    if len(joined.encode("utf-8")) > cfg.ask_max_input_bytes:
        raise InputTooLarge(f"input exceeds {cfg.ask_max_input_bytes} bytes")

    hz = await http.get(_healthz_url(cfg))
    if (hz.json() or {}).get("active_chat_profile") != "qwen3.8":
        raise ProfileBusy("chat slot is not serving qwen3.8")

    messages = [{"role": "user", "content": (prompt + "\n\n" + joined) if joined else prompt}]
    body = {"model": _alias(mode), "messages": messages, "stream": False}
    headers = {"Authorization": f"Bearer {cfg.router_token}", "Content-Type": "application/json"}
    url = cfg.router_url.rstrip("/") + "/chat/completions"

    for attempt in range(2):
        r = await http.post(url, json=body, headers=headers)
        if r.status_code == 200:
            data = r.json()
            text = data["choices"][0]["message"].get("content", "")
            u = data.get("usage") or {}
            return {"text": text, "usage": {"input": u.get("prompt_tokens", 0),
                                            "output": u.get("completion_tokens", 0)}}
        if attempt == 0 and r.status_code in (502, 503):
            continue
        raise RuntimeError(f"router error {r.status_code}: {r.json()}")

def _default_read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_router_client.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/router_client.py scripts/delegate/tests/test_router_client.py
git commit -m "feat(delegate): router client with alias routing, profile guard, size cap"
```

---

### Task 5: MCP service skeleton + `ask_local` tool + Claude Code registration

**Files:**
- Create: `scripts/delegate/service.py`
- Create: `scripts/delegate/tests/test_service_auth.py`
- Modify: `.mcp.json` (add the `local-delegate` server)

**Interfaces:**
- Consumes: `Config`, `ask_local`, `Ledger`, `GpuLease`.
- Produces: a Starlette app factory `build_app(cfg, deps) -> Starlette` mounting the MCP session manager at `/mcp` (pattern: `memory-vault-bridge.py:27,209-240`), gated by a bearer-auth middleware that 401s a request whose `Authorization` header ≠ `Bearer {cfg.bearer_token}`. Registers the MCP tool `ask_local`. `deps` is a small object exposing `http`, `ledger`, `lease` so tests inject fakes.

- [ ] **Step 1: Write the failing test** (auth middleware is unit-testable without a live GPU)

```python
# scripts/delegate/tests/test_service_auth.py
from starlette.testclient import TestClient
from scripts.delegate.config import load_config
from scripts.delegate.service import build_app

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN": "secret", "LOCAL_DELEGATE_ROUTER_TOKEN": "r"})

class Deps:  # minimal stand-ins; /mcp handshake not exercised here
    http = None; ledger = None; lease = None

def test_missing_bearer_is_401():
    client = TestClient(build_app(CFG, Deps()))
    r = client.post("/mcp", json={})
    assert r.status_code == 401

def test_wrong_bearer_is_401():
    client = TestClient(build_app(CFG, Deps()))
    r = client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401

def test_correct_bearer_passes_auth_gate():
    client = TestClient(build_app(CFG, Deps()))
    # correct token clears the 401 gate (handshake/4xx beyond auth is acceptable here)
    r = client.post("/mcp", json={}, headers={"Authorization": "Bearer secret"})
    assert r.status_code != 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_service_auth.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

Follow `scripts/files/memory-vault-bridge.py` for the `Server` + `StreamableHTTPSessionManager` + `Starlette(Mount("/mcp", app=handler))` wiring. Add a bearer middleware and register one tool.

```python
# scripts/delegate/service.py  (abridged to the auth gate + tool registration shape)
import contextlib
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount
from scripts.delegate.router_client import ask_local

def build_server(cfg, deps) -> Server:
    server = Server("local-delegate")

    @server.list_tools()
    async def _tools():
        return [Tool(name="ask_local",
                     description="Single-shot local-LLM call; server reads files so bulky input stays out of Claude's context.",
                     inputSchema={"type":"object",
                                  "properties":{"prompt":{"type":"string"},
                                                "content":{"type":"string"},
                                                "files":{"type":"array","items":{"type":"string"}},
                                                "mode":{"type":"string","enum":["summarize","code","general"]}},
                                  "required":["prompt"]})]

    @server.call_tool()
    async def _call(name, arguments):
        if name == "ask_local":
            if not deps.lease.acquire(timeout_s=0):
                return [TextContent(type="text", text="ERROR: GPU busy (a job holds the lease). Try later.")]
            try:
                out = await ask_local(deps.http, cfg, **arguments)
            finally:
                deps.lease.release()
            deps.ledger.append({"tool":"ask_local","mode":arguments.get("mode","summarize"),
                                "local_tokens":out["usage"]})
            return [TextContent(type="text", text=out["text"])]
        return [TextContent(type="text", text=f"unknown tool {name}")]
    return server

class BearerAuth(BaseHTTPMiddleware):
    def __init__(self, app, token): super().__init__(app); self.token = token
    async def dispatch(self, request, call_next):
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

def build_app(cfg, deps) -> Starlette:
    server = build_server(cfg, deps)
    manager = StreamableHTTPSessionManager(app=server, stateless=True)
    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield
    app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
    app.add_middleware(BearerAuth, token=cfg.bearer_token)
    return app
```

Add an `if __name__ == "__main__":` block that calls `load_config(os.environ)`, builds real deps (`httpx.AsyncClient`, `Ledger`, `GpuLease`), and runs `uvicorn.run(app, host=cfg.host, port=cfg.port)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_service_auth.py -v`
Expected: PASS

- [ ] **Step 5: Register with Claude Code and live-smoke `ask_local`**

Add to `.mcp.json`:

```json
"local-delegate": { "type": "http", "url": "http://127.0.0.1:3006/mcp",
                    "headers": { "Authorization": "Bearer ${LOCAL_DELEGATE_BEARER_TOKEN}" } }
```

Start the service in one terminal (`LOCAL_DELEGATE_BEARER_TOKEN=… LOCAL_DELEGATE_ROUTER_TOKEN=… python -m scripts.delegate.service`). In Claude Code, confirm the `local-delegate` MCP connects and `ask_local` returns text for a small prompt **only if `/healthz` shows `qwen3.8`**. Record whether `.mcp.json` honored the bearer `headers` field; if not, fall back to a token query-param on the URL and note it. (This is the one live check in this task; it needs the GPU profile loaded.)

- [ ] **Step 6: Commit**

```bash
git add scripts/delegate/service.py scripts/delegate/tests/test_service_auth.py .mcp.json
git commit -m "feat(delegate): MCP service skeleton with bearer auth and ask_local"
```

---

### Task 6: Isolated git store (export + WORKTREE snapshot + patch extraction)

**Files:**
- Create: `scripts/delegate/gitstore.py`
- Create: `scripts/delegate/tests/test_gitstore.py`

**Interfaces:**
- Consumes: `Config` (allowed roots).
- Produces:
  - `validate_repo(cfg, repo:str) -> str` — resolves `repo` via `os.path.realpath`, requires it to be a directory under one of `cfg.allowed_repo_roots` and to contain `.git`; else raises `RepoNotAllowed`.
  - `resolve_ref(repo:str, base_ref:str) -> str` — rejects refs matching `^-` or containing `..`; returns a commit sha via `git rev-parse --verify --end-of-options <ref>^{commit}`. For `base_ref=="WORKTREE"` returns the string `"WORKTREE"` (handled by `export`).
  - `export(repo:str, ref:str, dest:str) -> None` — creates `dest` as an **independent** tree with a fresh `.git` containing exactly one commit of the ref's content (tracked + untracked for WORKTREE). The job's `.git` must not point at `repo`'s `.git`.
  - `extract_patch(dest:str, base_sha:str) -> str` — returns `git -C dest diff <base>..HEAD` (after the runner commits the agent's edits, Task 8).

**Review-focus test included:** `WORKTREE` on a clean tree must not crash.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_gitstore.py
import subprocess, os, pytest
from scripts.delegate.config import load_config
from scripts.delegate import gitstore

def _run(*a, cwd): subprocess.run(a, cwd=cwd, check=True, capture_output=True)

def _repo(tmp_path):
    r = tmp_path / "repo"; r.mkdir()
    _run("git","init","-q", cwd=r); _run("git","config","user.email","t@t", cwd=r)
    _run("git","config","user.name","t", cwd=r)
    (r/"a.txt").write_text("base\n")
    _run("git","add","-A", cwd=r); _run("git","commit","-qm","init", cwd=r)
    return r

def _cfg(root): return load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r",
                                    "LOCAL_DELEGATE_ALLOWED_ROOTS":str(root)})

def test_validate_rejects_repo_outside_roots(tmp_path):
    r = _repo(tmp_path)
    with pytest.raises(gitstore.RepoNotAllowed):
        gitstore.validate_repo(_cfg(tmp_path/"other"), str(r))

def test_resolve_ref_rejects_dangerous(tmp_path):
    r = _repo(tmp_path)
    for bad in ("-x", "a..b"):
        with pytest.raises(Exception):
            gitstore.resolve_ref(str(r), bad)

def test_export_head_is_independent_git(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    sha = gitstore.resolve_ref(str(r), "HEAD")
    gitstore.export(str(r), sha, str(dest))
    assert (dest/"a.txt").read_text() == "base\n"
    # dest/.git is a real dir, not a pointer into repo/.git
    assert (dest/".git").is_dir()
    gitdir = subprocess.run(["git","-C",str(dest),"rev-parse","--git-dir"],
                            capture_output=True,text=True).stdout.strip()
    assert os.path.realpath(os.path.join(str(dest),gitdir)) != os.path.realpath(str(r/".git"))

def test_worktree_snapshot_includes_uncommitted_and_untracked(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    (r/"a.txt").write_text("edited\n")        # tracked, uncommitted
    (r/"new.txt").write_text("fresh\n")       # untracked
    gitstore.export(str(r), "WORKTREE", str(dest))
    assert (dest/"a.txt").read_text() == "edited\n"
    assert (dest/"new.txt").read_text() == "fresh\n"

def test_worktree_on_clean_tree_does_not_crash(tmp_path):
    r = _repo(tmp_path); dest = tmp_path/"job"
    gitstore.export(str(r), "WORKTREE", str(dest))   # stash create prints '' — must still export HEAD
    assert (dest/"a.txt").read_text() == "base\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_gitstore.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/gitstore.py
import os, re, shutil, subprocess, tempfile

class RepoNotAllowed(Exception): ...
class BadRef(Exception): ...

def _git(repo, *args, env=None):
    return subprocess.run(["git","-C",repo,*args], check=True, capture_output=True, text=True, env=env).stdout

def validate_repo(cfg, repo: str) -> str:
    real = os.path.realpath(repo)
    roots = [os.path.realpath(r) for r in cfg.allowed_repo_roots]
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise RepoNotAllowed(repo)
    if not os.path.isdir(os.path.join(real, ".git")):
        raise RepoNotAllowed(f"{repo} is not a git repo")
    return real

def resolve_ref(repo: str, base_ref: str) -> str:
    if base_ref == "WORKTREE":
        return "WORKTREE"
    if base_ref.startswith("-") or ".." in base_ref:
        raise BadRef(base_ref)
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}").strip()

def export(repo: str, ref: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    if ref == "WORKTREE":
        # snapshot tracked+untracked into a temp index, write a tree, archive it
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "index")
            env = {**os.environ, "GIT_INDEX_FILE": idx}
            head = _git(repo, "rev-parse", "HEAD").strip()
            subprocess.run(["git","-C",repo,"read-tree",head], check=True, env=env, capture_output=True)
            subprocess.run(["git","-C",repo,"add","-A"], check=True, env=env, capture_output=True)
            tree = subprocess.run(["git","-C",repo,"write-tree"], check=True, env=env,
                                  capture_output=True, text=True).stdout.strip()
            _archive_tree(repo, tree, dest)
    else:
        _archive_tree(repo, ref, dest)
    _init_fresh_git(dest)

def _archive_tree(repo: str, treeish: str, dest: str) -> None:
    tar = subprocess.run(["git","-C",repo,"archive","--format=tar",treeish],
                         check=True, capture_output=True).stdout
    import tarfile, io
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(dest)

def _init_fresh_git(dest: str) -> None:
    subprocess.run(["git","-C",dest,"init","-q"], check=True, capture_output=True)
    subprocess.run(["git","-C",dest,"config","user.email","delegate@local"], check=True, capture_output=True)
    subprocess.run(["git","-C",dest,"config","user.name","delegate"], check=True, capture_output=True)
    subprocess.run(["git","-C",dest,"add","-A"], check=True, capture_output=True)
    subprocess.run(["git","-C",dest,"commit","-qm","base"], check=True, capture_output=True)

def base_sha(dest: str) -> str:
    return _git(dest, "rev-parse", "HEAD").strip()

def extract_patch(dest: str, base: str) -> str:
    return _git(dest, "diff", f"{base}..HEAD")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_gitstore.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/gitstore.py scripts/delegate/tests/test_gitstore.py
git commit -m "feat(delegate): isolated git export with WORKTREE snapshot and patch extraction"
```

---

### Task 7: Delegate overlay + isolated opencode config (verify-by-running)

**Files:**
- Create: `clients/opencode-delegate/opencode.json` (project config injected into each job dir)
- Create: `clients/opencode-delegate/agent/delegate.md`
- Create: `clients/opencode-delegate/allowlist.toml`
- Create: `clients/opencode-delegate/README.md`
- Create: `scripts/delegate/overlay.py`
- Create: `scripts/delegate/tests/test_overlay.py`

**Interfaces:**
- Produces:
  - `strip_project_config(dest:str) -> None` — removes any `opencode.json*`, `.opencode/`, `AGENTS.md`, `CLAUDE.md` present in the exported job dir (attacker-controlled config from `base_ref`).
  - `install_overlay(cfg, dest:str, allow_web:bool) -> None` — writes the trusted `.opencode/agent/delegate.md` and a project `opencode.json` into `dest`, enabling `searxng`/web tools only if `allow_web`.
  - `opencode_env(cfg) -> dict` — returns env overrides that point opencode at an **isolated config home** (so the user's global RULES.md/TASK-LOOP.md/global MCPs/skills are not inherited). The exact variable is verified in Step 5.

The `delegate.md` frontmatter encodes the neutered capabilities:

```markdown
---
model: router/qwen3.8-think
permission:
  edit: allow
  bash:
    "*": deny
    "cat *": allow
    "ls *": allow
    "rg *": allow
    "sed -n *": allow
  webfetch: deny
  external_directory: deny
  skill: { "*": deny, "delegate-*": allow }
---
You are a delegated implementation agent working ONLY inside the current directory.
Read before writing. Finish with a section headed `SUMMARY:` describing what you changed and why.
Never run git, package managers, or interpreters; the orchestrator runs the checks.
```

(No `git`, no `python`/`pytest`/`node` in the allow-list — those are code-exec primitives per the feasibility probe. The orchestrator runs checks itself in Task 9.)

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_overlay.py
import os
from scripts.delegate.config import load_config
from scripts.delegate import overlay

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r",
                   "LOCAL_DELEGATE_OVERLAY": os.path.abspath("clients/opencode-delegate")})

def test_strip_removes_attacker_config(tmp_path):
    d = tmp_path/"job"; (d/".opencode").mkdir(parents=True)
    (d/"opencode.json").write_text('{"mcp":{"evil":{}}}')
    (d/"AGENTS.md").write_text("do bad things")
    overlay.strip_project_config(str(d))
    assert not (d/"opencode.json").exists()
    assert not (d/".opencode").exists()
    assert not (d/"AGENTS.md").exists()

def test_install_overlay_writes_agent_def(tmp_path):
    d = tmp_path/"job"; d.mkdir()
    overlay.install_overlay(CFG, str(d), allow_web=False)
    assert (d/".opencode"/"agent"/"delegate.md").exists()
    cfg_txt = (d/"opencode.json").read_text()
    assert "searxng" not in cfg_txt  # web off by default

def test_install_overlay_enables_web_when_asked(tmp_path):
    d = tmp_path/"job"; d.mkdir()
    overlay.install_overlay(CFG, str(d), allow_web=True)
    assert "searxng" in (d/"opencode.json").read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_overlay.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation** — create the overlay files above, plus:

```python
# scripts/delegate/overlay.py
import json, os, shutil

_STRIP = ("opencode.json", "opencode.jsonc", "AGENTS.md", "CLAUDE.md")

def strip_project_config(dest: str) -> None:
    for name in _STRIP:
        p = os.path.join(dest, name)
        if os.path.exists(p):
            os.remove(p)
    d = os.path.join(dest, ".opencode")
    if os.path.isdir(d):
        shutil.rmtree(d)

def install_overlay(cfg, dest: str, allow_web: bool) -> None:
    agent_src = os.path.join(cfg.overlay_dir, "agent", "delegate.md")
    agent_dst = os.path.join(dest, ".opencode", "agent")
    os.makedirs(agent_dst, exist_ok=True)
    shutil.copyfile(agent_src, os.path.join(agent_dst, "delegate.md"))
    conf = {"$schema": "https://opencode.ai/config.json", "mcp": {}}
    if allow_web:
        conf["mcp"]["searxng"] = {"type": "local", "command": ["npx","-y","mcp-searxng"], "enabled": True}
    with open(os.path.join(dest, "opencode.json"), "w", encoding="utf-8") as f:
        json.dump(conf, f)

def opencode_env(cfg) -> dict:
    # Isolated config home so global RULES/TASK-LOOP/MCPs/skills are NOT inherited.
    # The exact var is pinned by the Step-5 experiment; default to a scratch HOME.
    iso = os.path.join(cfg.jobs_dir, "_opencode_home")
    os.makedirs(iso, exist_ok=True)
    return {"HOME": iso, "USERPROFILE": iso, "XDG_CONFIG_HOME": os.path.join(iso, ".config")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_overlay.py -v`
Expected: PASS

- [ ] **Step 5: Verify isolation + silent-no-op detection by running opencode once**

With the service NOT involved, run `opencode run --agent delegate --dir <a scratch export>` under `overlay.opencode_env` and confirm, from `--format json` + stderr: (a) the system prompt does **not** contain the user's global RULES.md/TASK-LOOP.md and the tool list excludes `searxng_*`/`context7_*`; (b) a denied `external_directory` write produces a `rejected`/"auto-rejecting" signal rather than a silent success. Record the env var that actually isolates config; fix `opencode_env` to match. Capture the JSON shape of `rejected` events for Task 8.

- [ ] **Step 6: Commit**

```bash
git add clients/opencode-delegate/ scripts/delegate/overlay.py scripts/delegate/tests/test_overlay.py
git commit -m "feat(delegate): trusted opencode overlay and isolated config, strip attacker project config"
```

---

### Task 8: opencode runner (spawn + event parsing + process-tree kill)

**Files:**
- Create: `scripts/delegate/runner.py`
- Create: `scripts/delegate/tests/stub_opencode.py` (stand-in opencode for tests)
- Create: `scripts/delegate/tests/test_runner.py`

**Interfaces:**
- Consumes: `Config`, `overlay.opencode_env`.
- Produces: `def run_opencode(cfg, dest, prompt, *, spawn=subprocess.Popen, timeout_s=None) -> RunResult` where `RunResult` has `.exit_code:int`, `.text:str` (concatenated `text` parts), `.tokens:dict` (summed `input`/`output`/`reasoning`/`cache`), `.rejected:bool` (any `rejected`/"auto-rejecting"), `.stderr_tail:str`. Spawns with `stdin=DEVNULL`, resolves the real `.exe`, kills the whole process tree (`taskkill /PID <pid> /T /F`) on timeout. `spawn` is injected so tests substitute a fake that launches `stub_opencode.py`.

**Review-focus test included:** silent-no-op (`rejected`, exit 0) is surfaced as `.rejected`.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_runner.py
import sys, subprocess, functools, os
from scripts.delegate.config import load_config
from scripts.delegate import runner

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r",
                   "LOCAL_DELEGATE_OPENCODE_EXE": sys.executable})
STUB = os.path.join(os.path.dirname(__file__), "stub_opencode.py")

def _spawn_stub(scenario):
    def spawn(cmd, **kw):
        return subprocess.Popen([sys.executable, STUB, scenario], **kw)
    return spawn

def test_sums_tokens_and_concats_text():
    res = runner.run_opencode(CFG, ".", "p", spawn=_spawn_stub("ok"))
    assert res.exit_code == 0
    assert "hello world" in res.text
    assert res.tokens["input"] == 30 and res.tokens["output"] == 7
    assert res.rejected is False

def test_rejected_permission_is_flagged_even_on_exit_zero():
    res = runner.run_opencode(CFG, ".", "p", spawn=_spawn_stub("rejected"))
    assert res.exit_code == 0
    assert res.rejected is True

def test_timeout_kills_and_reports():
    res = runner.run_opencode(CFG, ".", "p", spawn=_spawn_stub("hang"), timeout_s=1)
    assert res.exit_code != 0
```

```python
# scripts/delegate/tests/stub_opencode.py  — emulates `opencode run --format json`
import sys, time, json
scenario = sys.argv[-1]
def emit(o): print(json.dumps(o), flush=True)
if scenario == "ok":
    emit({"type":"step_finish","part":{"tokens":{"input":20,"output":4,"reasoning":0,"cache":0}}})
    emit({"type":"text","text":"hello "})
    emit({"type":"step_finish","part":{"tokens":{"input":10,"output":3,"reasoning":0,"cache":0}}})
    emit({"type":"text","text":"world"})
    sys.exit(0)
if scenario == "rejected":
    sys.stderr.write("permission requested: external_directory (...); auto-rejecting\n")
    sys.exit(0)
if scenario == "hang":
    time.sleep(60); sys.exit(0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/runner.py
import json, subprocess, threading, dataclasses
from scripts.delegate import overlay

@dataclasses.dataclass
class RunResult:
    exit_code: int; text: str; tokens: dict; rejected: bool; stderr_tail: str

def _opencode_cmd(cfg, dest, prompt):
    return [cfg.opencode_exe, "run", "--agent", "delegate", "--dir", dest,
            "--format", "json", prompt]

def run_opencode(cfg, dest, prompt, *, spawn=subprocess.Popen, timeout_s=None) -> RunResult:
    env = {**overlay.opencode_env(cfg)}
    proc = spawn(_opencode_cmd(cfg, dest, prompt),
                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, env=env)
    texts, tok = [], {"input":0,"output":0,"reasoning":0,"cache":0}
    def _reap_timeout():
        subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"], capture_output=True)
    timer = threading.Timer(timeout_s, _reap_timeout) if timeout_s else None
    if timer: timer.start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line: continue
            try: evt = json.loads(line)
            except json.JSONDecodeError: continue
            if evt.get("type") == "text":
                texts.append(evt.get("text",""))
            elif evt.get("type") == "step_finish":
                for k in tok: tok[k] += int(evt.get("part",{}).get("tokens",{}).get(k,0))
        proc.wait()
    finally:
        if timer: timer.cancel()
    stderr = proc.stderr.read() if proc.stderr else ""
    rejected = ("auto-rejecting" in stderr) or ('"type": "rejected"' in stderr)
    return RunResult(exit_code=proc.returncode, text="".join(texts), tokens=tok,
                     rejected=rejected, stderr_tail=stderr[-2000:])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_runner.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/runner.py scripts/delegate/tests/stub_opencode.py scripts/delegate/tests/test_runner.py
git commit -m "feat(delegate): opencode runner with detached stdin, event parsing, tree-kill"
```

---

### Task 9: Server-side checks (argv allow-list + scrubbed env)

**Files:**
- Create: `scripts/delegate/checks.py`
- Create: `scripts/delegate/tests/test_checks.py`

**Interfaces:**
- Consumes: `clients/opencode-delegate/allowlist.toml` (the check allow-list).
- Produces: `run_checks(dest:str, checks:list[list[str]], *, runner=subprocess.run) -> list[CheckResult]` where each `checks` entry is an **argv list** (never a shell string). Rejects (raises `CheckNotAllowed`) any entry whose argv[0:n] prefix is not in the allow-list. Runs with a **scrubbed env** (no `LOCAL_DELEGATE_*`, `GH_TOKEN`, `SSH_*`), adding `PYTHONDONTWRITEBYTECODE=1`; for `python`/`pytest` prepends isolation flags (`-I`, `-p no:cacheprovider`). `CheckResult` has `.argv`, `.exit_code`, `.output`.

**Review-focus note:** the scrubbed env must not expose the router token.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_checks.py
import sys, pytest
from scripts.delegate import checks

ALLOW = [["python"], ["bash","-n"]]

def test_non_allowlisted_command_rejected(tmp_path):
    with pytest.raises(checks.CheckNotAllowed):
        checks.run_checks(str(tmp_path), [["rm","-rf","x"]], allowlist=ALLOW)

def test_scrubbed_env_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DELEGATE_ROUTER_TOKEN", "SECRET")
    probe = [sys.executable, "-c", "import os;print('TOK' if os.environ.get('LOCAL_DELEGATE_ROUTER_TOKEN') else 'NONE')"]
    res = checks.run_checks(str(tmp_path), [probe], allowlist=[[sys.executable]])
    assert res[0].output.strip().endswith("NONE")

def test_passing_check_reports_exit_zero(tmp_path):
    res = checks.run_checks(str(tmp_path), [[sys.executable,"-c","print(1)"]], allowlist=[[sys.executable]])
    assert res[0].exit_code == 0 and "1" in res[0].output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_checks.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/checks.py
import os, subprocess, dataclasses

class CheckNotAllowed(Exception): ...

@dataclasses.dataclass
class CheckResult:
    argv: list; exit_code: int; output: str

_SCRUB_PREFIXES = ("LOCAL_DELEGATE_",)
_SCRUB_EXACT = ("GH_TOKEN","GITHUB_TOKEN")
_SCRUB_CONTAINS = ("SSH",)

def _scrubbed_env() -> dict:
    out = {}
    for k, v in os.environ.items():
        if any(k.startswith(p) for p in _SCRUB_PREFIXES): continue
        if k in _SCRUB_EXACT: continue
        if any(s in k for s in _SCRUB_CONTAINS): continue
        out[k] = v
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    return out

def _allowed(argv, allowlist) -> bool:
    return any(argv[:len(p)] == p for p in allowlist)

def _isolate(argv) -> list:
    base = os.path.basename(argv[0]).lower()
    if base.startswith("python") and "-I" not in argv:
        return [argv[0], "-I", *argv[1:]]
    return argv

def run_checks(dest, checks_list, *, allowlist, runner=subprocess.run):
    results = []
    for argv in checks_list:
        if not _allowed(argv, allowlist):
            raise CheckNotAllowed(argv)
        proc = runner(_isolate(argv), cwd=dest, env=_scrubbed_env(),
                      capture_output=True, text=True)
        results.append(CheckResult(argv=argv, exit_code=proc.returncode,
                                    output=(proc.stdout or "") + (proc.stderr or "")))
    return results
```

(Load the allow-list from `allowlist.toml` where the service wires this; tests pass it directly.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_checks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/checks.py scripts/delegate/tests/test_checks.py
git commit -m "feat(delegate): argv-only checks with scrubbed env and interpreter isolation"
```

---

### Task 10: Result gate (reject dangerous diffs)

**Files:**
- Create: `scripts/delegate/resultgate.py`
- Create: `scripts/delegate/tests/test_resultgate.py`

**Interfaces:**
- Consumes: output of `gitstore.extract_patch`.
- Produces: `inspect_diff(dest:str) -> GateReport` running `git -C dest diff --raw --summary base..HEAD`; `GateReport` has `.rejected:bool`, `.reasons:list[str]`, `.flagged:list[str]` (dependency/CI/config paths needing mandatory attention). **Rejects** any diff introducing a symlink (mode `120000`), gitlink/submodule (mode `160000`), executable-bit change, binary blob, or a path under `.git`/`.github`/`.husky` or named `conftest.py`. **Flags** (not rejects) changes to `package.json`, `requirements*.txt`, lockfiles, CI yml.

**Review-focus tests included:** the exact escape classes the feasibility probe used.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_resultgate.py
import subprocess, os, stat, pytest
from scripts.delegate import resultgate, gitstore

def _job(tmp_path):
    d = tmp_path/"job"; d.mkdir()
    for a in (["init","-q"],["config","user.email","d@d"],["config","user.name","d"]):
        subprocess.run(["git","-C",str(d),*a], check=True, capture_output=True)
    (d/"a.txt").write_text("base\n")
    subprocess.run(["git","-C",str(d),"add","-A"], check=True, capture_output=True)
    subprocess.run(["git","-C",str(d),"commit","-qm","base"], check=True, capture_output=True)
    return d

def _commit_all(d):
    subprocess.run(["git","-C",str(d),"add","-A"], check=True, capture_output=True)
    subprocess.run(["git","-C",str(d),"commit","-qm","work"], check=True, capture_output=True)

def test_plain_edit_passes(tmp_path):
    d = _job(tmp_path); (d/"a.txt").write_text("changed\n"); _commit_all(d)
    rep = resultgate.inspect_diff(str(d))
    assert rep.rejected is False

def test_git_dir_path_rejected(tmp_path):
    d = _job(tmp_path); os.makedirs(d/".git"/"hooks", exist_ok=True)
    (d/".git"/"hooks"/"post-commit").write_text("#!/bin/sh\necho pwn\n")
    # staging under .git is unusual; emulate via a tracked path that maps into .github instead
    os.makedirs(d/".github", exist_ok=True); (d/".github"/"ci.yml").write_text("run: evil\n"); _commit_all(d)
    rep = resultgate.inspect_diff(str(d))
    assert rep.rejected is True and any(".github" in r for r in rep.reasons)

def test_mode_change_to_executable_rejected(tmp_path):
    d = _job(tmp_path); f = d/"a.txt"; os.chmod(f, os.stat(f).st_mode | stat.S_IXUSR)
    subprocess.run(["git","-C",str(d),"update-index","--chmod=+x","a.txt"], capture_output=True)
    _commit_all(d)
    rep = resultgate.inspect_diff(str(d))
    assert rep.rejected is True

def test_requirements_change_flagged_not_rejected(tmp_path):
    d = _job(tmp_path); (d/"requirements.txt").write_text("evil-pkg\n"); _commit_all(d)
    rep = resultgate.inspect_diff(str(d))
    assert rep.rejected is False and any("requirements" in p for p in rep.flagged)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_resultgate.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/delegate/resultgate.py
import subprocess, dataclasses, re, os

@dataclasses.dataclass
class GateReport:
    rejected: bool; reasons: list; flagged: list

_REJECT_PATH = re.compile(r"(^|/)(\.git|\.github|\.husky)(/|$)|(^|/)conftest\.py$")
_FLAG_PATH = re.compile(r"(^|/)(package\.json|requirements[^/]*\.txt|.*\.lock|.*lock\.json)$|(\.ya?ml)$")

def inspect_diff(dest: str) -> GateReport:
    raw = subprocess.run(["git","-C",dest,"diff","--raw","-M","base..HEAD"],
                         capture_output=True, text=True, check=True).stdout
    reasons, flagged = [], []
    for line in raw.splitlines():
        if not line.startswith(":"): continue
        meta, _, path = line.partition("\t")
        fields = meta[1:].split()
        src_mode, dst_mode = fields[0], fields[1]
        path = path.split("\t")[-1]
        if dst_mode == "120000": reasons.append(f"symlink: {path}")
        if dst_mode == "160000" or src_mode == "160000": reasons.append(f"gitlink: {path}")
        if src_mode not in ("000000", dst_mode) and dst_mode.endswith("755") and not src_mode.endswith("755"):
            reasons.append(f"mode-change +x: {path}")
        if _REJECT_PATH.search(path): reasons.append(f"protected path: {path}")
        if _FLAG_PATH.search(path): flagged.append(path)
    # binary detection
    numstat = subprocess.run(["git","-C",dest,"diff","--numstat","base..HEAD"],
                             capture_output=True, text=True, check=True).stdout
    for line in numstat.splitlines():
        if line.startswith("-\t-\t"): reasons.append(f"binary: {line.split(chr(9))[-1]}")
    return GateReport(rejected=bool(reasons), reasons=reasons, flagged=flagged)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_resultgate.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/resultgate.py scripts/delegate/tests/test_resultgate.py
git commit -m "feat(delegate): result gate rejecting symlink/gitlink/mode/binary/protected-path diffs"
```

---

### Task 11: Job queue (one worker, ULID, owner-PID liveness, persistence)

**Files:**
- Create: `scripts/delegate/ids.py`
- Create: `scripts/delegate/jobs.py`
- Create: `scripts/delegate/tests/test_jobs.py`

**Interfaces:**
- Consumes: `gitstore`, `overlay`, `runner`, `checks`, `resultgate`, `GpuLease`, `Ledger`, `Config`.
- Produces:
  - `ids.new_id() -> str` — a sortable, process-unique id (`<base32 time><base32 random>`).
  - `class JobStore(cfg, deps)` with `submit(spec:dict) -> str` (writes `jobs/<id>.json` state `queued`, enqueues), `get(id) -> dict`, `list(status=None) -> list[dict]`, and an internal single worker that runs one job at a time **holding the lease for the whole job**. Each job file records `owner_pid` and `owner_started` (process create time). `reconcile() -> None` (called at startup) marks a `running` job `abandoned` **only if its owner process is dead**; a job past its deadline that resumed is `interrupted`. The pipeline per job: validate → export (strip+overlay) → run → commit agent edits → checks → gate → persist (`diff`, check output, tokens, text, verdict-ready).

**Review-focus test included:** a live owner's `running` job is never abandoned.

- [ ] **Step 1: Write the failing test** (worker uses injected fakes; no GPU)

```python
# scripts/delegate/tests/test_jobs.py
import os, json, time
from scripts.delegate.config import load_config
from scripts.delegate import jobs, ids

def test_ids_are_sorted_and_unique():
    xs = [ids.new_id() for _ in range(100)]
    assert len(set(xs)) == 100 and xs == sorted(xs)

def _cfg(tmp_path):
    return load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r",
                        "LOCAL_DELEGATE_JOBS_DIR": str(tmp_path/"jobs")})

class FakeDeps:
    """Records calls; runs the pipeline synchronously with stubs."""
    def __init__(self): self.lease_held = 0; self.max_concurrent = 0; self.ran = []
    # lease
    def acquire(self, timeout_s=0):
        self.lease_held += 1; self.max_concurrent = max(self.max_concurrent, self.lease_held); return True
    def release(self): self.lease_held -= 1

def test_single_worker_never_runs_two_at_once(tmp_path):
    store = jobs.JobStore(_cfg(tmp_path), deps=jobs.make_test_deps(sleep=0.2))
    a = store.submit({"task":"t1","repo":".","base_ref":"HEAD","checks":[]})
    b = store.submit({"task":"t2","repo":".","base_ref":"HEAD","checks":[]})
    store.join()
    assert store.deps.max_concurrent == 1
    assert store.get(a)["status"] in ("done","failed")
    assert store.get(b)["status"] in ("done","failed")

def test_running_job_with_live_owner_not_abandoned(tmp_path):
    cfg = _cfg(tmp_path)
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    jid = ids.new_id()
    # a job owned by THIS live process
    with open(os.path.join(cfg.jobs_dir, f"{jid}.json"), "w") as f:
        json.dump({"id":jid,"status":"running","owner_pid":os.getpid(),
                   "owner_started": jobs.proc_start(os.getpid())}, f)
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps())
    store.reconcile()
    assert store.get(jid)["status"] == "running"   # live owner -> left alone

def test_running_job_with_dead_owner_abandoned(tmp_path):
    cfg = _cfg(tmp_path); os.makedirs(cfg.jobs_dir, exist_ok=True)
    jid = ids.new_id()
    with open(os.path.join(cfg.jobs_dir, f"{jid}.json"), "w") as f:
        json.dump({"id":jid,"status":"running","owner_pid":999999999,"owner_started":0.0}, f)
    store = jobs.JobStore(cfg, deps=jobs.make_test_deps())
    store.reconcile()
    assert store.get(jid)["status"] == "abandoned"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation** — `ids.py`, then `jobs.py` with a single `queue.Queue` + one worker thread that acquires the injected lease around each job, writes state files, and `reconcile()`/`proc_start()`/`make_test_deps()` helpers. `proc_start(pid)` reads the process create time (via `os.stat` on `/proc` is Linux-only; on Windows use `ctypes`/`OpenProcess` or fall back to "process exists" through `os.kill(pid,0)`-equivalent). For Phase 1, liveness = "a process with this PID exists AND its recorded start time matches"; `make_test_deps()` supplies stubs for `gitstore`/`runner`/`checks`/`resultgate` so the worker pipeline runs without a GPU. (Full code written during implementation; keep each function <50 lines.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_jobs.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/ids.py scripts/delegate/jobs.py scripts/delegate/tests/test_jobs.py
git commit -m "feat(delegate): single-worker job queue with lease, ULID ids, owner-PID liveness"
```

---

### Task 12: Agentic MCP tools + background wait/reap CLI

**Files:**
- Modify: `scripts/delegate/service.py` (register `submit_task`, `result`, `list_jobs`, `record_review`)
- Create: `scripts/delegate/cli.py`
- Create: `scripts/delegate/tests/test_service_tools.py`
- Create: `scripts/delegate/tests/test_cli.py`

**Interfaces:**
- Produces, as MCP tools calling `JobStore`:
  - `submit_task(task, repo, base_ref="HEAD", checks=[], task_type="", allow_web=False, timeout_s=1800) -> {"job_id"}`
  - `result(job_id) -> {status, diff, checks, summary, tokens, duration_s, flagged, gate_reasons}`
  - `list_jobs(status=None) -> [{id,status,task_type,ts}]`
  - `record_review(job_id, verdict, fix_lines=0, cause="") -> {ok}` — appends the A/B ledger row (`delegated=True`, `claude_tokens` left for Claude to fill from the transcript per spec §6) and removes the job's working dir.
- CLI: `python -m scripts.delegate.cli wait <job_id>` blocks until the job is terminal then exits 0 (so Claude runs it as a background Bash command and the harness notifies on completion); `... reap` prunes terminal job dirs older than N days and dead-owner `running` jobs.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_service_tools.py
from scripts.delegate.config import load_config
from scripts.delegate import service, jobs

CFG = load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r"})

def test_submit_returns_job_id_and_list_shows_it(tmp_path):
    store = jobs.JobStore(load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r",
                                       "LOCAL_DELEGATE_JOBS_DIR":str(tmp_path)}), deps=jobs.make_test_deps())
    tools = service.AgenticTools(CFG, store, ledger=jobs.MemLedger())
    jid = tools.submit_task(task="t", repo=".", base_ref="HEAD")["job_id"]
    store.join()
    ids = [r["id"] for r in tools.list_jobs()]
    assert jid in ids

def test_record_review_writes_ab_row(tmp_path):
    store = jobs.JobStore(load_config({"LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r",
                                       "LOCAL_DELEGATE_JOBS_DIR":str(tmp_path)}), deps=jobs.make_test_deps())
    led = jobs.MemLedger()
    tools = service.AgenticTools(CFG, store, ledger=led)
    jid = tools.submit_task(task="t", repo=".", base_ref="HEAD")["job_id"]
    store.join()
    tools.record_review(job_id=jid, verdict="accepted", fix_lines=0, cause="")
    assert led.rows[-1]["verdict"] == "accepted" and led.rows[-1]["delegated"] is True
```

```python
# scripts/delegate/tests/test_cli.py
import subprocess, sys, json, os
from scripts.delegate import ids

def test_wait_exits_when_job_terminal(tmp_path):
    jid = ids.new_id()
    with open(tmp_path/f"{jid}.json","w") as f: json.dump({"id":jid,"status":"done"}, f)
    r = subprocess.run([sys.executable,"-m","scripts.delegate.cli","wait",jid],
                       env={**os.environ,"LOCAL_DELEGATE_JOBS_DIR":str(tmp_path),
                            "LOCAL_DELEGATE_BEARER_TOKEN":"b","LOCAL_DELEGATE_ROUTER_TOKEN":"r"},
                       capture_output=True, timeout=15)
    assert r.returncode == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_service_tools.py scripts/delegate/tests/test_cli.py -v`
Expected: FAIL — `AttributeError`/`ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation** — add an `AgenticTools` class wrapping `JobStore` + ledger with the four methods, register them in `build_server` alongside `ask_local` (each takes/releases nothing itself — the worker owns the lease), and write `cli.py` with `wait` (poll the job file every 2 s until status ∈ terminal set) and `reap`. Add `MemLedger` and `make_test_deps` test helpers if not already in `jobs.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_service_tools.py scripts/delegate/tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/service.py scripts/delegate/cli.py scripts/delegate/tests/test_service_tools.py scripts/delegate/tests/test_cli.py
git commit -m "feat(delegate): agentic submit/result/list/record_review tools and background wait/reap CLI"
```

---

### Task 13: Claude-side guidance (eligibility rule + review skill + logon start)

**Files:**
- Create: `clients/opencode-delegate/CLAUDE-delegation-rule.md` (snippet to paste into the repo `AGENTS.md`/user `CLAUDE.md`)
- Create: `clients/memory-hooks/../` → actually: Create `.claude/skills/delegate-review/SKILL.md`
- Create: `scripts/delegate/README.md` (run/start/stop, env vars, logon Scheduled Task command)
- Create: `scripts/delegate/tests/test_docs_present.py`

**Interfaces:**
- `delegate-review` skill: how Claude writes a brief, reviews a `result` (read the diff, re-run the checks itself, respect the result gate's `flagged` list), records the verdict with `record_review`, and — **the A/B measurement** — records its own token counts for the task from the Claude Code transcript (spec §6). States the eligibility band (ask_local ≥~20K-token inputs; agentic for output-heavy ≥~200-line tasks with checks) and the first-30-tasks coin-flip.

- [ ] **Step 1: Write the failing test**

```python
# scripts/delegate/tests/test_docs_present.py
import os, re
def test_review_skill_has_required_sections():
    txt = open(".claude/skills/delegate-review/SKILL.md", encoding="utf-8").read()
    for needle in ("record_review", "coin", "result gate", "20K", "200-line"):
        assert needle.lower() in txt.lower(), needle
def test_readme_documents_env_and_start():
    txt = open("scripts/delegate/README.md", encoding="utf-8").read()
    for needle in ("LOCAL_DELEGATE_BEARER_TOKEN","LOCAL_DELEGATE_ROUTER_TOKEN","schtasks"):
        assert needle in txt, needle
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest scripts/delegate/tests/test_docs_present.py -v`
Expected: FAIL — `FileNotFoundError`

- [ ] **Step 3: Write the docs** — the skill, the CLAUDE rule snippet, and the README (env vars, `python -m scripts.delegate.service`, and a `schtasks /Create /SC ONLOGON` line to start it at logon bound to `127.0.0.1`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest scripts/delegate/tests/test_docs_present.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/delegate-review/ clients/opencode-delegate/CLAUDE-delegation-rule.md scripts/delegate/README.md scripts/delegate/tests/test_docs_present.py
git commit -m "docs(delegate): eligibility rule, review skill, run/start README"
```

---

### Task 14: End-to-end integration + mutation checks

**Files:**
- Create: `scripts/delegate/tests/test_e2e_stub.py`
- Create: `scripts/delegate/tests/test_live.py` (marked `-m live`, opt-in)

**Interfaces:** none new — exercises the whole pipeline with the stand-in opencode and a real temp git repo.

- [ ] **Step 1: Write the E2E test** (stand-in opencode edits a file; full submit→export→run→commit→checks→gate→result→review)

```python
# scripts/delegate/tests/test_e2e_stub.py
# Build a real temp git repo under an allowed root, submit a job whose runner is the
# stub that writes b.txt, assert result() returns a non-empty diff containing b.txt,
# the gate passes, checks ([sys.executable,"-c","print(1)"]) report exit 0, and
# record_review writes an accepted A/B row. (Full code written at implementation.)
```

- [ ] **Step 2: Run to verify it fails, then implement the wiring until it passes**

Run: `python -m pytest scripts/delegate/tests/test_e2e_stub.py -v`

- [ ] **Step 3: Mutation checks (manual, matched-Edit only — never `git checkout`)**

Per repo lesson: break each guard with a precise Edit, confirm the owning test fails, revert.
- Lease: make `acquire` always return True → `test_single_worker_never_runs_two_at_once` fails.
- Liveness: make `reconcile` ignore owner liveness → `test_running_job_with_live_owner_not_abandoned` fails.
- Result gate: make `inspect_diff` always return `rejected=False` → the symlink/mode/`.github` tests fail.
Record each as done; revert with a matched Edit.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest scripts/delegate/tests -q`
Expected: all green (live tests deselected by default).

- [ ] **Step 5: Commit**

```bash
git add scripts/delegate/tests/test_e2e_stub.py scripts/delegate/tests/test_live.py
git commit -m "test(delegate): end-to-end stub pipeline and opt-in live checks"
```

---

## Self-Review

**Spec coverage:** persistent service + lease (T3, T5, T11) ✓; `ask_local` (T4, T5) ✓; agentic submit/result/list/record_review (T11, T12) ✓; isolated git store incl. WORKTREE/untracked/clean-tree (T6) ✓; neutered capabilities + stripped attacker config + isolated opencode config (T7) ✓; runner detached-stdin/tree-kill/event-parse (T8) ✓; scrubbed-env argv checks (T9) ✓; result gate (T10) ✓; A/B ledger + eligibility + coin-flip (T2, T13) ✓; background wait/reap (T12) ✓; escape/mutation tests (T10, T14) ✓; redteam track — out of scope (spec §10), not planned here ✓. **Deferred by spec (not planned):** learning loop, OS sandbox (Phase 2), vault lessons — correctly absent.

**Open items the spec flagged, carried as in-task verification (not placeholders):** the opencode config-isolation env var (T7 Step 5, verify-by-running); `.mcp.json` bearer-header support (T5 Step 5). Both have a concrete fallback written in-task.

**Known plan-level engineering decision for the reviewer to accept or reject:** process-tree kill uses `taskkill /T /F` (stdlib) rather than a Windows Job Object. Rationale: Phase 1 is a measurement; `taskkill /T` + the startup reaper cover orphans; Job Object (stronger kill-on-crash) is noted for Phase 2. Reject this if you want the Job Object now.

**Placeholder scan:** Tasks 11 and 14 leave full bodies to implementation but pin every interface, test, and assertion; all other tasks carry complete code. No "TBD"/"add error handling"/"similar to Task N".
