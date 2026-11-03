#!/usr/bin/env bash
# 67-pct-fstrim-timer.sh — weekly `pct fstrim` of every running LXC.
#
# LXC rootfs volumes on local-lvm are thin LVs, and Proxmox never trims them:
# blocks a container frees stay allocated in the thin pool. On 2026-11-03 LXC
# 151's volume read 98.7% allocated while its filesystem was 45% full (model
# downloads + llama.cpp builds churn it). One `pct fstrim` pass reclaimed
# ~130 GiB across 8 containers (pool 14.5% -> 9.3%). This timer keeps it there,
# without the per-delete cost of a `discard` mount option.
#
# Components installed:
#   /usr/local/sbin/pct-fstrim-all.sh   — trims each running CT, logs per CT
#   /etc/systemd/system/pct-fstrim.service — oneshot, low IO priority
#   /etc/systemd/system/pct-fstrim.timer   — weekly (Sun 03:45 by default)
#
# A CT whose storage doesn't support discard (e.g. LXC 157) is logged and
# skipped; the run fails only if a trim on a supporting volume errors.
# Override the schedule via FSTRIM_TIMER_SCHEDULE. Idempotent.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host

FSTRIM_TIMER_SCHEDULE="${FSTRIM_TIMER_SCHEDULE:-Sun *-*-* 03:45:00}"
TRIM_SCRIPT=/usr/local/sbin/pct-fstrim-all.sh

step "67.1 — Install $TRIM_SCRIPT"
cat > "$TRIM_SCRIPT" <<'EOF'
#!/usr/bin/env bash
# Trim every running LXC (installed by local-gpu-cluster scripts/67-pct-fstrim-timer.sh).
set -uo pipefail
rc=0
for ct in $(pct list | awk 'NR > 1 && $2 == "running" { print $1 }'); do
  if out=$(pct fstrim "$ct" 2>&1); then
    echo "CT $ct: $out"
  elif grep -q "not supported" <<<"$out"; then
    echo "CT $ct: skipped (discard not supported)"
  else
    echo "CT $ct: FAILED: $out" >&2
    rc=1
  fi
done
exit "$rc"
EOF
chmod 0755 "$TRIM_SCRIPT"
ok "Installed $TRIM_SCRIPT"

step "67.2 — Install pct-fstrim.service + .timer"
cat > /etc/systemd/system/pct-fstrim.service <<EOF
[Unit]
Description=Trim the thin-LVM volumes of all running LXCs

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=1h
ExecStart=$TRIM_SCRIPT
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/pct-fstrim.timer <<EOF
[Unit]
Description=Weekly trim of LXC thin volumes

[Timer]
OnCalendar=$FSTRIM_TIMER_SCHEDULE
RandomizedDelaySec=15min
Persistent=true
Unit=pct-fstrim.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now pct-fstrim.timer

step "67.3 — Smoke-check timer state"
systemctl list-timers pct-fstrim.timer --no-pager || true

ok "Phase 67 complete."
echo
echo "Timer schedule:  $FSTRIM_TIMER_SCHEDULE (up to 15min randomized delay)"
echo "Manual run:      systemctl start pct-fstrim.service"
echo "Last run:        journalctl -u pct-fstrim.service -n 20"
echo "Thin usage:      lvs -o lv_name,data_percent pve"
