# Memory Vault — client wiring

Shared persistent-memory service on LXC 156 (`192.168.6.223`). The MCP-over-SSE
bridge is at `http://192.168.6.223:3005/sse`. Pin a **memory space per repo** via
`?space=<repo-slug>` so OpenCode and Claude Code working the same repo share memory.

> **IP note:** LXC 156 runs on **DHCP** (lease `192.168.6.223` as of 2026-06-18).
> `.156` from the original design was already taken by the monitoring/SearXNG LXC.
> Because this is a plain DHCP lease, the IP can change on renewal and break the
> pinned URLs below — **add a DHCP reservation** for MAC `bc:24:11:05:76:8d → 192.168.6.223`
> on your router to make it stable.

## OpenCode (`opencode.json`)

```jsonc
{
  "mcp": {
    "memory": {
      "type": "remote",
      "url": "http://192.168.6.223:3005/sse?space=local-gpu-cluster"
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
      "url": "http://192.168.6.223:3005/sse?space=local-gpu-cluster"
    }
  }
}
```

Or: `claude mcp add --transport sse memory "http://192.168.6.223:3005/sse?space=local-gpu-cluster"`

## Claude Code — run against the local LLM (optional)

Point Claude Code at the cluster's llama.cpp backend through the router's
Anthropic passthrough (LXC 153, `:8000`). The router applies admission control;
`<ROUTER_API_KEY>` is in `/etc/router.env` on LXC 153.

```bash
export ANTHROPIC_BASE_URL=http://192.168.6.153:8000
export ANTHROPIC_AUTH_TOKEN=<ROUTER_API_KEY>   # sent as 'Authorization: Bearer' — matches the router's bearer auth (NOT ANTHROPIC_API_KEY, which sends x-api-key)
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
