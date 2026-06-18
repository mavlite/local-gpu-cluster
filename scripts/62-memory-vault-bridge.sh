#!/usr/bin/env bash
# 62-memory-vault-bridge.sh — deploy the MCP-over-SSE bridge in LXC 156.
#
# Prereqs: scripts/61-lxc-memory-vault.sh created LXC 156 and the stack is up,
# and /etc/memory-vault-bridge.env exists (Task 3 Step 2).
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

MV_VMID="${MEMVAULT_VMID:-156}"
SERVER_SRC="$LGC_DIR/files/memory-vault-bridge.py"

[[ -r "$SERVER_SRC" ]] || die "Missing bridge source: $SERVER_SRC"
pct status "$MV_VMID" >/dev/null 2>&1 || die "LXC $MV_VMID missing. Run 61-lxc-memory-vault.sh first."
ensure_lxc_started "$MV_VMID"
pct exec "$MV_VMID" -- test -f /etc/memory-vault-bridge.env \
  || die "/etc/memory-vault-bridge.env missing in LXC $MV_VMID (see Task 3 Step 2)."

phase_install_python() {
  step "Install Python venv + mcp SDK + httpx + uvicorn + starlette"
  pct exec "$MV_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    if ! python3 -c 'import ensurepip' 2>/dev/null; then
      apt update && apt install -y python3 python3-venv python3-pip
    fi
    mkdir -p /opt/memory-vault-bridge
    [[ -x /opt/memory-vault-bridge/venv/bin/python ]] || python3 -m venv /opt/memory-vault-bridge/venv
    /opt/memory-vault-bridge/venv/bin/pip install --quiet --upgrade pip wheel
    /opt/memory-vault-bridge/venv/bin/pip install --quiet 'mcp>=1.2' httpx uvicorn starlette
GUEST
}

phase_deploy() {
  step "Push bridge server.py"
  pct push "$MV_VMID" "$SERVER_SRC" /opt/memory-vault-bridge/server.py --perms 0644
}

phase_systemd() {
  step "Install + enable memory-vault-bridge.service"
  pct exec "$MV_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/memory-vault-bridge.service <<'EOF'
[Unit]
Description=MCP-over-SSE bridge to Memory Vault REST (port 3005)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/memory-vault-bridge.env
WorkingDirectory=/opt/memory-vault-bridge
ExecStart=/opt/memory-vault-bridge/venv/bin/python /opt/memory-vault-bridge/server.py
Restart=on-failure
RestartSec=10
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable memory-vault-bridge.service
    systemctl restart memory-vault-bridge.service
    sleep 2
    systemctl is-active memory-vault-bridge.service
GUEST
}

main() {
  phase_install_python
  phase_deploy
  phase_systemd
  step "Bridge deployed."
  local ip; ip="$(lxc_get_ip "$MV_VMID" || true)"
  ok "SSE endpoint: http://${ip:-<lxc-156-ip>}:${MEMVAULT_BRIDGE_PORT:-3005}/sse?space=<repo-slug>"
}

main "$@"
