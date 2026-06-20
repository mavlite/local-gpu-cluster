#!/usr/bin/env bash
# 63-cluster-monitor.sh — install the read-only cluster monitor as a host
# systemd service. See docs/cluster-monitor-design.md.
#
# Installs:
#   /opt/cluster-monitor/cluster_monitor.py   — the service (stdlib-only)
#   /etc/cluster-monitor.json                 — config (written only if absent)
#   /var/lib/cluster-monitor/                 — SQLite state dir
#   /etc/systemd/system/cluster-monitor.service
#
# Why on the PVE host: needs native rocm-smi / pct / pct-exec-docker /
# systemctl access, all host-scoped. Idempotent — re-running updates the code
# and unit but never clobbers an existing /etc/cluster-monitor.json.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

SRC="${LGC_DIR}/files/cluster_monitor.py"
UNIT_SRC="${LGC_DIR}/files/cluster-monitor.service"
INSTALL_DIR="/opt/cluster-monitor"
STATE_DIR="/var/lib/cluster-monitor"
CONFIG_PATH="/etc/cluster-monitor.json"
MON_BIND_HOST="${MON_BIND_HOST:-127.0.0.1}"
MON_BIND_PORT="${MON_BIND_PORT:-8888}"

[[ -f "$SRC" ]] || die "cluster_monitor.py not found at $SRC"
require_cmd python3

step "63.1 — Smoke-test the module (syntax + --once)"
python3 "$SRC" --once --config /nonexistent.json >/dev/null 2>&1 || \
  log "(--once returned non-zero, expected when endpoints are unreachable)"
python3 -c "import py_compile,sys; py_compile.compile('$SRC', doraise=True)" \
  || die "cluster_monitor.py failed to compile"

step "63.2 — Install code + state dir"
mkdir -p "$INSTALL_DIR" "$STATE_DIR"
chmod 0755 "$INSTALL_DIR" "$STATE_DIR"
install -m 0755 "$SRC" "$INSTALL_DIR/cluster_monitor.py"
ok "Installed $INSTALL_DIR/cluster_monitor.py"

step "63.3 — Write default config (only if absent)"
if [[ -f "$CONFIG_PATH" ]]; then
  skip "$CONFIG_PATH exists — leaving as-is"
else
  cat > "$CONFIG_PATH" <<EOF
{
  "bind_host": "$MON_BIND_HOST",
  "bind_port": $MON_BIND_PORT,
  "bearer_token": "",
  "lxc_ram_ceilings": {"151": 32768}
}
EOF
  chmod 0644 "$CONFIG_PATH"
  ok "Wrote $CONFIG_PATH"
fi

# The service binds to whatever is in the config (which we may have just left
# as-is on a re-run). Read the effective bind back so the smoke-check + summary
# below target the address the service actually listens on — not the env default
# (a config bound to the LAN IP would otherwise fail a loopback probe).
if [[ -f "$CONFIG_PATH" ]]; then
  MON_BIND_HOST=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH')).get('bind_host','$MON_BIND_HOST'))" 2>/dev/null || echo "$MON_BIND_HOST")
  MON_BIND_PORT=$(python3 -c "import json;print(json.load(open('$CONFIG_PATH')).get('bind_port',$MON_BIND_PORT))" 2>/dev/null || echo "$MON_BIND_PORT")
fi

step "63.4 — Install + enable systemd unit"
install -m 0644 "$UNIT_SRC" /etc/systemd/system/cluster-monitor.service
systemctl daemon-reload
systemctl enable cluster-monitor.service
# Use restart (not just `enable --now`) so re-running this installer to upgrade
# actually reloads the new code — `enable --now` no-ops on an already-running
# service and would leave the old code resident.
systemctl restart cluster-monitor.service

step "63.5 — Smoke-check"
sleep 2
systemctl is-active cluster-monitor.service || die "service not active"
curl -fsS "http://${MON_BIND_HOST}:${MON_BIND_PORT}/healthz" >/dev/null \
  && ok "healthz reachable" || warn "healthz not reachable yet"

ok "Phase 63 complete."
echo
echo "Dashboard:     http://${MON_BIND_HOST}:${MON_BIND_PORT}/"
echo "JSON API:      http://${MON_BIND_HOST}:${MON_BIND_PORT}/api/status"
echo "One-shot scan: python3 $INSTALL_DIR/cluster_monitor.py --once"
echo "Logs:          journalctl -u cluster-monitor.service -f"
echo "Config:        $CONFIG_PATH (edit + 'systemctl restart cluster-monitor')"
