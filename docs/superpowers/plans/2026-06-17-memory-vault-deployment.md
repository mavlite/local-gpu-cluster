# Memory Vault Shared Memory Service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Memory Vault (`ghcr.io/mihaibuilds/memory-vault:1.0.1` + pgvector) as a shared persistent-memory service on the cluster, fronted by a systemd MCP-over-SSE bridge, consumed by OpenCode and Claude Code, with the router extended to let local Claude Code run against the llama.cpp backend.

**Architecture:** New LXC 156 (`memory-vault`, `192.168.6.156`) runs the Memory Vault Docker stack and a Python MCP-SSE bridge (port 3005) in the same container — exactly how LXC 155 runs Docker MCPs alongside the `mcp-sdg` Python service. The bridge translates the cluster's remote-SSE MCP model onto Memory Vault's REST API, scoping each connection to a per-project memory space via a `?space=` query param. Separately, the router (LXC 153) gains a gated Anthropic `/v1/messages` passthrough.

**Tech Stack:** Proxmox LXC (Ubuntu 24.04), Docker Compose, PostgreSQL 16 + pgvector, Python 3.11 (`mcp>=1.2`, `httpx`, `starlette`), FastAPI (router), systemd.

**Execution environment:** All `pct`/`zfs`/host commands run **as root on the Proxmox host**. The repo lives on a workstation; scripts are pushed to the host or run from a checkout on the host. Idempotency follows the existing `scripts/lib/common.sh` conventions.

**Conventions reused (already in this repo):**
- LXC create shape, Docker CE install heredoc, bind-mount + double-chown for unprivileged containers — see `scripts/54-lxc-anythingllm.sh`.
- `pct_set_if_changed`, `ensure_lxc_started`, `lxc_get_ip`, logging helpers — see `scripts/lib/common.sh`.
- Python venv + `mcp` SDK + systemd SSE service — see `scripts/58-mcp-sdg.sh` and `scripts/files/mcp-sdg-server.py`.
- Router is FastAPI on `:8000`, `chat_sem` admission, `sse_stream_with_keepalive`, `_error_body` — see `scripts/files/router-app.py`.

**Recon facts (confirmed from upstream):**
- App image `ghcr.io/mihaibuilds/memory-vault:1.0.1`, port 8000; db `pgvector/pgvector:pg16`, port 5432, PGDATA at `/var/lib/postgresql/data` (postgres runs as uid 999).
- App env: `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`, `API_HOST`, `API_PORT`, `API_AUTH_ENABLED`, `API_CORS_ORIGINS`, `API_RATE_LIMIT_PER_MIN`, `LOG_LEVEL`.
- REST endpoints (prefix `/api`): `/api/search`, `/api/ingest/text`, `/api/spaces`, `/api/chat`. Bearer auth. Token CLI: `memory-vault token create <name>`.
- **Exact REST request/response field names are pinned in Task 2** by capturing `/openapi.json` from the running service; the bridge (Task 3) centralizes all REST specifics in one constants block so any field-name correction is a one-line change verified against ground truth.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `scripts/config.env.example` (modify) | Document new `MEMVAULT_*` config knobs |
| `scripts/61-lxc-memory-vault.sh` (create) | Provision LXC 156: container, ZFS dataset + bind mount, Docker, compose stack |
| `scripts/files/memory-vault-compose.override.yml` (create) | Bind-mount PGDATA to `tank`, set generated DB password, unpublish 5432 |
| `scripts/62-memory-vault-bridge.sh` (create) | Deploy the MCP-SSE bridge (venv, push, env, systemd) |
| `scripts/files/memory-vault-bridge.py` (create) | The MCP-over-SSE bridge → Memory Vault REST |
| `scripts/files/router-app.py` (modify) | Add gated `/v1/messages` + `/v1/messages/count_tokens` passthrough |
| `scripts/60-verify.sh` (modify) | Add memory-vault smoke checks |
| `scripts/files/memory-vault-backup.sh` (create) | `pg_dump` → `/tank/backups` (run by systemd timer) |
| `docs/memory-vault-clients.md` (create) | OpenCode + Claude Code wiring, local-LLM `.env` |

---

## Task 1: config.env knobs

**Files:**
- Modify: `scripts/config.env.example`

- [ ] **Step 1: Add the MEMVAULT block**

Insert after the `MCP_*` lines (around line 25/34 of the `# ---------- LXC sizing ----------` / VMID sections). Add to the VMID section, hostname section, sizing section, and networking section, plus a new dedicated block:

```bash
# ---------- Memory Vault (61-lxc-memory-vault.sh, 62-memory-vault-bridge.sh) ----------
# MEMVAULT_VMID=156
# MEMVAULT_HOSTNAME=memory-vault
# MEMVAULT_IP=192.168.6.156           # static IP (reserve in DHCP or set explicit)
# MEMVAULT_CORES=4
# MEMVAULT_MEMORY=8192                 # Postgres HNSW builds + sentence-transformers + spaCy (CPU)
# MEMVAULT_ROOTFS_SIZE=32
# MEMVAULT_STORAGE_MOUNT=/tank/memory-vault   # ZFS dataset, bind-mounted at /opt/memory-vault-data
# MEMVAULT_IMAGE=ghcr.io/mihaibuilds/memory-vault:1.0.1
# MEMVAULT_API_PORT=8000               # dashboard + REST API (LAN-only)
# MEMVAULT_BRIDGE_PORT=3005            # MCP-over-SSE bridge
# MEMVAULT_DEFAULT_SPACE=default       # used when an SSE connection omits ?space=
# MEMVAULT_DB_PASSWORD generated at provision time -> /opt/memory-vault/.env (mode 600), not read from here.
# MEMVAULT_API_TOKEN minted at provision time -> /etc/memory-vault-bridge.env (mode 600).
```

- [ ] **Step 2: Verify the file still parses**

Run: `bash -n scripts/config.env.example`
Expected: no output, exit 0. (It is comments-only, but confirm no stray syntax.)

- [ ] **Step 3: Commit**

```bash
git add scripts/config.env.example
git commit -m "feat(memvault): document MEMVAULT_* config knobs"
```

---

## Task 2: Provisioning script — LXC 156 + Docker stack

**Files:**
- Create: `scripts/61-lxc-memory-vault.sh`
- Create: `scripts/files/memory-vault-compose.override.yml`

- [ ] **Step 1: Write the compose override**

Create `scripts/files/memory-vault-compose.override.yml`:

```yaml
# Overrides for the upstream memory-vault docker-compose.yml.
# - Bind PGDATA onto the tank ZFS dataset (snapshot + backup coverage).
# - Replace the default DB password with a generated one (${MEMVAULT_DB_PASSWORD}
#   is substituted by docker compose from /opt/memory-vault/.env).
# - Do NOT publish Postgres on the host network; only the app container needs it
#   over the compose network (lists in override REPLACE the base list).
services:
  db:
    environment:
      POSTGRES_PASSWORD: ${MEMVAULT_DB_PASSWORD}
    ports: []
    volumes:
      - /opt/memory-vault-data/pgdata:/var/lib/postgresql/data
  app:
    environment:
      DB_PASSWORD: ${MEMVAULT_DB_PASSWORD}
      API_AUTH_ENABLED: "true"
      API_CORS_ORIGINS: "*"
    restart: unless-stopped
```

- [ ] **Step 2: Write the provisioning script**

Create `scripts/61-lxc-memory-vault.sh`:

```bash
#!/usr/bin/env bash
# 61-lxc-memory-vault.sh — provision LXC 156 (memory-vault).
#
#   - Unprivileged LXC with nesting=1,keyctl=1 for Docker
#   - ZFS dataset tank/memory-vault, bind-mounted at /opt/memory-vault-data
#   - Install Docker CE (DEB822 sources, docker.asc keyring)
#   - Clone memory-vault, generate DB password, deploy via docker compose
#     with scripts/files/memory-vault-compose.override.yml
#
# The MCP-over-SSE bridge is deployed separately by 62-memory-vault-bridge.sh.
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

MV_VMID="${MEMVAULT_VMID:-156}"
MV_HOSTNAME="${MEMVAULT_HOSTNAME:-memory-vault}"
MV_CORES="${MEMVAULT_CORES:-4}"
MV_MEMORY="${MEMVAULT_MEMORY:-8192}"
MV_ROOTFS_SIZE="${MEMVAULT_ROOTFS_SIZE:-32}"
MV_STORAGE_MOUNT="${MEMVAULT_STORAGE_MOUNT:-/tank/memory-vault}"
MV_IMAGE="${MEMVAULT_IMAGE:-ghcr.io/mihaibuilds/memory-vault:1.0.1}"
LXC_STORAGE="${LXC_STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"
MV_REPO_URL="${MEMVAULT_REPO_URL:-https://github.com/MihaiBuilds/memory-vault.git}"
OVERRIDE_SRC="$LGC_DIR/files/memory-vault-compose.override.yml"

[[ -r "$OVERRIDE_SRC" ]] || die "Missing compose override: $OVERRIDE_SRC"

phase_create() {
  step "Create LXC $MV_VMID ($MV_HOSTNAME)"
  if lxc_exists "$MV_VMID"; then
    skip "LXC $MV_VMID already exists."
    return 0
  fi
  pct create "$MV_VMID" "$LXC_TEMPLATE_STORAGE:vztmpl/$LXC_TEMPLATE_NAME" \
    --hostname "$MV_HOSTNAME" \
    --cores "$MV_CORES" \
    --memory "$MV_MEMORY" \
    --swap 2048 \
    --rootfs "${LXC_STORAGE}:${MV_ROOTFS_SIZE}" \
    --net0 "name=eth0,bridge=${BRIDGE},firewall=0,ip=dhcp,type=veth" \
    --features "nesting=1,keyctl=1" \
    --unprivileged 1 \
    --ostype ubuntu \
    --onboot 1 \
    --startup order=5 \
    --start 0
}

phase_bind_mount() {
  step "ZFS dataset + bind-mount $MV_STORAGE_MOUNT"
  if ! zfs list "tank/memory-vault" >/dev/null 2>&1; then
    zfs create "tank/memory-vault"
  else
    skip "Dataset tank/memory-vault exists."
  fi
  mkdir -p "$MV_STORAGE_MOUNT"
  # Unprivileged LXC: container-root maps to host UID 100000.
  if [[ "$(stat -c '%u:%g' "$MV_STORAGE_MOUNT")" != "100000:100000" ]]; then
    chown -R 100000:100000 "$MV_STORAGE_MOUNT"
  fi
  pct_set_if_changed "$MV_VMID" mp0 "$MV_STORAGE_MOUNT,mp=/opt/memory-vault-data"
  ensure_lxc_started "$MV_VMID"
}

phase_docker() {
  step "Install Docker CE in LXC $MV_VMID"
  if pct exec "$MV_VMID" -- bash -c 'command -v docker >/dev/null 2>&1'; then
    skip "Docker already installed."
    return 0
  fi
  pct exec "$MV_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt update
    apt install -y ca-certificates curl gnupg lsb-release git
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt update
    apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
GUEST
}

phase_deploy() {
  step "Clone + deploy Memory Vault stack"
  # Push the override file into the LXC.
  pct push "$MV_VMID" "$OVERRIDE_SRC" /opt/memory-vault-override.yml --perms 0644

  pct exec "$MV_VMID" -- env "MV_REPO_URL=$MV_REPO_URL" "MV_IMAGE=$MV_IMAGE" bash -se <<'GUEST'
    set -Eeuo pipefail
    # Prepare bind-mounted PGDATA dir, owned by the postgres uid (999) inside the
    # pgvector/postgres image so initdb can write it.
    mkdir -p /opt/memory-vault-data/pgdata
    chown -R 999:999 /opt/memory-vault-data/pgdata

    if [[ ! -d /opt/memory-vault/.git ]]; then
      git clone --depth 1 "$MV_REPO_URL" /opt/memory-vault
    fi
    cd /opt/memory-vault
    install -m 0644 /opt/memory-vault-override.yml docker-compose.override.yml

    # Generate a strong DB password once; persist in .env for compose substitution.
    if [[ ! -f /opt/memory-vault/.env ]]; then
      umask 077
      printf 'MEMVAULT_DB_PASSWORD=%s\n' "$(openssl rand -hex 24)" > /opt/memory-vault/.env
    fi
    chmod 600 /opt/memory-vault/.env

    # Pin the app image tag from config (override base if it floats).
    docker compose pull
    docker compose up -d
GUEST
}

main() {
  phase_create
  phase_bind_mount
  phase_docker
  phase_deploy
  step "LXC 156 provisioned."
  local ip; ip="$(lxc_get_ip "$MV_VMID" || true)"
  ok "Memory Vault stack at: http://${ip:-<lxc-156-ip>}:${MEMVAULT_API_PORT:-8000}"
  echo "  Next: scripts/62-memory-vault-bridge.sh (mint token + deploy MCP bridge)"
}

main "$@"
```

- [ ] **Step 3: Lint the script**

Run: `bash -n scripts/61-lxc-memory-vault.sh && shellcheck scripts/61-lxc-memory-vault.sh || true`
Expected: `bash -n` exits 0. Address any shellcheck errors (warnings about `pct`/sourcing are acceptable, matching existing scripts).

- [ ] **Step 4: Run it on the Proxmox host**

Run (on host, as root): `LGC_DIR=scripts bash scripts/61-lxc-memory-vault.sh`
Expected: phases run; final line prints the stack URL. Re-running is a no-op (idempotent skips).

- [ ] **Step 5: Verify the stack is healthy**

Run: `pct exec 156 -- bash -lc 'cd /opt/memory-vault && docker compose ps'`
Expected: both `db` and `app` services `Up` (db `healthy`). If `app` is restarting, check `docker compose logs app` — the most common first-run cause is PGDATA ownership; confirm `/opt/memory-vault-data/pgdata` is owned by `999:999` inside the LXC.

- [ ] **Step 6: Confirm the dashboard answers on the LAN**

Run (from host): `curl -s -o /dev/null -w '%{http_code}\n' http://$(pct exec 156 -- hostname -I | awk '{print $1}'):8000/`
Expected: `200` (or `401`/`403` if the root requires auth — any non-000 response proves it is listening).

- [ ] **Step 7: Commit**

```bash
git add scripts/61-lxc-memory-vault.sh scripts/files/memory-vault-compose.override.yml
git commit -m "feat(memvault): provision LXC 156 with the Memory Vault docker stack"
```

---

## Task 3: Mint API token + pin the REST contract

**Files:**
- None created here; produces `/etc/memory-vault-bridge.env` inside LXC 156 and a captured `openapi.json` checked into `docs/`.

- [ ] **Step 1: Mint a bearer token for the bridge**

Run:
```bash
pct exec 156 -- bash -lc 'cd /opt/memory-vault && docker compose exec -T app memory-vault token create cluster-bridge'
```
Expected: prints a token string (e.g. `mv_...`). Copy it. If the command name differs, discover it: `docker compose exec -T app memory-vault --help`.

- [ ] **Step 2: Write the bridge env file (mode 600)**

Run (substitute `<TOKEN>` from Step 1):
```bash
pct exec 156 -- bash -lc 'umask 077; cat > /etc/memory-vault-bridge.env <<EOF
MEMVAULT_API_URL=http://127.0.0.1:8000
MEMVAULT_API_TOKEN=<TOKEN>
MEMVAULT_DEFAULT_SPACE=default
MCP_HOST=0.0.0.0
MCP_PORT=3005
EOF
chmod 600 /etc/memory-vault-bridge.env; echo wrote'
```
Expected: prints `wrote`.

- [ ] **Step 3: Capture the OpenAPI schema to pin endpoint/field names**

Run:
```bash
pct exec 156 -- bash -lc 'curl -s -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/openapi.json' > /tmp/memory-vault-openapi.json
python -c "import json,sys; d=json.load(open('/tmp/memory-vault-openapi.json')); print('\n'.join(sorted(d.get('paths',{}).keys())))"
```
Expected: a list of paths including `/api/search`, `/api/ingest/text`, `/api/spaces`. **Record the exact request body field names** for `/api/ingest/text` (text + space) and `/api/search` (query + space + limit/top_k), and the response shape for `/api/search` (the array key and per-item content/score fields). These confirm or correct the constants in Task 4 Step 1.

- [ ] **Step 4: Confirm an authenticated REST call works**

Run:
```bash
pct exec 156 -- bash -lc 'curl -s -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/api/spaces'
```
Expected: a JSON listing of spaces (possibly empty). A `401` means the token/header is wrong — revisit Step 1/2.

- [ ] **Step 5: Commit the captured schema (reference artifact)**

```bash
cp /tmp/memory-vault-openapi.json docs/memory-vault-openapi.json   # run on a host with the repo checkout
git add docs/memory-vault-openapi.json
git commit -m "docs(memvault): capture Memory Vault v1.0.1 OpenAPI schema (REST contract reference)"
```

> If the captured field names differ from Task 4's `REMEMBER_FIELD_TEXT`, `RECALL_FIELD_QUERY`, etc., update those constants in `memory-vault-bridge.py` before deploying. This is the single point of correction.

---

## Task 4: The MCP-over-SSE bridge

**Files:**
- Create: `scripts/files/memory-vault-bridge.py`

The bridge wraps the MCP SDK's low-level `Server` + `SseServerTransport` behind a Starlette app so it can read `?space=` from each SSE connection and stash it in a `ContextVar`. Tools also accept an explicit `space` arg that overrides the connection default (belt-and-suspenders if the per-connection value is unavailable). All REST specifics live in one constants block (top of file) confirmed against Task 3 Step 3.

- [ ] **Step 1: Write the bridge**

Create `scripts/files/memory-vault-bridge.py`:

```python
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

RECALL_PATH = "/api/search"
RECALL_FIELD_QUERY = "query"
RECALL_FIELD_SPACE = "space"
RECALL_FIELD_LIMIT = "limit"
RECALL_RESULTS_KEY = "results"          # array of hits in the /api/search response
RECALL_ITEM_TEXT_KEYS = ("content", "text", "chunk")
RECALL_ITEM_SCORE_KEYS = ("score", "rrf_score", "similarity")

FORGET_PATH = "/api/chunks/{id}"        # DELETE; confirm in openapi.json
STATUS_PATH = "/api/stats"             # confirm; falls back to /api/spaces

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
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
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
                RECALL_FIELD_SPACE: _space(arguments.get("space")),
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
                tag = f" (score {score:.3f})" if isinstance(score, (int, float)) else ""
                lines.append(f"{i}.{tag} {text}")
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


_sse = SseServerTransport("/messages/")


async def handle_sse(request):
    # Capture the per-connection memory space from ?space=.
    _space_var.set(request.query_params.get("space") or DEFAULT_SPACE)
    async with _sse.connect_sse(request.scope, request.receive, request._send) as (read, write):
        await server.run(read, write, server.create_initialization_options())


app = Starlette(routes=[
    Route("/sse", endpoint=handle_sse),
    Mount("/messages/", app=_sse.handle_post_message),
])


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "0.0.0.0"),
                port=int(os.environ.get("MCP_PORT", "3005")))
```

- [ ] **Step 2: Syntax-check locally**

Run: `python -m py_compile scripts/files/memory-vault-bridge.py`
Expected: exit 0, no output.

- [ ] **Step 3: Commit**

```bash
git add scripts/files/memory-vault-bridge.py
git commit -m "feat(memvault): MCP-over-SSE bridge to the Memory Vault REST API"
```

---

## Task 5: Deploy the bridge as a systemd service

**Files:**
- Create: `scripts/62-memory-vault-bridge.sh`

- [ ] **Step 1: Write the deploy script** (mirrors `scripts/58-mcp-sdg.sh`)

Create `scripts/62-memory-vault-bridge.sh`:

```bash
#!/usr/bin/env bash
# 62-memory-vault-bridge.sh — deploy the MCP-over-SSE bridge in LXC 156.
#
# Prereqs: scripts/61-lxc-memory-vault.sh created LXC 156 and the stack is up,
# and /etc/memory-vault-bridge.env exists (Task 3 Step 2).
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

MV_VMID="${MEMVAULT_VMID:-156}"
SERVER_SRC="$LGC_DIR/files/memory-vault-bridge.py"

[[ -r "$SERVER_SRC" ]] || die "Missing bridge source: $SERVER_SRC"
pct status "$MV_VMID" >/dev/null 2>&1 || die "LXC $MV_VMID missing. Run 61-lxc-memory-vault.sh first."
ensure_lxc_started "$MV_VMID"
pct exec "$MV_VMID" -- test -f /etc/memory-vault-bridge.env \
  || die "/etc/memory-vault-bridge.env missing in LXC $MV_VMID (see Task 3 Step 2)."

phase_install_python() {
  step "Install Python venv + mcp SDK + httpx + uvicorn + starlette"
  pct exec "$MV_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    if ! python3 -c 'import ensurepip' 2>/dev/null; then
      apt update && apt install -y python3 python3-venv python3-pip
    fi
    mkdir -p /opt/memory-vault-bridge
    [[ -x /opt/memory-vault-bridge/venv/bin/python ]] || python3 -m venv /opt/memory-vault-bridge/venv
    /opt/memory-vault-bridge/venv/bin/pip install --quiet --upgrade pip wheel
    /opt/memory-vault-bridge/venv/bin/pip install --quiet 'mcp>=1.2' httpx uvicorn starlette
GUEST
}

phase_deploy() {
  step "Push bridge server.py"
  pct push "$MV_VMID" "$SERVER_SRC" /opt/memory-vault-bridge/server.py --perms 0644
}

phase_systemd() {
  step "Install + enable memory-vault-bridge.service"
  pct exec "$MV_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/memory-vault-bridge.service <<'EOF'
[Unit]
Description=MCP-over-SSE bridge to Memory Vault REST (port 3005)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/memory-vault-bridge.env
WorkingDirectory=/opt/memory-vault-bridge
ExecStart=/opt/memory-vault-bridge/venv/bin/python /opt/memory-vault-bridge/server.py
Restart=on-failure
RestartSec=10
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable memory-vault-bridge.service
    systemctl restart memory-vault-bridge.service
    sleep 2
    systemctl is-active memory-vault-bridge.service
GUEST
}

main() {
  phase_install_python
  phase_deploy
  phase_systemd
  step "Bridge deployed."
  local ip; ip="$(lxc_get_ip "$MV_VMID" || true)"
  ok "SSE endpoint: http://${ip:-<lxc-156-ip>}:${MEMVAULT_BRIDGE_PORT:-3005}/sse?space=<repo-slug>"
}

main "$@"
```

- [ ] **Step 2: Lint + run**

Run: `bash -n scripts/62-memory-vault-bridge.sh`
Then on the host: `LGC_DIR=scripts bash scripts/62-memory-vault-bridge.sh`
Expected: service ends `active`.

- [ ] **Step 3: Verify the SSE handshake**

Run (from host): `curl -sN -m 5 http://$(pct exec 156 -- hostname -I | awk '{print $1}'):3005/sse | head -3`
Expected: an `event: endpoint` line (the MCP SSE handshake), then it blocks (Ctrl-C / timeout is fine).

- [ ] **Step 4: Round-trip test (remember → recall) through the bridge**

Use a one-shot MCP client to exercise the tools end-to-end. Run on the host (in the repo's Python venv with `mcp` installed):
```bash
LXC_IP=$(pct exec 156 -- hostname -I | awk '{print $1}')
python - <<PY
import anyio
from mcp.client.sse import sse_client
from mcp import ClientSession

async def main():
    url = f"http://$LXC_IP:3005/sse?space=plan-smoketest"
    async with sse_client(url) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await s.call_tool("remember", {"text": "The smoke-test canary phrase is BLUE-HERON-42."})
            res = await s.call_tool("recall", {"query": "what is the canary phrase?", "top_n": 3})
            print(res.content[0].text)

anyio.run(main)
PY
```
Expected: the recall output contains `BLUE-HERON-42`. If `recall` returns "No memories found", confirm the `/api/search` field names against `docs/memory-vault-openapi.json` and correct the constants in `memory-vault-bridge.py` (Task 4), then `systemctl restart memory-vault-bridge` and re-run.

- [ ] **Step 5: Commit**

```bash
git add scripts/62-memory-vault-bridge.sh
git commit -m "feat(memvault): deploy MCP-SSE bridge as a systemd service in LXC 156"
```

---

## Task 6: Router `/v1/messages` passthrough

**Files:**
- Modify: `scripts/files/router-app.py`

Adds a raw (non-mutating) SSE passthrough helper and two gated routes, so local Claude Code reaches llama-server through the existing `chat_sem` admission control. We must NOT run the `<think>`/`[CONTEXT]` regex over Anthropic SSE (it would corrupt the event JSON), hence a dedicated passthrough streamer.

- [ ] **Step 1: Add the raw passthrough streamer**

Insert immediately after `sse_stream_with_keepalive` (after line ~655, before `# ---------- Tool registry`):

```python
async def sse_passthrough_with_keepalive(upstream_url: str, payload: dict):
    """Stream an upstream SSE response verbatim with proactive keepalives.

    Identical keepalive/abort machinery to sse_stream_with_keepalive, but yields
    upstream chunks UNMODIFIED — used for the Anthropic /v1/messages passthrough,
    where the <think>/[CONTEXT] regex rewrites would corrupt the event-stream JSON.
    """
    stream_started = time.monotonic()
    yield b": ping\n\n"
    queue: asyncio.Queue = asyncio.Queue()

    async def upstream_reader():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", upstream_url, json=payload,
                                         headers=upstream_headers()) as r:
                    if r.status_code >= 500:
                        await queue.put(("degraded", None))
                        return
                    async for chunk in r.aiter_text():
                        await queue.put(("data", chunk))
                    await queue.put(("eof", None))
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException,
                httpx.RemoteProtocolError):
            await queue.put(("degraded", None))
        except Exception:
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
                    queue.get(), timeout=KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            if msg_type == "degraded":
                yield DEGRADED_FRAME
                yield DONE_FRAME
                return
            if msg_type == "eof":
                break
            yield payload_chunk.encode()
    finally:
        task.cancel()


def _approx_text_from_anthropic(body: dict) -> str:
    """Best-effort text for token-budget admission on an Anthropic Messages body.
    Reuses _approx_text_from_messages for messages and prepends the top-level
    `system` field (string or list of {type:text,text})."""
    parts = []
    system = body.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        for blk in system:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text", ""))
    parts.append(_approx_text_from_messages(body.get("messages", [])))
    return "\n".join(parts)
```

- [ ] **Step 2: Add the routes**

Insert after the `completions` handler (after line ~1764, before `# Qwen3 Embedding requires`):

```python
@app.post("/v1/messages")
@limiter.limit(RATE_LIMIT_CHAT)
async def anthropic_messages(request: Request):
    """Anthropic Messages API passthrough for local Claude Code.

    llama-server speaks /v1/messages natively (llama.cpp PR #17570); the router
    only adds bearer auth (middleware), chat_sem admission, token-budget guard,
    and SSE keepalive — it does NOT translate or mutate the body/stream.
    """
    global _last_chat_ts
    _last_chat_ts = time.monotonic()
    body = await request.json()
    stream = bool(body.get("stream", False))
    url = f"{V620_URL}/v1/messages"
    started = time.monotonic()
    client_ip = request.client.host if request.client else "?"
    model = body.get("model", "?")

    # Rewrite a known client alias to its backend (parity with /v1/chat/completions);
    # llama-server serves the loaded model regardless, so this is best-effort.
    alias_info = resolve_alias(model)
    if alias_info["backend"] != model:
        body["model"] = alias_info["backend"]

    text = _approx_text_from_anthropic(body)
    async with httpx.AsyncClient() as client:
        token_count = await count_tokens(client, V620_URL, text)
    if token_count != -1 and token_count > MAX_CHAT_INPUT_TOKENS:
        log_access("/v1/messages", model, token_count, 0,
                   int((time.monotonic() - started) * 1000), 413, client_ip,
                   error="exceeds_max_chat_input_tokens")
        raise HTTPException(
            status_code=413,
            detail=f"input is {token_count} tokens, exceeds MAX_CHAT_INPUT_TOKENS={MAX_CHAT_INPUT_TOKENS}",
        )

    async with chat_sem:
        if stream:
            log_access("/v1/messages", model, token_count, -1,
                       int((time.monotonic() - started) * 1000), 200, client_ip,
                       error="stream-started")
            return StreamingResponse(
                sse_passthrough_with_keepalive(url, body),
                media_type="text/event-stream",
            )
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as c:
            try:
                r = await c.post(url, json=body, headers=upstream_headers())
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                log_access("/v1/messages", model, token_count, 0,
                           int((time.monotonic() - started) * 1000), 502, client_ip,
                           error=type(e).__name__)
                return JSONResponse(
                    _error_body("service_degraded", f"upstream: {type(e).__name__}"),
                    status_code=502,
                )
            log_access("/v1/messages", model, token_count, 0,
                       int((time.monotonic() - started) * 1000), r.status_code, client_ip)
            return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/messages/count_tokens")
@limiter.limit(RATE_LIMIT_CHAT)
async def anthropic_count_tokens(request: Request):
    """Passthrough for Claude Code's token-counting preflight."""
    body = await request.json()
    async with httpx.AsyncClient(timeout=SMALL_TIMEOUT) as c:
        try:
            r = await c.post(f"{V620_URL}/v1/messages/count_tokens",
                             json=body, headers=upstream_headers())
            return JSONResponse(r.json(), status_code=r.status_code)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
            return JSONResponse(
                _error_body("service_degraded", f"upstream: {type(e).__name__}"),
                status_code=502,
            )
```

- [ ] **Step 3: Syntax-check**

Run: `python -m py_compile scripts/files/router-app.py`
Expected: exit 0.

- [ ] **Step 4: Redeploy the router**

Run on the host: `LGC_DIR=scripts bash scripts/53-lxc-router.sh`
(Re-running pushes the updated `app.py` and restarts the unit; the script is idempotent.)
Expected: router unit ends active; `curl -s http://192.168.6.153:8000/healthz` returns healthy JSON.

- [ ] **Step 5: Verify a non-streaming Anthropic completion through the router**

Run (substitute the router key from `/etc/router.env` on LXC 153 and the loaded model alias):
```bash
curl -s http://192.168.6.153:8000/v1/messages \
  -H "Authorization: Bearer <ROUTER_API_KEY>" \
  -H "content-type: application/json" \
  -d '{"model":"rag-qwen3.6","max_tokens":64,"messages":[{"role":"user","content":"Reply with the single word: pong"}]}'
```
Expected: a JSON Anthropic-shaped response whose content contains `pong`. A 502 means the loaded llama-server build lacks `/v1/messages` — confirm the chat unit build (≥ b9584 includes it).

- [ ] **Step 6: Commit**

```bash
git add scripts/files/router-app.py
git commit -m "feat(router): gated Anthropic /v1/messages passthrough for local Claude Code"
```

---

## Task 7: Nightly Postgres backup

**Files:**
- Create: `scripts/files/memory-vault-backup.sh`

- [ ] **Step 1: Write the backup script**

Create `scripts/files/memory-vault-backup.sh`:

```bash
#!/usr/bin/env bash
# memory-vault-backup.sh — pg_dump the Memory Vault DB to /tank/backups.
# Installed in LXC 156 and run by memory-vault-backup.timer (daily).
set -Eeuo pipefail
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/opt/memory-vault-data/backups"          # on the tank bind mount -> snapshotted
mkdir -p "$OUT"
cd /opt/memory-vault
docker compose exec -T db pg_dump -U memory_vault memory_vault \
  | gzip > "$OUT/memory_vault-${STAMP}.sql.gz"
# Retain 14 most recent dumps.
ls -1t "$OUT"/memory_vault-*.sql.gz | tail -n +15 | xargs -r rm -f
echo "backup written: $OUT/memory_vault-${STAMP}.sql.gz"
```

> The dump lands under the `tank/memory-vault` bind mount, which is the dataset ZFS already snapshots; this gives logical (pg_dump) + block (snapshot) coverage without a second `tank/backups` mount in this LXC.

- [ ] **Step 2: Install script + timer (extend `62-memory-vault-bridge.sh` or run inline)**

Run on the host:
```bash
pct push 156 scripts/files/memory-vault-backup.sh /opt/memory-vault-bridge/backup.sh --perms 0755
pct exec 156 -- bash -se <<'GUEST'
set -Eeuo pipefail
cat > /etc/systemd/system/memory-vault-backup.service <<'EOF'
[Unit]
Description=Memory Vault nightly pg_dump
After=docker.service
[Service]
Type=oneshot
ExecStart=/opt/memory-vault-bridge/backup.sh
EOF
cat > /etc/systemd/system/memory-vault-backup.timer <<'EOF'
[Unit]
Description=Run Memory Vault backup daily at 03:30
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now memory-vault-backup.timer
GUEST
```

- [ ] **Step 3: Verify a manual backup run**

Run: `pct exec 156 -- /opt/memory-vault-bridge/backup.sh`
Expected: prints `backup written: ...` and `ls /opt/memory-vault-data/backups` shows a `.sql.gz` file.

- [ ] **Step 4: Commit**

```bash
git add scripts/files/memory-vault-backup.sh
git commit -m "feat(memvault): nightly pg_dump backup script + systemd timer"
```

---

## Task 8: Client configuration + docs

**Files:**
- Create: `docs/memory-vault-clients.md`

- [ ] **Step 1: Write the client doc**

Create `docs/memory-vault-clients.md`:

````markdown
# Memory Vault — client wiring

Shared persistent-memory service on LXC 156 (`192.168.6.156`). The MCP-over-SSE
bridge is at `http://192.168.6.156:3005/sse`. Pin a **memory space per repo** via
`?space=<repo-slug>` so OpenCode and Claude Code working the same repo share memory.

## OpenCode (`opencode.json`)

```jsonc
{
  "mcp": {
    "memory": {
      "type": "remote",
      "url": "http://192.168.6.156:3005/sse?space=local-gpu-cluster"
    }
  }
}
```

## Claude Code — memory MCP (`.mcp.json`)

```json
{
  "mcpServers": {
    "memory": {
      "type": "sse",
      "url": "http://192.168.6.156:3005/sse?space=local-gpu-cluster"
    }
  }
}
```

Or: `claude mcp add --transport sse memory "http://192.168.6.156:3005/sse?space=local-gpu-cluster"`

## Claude Code — run against the local LLM (optional)

Point Claude Code at the cluster's llama.cpp backend through the router's
Anthropic passthrough (LXC 153, `:8000`). The router applies admission control;
`<ROUTER_API_KEY>` is in `/etc/router.env` on LXC 153.

```bash
export ANTHROPIC_BASE_URL=http://192.168.6.153:8000
export ANTHROPIC_AUTH_TOKEN=<ROUTER_API_KEY>
export ANTHROPIC_MODEL=rag-qwen3.6        # or a loaded coder alias (see GET /v1/models)
claude
```

Caveats: no prompt caching on the local backend (more tokens reprocessed per turn);
Claude Code is tuned for Claude models, so a local model's tool-calling discipline
will differ. Context floor ~32K — the coder profiles (128K–256K) are comfortable.

## Manual use

`remember`/`recall`/`forget`/`memory_status` are model-callable now. Automatic
recall-on-start and save-before-compaction are **phase 2** (see the design spec
§9 — OpenCode plugin + Claude Code SessionStart/PreCompact hooks).
````

- [ ] **Step 2: Commit**

```bash
git add docs/memory-vault-clients.md
git commit -m "docs(memvault): OpenCode + Claude Code client wiring"
```

---

## Task 9: Verification hooks + final integration check

**Files:**
- Modify: `scripts/60-verify.sh`

- [ ] **Step 1: Read the existing verify script to match its style**

Run: `sed -n '1,40p' scripts/60-verify.sh` (observe how it probes other services so the addition matches).

- [ ] **Step 2: Add memory-vault checks**

Append a check block to `scripts/60-verify.sh` (adapt variable names to the script's existing helpers; this is the content to add):

```bash
# ---------- Memory Vault (LXC 156) ----------
MV_VMID="${MEMVAULT_VMID:-156}"
if lxc_exists "$MV_VMID"; then
  step "Verify Memory Vault stack + bridge"
  MV_IP="$(lxc_get_ip "$MV_VMID" || true)"
  # 1. docker compose services up
  pct exec "$MV_VMID" -- bash -lc 'cd /opt/memory-vault && docker compose ps --status running --format "{{.Service}}"' \
    | grep -q app && ok "memory-vault app container up" || warn "memory-vault app not running"
  # 2. dashboard/REST listening
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://${MV_IP}:8000/" || echo 000)"
  [[ "$code" != "000" ]] && ok "REST/dashboard listening (HTTP $code)" || warn "REST not reachable"
  # 3. SSE bridge handshake
  if curl -sN -m 5 "http://${MV_IP}:3005/sse" | head -1 | grep -qi event; then
    ok "MCP-SSE bridge handshake OK on :3005"
  else
    warn "MCP-SSE bridge not responding on :3005"
  fi
  # 4. bridge service active
  pct exec "$MV_VMID" -- systemctl is-active --quiet memory-vault-bridge \
    && ok "memory-vault-bridge.service active" || warn "memory-vault-bridge.service not active"
fi

# ---------- Router Anthropic passthrough (LXC 153) ----------
step "Verify router /v1/messages route is registered"
if curl -s "http://${ROUTER_IP:-192.168.6.153}:8000/openapi.json" | grep -q '/v1/messages'; then
  ok "router exposes /v1/messages"
else
  warn "router /v1/messages not found — redeploy 53-lxc-router.sh"
fi
```

- [ ] **Step 3: Run the full verify**

Run on the host: `LGC_DIR=scripts bash scripts/60-verify.sh`
Expected: memory-vault checks print `[ ok ]` for app container, REST, SSE handshake, bridge service, and the router `/v1/messages` route.

- [ ] **Step 4: End-to-end integration check from a client**

- Add the OpenCode `mcp.memory` entry (Task 8) to a test repo's `opencode.json`, start OpenCode, and confirm the `memory` tools appear and a manual `recall` of the Task 5 canary (`BLUE-HERON-42`, space `plan-smoketest`) returns it.
- (Optional) Export the three `ANTHROPIC_*` vars (Task 8) and run `claude` against the local model; confirm a trivial prompt returns a completion.

- [ ] **Step 5: Commit**

```bash
git add scripts/60-verify.sh
git commit -m "test(memvault): add stack + bridge + router-passthrough smoke checks to 60-verify.sh"
```

---

## Self-Review (completed during authoring)

**Spec coverage:** §2 LXC 156 → Task 2; §2 compose/data on tank → Task 2 (override + dataset); §3 bridge/space scoping → Tasks 4–5; §2.4 router passthrough → Task 6; §3 client configs incl. local-LLM → Task 8; §7 backups → Task 7; §7 token mode-600 / LAN-only → Tasks 2–3; §8 verification → Tasks 5/6/9; §9 phase-2 explicitly deferred (not implemented, documented as such in Task 8). No spec requirement is unaddressed.

**Placeholder scan:** Remaining angle-bracket tokens (`<TOKEN>`, `<ROUTER_API_KEY>`, `<repo-slug>`) are genuine per-environment secrets/values an operator substitutes at run time, not unfinished design. The one true unknown — exact REST field names — is resolved by Task 3 Step 3 (capture OpenAPI) and isolated to the constants block in Task 4 Step 1.

**Type/name consistency:** `MEMVAULT_*` env names match between Task 1 (config), Task 2 (script), and Task 3/5 (env files). Bridge tool names (`remember`/`recall`/`forget`/`memory_status`) are consistent across Tasks 4, 5, 8. Router helper names (`sse_passthrough_with_keepalive`, `_approx_text_from_anthropic`, `_approx_text_from_messages`, `chat_sem`, `count_tokens`, `_error_body`, `upstream_headers`, `resolve_alias`) all match the existing `router-app.py`.

**Known risk carried from spec:** Memory Vault v1.0.1 / single-maintainer; the `/api/*` field names and the `memory-vault token create` CLI are confirmed at runtime in Task 3 before any dependent code runs.
