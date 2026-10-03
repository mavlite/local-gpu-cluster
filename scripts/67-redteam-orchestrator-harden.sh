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
  local net0
  net0="$(pct config "$CTID" | sed -n 's/^net0: //p')" || net0=""
  if [[ -n "$net0" ]]; then
    pct set "$CTID" --net0 "$(sed 's/firewall=1/firewall=0/' <<<"$net0")" || warn "could not revert firewall=1 on net0 of LXC $CTID"
  else
    warn "cannot read net0 of LXC $CTID — skipping net0 firewall revert"
  fi
  pve-firewall restart || true
  pct exec "$CTID" -- "$BACKEND/.venv/bin/python" -c "import re,sys; p='$AUTH_PY'; s=open(p).read(); n=re.sub(r'(?ms)^\\s*# local-gpu-cluster R1 hardening.*?registration disabled\"\\)\\n','',s); open(p,'w',newline='\\n').write(n); sys.exit(0 if n!=s else 3)" || warn "guard not found or removal failed — edit $AUTH_PY by hand"
  warn "reboot 157 and restart the orchestrator to fully revert; sessions are not restored"
}

case "$MODE" in
  --verify)   do_verify; exit 0 ;;
  --rollback) do_rollback; exit 0 ;;
  --*)        die "unknown mode: $MODE (use --verify or --rollback, or no arg to deploy)" ;;
esac

step "1 — preflight"
require_cmd pct pve-firewall
ct_status="$(pct status "$CTID")" || die "cannot query status of LXC $CTID"
grep -q running <<<"$ct_status" || die "LXC $CTID is not running"
pct exec "$CTID" -- test -f "$AUTH_PY" || die "auth.py not found in $CTID — orchestrator layout changed"
pct exec "$CTID" -- test -x "$ORCH_DIR/run.sh" || die "run.sh not found/executable in $CTID"
grep -q '^enable: 1' /etc/pve/firewall/cluster.fw || die "datacenter firewall (cluster.fw) is not enabled"
warn "workstation ${WORKSTATION_IP} is a DHCP lease — a reservation for it must exist, else a lease change strands the workstation (spec 8/9)"
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
# password goes via stdin, never argv
printf '%s' "$REDTEAM_ORCH_PASSWORD" | pct exec "$CTID" -- sh -c "cd $BACKEND && REDTEAM_ORCH_USER='$REDTEAM_ORCH_USER' REDTEAM_ORCH_PASSWORD=\$(cat) $VENV /tmp/provision_user.py" \
  || die "provisioning failed"
ok "service user provisioned"
pct exec "$CTID" -- rm -f /tmp/apply_auth_guard.py /tmp/provision_user.py \
  || warn "could not remove helper scripts from /tmp in $CTID"

step "6 — stop orchestrator and confirm it is down"
pct exec "$CTID" -- sh -c "cd $ORCH_DIR && ./stop.sh" || warn "stop.sh returned nonzero (may not have been running)"
down=0
for _ in 1 2 3 4 5 6; do
  code="$(pct exec "$CTID" -- sh -c 'curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:18000/healthz || true')" \
    || die "cannot query $CTID while confirming stop"
  if [[ "$code" != "200" ]]; then down=1; break; fi
  sleep 2
done
[[ "$down" == "1" ]] || die "orchestrator still serving after stop.sh — old code/sessions live; aborting before run.sh"
ok "orchestrator is down"

step "7 — purge sessions minted during the LAN-open window (server is down)"
DB="$BACKEND/data/orchestrator.sqlite3"
left="$(pct exec "$CTID" -- "$VENV" -c "import sqlite3; c=sqlite3.connect('$DB'); c.execute('delete from sessions'); c.commit(); print(c.execute('select count(*) from sessions').fetchone()[0])")" \
  || die "session purge failed — attacker-minted sessions may remain"
[[ "$left" == "0" ]] || die "session purge verification failed (remaining=$left) — attacker-minted sessions may remain"
ok "sessions purged and verified empty"

step "8 — start orchestrator"
pct exec "$CTID" -- sh -c "cd $ORCH_DIR && ./run.sh" || die "run.sh failed — orchestrator may be DOWN (pip/npm on egress-locked box?)"
sleep 5
code="$(pct exec "$CTID" -- sh -c 'curl -s -o /dev/null -w "%{http_code}" --max-time 6 http://127.0.0.1:18000/healthz || true')" \
  || die "cannot query $CTID for healthz"
[[ "$code" == "200" ]] || die "orchestrator did not come back ( /healthz=$code ) — investigate before continuing"
ok "orchestrator back up"

step "9 — Part A: enable NIC filtering + write policy"
# The .fw is inert until net0 opts in with firewall=1 (spec §4.1).
net0="$(pct config "$CTID" | sed -n 's/^net0: //p')" || die "cannot read net0 of LXC $CTID"
[[ -n "$net0" ]] || die "LXC $CTID has no net0"
if grep -q 'firewall=1' <<<"$net0"; then
  ok "net0 already has firewall=1"
else
  pct set "$CTID" --net0 "$(sed 's/firewall=0/firewall=1/; t; s/$/,firewall=1/' <<<"$net0")" \
    || die "could not set firewall=1 on net0 of LXC $CTID"
fi
write_file_if_changed "/etc/pve/firewall/${CTID}.fw" 0640 <<EOF
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -p tcp -dport 22 -source ${WORKSTATION_IP}
IN ACCEPT -p tcp -dport 18000 -source ${WORKSTATION_IP}
EOF
err="$(pve-firewall compile 2>&1)" || die "pve-firewall compile failed — fix ${CTID}.fw: $err"
pve-firewall restart >/dev/null 2>&1 || die "pve-firewall restart failed"

step "10 — reboot $CTID to materialize the filter bridge"
# firewall=1 on a running container does not create fwbr until reboot (spec §4.1).
pct reboot "$CTID" || die "pct reboot $CTID failed"
for _ in $(seq 1 30); do pct status "$CTID" | grep -q running && break; sleep 2; done
pct status "$CTID" | grep -q running || die "LXC $CTID did not return to running after reboot"
sleep 5
bridges="$(ip -br link show type bridge 2>/dev/null || true)"
grep -q "fwbr${CTID}i0" <<<"$bridges" \
  || die "fwbr${CTID}i0 absent after reboot — the .fw is filtering NOTHING; Part A FAILED"
ok "fwbr${CTID}i0 present — $CTID is filtered"

step "11 — post-reboot orchestrator health gate"
# The reboot killed the orchestrator started in step 8; it may not be boot-persistent.
orch_up() {
  local code
  for _ in $(seq 1 10); do
    code="$(pct exec "$CTID" -- sh -c 'curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:18000/healthz || true' 2>/dev/null)" || code=""
    [[ "$code" == "200" ]] && return 0
    sleep 3
  done
  return 1
}
if orch_up; then
  ok "orchestrator up after reboot (autostarted)"
else
  warn "orchestrator not answering after reboot — starting it with run.sh"
  pct exec "$CTID" -- sh -c "cd $ORCH_DIR && ./run.sh" \
    || die "run.sh failed after reboot — start it manually (cd $ORCH_DIR && ./run.sh) and re-run --verify"
  orch_up || die "orchestrator did not come up after reboot — start it manually (cd $ORCH_DIR && ./run.sh) and re-run --verify"
  ok "orchestrator started by run.sh after reboot"
fi

step "done — registration closed, user provisioned, sessions purged; orchestrator up, $CTID filtered to ${WORKSTATION_IP} on :22/:18000 (fwbr${CTID}i0 verified)"
