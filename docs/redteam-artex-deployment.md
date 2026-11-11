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
- `192.168.6.1:53` — DNS
- loopback + established/related

Everything else is REJECTed — the broad internet **and** the rest of the cluster
(`151/154/155/156` and the Proxmox host `.175`) — so the offensive box cannot scan production
and a compromised target cannot pivot inward. Applied to both `OUTPUT` (host) and
`DOCKER-USER` (containers — the operative chain, since ARTEX runs entirely in containers).
IPv6 is rejected.

**`.155` is deliberately not allowed.** It runs a tinyproxy on `:3128` that accepts all of
`192.168.6.0/24` and forwards to `github.com`, `githubusercontent.com`, `crt.sh`, NVD and
CIRCL — allowlisting `.155` was an internet path around the lock. ARTEX never used it.

**This lock is the only hard boundary.** ARTEX's own task scope is advisory and widens
itself: a goal phrased as "`example.com` and any subdomains" makes every subdomain in scope,
and ARTEX auto-adds whatever IP that subdomain resolves to — including third-party mail
hosts and CDNs. In the first engagement it ran three top-1000 port scans against the
domain's mail provider; only this lock stopped them. **Scope engagements by IP**, list
third-party infrastructure as out of scope in the goal, and remove any out-of-scope IPs from
the task's scope list.

Rejections use `icmp-admin-prohibited`, not a TCP reset — a reset looks exactly like a
closed port, and the agent recorded blocked hosts as "1000 closed ports". nmap now reports
`filtered … admin-prohibited` and curl fails instantly with "Couldn't connect".

The rules are applied atomically (one `iptables-restore` transaction), so a re-run or a
Docker restart never leaves `DOCKER-USER` momentarily empty.

### Audit: what did the lock reject?

Every new outbound connection the lock rejects is recorded per destination IP, protocol and
port (with a packet counter, kept 30 days) in the nftables table `inet redteam_egress_audit`.
Kernel `LOG` is not used because it is suppressed inside an LXC while the host has
`nf_log_all_netns=0`.

```sh
pct exec 158 -- /usr/local/sbin/redteam-egress-report.sh           # per destination
pct exec 158 -- /usr/local/sbin/redteam-egress-report.sh --ports   # per destination/proto/port
pct exec 158 -- /usr/local/sbin/redteam-egress-report.sh --reset   # clear (start of an engagement)
```

Persistence: `redteam-egress-lock.service` (oneshot, `After=docker`) +
`docker.service.d/redteam-egress.conf` `ExecStartPost` that re-applies after every Docker
restart (a restart flushes `DOCKER-USER`).

Verify (from the artex container and the host):

```sh
pct exec 158 -- docker exec artex-global-artex-1 \
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 6 http://192.168.6.153:8000/healthz   # 200
pct exec 158 -- docker exec artex-global-artex-1 \
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 6 https://1.1.1.1                      # 000
pct exec 158 -- docker exec artex-global-artex-1 \
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 6 -x http://192.168.6.155:3128 https://github.com  # 000
```

## Scoping an engagement (goal + constraints)

Write the task goal by **IP**, not by domain, and add task constraints (Task → constraints;
`allow`/`deny` rows that ARTEX injects at the top of the planner/worker prompt, above its
"find another entry surface" heuristics). Constraints are prompt-level guidance — the egress
lock remains the enforcement. Before starting, check the task's scope list and delete any
IP outside the lock's `TARGETS`.

For the colo, all of this is pre-filled in the ARTEX task template **"Colo pentest
(IP-scoped)"** ([`task-template-colo.sql`](../scripts/lxc-artex/task-template-colo.sql); apply
like the label SQL below). It carries the goal, the rules of engagement in the description,
and task intercept rules: five `exact_ip` **allow** rows (a whitelist — ARTEX's asset gate
refuses anything else) plus **block** rows for the mail/CDN hostnames. Templates cannot carry
task constraints or the asset-coverage switch: turn **asset coverage off** on the create form
(it auto-grows the task scope and exposes `add_task_scope`), and add constraint rows after
creating if wanted.

Goal template:

> Perform an authorised penetration test of the services on these IP addresses ONLY:
> `<IP list>`. Hostnames under `<domain>` are in scope only while they resolve to one of
> those IPs. Hostnames that resolve anywhere else (mail providers, CDNs, SaaS) are
> third-party infrastructure and OUT OF SCOPE — do not scan, probe or connect to them. An
> egress firewall blocks every out-of-scope IP: "filtered (admin-prohibited)" or an instant
> "Couldn't connect" on such an IP is the firewall, not the target — note nothing and move
> on. Locate and identify exploitable vulnerabilities and access beyond intended.

Constraints:

| kind | text |
|---|---|
| allow | Test only `<IP list>` and hostnames that resolve to them. |
| deny | Any connection, scan or request to an IP outside `<IP list>` — including the domain's MX/mail hosts and CDN addresses. |
| deny | Adding any IP or domain outside `<IP list>` to the task scope or assets. |
| deny | Re-scanning or widening scans of a host that answered "filtered (admin-prohibited)" / "Couldn't connect" — that is the egress firewall. |
| deny | Public lookup services (crt.sh, Shodan, search engines) — unreachable from this box. |

## English UI labels

The web UI translates rule names, asset notes and tool descriptions itself, but some seed
text stays Chinese: intercept-rule messages, the Report Writing trigger message, and
prompt-version notes. Built-in agent names/descriptions are translated only on screens that go
through `useAgentLabels()`; elsewhere the raw seed shows (Chinese, and **Korean** for the
reporter). Apply both SQL files after a fresh install (idempotent; each only rewrites values
still equal to the seed, so edits made in the UI survive; no restart):
[`i18n-seed-labels-en.sql`](../scripts/lxc-artex/i18n-seed-labels-en.sql) and
[`i18n-agent-labels-en.sql`](../scripts/lxc-artex/i18n-agent-labels-en.sql) (agent names,
descriptions and prompt-variable labels, using the UI's own English strings from
`web/messages/en.json`). Agents are selected by key, never by name, and the reporter seed runs
once behind a settings flag, so the English values are not reverted on restart.

```sh
for f in i18n-seed-labels-en.sql i18n-agent-labels-en.sql; do
  pct push 158 scripts/lxc-artex/$f /root/artex-global/$f
  pct exec 158 -- bash -c "docker exec -i artex-global-postgres-1 psql -U artex -d artex \
    -v ON_ERROR_STOP=1 < /root/artex-global/$f"
done
```

Conversation titles of triggered runs (`工具触发 · …` = "tool trigger", plus the scheduled /
finding / goal / timeout / task-created / merged variants) are generated by the Go server.
[`patches/0001-english-trigger-titles.patch`](../scripts/lxc-artex/patches/0001-english-trigger-titles.patch)
makes them English; `provision.sh` applies `patches/*.patch` after checking out the pinned
commit, so the version string ends in `-dirty`. The titles are display-only (never sent to the
model). Existing rows were renamed in `conversations.title` directly.

[`patches/0002-english-report-and-origin.patch`](../scripts/lxc-artex/patches/0002-english-report-and-origin.patch)
translates the generated penetration-test / findings report skeleton (headings, labels, CSV
header — the fork shipped it in **Korean**) and the task graph's start-point text
(`任务起点：… ；目标：…` → `Task origin: …; Goal: …`). The report is rendered on request, so
old tasks' reports come out in English too; existing start-point nodes were rewritten in
`exploration_nodes.payload`. The report tests keep their assertions, now against the English text.

[`patches/0004-english-run-notices.patch`](../scripts/lxc-artex/patches/0004-english-run-notices.patch)
translates the red "run interrupted" block shown when a task is paused, a run times out or a
worker is stopped (`（运行被中断：…）`, `终态`, `中断原因`, token/turn lines, and the cause texts
in `agent/cancelcause.go`), plus the Korean activity-feed dividers (`N차 계획 수립` →
`Planning round N`, goal breakdown, queue and timeout-wrap-up notices). The dividers are
display-only; the cause texts can reach the planner as an intent's stop reason, which is fine
in English. The upstream test that required the dividers to be Korean
(`runtime_activity_localized_test.go`) now requires them to contain no Hangul or Han, and was
checked to fail when a divider is reverted to Korean.

[`patches/0005-english-skill-descriptions.patch`](../scripts/lxc-artex/patches/0005-english-skill-descriptions.patch)
translates the one-line `description:` of the `api-recon` and `scopesentry` skills (what the
Skills list and the agents' skill index show). The skill bodies stay Chinese: they are the
model's working instructions, like the prompts. `skills/` is bind-mounted into the container,
so this takes effect without a rebuild.

### Tool-call repeat guard

[`patches/0003-tool-repeat-guard.patch`](../scripts/lxc-artex/patches/0003-tool-repeat-guard.patch)
adds `agent/repeatguard.go`: once an agent has made the **same tool call (same arguments) and
got the same result 3 times within its last 8 calls**, further repeats are refused with a tool error telling
it to use the result it has and move on. This catches straight runs (A A A) and cycles (A B A B); a call whose
result changes (polling) is never refused, a call is allowed again once 8 other calls have pushed its repeats out of the window, and each agent run has its own guard. Why: on
2026-11-10 the planner of task 3 called `list_assets` with one identical DSL **300 times** in
10 minutes (empty result each time, no reasoning text, ~43k-token prompt per call), and no
worker ever started. Router sampling penalties cannot stop this — they act within one
completion, not across turns — and ARTEX had no guard of its own. Refusals are logged as
`[tools] repeat guard: refused …` in `docker logs artex-global-artex-1`.

Rebuilding on the live box must work **offline** (the egress lock blocks Docker Hub, the Go
proxy and npm): build the binary with `GOPROXY=off ARTEX_SKIP_FRONTEND=1`, then layer it onto
the current image instead of re-running the Dockerfile (its `# syntax=` line makes BuildKit
contact Docker Hub):

```sh
docker tag artex:local artex:prev
printf 'FROM artex:prev\nCOPY dist/amd64/artex /app/artex\n' > /root/artex-patches/Dockerfile.binary-swap
docker build -f /root/artex-patches/Dockerfile.binary-swap -t artex:local . && docker compose up -d artex
```

Still Chinese: the `【任务 …】` / `【本次由工具调用触发】` framing inside triggered runs (it is
the model's input, kept with the Chinese prompts), some canned tool-result strings, and the
skill bodies. The agent prompts are left Chinese on
purpose — upstream treats them as the benchmarked "brain" and only steers the output language
(`ARTEX_LANG=en`).

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
