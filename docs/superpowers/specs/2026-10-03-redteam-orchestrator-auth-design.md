# Redteam orchestrator access hardening (R1) — design

**Status:** revised after adversarial review (3 reviewers, verified live) · **Date:** 2026-10-03
**Path:** architectural (brainstorming → spec → plan)
**Scope:** prerequisite R1 of the redteam-integration track. R2 (mode-aware GPU
arbitration) and R3 (secret scrubber) are separate specs. The Claude→MCP→157
integration itself remains deferred to its own spec after the local-delegate
Phase-1 go/no-go (see `2026-10-02-local-delegate-mcp-design.md` §10).

## 1. Intent

**Outcome:** close the orchestrator's **LAN** attack surface — no host other than
the workstation can reach `:18000`, and nobody can self-provision access through the
HTTP API. This is standalone defensive hardening; it does not depend on, and must
not build any part of, the deferred Claude integration.

**Explicitly not in R1's reach (residual, see §9):** the orchestrator's own design
hands full 24-hour API tokens to every dispatched agent container, which reach the
API over `docker0` — a plane no network/registration control can gate. R1 narrows
its claim to the LAN boundary; the container token plane is documented as residual
and left to 157's egress lock + R3/the integration spec.

**Why now:** R1 is the first redteam prerequisite and closes a live exposure. It
is security-sensitive (auth/network boundary), so it is implemented directly, not
delegated to the local model.

## 2. Ground truth (verified by running, 2026-10-02/03)

The §10 premise — "157:18000 is LAN-open, no auth" — is **false as written**, and
an earlier "wide open" probe was an artifact of hitting the SPA catch-all. Settled
by probing the real routes:

- The orchestrator is a third-party clone at `/root/RedteamAgent/orchestrator`
  inside LXC 157: a FastAPI backend (`uvicorn app.main:app --host 0.0.0.0 --port
  18000`, launched by `run.sh`) plus a built SPA frontend served by the backend.
- **Auth exists and is enforced.** `app/security.py` implements PBKDF2 password
  hashing + DB-backed session tokens and a `get_current_user` bearer dependency
  (`CurrentUser`). Every engagement router (`/projects`, `/projects/{id}/runs`,
  `/auth/me`, cases, dispatches, events, artifacts, documents) returns **401**
  without a valid token. `/healthz` is public (correct). Routers are mounted at
  **root** (no `/api` prefix); a catch-all `@app.get("/{full_path:path}")` serves
  the SPA, which is why any `/api/*` or unknown GET returns 200 HTML.
- **Gap 1 — open registration.** `POST /auth/register` (`app/api/auth.py`) takes
  only a request body: no `CurrentUser`, no first-user-only, no invite, no role or
  env gate. Anyone who can reach `:18000` can register, log in, and receive a
  full-access session token — defeating the enforced auth.
- **Gap 2 — LAN bind, IPv4-only.** uvicorn binds `0.0.0.0:18000` (while `config.py`
  defaults `orchestrator_public_url` to `127.0.0.1:18000`), so the whole LAN can
  reach the port. There is **no `[::]:18000` listener** (IPv4 only, confirmed by
  `ss -tlnp`), so an IPv4 firewall rule is sufficient — IPv6 is not a bypass. The
  bind cannot simply move to loopback: dispatched agent containers call the
  orchestrator back via `host.docker.internal:18000` → `172.17.0.1` on 157's
  `docker0`, internal to 157's netns; those callbacks never cross the LAN veth, so
  the PVE firewall will not filter them (verified by `docker inspect`).
- **157's `net0` has `firewall=0`** (`/etc/pve/lxc/157.conf`). This is load-bearing:
  a PVE per-guest `.fw` does nothing until the NIC opts in with `firewall=1`, and on
  a running container the filter bridge (`fwbr157i0`) only materializes after a
  **reboot**. VMs 170/172 already carry `firewall=1`; 157 does not. See §4.1.
- **Live consumers exist right now** (corrects an earlier snapshot): `ss` shows
  established connections from the workstation `192.168.6.226` to `:18000`, and a
  dispatched engagement container `redteam-orch-run-0022` is running. Both are
  intended consumers — the workstation (allowed by the rule) and docker-internal
  callbacks (unfiltered) — so restricting LAN reach breaks neither; but a restart or
  reboot *does* disrupt the live engagement (see §4.4, §6).
- **Auth internals confirmed:** `register()` is the **only** HTTP user-creation
  route (login only issues sessions for existing users; `/auth/ws-ticket` and the
  `/ws/...` websocket both require `CurrentUser`). `app/api/auth.py` imports
  `status` but **not `os`** — the §4.2 guard must add `import os`. The engagement
  routers `cases/dispatches/events/artifacts/documents` are **nested** under
  `/projects/{id}/runs/{id}/…`, so a bare `GET /cases` hits the SPA catch-all (200);
  only the real nested paths 401 — do not re-probe bare paths and conclude "open."
- **Unauthenticated schema disclosure:** `/openapi.json` and `/docs` return the full
  API schema without a token (real routes, not the SPA). Enumerable by anyone who
  can reach `:18000`; mitigated to workstation-only once Part A works (see §9).
- **DB state (read-only audit):** `users` = exactly `1|mavlite` (no rogue
  registration during the LAN-open window), but `sessions` holds ~32 live,
  non-expired rows — a restart revokes nothing (see §4.4 session purge).
- **`run.sh` restart is heavier than "stop/start" implies:** on every start it runs
  `pip install -e` (editable — so the `auth.py` edit *is* picked up on restart, no
  reinstall) and `npm run build`, and has an "already running" short-circuit. On an
  **egress-locked** box a pip/npm fetch can fail and leave the service *down*, and a
  bare `run.sh` exits without applying the edit — the restart must be
  `stop.sh` **then** `run.sh`, with deps pre-staged/offline.
- **Egress lock is in-container iptables** (`/usr/local/sbin/redteam-egress-lock.sh`,
  `OUTPUT`/`DOCKER-USER` allow-list then `-o eth0 REJECT`), **not** a PVE firewall
  file — so a new inbound `157.fw` with `policy_out: ACCEPT` is a different layer and
  will not clobber it. (Confirm the egress posture does not depend on `net0.firewall`
  staying `0` before flipping it to `1`.)
- **Workstation:** `192.168.6.226` (static `157` itself; the workstation is Wi-Fi
  **DHCP** — the rule *source* is the DHCP risk, see §9).
- **Deploy pattern precedent:** `62-memory-vault-bridge.sh` (pct push + restart),
  `71-/73-` firewall scripts (per-guest PVE `*.fw`; both **die** if the target NIC
  lacks `firewall=1` — the pattern R1 must follow).

## 3. Scope

**In:** a PVE firewall limiting `:18000` and `:22` to the workstation (including the
`net0 firewall=1` flip + 157 reboot it requires); a tracked, idempotent close of open
registration; one provisioned service user; a session purge of the LAN-open window;
behavioral verification; documented rollback.

**Out (other specs / deferred):** the Claude→MCP bridge and any credential wiring
for it; R2 GPU arbitration; R3 scrubber; any change to engagement/launcher logic;
TLS on `:18000`; moving the orchestrator off 157.

## 4. Design

Two independent controls (defense-in-depth); either alone is insufficient — the
firewall still leaves registration open to the workstation, and closing
registration still leaves the SPA/health and any future route on the LAN.

### 4.1 Part A — network reach (PVE firewall on LXC 157)

A `/etc/pve/firewall/157.fw`, same shape as `73-vm-tester-firewall.sh`'s output:

```
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -p tcp -dport 22 -source 192.168.6.226    # SSH: workstation only
IN ACCEPT -p tcp -dport 18000 -source 192.168.6.226 # orchestrator: workstation only
# all other inbound dropped by policy_in
```

- **Enabling the NIC filter requires a reboot.** `157`'s `net0` has `firewall=0`, so
  the `.fw` is inert until the deploy sets `firewall=1` on `net0` in
  `/etc/pve/lxc/157.conf` (`pct set 157 --net0 ...,firewall=1`). On an already-running
  container the `fwbr157i0` bridge only appears after `pct reboot 157`. So Part A is
  **not** a hot change: it needs a **maintenance window with no engagement running**
  (see §4.4). The deploy must flip the flag, reboot, then confirm `fwbr157i0` exists
  — and **die** if it does not, exactly as `71-`/`73-` do (a `.fw` that filters
  nothing is the failure mode to guard against).
- `policy_in: DROP` with explicit allows is stronger than enumerating denies (the
  VM 172/170 lesson: deny-by-default doesn't go stale when a port is added).
- **SSH is scoped to the workstation**, not left LAN-open. `pct exec`/`pct console`
  from the host do not traverse the network, so restricting guest `sshd` to `.226`
  does not affect host management — it removes an unnecessary LAN exposure (an
  earlier draft's "SSH/pct path" rationale was wrong).
- The datacenter switch (`cluster.fw`) is already `enable: 1` (verified). The deploy
  asserts that only 157 — plus the already-filtered VMs 170/172 — opt into filtering,
  mirroring the blast-radius guard those scripts use.
- Bind stays `0.0.0.0`: container callbacks arrive on `docker0` (172.17.0.1), internal
  to 157's netns, and are not filtered by the host PVE firewall on the LAN veth — so
  dispatched agents keep working; only LAN-side inbound on `eth0` is gated (verified).
- Verify via `fwbr157i0` presence, not config flags (same as 71/73).

### 4.2 Part B — close registration (minimal, tracked guard)

Insert an env guard at the top of `register()` in
`/root/RedteamAgent/orchestrator/backend/app/api/auth.py`:

```python
    # local-gpu-cluster R1 hardening — self-service registration disabled by default
    if os.environ.get("REDTEAM_ALLOW_REGISTRATION") != "1":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="registration disabled")
```

- Smaller and more localized than swapping the launch target to a wrapper module
  (`run.sh` hard-codes `app.main:app` in two places, including a daemon spawn), and
  leaves the auth logic itself untouched save for the guard.
- **`import os` must be added** (confirmed absent; `status` is already imported). As
  written without it, the guard raises `NameError` → HTTP 500, not the 403 the §5
  verification asserts. The deploy adds the import (marker-guarded, idempotent).
- **Drift control:** the insert is applied by the deploy script (§4.4) guarded by a
  unique marker comment — present → skip, absent → insert — so it is idempotent and
  re-applied after any upstream pull. The lesson that bit this cluster was
  *untracked* drift (`host_checkout_drift`); a tracked, re-applied, verified patch
  is the mitigation, not a new instance of it.
- The service runs with `REDTEAM_ALLOW_REGISTRATION` unset, so registration is
  closed in normal operation.

### 4.3 Provisioning the service user

A one-shot script (`provision_user.py`, pushed into 157) calls the orchestrator's
own `db.create_user(username, password_hash, salt)` via `security.hash_password`,
so no HTTP registration is needed and the guard stays closed throughout.
`hash_password` returns `(salt, password_hash)` — **salt first** — and `create_user`
takes `(username, password_hash, salt)`; the script must preserve that order or
login silently breaks. `create_user` **raises** `db.UsernameAlreadyExistsError` on
an existing user (it does not skip), so idempotency means catching that and treating
it as success. The password is generated at
provision time and recorded only in the workstation's user environment
(`REDTEAM_ORCH_USER` / `REDTEAM_ORCH_PASSWORD`), never committed — consistent with
the `LOCAL_DELEGATE_*` secret handling. R1 only ensures a usable credential exists
and that `login` returns a token; the bridge's use of it is the deferred
integration's concern.

### 4.4 Deploy & drift — `scripts/67-redteam-orchestrator-harden.sh`

Idempotent, following `62-`/`71-`/`73-` conventions. **Deploy requires a maintenance
window** because Part A needs an LXC 157 reboot and Part B an orchestrator restart,
both of which disrupt a live engagement.

1. Preflight: 157 running; `auth.py`, `run.sh`, `config.py` present; `pve-firewall`
   available; `cluster.fw` `enable: 1`; blast-radius guard — assert no guest other
   than 157/170/172 has `firewall=1`.
2. **Engagement gate:** refuse to proceed if any `redteam-orch-run-*` container is
   running (`pct exec 157 -- docker ps`), since the reboot/restart would drop its
   in-flight callbacks. Print how to wait for it to finish.
3. `pct push` `provision_user.py`; apply the marker-guarded `auth.py` edits
   (`import os` + the register guard); run `provision_user.py` once.
4. Part B — restart the orchestrator **`stop.sh` then `run.sh`** (a bare `run.sh`
   short-circuits as "already running") with `REDTEAM_ALLOW_REGISTRATION` unset;
   confirm it comes back up (pip/npm are editable/offline — a failed fetch leaves it
   down, so verify `:18000` answers before continuing).
5. **Session hygiene:** audit the `users` table (expect only the intended user) and
   purge existing `sessions` rows, so any token minted during the LAN-open window is
   revoked rather than surviving the restart.
6. Part A — `pct set 157 --net0 ...,firewall=1`; write `/etc/pve/firewall/157.fw`;
   `pve-firewall compile`/`restart`; **`pct reboot 157`**; confirm `fwbr157i0`
   present and no stray filter bridges. **Die** if `fwbr157i0` is absent (the `.fw`
   would otherwise filter nothing).
7. Run the §5 verification probes; fail loudly if any control is not in place — never
   leave the service half-hardened silently.

**Rollback / uninstall** (success-path, documented and scripted, as `71-`/`73-` do):
remove the `auth.py` guard + `import os`, delete `/etc/pve/firewall/157.fw`, revert
`net0` to `firewall=0`, `pve-firewall restart`. Note the reboot and the session purge
are not reversible; the recipe restores the *access posture*, not prior sessions.

Transfer scripts via `pct push`/base64, never inline heredocs or parens in
`bash -lc` (the nested-quoting footgun in AGENTS.md).

## 5. Testing / verification

- **Unit (this repo, pytest):** the guard-insert transform is idempotent — given an
  `auth.py` with and without the marker, applying it once inserts, twice is a no-op;
  and the inserted guard is syntactically valid Python. `bash -n` on the deploy
  script.
- **Behavioral, settled by running (not asserted):** after deploy —
  - `POST /auth/register` (workstation) → **403** "registration disabled" (and note
    it is **403**, not a crash — the `import os` fix is what distinguishes them);
  - provisioned user `POST /auth/login` → **200** + token; a protected route with
    that token → 200; without a token → **401** (unchanged);
  - `fwbr157i0` bridge present; `:18000` and `:22` reachable from the workstation,
    **refused** from another LAN host (probe from LXC 156);
  - a dispatched container callback still succeeds (docker0 path unfiltered) — or, if
    no engagement is running, assert the path is reachable from inside 157;
  - purged sessions: a pre-deploy token (if any) no longer authenticates;
  - `/healthz` still **200**.
- The verification runs inside the deploy script and is also re-runnable standalone.

## 6. Failure handling

| Situation | Behaviour |
|---|---|
| A `redteam-orch-run-*` container is running | Refuse to deploy (the reboot/restart would corrupt the live engagement); wait for the window. |
| `fwbr157i0` absent after `firewall=1` + reboot | Fail loudly — the `.fw` filters nothing; do not report success. |
| Orchestrator doesn't come back after `stop`/`run` (pip/npm fetch failed on the egress-locked box) | Fail; `:18000` is down. Rollback and investigate deps; do not leave it down silently. |
| `auth.py` structure changed upstream (marker absent, insert point gone) | Deploy fails loudly naming the file; do not guess an insert point. |
| `pve-firewall` not enabled/running after restart | Fail; 157 would otherwise be unconfined. Print rollback. |
| Provisioning: user already exists | Catch `UsernameAlreadyExistsError` and treat as success; a real DB error fails the deploy. |
| Workstation IP changed (DHCP) | `:18000`/`:22` unreachable from the workstation; §9 addresses with a reservation (effectively mandatory). |

## 7. Isolation / clarity

Three small, independently testable units: the firewall file (declarative), the
marker-guarded source transform (pure string op, unit-tested), and the provisioning
script (idempotent DB write). The deploy script composes them and verifies. None
depends on the deferred integration.

## 8. Open items carried into planning

- **DHCP source IP is effectively a reservation requirement.** `157` is static, but
  the rule *source* `192.168.6.226` is a Wi-Fi DHCP lease. On lease change the
  workstation is silently stranded (DROP → connection refused, no signal); worse, if
  `.226` is reassigned to another host, that host inherits sole access. Add a DHCP
  reservation for the workstation before or as part of deploy — treat as required.
- Confirm the egress lock does not depend on `net0.firewall` staying `0` before
  flipping it to `1` (§2 says it is in-container iptables, independent — verify once
  more at implementation).
- Resolved by review and folded into §2/§4 (no longer open): `os` import absent (add
  it); IPv6 is not a bypass (no `[::]` listener, so no extra `--host` needed).

## 9. Residual risks (accepted, documented)

- **Container token plane (primary residual).** The launcher mints a 24h session
  token per run and injects it into each dispatched agent container, which reaches
  the full API over `docker0` — unreachable by any network/registration control. A
  subverted run is therefore an authenticated path R1 does not close. It is contained
  today by 157's **egress lock** (a run cannot exfiltrate off-box) and is the proper
  subject of R3 (scrubber) and the deferred integration spec (token TTL/scoping,
  least privilege). R1 explicitly does not address it.
- **Schema disclosure.** `/openapi.json` and `/docs` are unauthenticated; once Part A
  works they are workstation-only, which is deemed acceptable. Disabling them is a
  possible later hardening, out of R1 scope.
- **DHCP source** (see §8) — accepted only once a reservation is in place.
