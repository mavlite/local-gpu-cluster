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
ct_status="$(pct status "$CTID")" || die "cannot query status of LXC $CTID"
grep -q running <<<"$ct_status" || die "LXC $CTID is not running"
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
running="$(pct exec "$CTID" -- docker ps --format '{{.Names}}')" || die "cannot query docker in $CTID — refusing to proceed (gate must fail closed)"
if grep -q '^redteam-orch-run-' <<<"$running"; then
  die "an engagement (redteam-orch-run-*) is running in $CTID — wait for it to finish; the restart/reboot would corrupt it"
fi
ok "no engagement container running — safe to restart/reboot"

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

# Part A (Task 5) is appended below this line.
step "done (preflight only — no changes applied yet)"
