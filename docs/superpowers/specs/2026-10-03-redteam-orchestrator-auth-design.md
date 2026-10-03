# Redteam orchestrator access hardening (R1) — design

**Status:** draft for review · **Date:** 2026-10-03
**Path:** architectural (brainstorming → spec → plan)
**Scope:** prerequisite R1 of the redteam-integration track. R2 (mode-aware GPU
arbitration) and R3 (secret scrubber) are separate specs. The Claude→MCP→157
integration itself remains deferred to its own spec after the local-delegate
Phase-1 go/no-go (see `2026-10-02-local-delegate-mcp-design.md` §10).

## 1. Intent

**Outcome:** the RedteamAgent orchestrator on LXC 157 (`:18000`) can only be
reached and used by the workstation, and nobody can self-provision access to it.
This is standalone defensive hardening; it does not depend on, and must not
build any part of, the deferred Claude integration.

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
- **Gap 2 — LAN bind.** uvicorn binds `0.0.0.0` (while `config.py` defaults
  `orchestrator_public_url` to `127.0.0.1:18000`), so the whole LAN can reach the
  port. The bind cannot simply move to loopback: dispatched agent containers call
  the orchestrator back via `host.docker.internal:18000` (`orchestrator_container_url`),
  and those callbacks arrive on 157's docker bridge, not loopback.
- **No current external consumer.** `ss` on 157 shows no active connection to
  `:18000`, so restricting reach to the workstation breaks nothing today.
- **Workstation:** `192.168.6.226` (Wi-Fi, **DHCP** — see §8 open items).
- **Deploy pattern precedent:** `62-memory-vault-bridge.sh` (pct push + restart),
  `71-/73-` firewall scripts (per-guest PVE `*.fw`, idempotent, verify-after).

## 3. Scope

**In:** a PVE firewall limiting `:18000` to the workstation; a tracked, idempotent
close of open registration; one provisioned service user; behavioral verification.

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
IN ACCEPT -p tcp -dport 22                         # SSH / pct console path
IN ACCEPT -p tcp -dport 18000 -source 192.168.6.226 # orchestrator: workstation only
# all other inbound dropped by policy_in
```

- `policy_in: DROP` with explicit allows is stronger than enumerating denies (the
  lesson from the VM 172/170 work: deny-by-default doesn't go stale when a port is
  added). SSH stays open so the pct/console path is unaffected.
- The datacenter firewall switch (`cluster.fw`) is already enabled (from the VM
  170/172 work). The deploy script asserts that only 157 — plus the already-filtered
  VMs 170/172 — opt into filtering, mirroring the blast-radius guard those scripts
  use, before relying on the switch.
- Bind stays `0.0.0.0`: container callbacks on the docker bridge are internal to
  157 and not filtered by the host-level PVE firewall on the veth, so they continue
  to work; only LAN-side inbound is gated.
- Verify via `fwbr157i*` presence, not config flags (same as 71/73).

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
- `os` and `status` are already imported in the module (verify at implementation;
  add the import to the insert only if absent).
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
so no HTTP registration is needed and the guard stays closed throughout. It is
idempotent (if the user exists, it is a no-op). The password is generated at
provision time and recorded only in the workstation's user environment
(`REDTEAM_ORCH_USER` / `REDTEAM_ORCH_PASSWORD`), never committed — consistent with
the `LOCAL_DELEGATE_*` secret handling. R1 only ensures a usable credential exists
and that `login` returns a token; the bridge's use of it is the deferred
integration's concern.

### 4.4 Deploy & drift — `scripts/67-redteam-orchestrator-harden.sh`

Idempotent, following `62-`/`71-`/`73-` conventions:

1. Preflight: 157 running; `auth.py`, `run.sh` present; `pve-firewall` available.
2. Blast-radius guard: assert no guest other than 157/170/172 has `firewall=1`.
3. `pct push` `provision_user.py`; apply the marker-guarded `auth.py` insert.
4. Write `/etc/pve/firewall/157.fw`; `pve-firewall compile`/`restart`; confirm
   `enabled/running` and `fwbr157i*` present; no stray filter bridges.
5. Run `provision_user.py` once (direct `db.create_user`, so the registration flag
   is never needed); restart the orchestrator (`run.sh` stop/start) with
   `REDTEAM_ALLOW_REGISTRATION` unset.
6. Run the §5 verification probes; fail loudly (and print a rollback recipe) if any
   control is not in place — never leave the service half-hardened silently.

Transfer scripts via `pct push`/base64, never inline heredocs (the nested-quoting
footgun in AGENTS.md).

## 5. Testing / verification

- **Unit (this repo, pytest):** the guard-insert transform is idempotent — given an
  `auth.py` with and without the marker, applying it once inserts, twice is a no-op;
  and the inserted guard is syntactically valid Python. `bash -n` on the deploy
  script.
- **Behavioral, settled by running (not asserted):** after deploy —
  - `POST /auth/register` (workstation) → **403** "registration disabled";
  - provisioned user `POST /auth/login` → **200** + token; a protected route with
    that token → 200; without a token → **401** (unchanged);
  - `:18000` reachable from the workstation; **refused** from another LAN host
    (e.g. a probe from LXC 156);
  - `/healthz` still **200**.
- The verification runs inside the deploy script and is also re-runnable standalone.

## 6. Failure handling

| Situation | Behaviour |
|---|---|
| `auth.py` structure changed upstream (marker absent, insert point gone) | Deploy fails loudly naming the file; do not guess an insert point. |
| `pve-firewall` not enabled/running after restart | Fail; 157 would otherwise be unconfined. Print rollback. |
| Provisioning can't reach the DB / user already exists | create_user is a no-op if present; a real DB error fails the deploy. |
| Workstation IP changed (DHCP) | `:18000` becomes unreachable from the workstation; §8 addresses with a reservation. |

## 7. Isolation / clarity

Three small, independently testable units: the firewall file (declarative), the
marker-guarded source transform (pure string op, unit-tested), and the provisioning
script (idempotent DB write). The deploy script composes them and verifies. None
depends on the deferred integration.

## 8. Open items carried into planning

- **Workstation IP is DHCP (Wi-Fi, `192.168.6.226`).** Prefer a DHCP reservation on
  the router, or allow the workstation's reserved address, so the firewall rule does
  not silently strand access on lease change. Decide at planning.
- Whether `os`/`status` are already imported in `auth.py` (adjust the insert).
- Whether to also set `--host` to a specific address in addition to the firewall
  (belt-and-braces); default is firewall-only, bind unchanged, per §4.1.
