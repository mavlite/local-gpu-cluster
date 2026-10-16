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
UNIT_DEST=/etc/systemd/system/live-dashboard.service
ROLLBACK=/root/live-dashboard-rollback.sh
HOST_IP=192.168.6.175
PY=/usr/bin/python3
# Config template; @PORT@ is the only thing that differs between side-by-side and cut-over.
CFG_TEMPLATE='{"bind_host": "192.168.6.175", "bind_port": @PORT@, "history_path": "/var/lib/live-dashboard/history.db"}'
# Sources that must be ok before the dashboard may own :8888.
REQUIRED_SOURCES="slots journal gpu host"

[[ "${EUID}" -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

write_cfg() {
  local port="$1"
  printf '%s\n' "${CFG_TEMPLATE//@PORT@/$port}" > "$CFG.new"
  chmod 0644 "$CFG.new"
  mv -f "$CFG.new" "$CFG"
}

check_python() {
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "python3 >= 3.11 required on this host" >&2; exit 1; }
}

check_import() {
  "$PY" -c "import sys; sys.path.insert(0,'$DEST'); import live_dashboard.main"
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

# Read the first `data:` event of /api/stream (max 10 s) and require the REQUIRED_SOURCES to be ok.
# Prints the sources that are not ok. Returns 0 only if all are ok.
check_sources() {
  local port="$1" line bad
  line="$(curl -sS -N --max-time 10 "http://$HOST_IP:$port/api/stream" 2>/dev/null | grep -a -m1 '^data: ' || true)"
  if [[ -z "$line" ]]; then
    echo "no snapshot received from :$port/api/stream within 10 s" >&2
    return 1
  fi
  bad="$(printf '%s' "${line#data: }" | "$PY" -c '
import json, sys
need = sys.argv[1].split()
try:
    src = json.loads(sys.stdin.read()).get("sources", {})
except ValueError:
    print("unparseable-snapshot"); sys.exit(0)
print(" ".join(n for n in need if not (isinstance(src.get(n), dict) and src[n].get("ok") is True)))
' "$REQUIRED_SOURCES")"
  if [[ -n "$bad" ]]; then
    echo "sources not ok: $bad" >&2
    return 1
  fi
  echo "sources ok: $REQUIRED_SOURCES"
}

install_files() {
  [[ -d "$SRC" && -f "$UNIT_SRC" ]] || { echo "missing $SRC or $UNIT_SRC" >&2; exit 1; }
  check_python
  install -d -m 0755 "$DEST"
  rm -rf "$DEST/live_dashboard.new"
  cp -a "$SRC" "$DEST/live_dashboard.new"
  find "$DEST/live_dashboard.new" -name '__pycache__' -prune -exec rm -rf {} +
  chown -R root:root "$DEST/live_dashboard.new"
  local swapped=0
  if [[ -d "$DEST/live_dashboard" ]] && diff -rq -x __pycache__ "$DEST/live_dashboard.new" "$DEST/live_dashboard" >/dev/null; then
    rm -rf "$DEST/live_dashboard.new"      # identical: keep the current tree and the existing .prev
  else
    # Keep the previous install so a bad upgrade can be reverted (see the rollback script).
    if [[ -d "$DEST/live_dashboard" ]]; then
      rm -rf "$DEST/live_dashboard.prev"
      mv "$DEST/live_dashboard" "$DEST/live_dashboard.prev"
    fi
    mv "$DEST/live_dashboard.new" "$DEST/live_dashboard"
    swapped=1
  fi
  if ! check_import; then
    echo "package does not import after install" >&2
    if [[ "$swapped" -eq 1 ]]; then
      rm -rf "$DEST/live_dashboard"
      if [[ -d "$DEST/live_dashboard.prev" ]]; then mv "$DEST/live_dashboard.prev" "$DEST/live_dashboard"; fi
      echo "reverted to the previous install" >&2
    fi
    exit 1
  fi
  install -d -m 0700 /var/lib/live-dashboard
  install -m 0644 "$UNIT_SRC" "$UNIT_DEST.new"
  mv -f "$UNIT_DEST.new" "$UNIT_DEST"
  systemctl daemon-reload
  cat > "$ROLLBACK.new" <<'EOF'
#!/bin/bash
# Restore the old cluster monitor on :8888 and stop the live dashboard.
# To revert a bad dashboard UPGRADE instead (keeping the dashboard): swap the previous install back in --
#   rm -rf /opt/live-dashboard/live_dashboard && mv /opt/live-dashboard/live_dashboard.prev /opt/live-dashboard/live_dashboard
#   systemctl restart live-dashboard
systemctl disable --now live-dashboard || true
systemctl enable --now cluster-monitor
systemctl --no-pager --lines=0 status cluster-monitor
EOF
  chmod 700 "$ROLLBACK.new"
  mv -f "$ROLLBACK.new" "$ROLLBACK"
}

case "${1:-}" in
  install)
    install_files
    [[ -f "$CFG" ]] || write_cfg 8889
    if systemctl is-active --quiet live-dashboard; then
      echo "live-dashboard is running: restart live-dashboard to apply the new files (not restarted implicitly)"
    fi
    echo "installed; run '$0 side-by-side' to start on :8889" ;;
  side-by-side)
    if ! systemctl is-active --quiet cluster-monitor; then
      echo "cluster-monitor is not active (already cut over?); refusing, it would move the dashboard off :8888." >&2
      echo "use 'rollback' first if you want to return to the side-by-side layout" >&2
      exit 1
    fi
    install_files
    write_cfg 8889
    # "bind_port": 8889 -- next to cluster-monitor on :8888
    systemctl enable live-dashboard
    systemctl restart live-dashboard
    if ! wait_health 8889; then
      echo "dashboard did not answer /healthz on :8889; run 'systemctl stop live-dashboard' to back out" >&2
      exit 1
    fi
    check_sources 8889 || echo "WARNING: not all sources are ok yet (non-fatal in side-by-side)" >&2 ;;
  cutover)
    [[ -d "$DEST/live_dashboard" && -f "$ROLLBACK" ]] || { echo "not installed" >&2; exit 1; }
    systemctl disable --now cluster-monitor
    write_cfg 8888
    # "bind_port": 8888 -- the dashboard takes over the monitor's port
    systemctl restart live-dashboard
    if ! wait_health 8888 || ! check_sources 8888; then
      echo "dashboard is not healthy on :8888; rolling back" >&2
      bash "$ROLLBACK" || echo "ROLLBACK FAILED — run $ROLLBACK manually" >&2
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
