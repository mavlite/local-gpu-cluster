# Redteam Orchestrator Access Hardening (R1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the LAN attack surface of the RedteamAgent orchestrator on LXC 157 — restrict `:18000`/`:22` to the workstation, disable HTTP self-registration, provision one service user, and purge stale sessions — without drifting the vendored app.

**Architecture:** Two controls. Part B (app-side, no reboot): a marker-guarded, idempotent source edit to the vendored `auth.py` adding `import os` + an env-gated `register()` guard, applied on 157; one user provisioned via the app's own `db.create_user`; a session purge; the orchestrator restarted `stop.sh` then `run.sh`. Part A (network-side, needs a reboot): set `net0 firewall=1`, write `/etc/pve/firewall/157.fw` (allow `:22`/`:18000` from the workstation only), `pct reboot 157`, verify `fwbr157i0`. A single idempotent deploy script `scripts/67-redteam-orchestrator-harden.sh` composes both behind an engagement gate, with a documented rollback.

**Tech Stack:** Bash (deploy orchestration), Python 3 (the guard-insert transform + provisioning script), Proxmox `pct`/`pve-firewall`, the vendored FastAPI orchestrator inside LXC 157.

**Spec:** `docs/superpowers/specs/2026-10-03-redteam-orchestrator-auth-design.md` (read it; this plan argues from it).

## Global Constraints

- **Target host:** Proxmox at `192.168.6.175`; orchestrator in **LXC 157** at `/root/RedteamAgent/orchestrator` (backend `…/backend/app`). Reach it as `ssh -o BatchMode=yes root@192.168.6.175 'pct exec 157 -- …'`.
- **Workstation (sole allowed LAN client):** `192.168.6.226`. A DHCP reservation for it is a prerequisite to deploy (spec §8/§9).
- **Quoting footgun (AGENTS.md):** nested ssh/pct quoting and parens in `bash -lc "…"` break. Transfer scripts via `pct push` or base64; never inline heredocs/parens remotely.
- **Do not drift the vendored app:** all edits to files under `/root/RedteamAgent` are marker-guarded and re-applied by the deploy script; nothing is hand-edited on the box.
- **Commits:** conventional commits, **no `Co-Authored-By` trailer** (AGENTS.md).
- **Checks before done:** `bash -n` every edited `.sh`; `shellcheck` if available; `python3 -m pytest scripts/redteam/tests -q` for the transform.
- **Execution staging:** Tasks 1–3 (the transform, the provisioning script, and the deploy script's non-destructive preflight/compose) can be built and tested now. Tasks 4–5 **run** against the box only in a **maintenance window with no `redteam-orch-run-*` container live** (Task 4 restarts the orchestrator; Task 5 reboots 157). Building and `bash -n`/shellcheck of their sections is not gated; live execution is.

## Review Focus

- **Deploy run while an engagement is live** → must refuse before any restart/reboot, not corrupt the run. (Task 3 engagement-gate step.)
- **Re-running the deploy** → idempotent: guard not double-inserted, user not duplicated, `.fw` rewrite is a no-op. (Task 1 transform tests; Task 2 exists-path; Task 6 re-run check.)
- **`firewall=1` set but `fwbr157i0` absent after reboot** → die loudly; a `.fw` that filters nothing must never be reported as success. (Task 5 verify step.)
- **Orchestrator fails to come back after `stop`/`run`** (pip/npm fetch fails on the egress-locked box) → detect `:18000` down and fail/rollback, not leave it down silently. (Task 4 come-back-up step.)
- **A pre-deploy session token still authenticates after hardening** → the session purge must revoke it. (Task 4 purge + verify step.)

---

### Task 1: Idempotent `auth.py` guard transform (in-repo, unit-tested)

**Files:**
- Create: `scripts/redteam/__init__.py` (empty package marker)
- Create: `scripts/redteam/apply_auth_guard.py`
- Test: `scripts/redteam/tests/__init__.py` (empty), `scripts/redteam/tests/test_apply_auth_guard.py`

**Interfaces:**
- Produces: `apply_guard(src: str) -> str` — returns `auth.py` text with `import os` and the register guard inserted; idempotent; raises `ValueError` if `def register(` is absent. `MARKER = "local-gpu-cluster R1 hardening"`. CLI: `python -m scripts.redteam.apply_auth_guard <path>` edits in place; `--check` prints "applied"/"needs-apply" and exits 0/1.

- [ ] **Step 1: Write the failing test**

```python
# scripts/redteam/tests/test_apply_auth_guard.py
import ast
import pytest
from scripts.redteam.apply_auth_guard import apply_guard, MARKER

SAMPLE = (
    "from __future__ import annotations\n"
    "from fastapi import APIRouter, HTTPException, status\n"
    "from .. import db\n"
    "router = APIRouter(prefix='/auth')\n"
    "\n"
    "@router.post('/register')\n"
    "def register(request: RegisterRequest) -> UserResponse:\n"
    "    if db.get_user_by_username(request.username) is not None:\n"
    "        raise HTTPException(status_code=400, detail='exists')\n"
    "    return _user_response(db.create_user(request.username))\n"
)

def test_inserts_import_and_guard_and_stays_valid_python():
    out = apply_guard(SAMPLE)
    assert "\nimport os\n" in out
    assert MARKER in out
    assert 'REDTEAM_ALLOW_REGISTRATION' in out
    ast.parse(out)  # still valid Python
    # guard sits inside register(), before the existing first statement
    body = out.split("def register(", 1)[1]
    assert body.index(MARKER) < body.index("if db.get_user_by_username")

def test_idempotent_applied_twice_equals_once():
    once = apply_guard(SAMPLE)
    assert apply_guard(once) == once

def test_does_not_duplicate_existing_import_os():
    src = SAMPLE.replace("import os\n", "")  # ensure absent
    src = "import os\n" + src
    out = apply_guard(src)
    assert out.count("\nimport os\n") + (1 if out.startswith("import os\n") else 0) == 1

def test_raises_when_register_absent():
    with pytest.raises(ValueError):
        apply_guard("from fastapi import status\n# no register here\n")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest scripts/redteam/tests/test_apply_auth_guard.py -q`
Expected: FAIL — `ModuleNotFoundError: scripts.redteam.apply_auth_guard`.

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/redteam/apply_auth_guard.py
"""Idempotently insert `import os` + an env-gated register guard into the
vendored orchestrator auth.py. Marker-guarded so re-applying is a no-op and
an upstream pull can be re-hardened by re-running (spec §4.2)."""
import re
import sys

MARKER = "local-gpu-cluster R1 hardening"

_GUARD = (
    "    # " + MARKER + " — self-service registration disabled by default\n"
    "    import os\n"
    "    from fastapi import HTTPException, status\n"
    "    if os.environ.get(\"REDTEAM_ALLOW_REGISTRATION\") != \"1\":\n"
    "        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,\n"
    "                            detail=\"registration disabled\")\n"
)


def apply_guard(src: str) -> str:
    if MARKER in src:
        return src  # already hardened
    lines = src.splitlines(keepends=True)
    # find the `def register(` line (single-line signature ending in ':')
    for i, line in enumerate(lines):
        if re.match(r"\s*def register\(", line) and line.rstrip().endswith(":"):
            insert_at = i + 1
            break
    else:
        raise ValueError("def register( not found — auth.py structure changed; refusing to guess")
    lines.insert(insert_at, _GUARD)
    out = "".join(lines)
    if not re.search(r"^import os$", out, re.MULTILINE):
        # top-level import too, so module-level references are safe even if the
        # function-local import above is ever refactored away
        out = "import os\n" + out
    return out


def _main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: apply_auth_guard.py <path> [--check]"); return 2
    path = argv[0]
    check = "--check" in argv[1:]
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if MARKER in src:
        print("applied"); return 0
    if check:
        print("needs-apply"); return 1
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(apply_guard(src))
    print("applied"); return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest scripts/redteam/tests/test_apply_auth_guard.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/redteam/__init__.py scripts/redteam/apply_auth_guard.py \
        scripts/redteam/tests/__init__.py scripts/redteam/tests/test_apply_auth_guard.py
git commit -m "feat(redteam): idempotent auth.py register-guard transform"
```

---

### Task 2: Provisioning script `provision_user.py` (runs inside 157)

**Files:**
- Create: `scripts/redteam/provision_user.py`

**Interfaces:**
- Produces: a standalone script run by the orchestrator's venv **inside 157** (cwd = `backend/`, so `from app import db, security` resolves). Reads `REDTEAM_ORCH_USER`/`REDTEAM_ORCH_PASSWORD` from env. Creates the user idempotently. Exit 0 on created-or-exists, non-zero on real error. It does NOT import anything from this repo (it runs in the vendored venv).

- [ ] **Step 1: Write the script** (no in-repo unit test — it imports the orchestrator's `app.db`, which lives only in 157; it is verified behaviorally in Task 4)

```python
# scripts/redteam/provision_user.py
"""Provision one orchestrator user via the app's own db + security modules.
Run INSIDE LXC 157 from the backend dir with the orchestrator venv:
  REDTEAM_ORCH_USER=… REDTEAM_ORCH_PASSWORD=… \
    /root/RedteamAgent/orchestrator/backend/.venv/bin/python provision_user.py
Idempotent: an existing username is treated as success (create_user raises
UsernameAlreadyExistsError, it does not skip — spec §4.3)."""
import os
import sys

from app import db, security  # resolved from backend/ cwd inside 157


def main() -> int:
    user = os.environ.get("REDTEAM_ORCH_USER")
    pw = os.environ.get("REDTEAM_ORCH_PASSWORD")
    if not user or not pw:
        print("REDTEAM_ORCH_USER and REDTEAM_ORCH_PASSWORD are required", file=sys.stderr)
        return 2
    salt, password_hash = security.hash_password(pw)  # salt FIRST (spec §4.3)
    try:
        db.create_user(user, password_hash, salt)     # (username, hash, salt)
        print("created", user)
    except db.UsernameAlreadyExistsError:
        print("exists", user)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Syntax-check locally**

Run: `python3 -m py_compile scripts/redteam/provision_user.py`
Expected: no output, exit 0. (Imports resolve only inside 157; do not run it here.)

- [ ] **Step 3: Commit**

```bash
git add scripts/redteam/provision_user.py
git commit -m "feat(redteam): idempotent orchestrator user provisioning script"
```

---

### Task 3: Deploy script — preflight, engagement gate, lib (non-destructive)

**Files:**
- Create: `scripts/67-redteam-orchestrator-harden.sh`

**Interfaces:**
- Consumes: `scripts/lib/common.sh` (`require_root`, `require_pve_host`, `step`, `ok`, `die`, `warn` — follow `71-`/`73-` usage).
- Produces: the deploy entrypoint. After this task it runs preflight + engagement gate and exits before any change. Later tasks append the mutating sections.

- [ ] **Step 1: Write the preflight + gate**

```bash
#!/usr/bin/env bash
# 67-redteam-orchestrator-harden.sh — R1: restrict LXC 157 orchestrator to the
# workstation and disable HTTP self-registration. See
# docs/superpowers/specs/2026-10-03-redteam-orchestrator-auth-design.md
#
# REQUIRES A MAINTENANCE WINDOW: Part B restarts the orchestrator and Part A
# reboots LXC 157; both abort if an engagement container is running.
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

CTID="${REDTEAM_CTID:-157}"
CT_IP="${REDTEAM_CT_IP:-192.168.6.157}"
WORKSTATION_IP="${REDTEAM_WS_IP:-192.168.6.226}"
ORCH_DIR="/root/RedteamAgent/orchestrator"
BACKEND="$ORCH_DIR/backend"
AUTH_PY="$BACKEND/app/api/auth.py"

require_root
require_pve_host

step "1 — preflight"
require_cmd pct pve-firewall
pct status "$CTID" | grep -q running || die "LXC $CTID is not running"
pct exec "$CTID" -- test -f "$AUTH_PY" || die "auth.py not found in $CTID — orchestrator layout changed"
pct exec "$CTID" -- test -x "$ORCH_DIR/run.sh" || die "run.sh not found/executable in $CTID"
grep -q '^enable: 1' /etc/pve/firewall/cluster.fw || die "datacenter firewall (cluster.fw) is not enabled"
ok "157 running; orchestrator present; datacenter firewall on"

step "2 — blast-radius guard"
others=""
for conf in /etc/pve/lxc/*.conf /etc/pve/qemu-server/*.conf; do
  [[ -e "$conf" ]] || continue
  id="$(basename "$conf" .conf)"
  [[ "$id" == "$CTID" || "$id" == "170" || "$id" == "172" ]] && continue
  grep -qE '^net[0-9]+:.*firewall=1' "$conf" && others="$others $id"
done
[[ -z "$others" ]] || die "unexpected filtered guests:$others — investigate before enabling 157"
ok "only 157/170/172 will be filtered"

step "3 — engagement gate"
if pct exec "$CTID" -- docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^redteam-orch-run-'; then
  die "an engagement (redteam-orch-run-*) is running in $CTID — wait for it to finish; the restart/reboot would corrupt it"
fi
ok "no engagement container running — safe to restart/reboot"

# Part B (Task 4) and Part A (Task 5) are appended below this line.
step "done (preflight only — no changes applied yet)"
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n scripts/67-redteam-orchestrator-harden.sh && command -v shellcheck >/dev/null && shellcheck scripts/67-redteam-orchestrator-harden.sh || echo "shellcheck absent"`
Expected: no syntax errors.

- [ ] **Step 3: Run preflight on the host (read-only; makes no changes)**

Run: `ssh -o BatchMode=yes root@192.168.6.175 'LGC_DIR=/root/local-gpu-cluster/scripts bash /root/local-gpu-cluster/scripts/67-redteam-orchestrator-harden.sh' || true`
Expected: steps 1–3 pass OR step 3 dies with the engagement-running message if a run is live. (Requires the host checkout to be synced; otherwise `pct push` the script first.)

- [ ] **Step 4: Commit**

```bash
git add scripts/67-redteam-orchestrator-harden.sh
git commit -m "feat(redteam): 67 deploy preflight + engagement gate"
```

---

### Task 4: Part B — app-side hardening (guard, provision, purge, restart)

**Files:**
- Modify: `scripts/67-redteam-orchestrator-harden.sh` (append Part B before the final `step "done"`)

**Interfaces:**
- Consumes: `scripts/redteam/apply_auth_guard.py` (Task 1), `scripts/redteam/provision_user.py` (Task 2), `REDTEAM_ORCH_USER`/`REDTEAM_ORCH_PASSWORD` from env.
- Produces: registration closed (403), one user provisioned, sessions purged, orchestrator back up.

- [ ] **Step 1: Append the Part B section**

```bash
step "4 — Part B: close registration (apply guard)"
VENV="$BACKEND/.venv/bin/python"
pct push "$CTID" "$LGC_DIR/redteam/apply_auth_guard.py" /tmp/apply_auth_guard.py
pct push "$CTID" "$LGC_DIR/redteam/provision_user.py" /tmp/provision_user.py
pct exec "$CTID" -- "$VENV" /tmp/apply_auth_guard.py "$AUTH_PY"
pct exec "$CTID" -- "$VENV" -m py_compile "$AUTH_PY" || die "auth.py no longer compiles after guard — aborting"
ok "guard applied and auth.py compiles"

step "5 — provision the service user"
[[ -n "${REDTEAM_ORCH_USER:-}" && -n "${REDTEAM_ORCH_PASSWORD:-}" ]] \
  || die "set REDTEAM_ORCH_USER and REDTEAM_ORCH_PASSWORD before deploy"
pct exec "$CTID" -- env REDTEAM_ORCH_USER="$REDTEAM_ORCH_USER" \
  REDTEAM_ORCH_PASSWORD="$REDTEAM_ORCH_PASSWORD" \
  sh -c "cd $BACKEND && $VENV /tmp/provision_user.py" || die "provisioning failed"
ok "service user provisioned"

step "6 — purge sessions minted during the LAN-open window"
DB="$BACKEND/data/orchestrator.sqlite3"
pct exec "$CTID" -- "$VENV" -c "import sqlite3,sys; c=sqlite3.connect('$DB'); c.execute('delete from sessions'); c.commit(); print('sessions purged')" \
  || warn "session purge failed — verify manually"

step "7 — restart orchestrator (stop then start; never bare run.sh)"
pct exec "$CTID" -- sh -c "cd $ORCH_DIR && ./stop.sh" || warn "stop.sh returned nonzero (may not have been running)"
pct exec "$CTID" -- sh -c "cd $ORCH_DIR && ./run.sh" || die "run.sh failed — orchestrator may be DOWN (pip/npm on egress-locked box?)"
sleep 5
code="$(pct exec "$CTID" -- sh -c 'curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://127.0.0.1:18000/healthz || true')"
[[ "$code" == "200" ]] || die "orchestrator did not come back ( /healthz=$code ) — investigate before continuing"
ok "orchestrator back up"
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n scripts/67-redteam-orchestrator-harden.sh`
Expected: no errors.

- [ ] **Step 3: Verify behaviorally (maintenance window, from the workstation)**

```bash
# registration closed, login works, auth still enforced, sessions purged
curl -s -o /dev/null -w "register=%{http_code}\n" -X POST -H 'Content-Type: application/json' \
  -d '{"username":"x","password":"y"}' http://192.168.6.157:18000/auth/register   # expect 403
TOK=$(curl -s -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"$REDTEAM_ORCH_USER\",\"password\":\"$REDTEAM_ORCH_PASSWORD\"}" \
  http://192.168.6.157:18000/auth/login | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -o /dev/null -w "me_with_token=%{http_code}\n" -H "Authorization: Bearer $TOK" \
  http://192.168.6.157:18000/auth/me       # expect 200
curl -s -o /dev/null -w "projects_no_token=%{http_code}\n" http://192.168.6.157:18000/projects  # expect 401
```
Expected: `register=403`, `me_with_token=200`, `projects_no_token=401`.

- [ ] **Step 4: Commit**

```bash
git add scripts/67-redteam-orchestrator-harden.sh
git commit -m "feat(redteam): 67 Part B — guard, provision, session purge, restart"
```

---

### Task 5: Part A — network reach (firewall=1, 157.fw, reboot, verify)

**Files:**
- Modify: `scripts/67-redteam-orchestrator-harden.sh` (append Part A before the final `step "done"`)

**Interfaces:**
- Consumes: `CTID`, `WORKSTATION_IP`.
- Produces: `fwbr157i0` filtering `eth0`; `:18000`/`:22` reachable only from the workstation.

- [ ] **Step 1: Append the Part A section**

```bash
step "8 — Part A: enable NIC filtering + write policy"
# The .fw is inert until net0 opts in with firewall=1 (spec §4.1).
pct set "$CTID" --net0 "$(pct config "$CTID" | sed -n 's/^net0: //p' | sed 's/firewall=0/firewall=1/; t; s/$/,firewall=1/')"
write_file_if_changed "/etc/pve/firewall/${CTID}.fw" 0640 <<EOF
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -p tcp -dport 22 -source ${WORKSTATION_IP}
IN ACCEPT -p tcp -dport 18000 -source ${WORKSTATION_IP}
EOF
pve-firewall compile >/dev/null 2>&1 || die "pve-firewall compile failed — fix 157.fw"
pve-firewall restart >/dev/null 2>&1 || die "pve-firewall restart failed"

step "9 — reboot 157 to materialize the filter bridge"
# firewall=1 on a running container does not create fwbr until reboot (spec §4.1).
pct reboot "$CTID"
for _ in $(seq 1 30); do pct status "$CTID" | grep -q running && break; sleep 2; done
sleep 5
ip -br link show type bridge 2>/dev/null | grep -q "fwbr${CTID}i" \
  || die "fwbr${CTID}i0 absent after reboot — the .fw is filtering NOTHING; Part A FAILED"
ok "fwbr${CTID}i0 present — 157 is filtered"
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n scripts/67-redteam-orchestrator-harden.sh`
Expected: no errors.

- [ ] **Step 3: Verify reach (maintenance window)**

```bash
# from the workstation: allowed
curl -s -o /dev/null -w "ws_18000=%{http_code}\n" --max-time 6 http://192.168.6.157:18000/healthz  # expect 200
# from a non-allowed LAN host (LXC 156): refused/timeout
ssh root@192.168.6.175 'pct exec 156 -- sh -c "curl -s -o /dev/null -m 5 -w \"lxc156_18000=%{http_code}\n\" http://192.168.6.157:18000/healthz || echo lxc156_18000=refused"'
# confirm the orchestrator still answers its own docker-internal callbacks
ssh root@192.168.6.175 'pct exec 157 -- sh -c "curl -s -o /dev/null -w \"docker0=%{http_code}\n\" http://172.17.0.1:18000/healthz"'  # expect 200
```
Expected: `ws_18000=200`, `lxc156_18000=refused` (or timeout), `docker0=200`.

- [ ] **Step 4: Commit**

```bash
git add scripts/67-redteam-orchestrator-harden.sh
git commit -m "feat(redteam): 67 Part A — NIC firewall, 157.fw, reboot, verify"
```

---

### Task 6: Rollback, standalone verify, and re-run idempotency

**Files:**
- Modify: `scripts/67-redteam-orchestrator-harden.sh` (add a `--rollback` path and a `--verify` path; final `step "done"`)

**Interfaces:**
- Produces: `67-…sh --verify` (re-runnable probes, no changes) and `67-…sh --rollback` (restore access posture).

- [ ] **Step 1: Add arg handling + rollback + verify**

```bash
# near the top, after sourcing common.sh:
MODE="${1:-deploy}"

do_verify() {
  step "verify"
  local ws ct
  ws="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://${CT_IP}:18000/healthz" || true)"
  ct="$(pct exec "$CTID" -- sh -c 'curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://127.0.0.1:18000/auth/register -X POST -H "Content-Type: application/json" -d "{\"username\":\"p\",\"password\":\"p\"}" || true')"
  echo "healthz(ws)=$ws register(local)=$ct"
  [[ "$ct" == "403" ]] || die "registration not closed (got $ct)"
  ip -br link show type bridge 2>/dev/null | grep -q "fwbr${CTID}i" || warn "fwbr${CTID}i absent — Part A not active"
  ok "verify complete"
}

do_rollback() {
  step "rollback — restoring prior access posture"
  rm -f "/etc/pve/firewall/${CTID}.fw"
  pct set "$CTID" --net0 "$(pct config "$CTID" | sed -n 's/^net0: //p' | sed 's/firewall=1/firewall=0/')"
  pve-firewall restart || true
  pct exec "$CTID" -- "$BACKEND/.venv/bin/python" -c "import re,io; p='$AUTH_PY'; s=open(p).read(); s=re.sub(r'(?ms)^\\s*# local-gpu-cluster R1 hardening.*?registration disabled\"\\)\\n','',s); open(p,'w',newline='\\n').write(s)" || warn "guard removal failed — edit $AUTH_PY by hand"
  warn "reboot 157 and restart the orchestrator to fully revert; sessions are not restored"
}

case "$MODE" in
  --verify)   require_root; require_pve_host; do_verify; exit 0 ;;
  --rollback) require_root; require_pve_host; do_rollback; exit 0 ;;
esac
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n scripts/67-redteam-orchestrator-harden.sh && shellcheck scripts/67-redteam-orchestrator-harden.sh 2>/dev/null || echo "shellcheck absent"`
Expected: no errors.

- [ ] **Step 3: Idempotency re-run (maintenance window)**

Run the full deploy twice; the second run inserts no duplicate guard (`apply_auth_guard` prints "applied"), reports the user "exists", and `write_file_if_changed` reports the `.fw` unchanged. Confirm `67-…sh --verify` → `register(local)=403`.

- [ ] **Step 4: Commit**

```bash
git add scripts/67-redteam-orchestrator-harden.sh
git commit -m "feat(redteam): 67 rollback + standalone verify + idempotent re-run"
```

---

## A/B eligibility note (for the Phase-1 delegation experiment)

Most of R1 is security-sensitive or infra and is **self-done** per the delegation rule (auth, firewall, the vendored app). The one plausibly-delegatable, non-sensitive, in-repo unit is **Task 1** (the pure string transform + its tests) — small, so likely below the ≥200-line A/B band; record it as a self-done data point if it is run as an A/B task at all. Tasks 2–6 are self-done.
