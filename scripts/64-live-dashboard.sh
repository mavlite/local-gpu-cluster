#!/usr/bin/env bash
# 64-live-dashboard.sh -- install / side-by-side / cut-over / rollback for the live inference dashboard
# (spec docs/superpowers/specs/2026-10-16-live-dashboard-design.md). Run as root on the Proxmox host from a detached
# worktree of main (the host checkout lags main). Side-by-side runs on :8889 next to cluster-monitor on :8888;
# cut-over stops cluster-monitor (files kept) and moves the dashboard to :8888.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/files/live_dashboard"
UNIT_SRC="$HERE/files/live-dashboard.service"
DEST=/opt/live-dashboard
CFG=/etc/live-dashboard.json
ROLLBACK=/root/live-dashboard-rollback.sh
HOST_IP=192.168.6.175
# Config template; @PORT@ is the only thing that differs between side-by-side and cut-over.
CFG_TEMPLATE='{"bind_host": "192.168.6.175", "bind_port": @PORT@, "history_path": "/var/lib/live-dashboard/history.db"}'

[[ "${EUID}" -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

write_cfg() {
  local port="$1" tmp
  tmp="$(mktemp)"
  printf '%s\n' "${CFG_TEMPLATE//@PORT@/$port}" > "$tmp"
  install -m 0644 "$tmp" "$CFG"
  rm -f "$tmp"
}

check_python() {
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "python3 >= 3.11 required on this host" >&2; exit 1; }
}

check_import() {
  python3 -c "import sys; sys.path.insert(0,'$DEST'); import live_dashboard.main"
}

# Poll /healthz on a port for up to ~15 s. Returns 0 as soon as it answers 2xx.
wait_health() {
  local port="$1" i
  for i in $(seq 1 15); do
    if curl -fsS -m 3 "http://$HOST_IP:$port/healthz" >/dev/null 2>&1; then
      curl -s -m 3 "http://$HOST_IP:$port/healthz"; echo
      return 0
    fi
    sleep 1
  done
  return 1
}

install_files() {
  [[ -d "$SRC" && -f "$UNIT_SRC" ]] || { echo "missing $SRC or $UNIT_SRC" >&2; exit 1; }
  check_python
  install -d -m 0755 "$DEST"
  rm -rf "$DEST/live_dashboard.new"
  cp -a "$SRC" "$DEST/live_dashboard.new"
  find "$DEST/live_dashboard.new" -name '__pycache__' -prune -exec rm -rf {} +
  # Keep the previous install so a bad upgrade can be reverted (see the rollback script).
  if [[ -d "$DEST/live_dashboard" ]]; then
    rm -rf "$DEST/live_dashboard.prev"
    mv "$DEST/live_dashboard" "$DEST/live_dashboard.prev"
  fi
  mv "$DEST/live_dashboard.new" "$DEST/live_dashboard"
  if ! check_import; then
    echo "package does not import after install; reverting" >&2
    rm -rf "$DEST/live_dashboard"
    [[ -d "$DEST/live_dashboard.prev" ]] && mv "$DEST/live_dashboard.prev" "$DEST/live_dashboard"
    exit 1
  fi
  install -d -m 0700 /var/lib/live-dashboard
  install -m 0644 "$UNIT_SRC" /etc/systemd/system/live-dashboard.service
  systemctl daemon-reload
  cat > "$ROLLBACK" <<'EOF'
#!/bin/bash
# Restore the old cluster monitor on :8888 and stop the live dashboard.
# To revert a bad dashboard UPGRADE instead (keeping the dashboard): swap the previous install back in --
#   rm -rf /opt/live-dashboard/live_dashboard && mv /opt/live-dashboard/live_dashboard.prev /opt/live-dashboard/live_dashboard
#   systemctl restart live-dashboard
systemctl disable --now live-dashboard || true
systemctl enable --now cluster-monitor
systemctl --no-pager --lines=0 status cluster-monitor
EOF
  chmod 700 "$ROLLBACK"
}

case "${1:-}" in
  install)
    install_files
    [[ -f "$CFG" ]] || write_cfg 8889
    echo "installed; run '$0 side-by-side' to start on :8889" ;;
  side-by-side)
    install_files
    write_cfg 8889
    # "bind_port": 8889 -- next to cluster-monitor on :8888
    systemctl enable --now live-dashboard
    systemctl restart live-dashboard
    wait_health 8889 || { echo "dashboard did not answer /healthz on :8889" >&2; exit 1; } ;;
  cutover)
    [[ -d "$DEST/live_dashboard" && -f "$ROLLBACK" ]] || { echo "not installed" >&2; exit 1; }
    systemctl disable --now cluster-monitor
    write_cfg 8888
    # "bind_port": 8888 -- the dashboard takes over the monitor's port
    systemctl restart live-dashboard
    if ! wait_health 8888; then
      echo "dashboard did not answer /healthz on :8888; rolling back" >&2
      bash "$ROLLBACK" || true
      exit 1
    fi
    echo "rollback: $ROLLBACK" ;;
  rollback)
    [[ -f "$ROLLBACK" ]] || { echo "no rollback script at $ROLLBACK" >&2; exit 1; }
    bash "$ROLLBACK" ;;
  status)
    systemctl --no-pager --lines=5 status live-dashboard cluster-monitor || true ;;
  *)
    echo "usage: $0 install|side-by-side|cutover|rollback|status" >&2; exit 2 ;;
esac
