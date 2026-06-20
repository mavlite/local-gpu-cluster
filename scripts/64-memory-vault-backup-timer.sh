#!/usr/bin/env bash
# 64-memory-vault-backup-timer.sh — schedule nightly Memory Vault DB backups.
#
# Installs a HOST systemd timer that runs the pg_dump backup INSIDE LXC 156
# via `pct exec`. Host placement (not an in-156 timer) keeps all scheduled
# jobs visible on the PVE host alongside rag-refresh.timer, and matches what
# the cluster-monitor's `backup_timer` check probes (host-side
# `systemctl is-active memory-vault-backup.timer`).
#
# Components installed:
#   LXC 156:/usr/local/bin/memory-vault-backup.sh   — the pg_dump script (pushed)
#   /etc/systemd/system/memory-vault-backup.service — oneshot, pct-execs the script
#   /etc/systemd/system/memory-vault-backup.timer   — daily schedule
#
# The backup script itself (scripts/files/memory-vault-backup.sh) dumps the
# memory_vault DB to /opt/memory-vault-data/backups (the tank bind mount, so it
# rides ZFS snapshots) and retains the 14 most recent dumps.
#
# Schedule rationale: daily at 02:30 (after ZFS snapshots ~02:00, before
# rag-refresh at 03:15). Override via BACKUP_TIMER_SCHEDULE.
#
# Idempotent — re-running re-pushes the script, rewrites the units, re-enables.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

MEMVAULT_VMID="${MEMVAULT_VMID:-156}"
BACKUP_SCRIPT_SRC="${BACKUP_SCRIPT_SRC:-${LGC_DIR}/files/memory-vault-backup.sh}"
BACKUP_SCRIPT_DST="/usr/local/bin/memory-vault-backup.sh"
BACKUP_TIMER_SCHEDULE="${BACKUP_TIMER_SCHEDULE:-*-*-* 02:30:00}"
PCT_BIN="$(command -v pct)"

[[ -f "$BACKUP_SCRIPT_SRC" ]] || die "backup script not found at $BACKUP_SCRIPT_SRC"
lxc_exists "$MEMVAULT_VMID" || die "LXC $MEMVAULT_VMID does not exist"
ensure_lxc_started "$MEMVAULT_VMID"

step "64.1 — Push backup script into LXC $MEMVAULT_VMID"
push_to_lxc "$MEMVAULT_VMID" "$BACKUP_SCRIPT_SRC" "$BACKUP_SCRIPT_DST" 0755
ok "Installed $BACKUP_SCRIPT_DST in LXC $MEMVAULT_VMID"

step "64.2 — Install memory-vault-backup.service + .timer (host)"
cat > /etc/systemd/system/memory-vault-backup.service <<EOF
[Unit]
Description=Memory Vault DB backup (pg_dump inside LXC $MEMVAULT_VMID)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=30min
ExecStart=$PCT_BIN exec $MEMVAULT_VMID -- $BACKUP_SCRIPT_DST
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/memory-vault-backup.timer <<EOF
[Unit]
Description=Daily Memory Vault DB backup
After=network-online.target

[Timer]
OnCalendar=$BACKUP_TIMER_SCHEDULE
RandomizedDelaySec=10min
Persistent=true
Unit=memory-vault-backup.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now memory-vault-backup.timer

step "64.3 — Run one backup now to validate (non-fatal)"
if systemctl start memory-vault-backup.service; then
  ok "Initial backup completed"
else
  warn "Initial backup run failed — check 'journalctl -u memory-vault-backup.service'"
fi

step "64.4 — Smoke-check timer state"
systemctl list-timers memory-vault-backup.timer --no-pager || true

ok "Phase 64 complete."
echo
echo "Timer schedule:     $BACKUP_TIMER_SCHEDULE (with up to 10min randomized delay)"
echo "Manual run:         systemctl start memory-vault-backup.service"
echo "Tail the run:       journalctl -u memory-vault-backup.service -f"
echo "Backups land in:    LXC $MEMVAULT_VMID:/opt/memory-vault-data/backups (tank, snapshotted; 14 retained)"
echo "List backups:       pct exec $MEMVAULT_VMID -- ls -lt /opt/memory-vault-data/backups"
