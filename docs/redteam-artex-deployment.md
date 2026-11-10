# ARTEX red-team box — LXC 158 (`artex-redteam`)

Deployment reference for the ARTEX autonomous-pentest framework on the cluster, for
**authorised testing of the operator's own colo infra only**. Sibling to LXC 157
(RedteamAgent); 157 is left intact. Built 2026-11-10.

## What it is

[ARTEX](https://github.com/jiwoochris/artex-global) is an LLM multi-agent autonomous
penetration-testing framework (Go backend + Next.js UI). We run the **artex-global**
fork (English edition of upstream `Autumn-27/ARTEX`, AGPL-3.0). Upstream was deleted in
October 2026 after widespread abuse; we build from a **source-reviewed, pinned commit**
rather than trusting a prebuilt image.

## Container

| | |
|---|---|
| CTID / IP | 158 / 192.168.6.158 |
| OS | Debian 12, **privileged**, `nesting=1,keyctl=1` (Docker-in-LXC) |
| Resources | 6 cores / 12 GB / 60 GB on `tank-lxc`, swap 2 GB |
| `onboot` | 0 — started per engagement, not at host boot |

## Build (source, pinned)

Image `artex:local` is built in-container from commit `133f3cf` via the project's own
`build.sh` (Go 1.26.3 + Node 20; `rsync` is required and is **not** in the minimal Debian
template). `docker-compose.yml`'s `image:` is repointed from the GHCR prebuilt to
`artex:local`. Clone lives at `/root/artex-global`. Reproduce with
[`scripts/lxc-artex/provision.sh`](../scripts/lxc-artex/provision.sh).

### Source review (commit `133f3cf`) — clean

- No telemetry/analytics SDKs in `go.mod` (just the `norma` agent SDK, postgres, mitmproxy, DNS, sqlite).
- Self-update is **user-triggered only**, GitHub-only, and blocked by the egress lock; `Bootstrap()` at startup applies an already-staged binary from disk and makes no network call.
- Notification channels (DingTalk/Telegram/Feishu/WeCom/webhook) are optional, off by default, and SSRF-guarded.
- UI/proxy bind to loopback by default; ports are opt-in.

## Stack & model

`docker compose up -d` runs `artex` (`:8787` UI, `:8788` mitmproxy) + `postgres:16-alpine`.
The LLM is wired via env (see [`env.example`](../scripts/lxc-artex/env.example)) to the
V620 router's OpenAI-compatible endpoint:

```
ARTEX_LLM_PROVIDER=openai
ARTEX_LLM_BASE_URL=http://192.168.6.153:8000/v1
ARTEX_LLM_MODEL=qwen3.8-redteam
OPENAI_API_KEY=<router key, reused from LXC 157>
```

**The model only answers while the red-team profile is deliberately loaded** on the router
(`swap-chat-model.sh qwen3.8-redteam`, which needs 3-slot/redteam mode). It is single-tenant —
whatever profile is loaded answers every chat request — so load it for an engagement and swap
back afterwards (`swap-chat-model.sh qwen3.8`).

## Egress lock — the hard scope boundary

`/usr/local/sbin/redteam-egress-lock.sh`
([source](../scripts/lxc-artex/redteam-egress-lock.sh), iptables-nft, REJECT fast-fail)
allows outbound **only** to:

- the 5 colo targets (`38.107.232.37/39/51/103/210`) — the engagement scope
- `192.168.6.153` — the router (model)
- `192.168.6.155` — searxng + docs MCP (drop this line for full cluster isolation; ARTEX may not need it)
- `192.168.6.1:53` — DNS
- loopback + established/related

Everything else is REJECTed — the broad internet **and** the rest of the cluster
(`151/154/156` and the Proxmox host `.175`) — so the offensive box cannot scan production
and a compromised target cannot pivot inward. Applied to both `OUTPUT` (host) and
`DOCKER-USER` (containers — the operative chain, since ARTEX runs entirely in containers).
IPv6 is rejected.

Persistence: `redteam-egress-lock.service` (oneshot, `After=docker`) +
`docker.service.d/redteam-egress.conf` `ExecStartPost` that re-applies after every Docker
restart (a restart flushes `DOCKER-USER`).

Verify (from the artex container and the host):

```sh
pct exec 158 -- docker exec artex-global-artex-1 \
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 6 http://192.168.6.153:8000/healthz   # 200
pct exec 158 -- docker exec artex-global-artex-1 \
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 6 https://1.1.1.1                      # 000
```

## Access & first-run

UI is published on `0.0.0.0:8787` → **http://192.168.6.158:8787** (plain HTTP; add a
reverse proxy for TLS if wanted). The mitmproxy port `8788` stays loopback-only.

Set the admin password on first run: read the one-time setup token from the logs and enter
it at `/setup`.

```sh
pct exec 158 -- docker logs artex-global-artex-1 2>&1 | grep -i "setup token"
```

## Operate

```sh
# load the model profile for an engagement (on the router, LXC 153)
swap-chat-model.sh qwen3.8-redteam
# use ARTEX at http://192.168.6.158:8787
# afterwards, swap the chat slot back
swap-chat-model.sh qwen3.8
```

Secrets (`/root/artex-global/.env`, chmod 600) are never committed. To change scope, edit
`TARGETS` in the egress-lock script and re-run it.
