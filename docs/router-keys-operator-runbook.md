# Router scoped keys — operator runbook

Scoped keys let a client (for example the workforce sandbox) use the router without the owner key
(`ROUTER_API_KEY`). A scoped key:

- may call only `POST /v1/chat/completions` and `GET /v1/models`;
- may request only the aliases it was issued for;
- never runs server-side tools (requests are forced to `tool_execution: "client"`);
- never triggers a chat-profile swap;
- expires (at most 14 days after issue);
- has its own rate-limit bucket (per key), so workforce traffic never spends the owner's per-IP
  budget.

Scoped requests also pass through a **reserved lane**: they may hold at most
`chat slots - RESERVED_SLOTS` chat slots (`RESERVED_SLOTS` defaults to 1), so the owner always has a
slot. In 1-slot mode the lane is closed and scoped requests get HTTP 503 at once; scoped keys are
useful only in 3-slot mode.

The keys file `/etc/router-keys.json` (in LXC 153, `root:router 0640`) stores SHA-256 hashes only.
The router re-reads it when its mtime changes, so issuing and revoking need no restart. The
plaintext key exists only in the `--out` file written at issue time. The router skips any entry that
fails validation (hash not 64 hex characters, aliases not a non-empty list of strings, missing or
non-finite `expires`, reserved or unsafe name) and logs `skipping malformed entry`; the other
entries keep working.

All commands below run on the Proxmox host as root.

## Issue a key

```bash
pct exec 153 -- router-keys add --name wf-run-1 --aliases qwen3.8-nothink --ttl-hours 72 --out /root/wf-run-1.key
pct pull 153 /root/wf-run-1.key /root/gate/wf-run-1.key && chmod 600 /root/gate/wf-run-1.key
pct exec 153 -- shred -u /root/wf-run-1.key
```

- `--ttl-hours` must be in (0, 336].
- `--name` must match `[a-z0-9][a-z0-9._-]{0,63}`, must not be `owner`, and must be unique among
  issued keys. It appears as `principal` in the access log.
- `--out` must not already exist (it is created 0600).
- `add` and `revoke` take an exclusive lock (`/etc/router-keys.json.lock`), so overlapping runs
  queue instead of undoing each other.
- The key is never printed; `add` prints only the name, aliases and expiry.

## List and revoke

```bash
pct exec 153 -- router-keys list                      # name, active/expired, expiry, aliases; no secrets
pct exec 153 -- router-keys revoke --name wf-run-1    # or: router-keys revoke --all
```

Revocation takes effect on the next request. A stream that was already admitted runs to completion.

## Troubleshoot

Errors use the OpenAI envelope `{"error": {"type": ..., "message": ...}}`.

| Status | `error.type` | `error.message` | Cause |
|---|---|---|---|
| 403 | `unauthorized` | `invalid Bearer token` | Unknown, revoked or expired key — or the keys file is malformed. The owner key still works; `journalctl -u llm-router` (in LXC 153) shows `router keys file unreadable, scoped keys disabled`. |
| 403 | `forbidden` | `this key may not call this endpoint` | A scoped key on any endpoint other than chat completions / models. |
| 403 | `forbidden` | `model_not_allowed` | The alias is not in the key's allowlist. |
| 403 | `forbidden` | `server_tools_not_allowed` | The request asked for `tool_execution: "server"`. |
| 503 | `service_unavailable` | `workforce_lane_closed: …` | Chat is in 1-slot mode; scoped keys need 3-slot mode. A stream that was already waiting when the layout shrank gets the same error as an SSE `data:` frame followed by `[DONE]`. |
| 409 | `error` | `model '…' targets profile backend …` | The alias belongs to a profile that is not loaded. Scoped keys never swap; switch the profile with the owner key or `scripts/swap-chat-model.sh`. |

Every access-log line (`/var/log/llm-router/access.log` in LXC 153) carries `"principal"`: `owner`
or the key's name. A scoped key refused on another endpoint is logged with
`"error": "endpoint_forbidden"` -- the line to grep for when checking whether a sandboxed agent
probed outside its scope.

## Roll back the feature

The deploy writes `/root/router-rollback.sh` in LXC 153, which restores `/opt/llm-router` from the
pre-deploy backup.

```bash
pct exec 153 -- systemctl stop llm-router
pct exec 153 -- sh /root/router-rollback.sh
pct exec 153 -- systemctl start llm-router
```

After a rollback the keys file and CLI remain but are unused; every non-owner key gets 403.
