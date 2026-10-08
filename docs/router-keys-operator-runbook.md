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
The router re-reads it whenever the file is replaced or changes (inode, mtime or size), so issuing
and revoking need no restart. The
plaintext key exists only in the `--out` file written at issue time. The router skips any entry that
fails validation (hash not 64 hex characters, aliases not a non-empty list of strings, missing or
non-finite `expires`, reserved or unsafe name) and logs `skipping malformed entry`; the other
entries keep working.

All commands below run on the Proxmox host as root. Use the CLI's full path
`/usr/local/sbin/router-keys`: `pct exec` runs with `PATH=/sbin:/bin:/usr/sbin:/usr/bin`, so the bare
name is not found (it works in an interactive root shell inside the container).

## Issue a key

```bash
pct exec 153 -- /usr/local/sbin/router-keys add --name wf-run-1 --aliases qwen3.8-nothink --ttl-hours 72 --out /root/wf-run-1.key
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
pct exec 153 -- /usr/local/sbin/router-keys list                      # name, active/expired, expiry, aliases; no secrets
pct exec 153 -- /usr/local/sbin/router-keys revoke --name wf-run-1    # or: revoke --all
```

Revocation takes effect on the next request. A stream that was already admitted runs to completion.

## Troubleshoot

Errors use the OpenAI envelope `{"error": {"type": ..., "message": ...}}`.

| Status | `error.type` | `error.message` | Cause |
|---|---|---|---|
| 403 | `unauthorized` | `invalid Bearer token` | Unknown, revoked or expired key — or the keys file is unreadable or malformed. The owner key still works. In LXC 153, `journalctl -u llm-router` shows `router keys file unreadable, scoped keys disabled` (file unparseable or not readable by the `router` user) or `router keys file: skipping malformed entry` (one bad entry). A **missing** file, or one whose `keys` is not a list, disables every scoped key without a log line. After fixing only the file's ownership or mode, `touch` it (or restart the router): a permission change alone is not noticed. |
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

`/root/router-rollback.sh` in LXC 153 restores `/opt/llm-router` from the pre-deploy backup. It is
written by hand at deploy time (see the deployment record below); `53-lxc-router.sh` does not
create it.

```bash
pct exec 153 -- systemctl stop llm-router
pct exec 153 -- sh /root/router-rollback.sh
pct exec 153 -- systemctl start llm-router
```

After a rollback the keys file and CLI remain but are unused; every non-owner key gets 403.

## Deployment record

**2026-10-07, 16:15 UTC** — `main` 7e9200d (PR #7) deployed with `scripts/53-lxc-router.sh`.

- Backup: `/opt/llm-router.bak-20261007T161428Z` in LXC 153; rollback script `/root/router-rollback.sh`
  restores it (commands above). Delete the backup once satisfied.
- Owner path unchanged: health ok, 7 aliases incl. `qwen3.8-nothink`, owner chat 200; deployed
  `app.py`, `access_keys.py`, `workforce_lane.py` match the repo by sha256.
- Keys file `root:router 640` containing `{"keys": []}`; CLI `root:root 750`.
- Probe keys (1 h, revoked and shredded afterwards):
  - 1-slot mode: scoped chat 503 `workforce_lane_closed` (so the router, as the `router` user, reads the
    keys file), embeddings 403 `forbidden`, other alias 403, models 200, both refusals in the access log.
  - 3-slot mode (redteam enter/exit): scoped alias on another backend (`qwen3-coder`) 409 with no swap;
    a scoped stream in flight during the 3 -> 1 shrink ended with a `service_degraded` frame and `[DONE]`
    (slots released); afterwards a new scoped request got 503 and the owner 200.
- `redteam-mode-idle-check` reads `/run/redteam-mode.last`; after a host reboot that file is absent and
  counts as idle since 1970, so a manual `redteam-mode-enter.sh` is reverted at the next 2-minute tick
  unless the file is written first (`date +%s > /run/redteam-mode.last`).
