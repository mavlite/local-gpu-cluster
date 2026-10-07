# Router Scoped Keys and Reserved User Lane: Implementation Plan (Plan A of 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking.

**Goal:** give the LLM router per-run **scoped API keys** and a **reserved user lane**, then deploy them to
production with a rollback.
- Scoped keys carry an alias allowlist, an expiry and SHA-256 storage. They may call chat completions and
  the model list only. They get no server-side tools and can never trigger a profile swap.
- The reserved lane means workforce traffic can never hold the owner's last chat slot.

**Architecture:** two new sibling modules, `access_keys.py` (principals, key store, policy) and
`workforce_lane.py` (reserved lane gate), wired into `router-app.py`:
- the auth middleware resolves a principal;
- `chat()` applies the policy and admits each request through a per-principal gate;
- the auto-swap path is owner-only;
- access-log lines name the principal.

A stdlib CLI, `router-keys`, issues, lists and revokes keys inside LXC 153. `53-lxc-router.sh` ships it all
and creates an empty keys file.

**Tech stack:** Python 3.12 (router venv: FastAPI 0.141.1, Starlette 1.6.0, httpx 0.28.1, slowapi 0.1.10);
bash deploy script; pytest.

**Spec:** `docs/superpowers/specs/2026-10-07-distributed-workforce-design.md` (rev 2, approved 2026-10-07),
§5.3 Keys and §5.4 Router changes. This is **Plan A of 4**:
- B: sandbox VM and host readiness;
- C: workforce harness;
- D: experiment runbook.

Plan A ships on its own and is useful without the others.

**How this plan carries its code.** All new code and both edits to existing files were written
test-first and **executed before this plan was written**.

What was executed:
- 13 integration tests run the real router app under pinned production versions against a stub chat
  server and a stub swap webhook.
- 25 unit tests.
- Mutation checks: the reserve removed, key leaks to stdout or the keys file, a broken deploy JSON, and a
  dropped lane release. Each mutant was killed.

Where it lives: the appendix `docs/superpowers/plans/2026-10-07-router-scoped-keys/`, committed with this
plan.
- New files are stored verbatim under `files/`.
- Edits to the two existing files are exact diffs against `main` 4e1791f: `router-app.patch` and
  `53-lxc-router.patch`.
- Applied to a clean `main` checkout, the appendix was verified to produce the tested tree byte for byte.

Tasks **apply** the appendix; they never retype it. Code transcribed from a plan is a defect source (repo
memory). Reviewers must still read the code: the plan is not evidence that it is right.

**Facts established by running (2026-10-07):**
- **Production router baseline.**
  - Live LXC 153 runs Python 3.12.3 with the versions above.
  - `/v1/models` lists 7 aliases, including `qwen3.8-nothink`.
  - Normal mode has 1 chat slot; redteam / 3-slot mode has 3.
- **`hold_slot()`** needs only an awaitable `acquire()` and a `release()` that is sync or async. The
  composite lane gate drops in.
- **`TOOL_EXECUTION_DEFAULT`** can make server-side tools the default. Scoped keys are therefore *forced*
  to `"client"`, not just refused when they ask. The integration test sets the default to `"server"` to
  prove this.
- **Quoting.** A first version of the deploy line wrote `'{keys: []}'`, which is invalid JSON, through
  nested `pct exec … sh -c` quoting. The fix writes the file on the host and uses `pct push`. A behavioural
  test now runs that exact block against a fake `pct`.

## Global Constraints

**Git and commits**
- Conventional commits, **no `Co-Authored-By` trailer**.
- Commit **by explicit path only**: `git add -- <paths>` then `git commit -m … -- <paths>`. Before each
  commit, check `git branch --show-current` and `HEAD`. Other sessions share this checkout.
- **Never push.** The user pushes; the host takes commits by `git bundle` when the user hasn't pushed yet.

**Secrets**
- A plaintext key is never printed, logged, echoed, put in argv, or committed.
- `router-keys add` writes it only to a new 0600 `--out` file.

**Live system**
- Never use nested `pct exec … sh -c '…'` quoting. Push a script file and run it (AGENTS.md footgun).
- Production router changes require all of:
  - a backup of `/opt/llm-router` inside LXC 153 before deploy;
  - live verification of health, aliases (including `qwen3.8-nothink`) and owner chat after deploy;
  - a documented rollback command.

**Fixed values**
- Keys file `/etc/router-keys.json`, owned `root:router`, mode `0640`.
- CLI at `/usr/local/sbin/router-keys`, mode `0750`.
- `RESERVED_SLOTS` defaults to `1`.
- A scoped key may call exactly `POST /v1/chat/completions` and `GET /v1/models`.
- Key TTL is at most 14 days.

**Checks**
- `python3 -m pytest scripts/rag/tests scripts/files/tests -q`. The integration test skips without the
  router's dependencies.
- Plus the router-pinned venv run, Task 1 Step 3.
- `bash -n scripts/53-lxc-router.sh`.

## Review Focus

These are the failure modes most likely to bite, with where each is pinned.

1. **A disconnecting workforce client pins the reserve.** Expected: both the lane and chat slots are
   released when Starlette closes the body.
   - Pinned by `test_client_disconnect_mid_stream_releases_lane_and_chat_slot` (Task 1, mutation-checked).
2. **Chat capacity shrinks while workforce requests are in flight**, for example a 3→1 swap. Expected:
   in-flight streams finish; new scoped requests get 503 at once; the owner queues behind in-flight
   requests, because the AdmissionGate never evicts.
   - Live check in Task 4 Step 6.
3. **The keys file is hand-edited and malformed.** Expected: scoped keys fail closed and the owner is
   unaffected.
   - Pinned by `test_malformed_or_missing_file_fails_closed_for_scoped_keys_only` (Task 1).
   - Live check in Task 3.
4. **A key expires mid-run.** Expected: the next request gets 403; a stream already admitted completes.
   - Pinned by `test_expired_key_rejected` (Task 1).
   - Teardown in Plan D revokes explicitly anyway.
5. **A scoped key presents a valid alias whose backend is not loaded.** Expected: 409, never a swap.
   - Pinned by `test_scoped_key_never_triggers_a_profile_swap` (Task 1).
   - Live check in Task 4 Step 5.

---

## File structure

| Path | Responsibility |
|---|---|
| `scripts/files/access_keys.py` | `Principal`, `OWNER`, `KeyStore` (hash lookup, expiry, mtime reload, fail-closed); `endpoint_allowed`, `chat_violation`, `apply_chat_policy`, `may_swap` |
| `scripts/files/workforce_lane.py` | `ReservedLane` (capacity = chat capacity − reserve), `LaneClosed`, `gate_for(principal, chat_gate, lane)` |
| `scripts/files/router-keys.py` | stdlib CLI: `add`, `list`, `revoke`; atomic 0640 writes, 0600 key out-file |
| `scripts/files/router-app.py` | edits: key store + principal in middleware; lane next to `chat_sem`; `chat()` policy, gate, owner-only swap; `principal` in access log |
| `scripts/53-lxc-router.sh` | edits: ship the two modules and the CLI; create an empty keys file only if absent |
| `scripts/files/tests/test_access_keys.py` | key store, policy, deploy-script behaviour (fake `pct`) |
| `scripts/files/tests/test_workforce_lane.py` | reserve property, closed lane, cancellation, capacity changes, streams, disconnect |
| `scripts/files/tests/test_router_keys_cli.py` | CLI: key never on stdout, hash only on disk, 0600, refusals, revoke |
| `scripts/files/tests/test_router_scoped_keys.py` | integration against the real app (skips without router deps) |
| `docs/router-keys-operator-runbook.md` | issue, list, revoke, rotate, troubleshoot, roll back |

---

### Task 1: Apply the staged implementation on a feature branch

**Files:**
- Create: the 7 files under `docs/superpowers/plans/2026-10-07-router-scoped-keys/files/`, copied to
  `scripts/files/` and `scripts/files/tests/`.
- Modify: `scripts/files/router-app.py` and `scripts/53-lxc-router.sh`, via the appendix patches.
- Create: `docs/router-keys-operator-runbook.md`.

**Interfaces (produced, used by Plans C/D and by later tasks here):**
- `access_keys.KeyStore(path, owner_key, clock=time.time).authenticate(presented) -> Principal | None`
- `access_keys.Principal(name, kind, aliases: frozenset, expires: float | None)`, with `.is_owner`
- `access_keys.endpoint_allowed(principal, method, path) -> bool`
- `access_keys.chat_violation(principal, body) -> None | "model_not_allowed" | "server_tools_not_allowed"`
- `access_keys.apply_chat_policy(principal, body) -> dict` (a new dict for scoped keys)
- `access_keys.may_swap(principal) -> bool`
- `workforce_lane.ReservedLane(chat_gate, reserve=1)`, with `.capacity` and `.in_use`
- `workforce_lane.gate_for(principal, chat_gate, lane)`
- `workforce_lane.LaneClosed`
- CLI: `router-keys [--keys-file F] add --name N --aliases a,b --ttl-hours H --out PATH | list | revoke (--name N | --all)`
- Router behaviour:
  - a scoped key in a closed lane gets HTTP 503 `workforce_lane_closed`;
  - a policy violation gets 403 `model_not_allowed` / `server_tools_not_allowed`;
  - another endpoint gets 403 with error type `forbidden`;
  - access-log JSON gains a `"principal"` field.

- [ ] **Step 1: Create the branch in its own worktree, off `main`**

```bash
cd /c/Users/willi/Documents/GitHub/local-gpu-cluster
git fetch -q origin
git worktree add -b feat/router-scoped-keys ../lgc-router-keys origin/main
cd ../lgc-router-keys && git log --oneline -1
```

Expected: `4e1791f Merge fix/router-webfetch-redirects …`, or a later `main`.

If `main` has moved past `4e1791f`, run `git apply --check` (Step 2) before anything else. A conflict
means the plan is stale: stop and report.

- [ ] **Step 2: Apply the appendix**

```bash
A=../local-gpu-cluster/docs/superpowers/plans/2026-10-07-router-scoped-keys
git apply --check "$A/router-app.patch" "$A/53-lxc-router.patch" && git apply "$A/router-app.patch" "$A/53-lxc-router.patch"
cp "$A"/files/access_keys.py "$A"/files/workforce_lane.py "$A"/files/router-keys.py scripts/files/
cp "$A"/files/tests/*.py scripts/files/tests/
git status --short
```

Expected:
- 2 modified files: `scripts/53-lxc-router.sh` and `scripts/files/router-app.py`;
- 7 untracked files: 3 modules and 4 tests.

- [ ] **Step 3: Run the suites in both environments**

```bash
python -m pytest scripts/rag/tests scripts/files/tests -q -p no:cacheprovider | tail -1
bash -n scripts/53-lxc-router.sh && echo "bash -n ok"
# Router-pinned venv, so the integration test actually runs (create once, reuse):
python -m venv /tmp/routervenv && /tmp/routervenv/Scripts/python -m pip install -q \
  "fastapi==0.141.1" "starlette==1.6.0" "httpx==0.28.1" "httpcore==1.0.9" "slowapi==0.1.10" \
  "prometheus-fastapi-instrumentator==8.1.0" "uvicorn==0.52.4" "anyio==4.13.0" "pydantic==2.13.4" pytest
/tmp/routervenv/Scripts/python -m pytest scripts/files/tests -q -p no:cacheprovider \
  --ignore=scripts/files/tests/test_memory_vault_bridge_routing.py | tail -1
```

Expected:
- `232 passed, 1 skipped …` (the skip is the integration test without router deps);
- `bash -n ok`;
- `216 passed …` in the venv.

The ignored file needs the `mcp` package, which is not a router dependency.

- [ ] **Step 4: Prove the key tests are live (mutation by matched replace, then revert; never `git checkout`)**

```bash
cat > /tmp/mut.py <<'EOF'
import sys
p, a, b = sys.argv[1:]
s = open(p, encoding="utf-8").read()
assert s.count(a) == 1, (p, s.count(a))
open(p, "w", encoding="utf-8", newline="\n").write(s.replace(a, b))
EOF
F=scripts/files
python /tmp/mut.py $F/workforce_lane.py "        return max(0, self._chat.capacity - self._reserve)" "        return max(0, self._chat.capacity)"
python -m pytest $F/tests/test_workforce_lane.py -q -p no:cacheprovider | tail -1
python /tmp/mut.py $F/workforce_lane.py "        return max(0, self._chat.capacity)" "        return max(0, self._chat.capacity - self._reserve)"
python /tmp/mut.py $F/router-keys.py '          f" key-> {a.out}")' '          f" key-> {a.out} ({key})")'
python -m pytest $F/tests/test_router_keys_cli.py -q -p no:cacheprovider | tail -1
python /tmp/mut.py $F/router-keys.py '          f" key-> {a.out} ({key})")' '          f" key-> {a.out}")'
git diff --stat -- $F/workforce_lane.py $F/router-keys.py   # must print nothing (both untracked) / no change
```

Expected:
- the reserve mutant fails 4 lane tests, including `test_workforce_can_never_take_the_last_slot`;
- the key-leak mutant fails `test_add_writes_key_only_to_out_file_and_hash_only_to_keys_file`;
- after the reverts, both suites pass again.

- [ ] **Step 5: Write `docs/router-keys-operator-runbook.md`**

````markdown
# Router scoped keys — operator runbook

Scoped keys let a client (e.g. the workforce sandbox) use the router without the owner key. A scoped key
can call only `POST /v1/chat/completions` and `GET /v1/models`, only its allowlisted aliases, never
server-side tools, never triggers a profile swap, and expires. Scoped requests also pass a reserved lane:
they never hold the last chat slot (`RESERVED_SLOTS`, default 1), so in 1-slot mode they get HTTP 503
`workforce_lane_closed` and the owner keeps the slot.

The router re-reads `/etc/router-keys.json` (root:router 0640, SHA-256 hashes only) when it changes, so
issuing and revoking need no restart. All commands run on the Proxmox host as root.

## Issue a key (plaintext only ever in the --out file)
```bash
pct exec 153 -- router-keys add --name wf-run-1 --aliases qwen3.8-nothink --ttl-hours 72 --out /root/wf-run-1.key
pct pull 153 /root/wf-run-1.key /root/gate/wf-run-1.key && chmod 600 /root/gate/wf-run-1.key
pct exec 153 -- shred -u /root/wf-run-1.key
```
TTL is at most 336 h (14 days). Names must be unique; `--out` must not exist.

## List (no secrets) and revoke
```bash
pct exec 153 -- router-keys list
pct exec 153 -- router-keys revoke --name wf-run-1      # or: revoke --all
```
Revocation takes effect on the next request (streams already admitted finish).

## Troubleshoot
| Symptom | Cause |
|---|---|
| 403 `unauthorized` | unknown, revoked or expired key; or keys file malformed (owner key still works; check `journalctl -u llm-router` for "router keys file unreadable") |
| 403 `forbidden` | scoped key on an endpoint other than chat completions / models |
| 403 `model_not_allowed` / `server_tools_not_allowed` | alias not in the key's allowlist / request asked for `tool_execution: server` |
| 503 `workforce_lane_closed` | chat is in 1-slot mode; scoped keys need 3-slot mode (`RESERVED_SLOTS` keeps one slot for the owner) |
| 409 profile mismatch | scoped keys never swap profiles; switch the profile with the owner key or swap-chat-model.sh |
Access-log lines (`/var/log/llm-router/access.log` in LXC 153) carry `"principal"`: `owner` or the key name.

## Roll back the feature
```bash
pct exec 153 -- systemctl stop llm-router
pct exec 153 -- sh /root/router-rollback.sh     # restores /opt/llm-router from the pre-deploy backup
pct exec 153 -- systemctl start llm-router
```
(`/root/router-rollback.sh` is written at deploy time; see the deploy plan.)
````

- [ ] **Step 6: Commit (by path)**

```bash
[ "$(git branch --show-current)" = feat/router-scoped-keys ] || exit 1
P="scripts/files/access_keys.py scripts/files/workforce_lane.py scripts/files/router-keys.py scripts/files/router-app.py scripts/53-lxc-router.sh scripts/files/tests/test_access_keys.py scripts/files/tests/test_workforce_lane.py scripts/files/tests/test_router_keys_cli.py scripts/files/tests/test_router_scoped_keys.py docs/router-keys-operator-runbook.md"
git add -- $P
git commit -m "feat(router): scoped per-run keys and a reserved user lane" -- $P
git show --stat --oneline HEAD | tail -3
```

Expected: one commit with 10 files.

---

### Task 2: Independent security review of the branch

**Files:** fixes only, to the files above, plus new tests for each confirmed finding.

**Interfaces:** consumes the Task 1 commit. Produces a reviewed branch.

- [ ] **Step 1: Dispatch a `security-reviewer` subagent, read-only, on the branch diff**

Brief it with:
- `git -C ../lgc-router-keys diff origin/main...HEAD`;
- the spec §5.3–5.4;
- this plan's Review Focus;
- instructions to check:
  - auth bypass: an empty or whitespace key, header case, the owner key compared to a scoped hash, timing;
  - endpoint-allowlist gaps: trailing slashes, `HEAD`/`OPTIONS`, path normalisation, `/docs`, `/openapi.json`;
  - alias-check gaps: `model` missing or non-string, alias resolution after the check;
  - `tool_execution` gaps: missing, mixed case, non-string;
  - lane deadlocks and leaks under cancellation;
  - CLI race conditions and file permissions;
  - log injection through key names.

Have it write `.superpowers/sdd/workforce-review/review_router_keys.md`.

- [ ] **Step 2: For each CRITICAL or HIGH finding, write a failing test first, watch it fail, fix, watch it pass**

Repeat the Task 1 Step 3 commands after each fix. Commit by path as
`fix(router): <finding> (security review)`.

- [ ] **Step 3: Record MEDIUM and LOW findings** in the review file, with a disposition: fixed now, or
  deferred with a reason.

---

### Task 3: Staging probe inside LXC 153 (production untouched)

**Files:** none in the repo. A temporary directory `/tmp/rk-stage` in LXC 153 is removed at the end.

**Interfaces:** consumes the reviewed branch. Produces proof that the patched app behaves correctly in the
router's own Python 3.12.3 venv against the **real chat server**.

- [ ] **Step 1: Copy the branch's router files to a temporary directory in LXC 153**

Use a tarball and `pct push`; extract with `--no-same-owner`, because Windows tar uids break extraction.

```bash
cd /c/Users/willi/Documents/GitHub/lgc-router-keys/scripts/files
tar -cf /tmp/rk.tar router-app.py access_keys.py workforce_lane.py router-keys.py web_fetch_guard.py alias_defaults.py tavily_cache.py stream_admission.py embed_admission.py
scp -q /tmp/rk.tar root@192.168.6.175:/tmp/rk.tar && rm -f /tmp/rk.tar
ssh root@192.168.6.175 'pct exec 153 -- mkdir -p /tmp/rk-stage && pct push 153 /tmp/rk.tar /tmp/rk-stage/rk.tar && pct exec 153 -- tar --no-same-owner -xf /tmp/rk-stage/rk.tar -C /tmp/rk-stage && rm -f /tmp/rk.tar'
```

- [ ] **Step 2: Write the staging driver locally and push it as a file (no inline quoting)**

```bash
cat > /tmp/rk_stage.sh <<'EOF'
#!/bin/sh
# Staging run of the patched router on 127.0.0.1:8099 with a throwaway keys file. Production
# (:8000, /etc/router-keys.json) is untouched. The owner key is read from /etc/router.env into the
# process environment only.
set -eu
cd /tmp/rk-stage
OWNER="$(sed -n 's/^ROUTER_API_KEY=//p' /etc/router.env)"
LLK="$(sed -n 's/^LLAMACPP_API_KEY=//p' /etc/router.env)"
run() {  # run <RESERVED_SLOTS>
  ROUTER_API_KEY="$OWNER" LLAMACPP_API_KEY="$LLK" ROUTER_KEYS_FILE=/tmp/rk-stage/keys.json \
  ACCESS_LOG_PATH=/tmp/rk-stage/access.log RESERVED_SLOTS="$1" RATE_LIMIT_CHAT=1000/minute \
  /opt/llm-router/venv/bin/uvicorn app:app --app-dir /tmp/rk-stage --host 127.0.0.1 --port 8099 \
    > /tmp/rk-stage/uvicorn.log 2>&1 &
  echo $! > /tmp/rk-stage/pid
  for i in $(seq 1 40); do curl -sf -m 2 http://127.0.0.1:8099/healthz >/dev/null && return 0; sleep 0.5; done
  echo "staging router did not start"; cat /tmp/rk-stage/uvicorn.log; exit 1
}
stop() { kill "$(cat /tmp/rk-stage/pid)" 2>/dev/null || true; sleep 1; }
cp router-app.py app.py
/opt/llm-router/venv/bin/python router-keys.py --keys-file /tmp/rk-stage/keys.json add --name stage-key \
  --aliases qwen3.8-nothink --ttl-hours 1 --out /tmp/rk-stage/stage.key >/dev/null
SK="$(cat /tmp/rk-stage/stage.key)"
code() {  # code <key> <method> <path> [json]
  if [ -n "${4:-}" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X "$2" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d "$4" "http://127.0.0.1:8099$3"
  else
    curl -s -o /dev/null -w '%{http_code}' -X "$2" -H "Authorization: Bearer $1" "http://127.0.0.1:8099$3"
  fi
}
CHAT='{"model":"qwen3.8-nothink","max_tokens":4,"messages":[{"role":"user","content":"Reply with the single word: ready"}]}'
echo "== RESERVED_SLOTS=0 (lane open even in 1-slot mode, to exercise the allowed path)"
run 0
echo "owner chat            $(code "$OWNER" POST /v1/chat/completions "$CHAT")   want 200"
echo "scoped chat           $(code "$SK" POST /v1/chat/completions "$CHAT")   want 200"
echo "scoped other alias    $(code "$SK" POST /v1/chat/completions '{"model":"qwen3.8-think","max_tokens":4,"messages":[{"role":"user","content":"x"}]}')   want 403"
echo "scoped server tools   $(code "$SK" POST /v1/chat/completions '{"model":"qwen3.8-nothink","tool_execution":"server","max_tokens":4,"messages":[{"role":"user","content":"x"}]}')   want 403"
echo "scoped embeddings     $(code "$SK" POST /v1/embeddings '{"model":"x","input":"x"}')   want 403"
echo "scoped models         $(code "$SK" GET /v1/models)   want 200"
echo "bad key               $(code wf_nope POST /v1/chat/completions "$CHAT")   want 403"
printf '{not json' > /tmp/rk-stage/keys.bad && cp /tmp/rk-stage/keys.json /tmp/rk-stage/keys.good && mv /tmp/rk-stage/keys.bad /tmp/rk-stage/keys.json
echo "malformed file scoped $(code "$SK" POST /v1/chat/completions "$CHAT")   want 403"
echo "malformed file owner  $(code "$OWNER" GET /v1/models)   want 200"
mv /tmp/rk-stage/keys.good /tmp/rk-stage/keys.json
/opt/llm-router/venv/bin/python router-keys.py --keys-file /tmp/rk-stage/keys.json revoke --name stage-key >/dev/null
echo "revoked scoped        $(code "$SK" POST /v1/chat/completions "$CHAT")   want 403"
grep -c '"principal": "stage-key"' /tmp/rk-stage/access.log | sed 's/^/access-log stage-key lines: /'
stop
echo "== RESERVED_SLOTS=1 in 1-slot mode (lane closed)"
/opt/llm-router/venv/bin/python router-keys.py --keys-file /tmp/rk-stage/keys.json add --name stage-key2 \
  --aliases qwen3.8-nothink --ttl-hours 1 --out /tmp/rk-stage/stage2.key >/dev/null
SK2="$(cat /tmp/rk-stage/stage2.key)"
run 1
echo "scoped chat (closed)  $(code "$SK2" POST /v1/chat/completions "$CHAT")   want 503"
echo "owner chat            $(code "$OWNER" POST /v1/chat/completions "$CHAT")   want 200"
stop
EOF
scp -q /tmp/rk_stage.sh root@192.168.6.175:/tmp/rk_stage.sh && rm -f /tmp/rk_stage.sh
```

- [ ] **Step 3: Run it, and confirm production was not touched**

```bash
ssh root@192.168.6.175 'pct push 153 /tmp/rk_stage.sh /tmp/rk-stage/stage.sh --perms 0700 && rm -f /tmp/rk_stage.sh && pct exec 153 -- /tmp/rk-stage/stage.sh; pct exec 153 -- systemctl is-active llm-router; pct exec 153 -- test -e /etc/router-keys.json && echo "PROD KEYS FILE EXISTS (unexpected before deploy)" || echo "prod keys file absent (expected)"'
```

Expected:
- every line shows its `want` value;
- `access-log stage-key lines` is at least 1;
- `llm-router` stays `active`;
- the production keys file is absent.

Any mismatch: stop, fix it test-first on the branch (Task 2 Step 2), then rerun this task.

- [ ] **Step 4: Clean up the staging directory**

```bash
ssh root@192.168.6.175 'pct exec 153 -- rm -rf /tmp/rk-stage && pct exec 153 -- test ! -e /tmp/rk-stage && echo clean'
```

Expected: `clean`. This also shreds the staging keys, which never left LXC 153 and were revoked or expire
within 1 h.

---

### Task 4: Deploy to production with rollback, and verify live

**Files:** none in the repo. On the host: `/root/router-rollback.sh` in LXC 153 and
`/opt/llm-router.bak-<ts>`.

**Interfaces:** consumes the reviewed, staged branch. Produces the live router with scoped keys and the
reserved lane.

- [ ] **Step 1: Get the branch onto `main`**

The user merges and pushes, or approves a local merge into `main`. In the local case, carry the commits to
the host by bundle as on 2026-10-07:
- `git bundle create … <host HEAD>..main`;
- verify the bundle;
- on the host, `git fetch <bundle> main:refs/remotes/bundle/main` then `git merge --ff-only bundle/main`.

**Stop if the host checkout is not clean** (untracked `*.bak*` files are fine) or cannot fast-forward.

- [ ] **Step 2: Back up the live router and write the rollback script**

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
cat > /tmp/router-rollback.sh <<EOF
#!/bin/sh
# Restore /opt/llm-router to the pre-deploy backup $TS (scoped keys + lane removed).
set -eu
rm -rf /opt/llm-router.failed && mv /opt/llm-router /opt/llm-router.failed
cp -a /opt/llm-router.bak-$TS /opt/llm-router
echo "restored /opt/llm-router from /opt/llm-router.bak-$TS"
EOF
scp -q /tmp/router-rollback.sh root@192.168.6.175:/tmp/router-rollback.sh && rm -f /tmp/router-rollback.sh
ssh root@192.168.6.175 "pct exec 153 -- cp -a /opt/llm-router /opt/llm-router.bak-$TS && pct push 153 /tmp/router-rollback.sh /root/router-rollback.sh --perms 0700 && rm -f /tmp/router-rollback.sh && pct exec 153 -- ls -d /opt/llm-router.bak-$TS"
```

Expected: the backup directory is listed.

- [ ] **Step 3: Deploy**

```bash
ssh root@192.168.6.175 'cd /root/local-gpu-cluster && bash scripts/53-lxc-router.sh > /root/router-deploy-scoped-keys.log 2>&1; echo "deploy rc=$?"; pct exec 153 -- systemctl is-active llm-router; pct exec 153 -- stat -c "%U:%G %a" /etc/router-keys.json /usr/local/sbin/router-keys; pct exec 153 -- cat /etc/router-keys.json'
```

Expected:
- `deploy rc=0` and `active`;
- `root:router 640` for the keys file and `root:root 750` for the CLI;
- the keys file holds `{"keys": []}`.

- [ ] **Step 4: Verify owner behaviour is unchanged**

Push a verify script as a file, as on 2026-10-07. It must check:
- `/healthz` reports chat, embed and rerank `ok`;
- `GET /v1/models` with the owner key lists **7** aliases, including `qwen3.8-nothink`;
- an owner chat on `qwen3.8-nothink` answers;
- the deployed `app.py`, `access_keys.py` and `workforce_lane.py` sha256 match the host checkout.

Expected: all true. **On any failure, run `/root/router-rollback.sh`, restart, and verify again.**

- [ ] **Step 5: Verify scoped behaviour live with a 1-hour probe key, then revoke it**

```bash
ssh root@192.168.6.175 'pct exec 153 -- router-keys add --name deploy-probe --aliases qwen3.8-nothink,devstral --ttl-hours 1 --out /root/deploy-probe.key'
```

Then push and run a probe script that reads `/root/deploy-probe.key` and checks:
- in 1-slot mode, scoped chat returns **503** `workforce_lane_closed`;
- scoped `/v1/embeddings` returns **403** `forbidden`;
- a scoped `devstral` request (not loaded) returns **409** and never swaps (`/healthz` `active_chat_profile` unchanged);
- the access log gains a `"principal": "deploy-probe"` line.

Then:

```bash
ssh root@192.168.6.175 'pct exec 153 -- router-keys revoke --name deploy-probe && pct exec 153 -- shred -u /root/deploy-probe.key && pct exec 153 -- router-keys list'
```

Expected:
- every probe as listed;
- after revoke, the probe key gets 403;
- `list` prints nothing.

- [ ] **Step 6: Review Focus 2, capacity shrink, checked once in 3-slot mode**

This needs a window when the user agrees to 3-slot mode briefly. Steps:
1. Enter 3-slot mode, `redteam-mode-enter.sh`.
2. Issue a probe key and start one scoped streaming request.
3. Exit 3-slot mode, `redteam-mode-exit.sh`, then confirm:
   - the in-flight stream completed or was cut by the chat restart, not hung;
   - a new scoped request returns 503;
   - the owner chats normally.
4. Revoke the key.

If the user declines the window, record it as deferred to Plan D's first 3-slot run.

- [ ] **Step 7: Record the deployment**

Append to `docs/router-keys-operator-runbook.md` the deploy timestamp, the backup path and the rollback
command. Commit it by path on `main` or the branch. Tell the user to delete `/opt/llm-router.bak-<ts>` once
satisfied.

---

## Self-review (done while writing)

- **Spec coverage.**

  | Spec item | Where covered |
  |---|---|
  | §5.3 scoped keys: alias allowlist, expiry, no server tools, revocation | Tasks 1, 3, 4 |
  | §5.4 reserved lane | Tasks 1, 3, 4 |
  | §5.4 no swap for scoped keys | Tasks 1, 4.5 |
  | §5.4 test-first, security review, rollback, live alias verification | Tasks 1–4 |

  The spec says "no production key enters the VM". That is satisfied by issuing scoped keys; distributing
  them belongs to Plan B/D.
- **Placeholder scan.** Task 4 Steps 4–5 describe verify scripts instead of inlining them. Their exact
  assertions are listed, and they follow the pushed-script pattern proven on 2026-10-07. This is accepted,
  because inline nested quoting is the documented failure mode.
- **Type consistency.** These names match the appendix code: `KeyStore.authenticate`, `Principal.is_owner`,
  `chat_violation`, `apply_chat_policy`, `may_swap`, `ReservedLane.capacity` / `.in_use`, `gate_for` and
  `LaneClosed`. So do the HTTP codes and error strings: `workforce_lane_closed`, `forbidden`,
  `model_not_allowed` and `server_tools_not_allowed`.
- **Review Focus.** All five lines are pinned by a test or a live step.
