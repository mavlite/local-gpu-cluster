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

# Part B (Task 4) and Part A (Task 5) are appended below this line.
step "done (preflight only — no changes applied yet)"
