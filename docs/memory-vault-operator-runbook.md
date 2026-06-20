# Memory Vault — operator runbook (host-side deploy)

The steps below run **as root on the Proxmox host** (and a couple from a client).
Architecture and rationale: [`memory-vault-architecture.md`](./memory-vault-architecture.md).

Prereqs: a checkout of this repo on the Proxmox host (or push `scripts/` to it),
`config.env` present, ZFS pool `tank` mounted, router (LXC 153) reachable.

## 1. Provision LXC 156 + the Docker stack
```bash
LGC_DIR=scripts bash scripts/61-lxc-memory-vault.sh
pct exec 156 -- bash -lc 'cd /opt/memory-vault && docker compose ps'   # expect db (healthy) + app Up
```
If `app` crash-loops, check `docker compose logs app`; the usual first-run cause is
PGDATA ownership — confirm `/opt/memory-vault-data/pgdata` is `999:999` inside the LXC.

## 2. Mint the API token, write the bridge env, pin the REST contract (plan Task 3)
```bash
# Mint a bearer token (note the printed value as <TOKEN>):
pct exec 156 -- bash -lc 'cd /opt/memory-vault && docker compose exec -T app memory-vault token create cluster-bridge'

# Write the bridge env (substitute <TOKEN>):
pct exec 156 -- bash -lc 'umask 077; cat > /etc/memory-vault-bridge.env <<EOF
MEMVAULT_API_URL=http://127.0.0.1:8000
MEMVAULT_API_TOKEN=<TOKEN>
MEMVAULT_DEFAULT_SPACE=default
MCP_HOST=0.0.0.0
MCP_PORT=3005
EOF
chmod 600 /etc/memory-vault-bridge.env; echo wrote'

# Capture the live OpenAPI and CONFIRM the REST field names the bridge assumes:
pct exec 156 -- bash -lc 'curl -s -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/openapi.json' > /tmp/mv-openapi.json
```
**Confirm against `/tmp/mv-openapi.json`** the request fields for `/api/ingest/text`
(text + space) and `/api/search` (query + space + limit/top_k), the `/api/search`
results array key + per-item content/score fields, and the `forget`/`status` paths.
If any differ from the constants block at the top of
`scripts/files/memory-vault-bridge.py` (`REMEMBER_*`, `RECALL_*`, `FORGET_PATH`,
`STATUS_PATH`), edit those constants (one place) before step 3. Optionally commit the
schema: `cp /tmp/mv-openapi.json docs/memory-vault-openapi.json` and commit.

## 3. Deploy the MCP (Streamable HTTP) bridge
```bash
LGC_DIR=scripts bash scripts/62-memory-vault-bridge.sh
# Liveness (Streamable HTTP at /mcp): expect an HTTP status (e.g. 400/406), NOT a refused connection
curl -s -o /dev/null -w "%{http_code}\n" "http://$(pct exec 156 -- hostname -I | awk '{print $1}'):3005/mcp"
```
Round-trip test (from a host/client with python + `mcp` installed) — see plan Task 5
Step 4; confirm a `remember` then `recall` of a canary phrase returns it. If `recall`
says "No memories found", re-confirm the `/api/search` field names (step 2) and
`systemctl restart memory-vault-bridge` in LXC 156.

## 4. Router Anthropic passthrough
```bash
LGC_DIR=scripts bash scripts/53-lxc-router.sh        # redeploys updated app.py
curl -s http://192.168.6.153:8000/openapi.json | grep -q /v1/messages && echo OK
# Functional check (substitute ROUTER_API_KEY from /etc/router.env on LXC 153, and a loaded alias):
curl -s http://192.168.6.153:8000/v1/messages -H "Authorization: Bearer <ROUTER_API_KEY>" \
  -H 'content-type: application/json' \
  -d '{"model":"rag-qwen3.6","max_tokens":64,"messages":[{"role":"user","content":"Reply with one word: pong"}]}'
```
A 502 means the loaded llama-server build lacks `/v1/messages` (need ≥ b9584).

## 5. Nightly backup timer
Run the installer on the PVE host — it pushes `scripts/files/memory-vault-backup.sh`
into LXC 156 (`/usr/local/bin/memory-vault-backup.sh`), installs a **host** systemd
`memory-vault-backup.service` + `.timer` (daily 02:30) whose service runs the dump
inside 156 via `pct exec`, and runs one backup immediately to validate:
```bash
LGC_DIR=scripts bash scripts/64-memory-vault-backup-timer.sh
```
Host placement keeps scheduled jobs visible alongside `rag-refresh.timer` and matches
what the cluster-monitor `backup_timer` check probes (`systemctl is-active
memory-vault-backup.timer` on the host). Verify:
```bash
systemctl list-timers memory-vault-backup.timer --no-pager   # NEXT/LAST populated
pct exec 156 -- ls -lt /opt/memory-vault-data/backups        # expect memory_vault-*.sql.gz
```

## 6. Full verification + clients
```bash
LGC_DIR=scripts bash scripts/60-verify.sh             # memory-vault + /v1/messages checks should print [ ok ]
```
Wire clients per `docs/memory-vault-clients.md` (OpenCode `opencode.json`, Claude Code
`.mcp.json`, optional local-LLM `ANTHROPIC_*` env). Pin a `?space=<repo-slug>` per repo.

## Deferred to phase 2 (designed, not built)
Automatic recall-on-start / save-before-compaction via OpenCode plugin
(`session.created` / `experimental.session.compacting` / `session.compacted`) and
Claude Code `SessionStart` / `PreCompact` hooks calling the REST API directly. See
design spec §9.

## Verify-at-deploy notes (from code review)
- `memory-vault-bridge.py` uses the MCP SDK low-level `SseServerTransport` + a private
  `request._send`; confirm against the installed `mcp` version (step 3 handshake + round-trip exercise it).
- The `?space=` ContextVar is backstopped by an explicit `space` tool argument.
