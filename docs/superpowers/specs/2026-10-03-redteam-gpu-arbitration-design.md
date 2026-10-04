# Mode-aware GPU arbitration (R2) — design

**Status:** draft for review · **Date:** 2026-10-03
**Path:** architectural (brainstorming → spec → plan)
**Scope:** prerequisite R2 of the redteam-integration track. R1 (orchestrator
auth) and R3 (secret scrubber) are separate specs; the Claude→MCP→157 integration
remains deferred to its own spec after the local-delegate Phase-1 go/no-go
(`2026-10-02-local-delegate-mcp-design.md` §10).

## 1. Intent

**Outcome:** an engagement (redteam-mode) and a local-delegate job never fight the
single chat GPU slot. **Policy (decided):** engagements win. A delegate job will
not start against an active engagement, and an in-flight delegate job is preempted
after a short grace drain when an engagement begins. Delegate jobs are already
designed preemptible/resubmittable (`profile_changed`/`upstream_restart`), so
preemption costs a resubmit, not lost correctness.

**Why now:** R2 is a redteam prerequisite and is the piece with genuinely
delegatable, non-security-sensitive subtasks (the delegate-side guard + its tests),
so it can also feed the Phase-1 A/B experiment.

## 2. Ground truth (verified by running, 2026-10-03)

- **`active_chat_profile` stays `"qwen3.8"` during redteam-mode.** `redteam-mode-enter`
  applies a chat drop-in with `--alias "qwen3.8"` (3 slots × 128K, same Qwen3.8-27B
  model), so `/healthz` reports `active_chat_profile: "qwen3.8"` throughout an
  engagement. **The profile string cannot signal an engagement.**
- **Authoritative engagement marker exists:** `/run/redteam-mode.state` on LXC 151 —
  `redteam-mode-enter.sh` writes `active`; `redteam-mode-exit.sh` `rm -f`s it. Enter
  also **stops embed+rerank** (so `/healthz upstream.embed/rerank` go non-`ok`) and
  **restarts `llamacpp-chat`** with no delegate awareness and no drain. Exit restarts
  chat (base 1-slot/256K), restarts embed+rerank, restores the GPU-defrag timer.
- **The delegate is localhost-only** (`LOCAL_DELEGATE_HOST=127.0.0.1`): the cluster
  cannot poll it. A shared signal must reach the delegate via the router `/healthz`,
  which the delegate already queries.
- **Delegate guard today is one-directional and incomplete:** `router_client.ask_local`
  checks `active_chat_profile == "qwen3.8"` → `ProfileBusy` (so it backs off for a
  *coder* swap, which does change the alias). The **agentic job path has no guard at
  all** (`jobs.py` only acquires the local `GpuLease`), so a delegated job would run
  against an engagement-reconfigured slot.
- **Router code is in-repo:** `scripts/files/router-app.py` builds `/healthz`;
  `scripts/files/swap-webhook.py` is prior art for an authenticated router-side
  setter endpoint.
- **In-flight count for the drain-wait:** the chat server runs `--metrics` (enabled),
  so `/metrics` exposes a processing gauge; `/slots` is not enabled (`--slots` absent)
  and both require the API key. The drain-wait therefore runs **inside
  `redteam-mode-enter`** (which has `pct exec` into 151 and the key), querying
  `/metrics` — not from the delegate.
- **Coder profile swaps** remain covered by the existing `active_chat_profile`
  check (the alias changes to the coder profile); R2 keeps that check and *adds* the
  engagement signal, so the delegate backs off on EITHER condition.

## 3. Scope

**In:** (A) the `redteam_mode` signal — a boolean in router `/healthz`, set via an
authenticated router setter that `redteam-mode-enter`/`-exit` call; (B) the
delegate-side guard keyed on `redteam_mode` (OR profile≠qwen3.8), applied to BOTH
`ask_local` and the agentic job dequeue; (C) a grace drain + preempt in
`redteam-mode-enter` before the chat restart. Unit tests for the delegate guard and
the router field; `bash -n` for the mode scripts.

**Out / deferred:** R1, R3, the Claude integration; multi-GPU; changing the
preemption policy; arbitrating workloads other than delegate vs redteam-mode; a
distributed lock (the preempt policy makes mutual exclusion unnecessary).

## 4. Design

### 4.1 Part C — the `redteam_mode` signal (router + mode scripts)

- **Router `/healthz`** (`scripts/files/router-app.py`) gains a boolean
  `redteam_mode` (default `false`), held in router process state.
- **Setter:** a small authenticated endpoint (e.g. `POST /internal/redteam-mode
  {"active": true|false}`), guarded by the router's existing auth (same bearer the
  router already requires), following `swap-webhook.py`'s pattern. It sets the
  in-memory flag and is idempotent.
- **`redteam-mode-enter.sh`** POSTs `active:true` **before** it reconfigures; 
  **`redteam-mode-exit.sh`** POSTs `active:false` after it restores. Both already run
  on the host with cluster credentials; they gain the router URL + token (from the
  cluster env/config, not hard-coded). A failed POST **must not block the
  engagement** — enter/exit log loudly and proceed (the embed/rerank-down state is a
  secondary observable backstop, but R2 does not depend on it).
- Rationale for in-memory (not reading 151's file): the router (LXC 153) reaches 151
  only over HTTP and cannot read its filesystem; an explicit push from the
  authoritative actor (the mode scripts) is the least-mechanism robust path.

### 4.2 Part A — delegate yields (both paths)

- A single helper resolves the signal from `/healthz`: **engagement/busy = true when
  `redteam_mode == true` OR `active_chat_profile != "qwen3.8"`.**
- `ask_local` keeps its pre-call check, switched to this helper (so it now also
  catches redteam-mode, which it currently misses).
- **The agentic job path gains the same check at dequeue** (`jobs.py`, when the
  worker picks a job, before/after acquiring the local lease): if the signal says
  busy, the job does **not** run the model — it is recorded `profile_changed`
  (terminal, resubmittable), not `failed`. This is the main new delegate-side code.
- **Fail-safe:** if `/healthz` is unreachable or the field is missing/unparseable,
  treat it as busy (do not dispatch) — protecting the engagement is the safe
  default, and it matches `ask_local` already erroring on a `/healthz` failure.

### 4.3 Part B — grace drain + preempt in `redteam-mode-enter`

- Before `systemctl restart llamacpp-chat`, enter queries the chat server's
  `/metrics` (with the key, via `pct exec 151`) for the in-flight/processing request
  count and **waits a bounded grace window** (e.g. up to ~30–45s, a few polls) for it
  to reach 0. It POSTs `redteam_mode:true` (Part C) **first**, so no *new* delegate
  job starts during the drain.
- If the window expires with work still in flight, enter **proceeds anyway**
  (preempt): the restart drops the in-flight request; the delegate job ends as
  `upstream_restart`/`profile_changed` and is resubmittable. The engagement is never
  blocked indefinitely by delegate work.
- This mirrors the courtesy enter already extends to the GPU-defrag restart timer
  (which it inhibits for the same "don't kill mid-stream" reason).

## 5. Testing

- **Delegate unit (this repo, pytest), the delegatable/A/B-eligible slice:**
  - the signal helper: busy when `redteam_mode:true`; busy when profile≠qwen3.8;
    not-busy only when `redteam_mode:false` AND profile==qwen3.8; busy (fail-safe) on
    unreachable/missing/malformed `/healthz` — all with a fake HTTP `/healthz`.
  - `ask_local` backs off (`ProfileBusy`) when the helper says busy (incl. the
    redteam-mode case it currently misses).
  - the job path records `profile_changed` (terminal, resubmittable, lease released)
    — not `failed` — when the helper says busy at dequeue; and runs normally when
    not busy. Assert on recorded state, not timing (repo lesson).
- **Router unit:** `/healthz` includes `redteam_mode` (default false); the setter
  flips it and requires auth (401 without the bearer). Follow `swap-webhook.py`/
  existing router tests.
- **Scripts:** `bash -n` (and `shellcheck` if available) for enter/exit; a dry check
  that the POST uses the configured URL+token and that a POST failure does not abort.
- **Live (opt-in, maintenance window):** with a scripted in-flight delegate request,
  run `redteam-mode-enter` and confirm the drain waits then preempts, the delegate
  job comes back `profile_changed`/`upstream_restart` and resubmits, and a job
  submitted *during* mode is refused; `redteam-mode-exit` clears `redteam_mode` and
  the delegate resumes.

## 6. Failure handling

| Situation | Behaviour |
|---|---|
| `/healthz` unreachable/malformed when delegate checks | Treat as busy — do not dispatch (fail-safe toward the engagement). |
| Router setter POST fails in enter/exit | Log loudly, **proceed** — never block/strand an engagement on a flag write. `redteam_mode` may be briefly stale; the drain + the delegate's own next `/healthz` read limit the window. |
| Drain window expires with work in flight | Proceed (preempt); delegate job → resubmittable. |
| Router restarts (in-memory flag lost) | Flag resets to false; if an engagement is live, the next enter is idempotent but won't re-POST — see §7 open item (consider exit/enter re-assert or a short TTL). |
| Coder profile loaded (not redteam-mode) | Existing `active_chat_profile != qwen3.8` branch handles it unchanged. |

## 7. Open items carried into planning

- **Router-flag durability across a router restart.** The flag is in-memory; a router
  bounce mid-engagement would drop it to false and let the delegate dispatch into the
  engagement. Options: have `redteam-mode-watch`/idle-check re-assert the flag
  periodically while state is `active`, or persist it router-side. Decide at planning;
  low-probability but worth a cheap guard.
- Exact `/metrics` field name for in-flight count (confirm against the running
  server at implementation; e.g. a `*_requests_processing`/slots-busy gauge).
- The router setter's exact path/auth shape — align with `swap-webhook.py`.

## 8. A/B eligibility note

Part A (the delegate signal helper + guard + its unit tests) is in-repo,
non-security-sensitive, and the kind of well-specified, test-backed change the
delegation experiment targets — a genuine A/B candidate (flip the coin at
implementation). Part C (router) and Part B (host mode scripts) are
infra/coordination and security-adjacent; treat as self-done.
