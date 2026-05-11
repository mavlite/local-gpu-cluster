#!/usr/bin/env bash
# 53-lxc-router.sh — Phase 7 of setup-runbook.md.
#
# Provision LXC 153 (llm-router):
#   - Lightweight LXC (2 cores / 4 GB), no GPU passthrough
#   - Python venv + FastAPI + httpx
#   - Deploy router app from scripts/files/router-app.py
#   - systemd unit that points at the AMD and NV LXC IPs

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

ROUTER_VMID="${ROUTER_VMID:-153}"
ROUTER_HOSTNAME="${ROUTER_HOSTNAME:-llm-router}"
ROUTER_CORES="${ROUTER_CORES:-2}"
ROUTER_MEMORY="${ROUTER_MEMORY:-4096}"
ROUTER_ROOTFS_SIZE="${ROUTER_ROOTFS_SIZE:-8}"
LXC_STORAGE="${LXC_STORAGE:-local-zfs}"
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"

# Upstream URLs the router proxies to. Defaults derive from per-LXC IPs in config.env.
AMD_IP="${AMD_IP:-192.168.6.151}"
NV_IP="${NV_IP:-192.168.6.152}"
V620_URL="${V620_URL:-http://${AMD_IP}:8080}"
FAST_URL="${FAST_URL:-http://${NV_IP}:8081}"
EMBED_URL="${EMBED_URL:-http://${NV_IP}:8082}"
RERANK_URL="${RERANK_URL:-http://${NV_IP}:8083}"
KEEPALIVE_INTERVAL="${KEEPALIVE_INTERVAL:-12}"
# Aliases routed to FAST_URL (3060). Default matches FAST_ALIAS in 52-lxc-nv.sh.
FAST_ALIASES="${FAST_ALIASES:-qwen3-4b-fast}"

APP_SRC="$LGC_DIR/files/router-app.py"
[[ -r "$APP_SRC" ]] || die "router-app.py not found at $APP_SRC"

phase_7_1_create() {
  step "7.1 — Create LXC $ROUTER_VMID ($ROUTER_HOSTNAME)"
  if lxc_exists "$ROUTER_VMID"; then
    skip "LXC $ROUTER_VMID already exists."
    return 0
  fi
  pct create "$ROUTER_VMID" "$LXC_TEMPLATE_STORAGE:vztmpl/$LXC_TEMPLATE_NAME" \
    --hostname "$ROUTER_HOSTNAME" \
    --cores "$ROUTER_CORES" \
    --memory "$ROUTER_MEMORY" \
    --rootfs "${LXC_STORAGE}:${ROUTER_ROOTFS_SIZE}" \
    --net0 "name=eth0,bridge=${BRIDGE},firewall=0,ip=dhcp,type=veth" \
    --features nesting=0 \
    --unprivileged 1 \
    --ostype ubuntu \
    --start 1
  sleep 5
}

phase_7_2_python_env() {
  step "7.2 — Python venv + FastAPI"
  ensure_lxc_started "$ROUTER_VMID"

  pct exec "$ROUTER_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt update
    apt install -y python3 python3-venv python3-pip

    id -u router >/dev/null 2>&1 || useradd -r -m -d /opt/llm-router -s /usr/sbin/nologin router
    mkdir -p /opt/llm-router
    chown router:router /opt/llm-router

    if [ ! -x /opt/llm-router/venv/bin/uvicorn ]; then
      sudo -u router bash -c '
        python3 -m venv /opt/llm-router/venv
        /opt/llm-router/venv/bin/pip install --upgrade pip
        /opt/llm-router/venv/bin/pip install fastapi "uvicorn[standard]" httpx
      '
    fi
GUEST
}

phase_7_3_deploy_app() {
  step "7.3 — Deploy router app.py"
  pct push "$ROUTER_VMID" "$APP_SRC" /opt/llm-router/app.py --perms 0644
  pct exec "$ROUTER_VMID" -- chown router:router /opt/llm-router/app.py
}

phase_7_4_systemd() {
  step "7.4 — Install systemd unit"
  pct exec "$ROUTER_VMID" -- env \
    "V620_URL=$V620_URL" \
    "FAST_URL=$FAST_URL" \
    "EMBED_URL=$EMBED_URL" \
    "RERANK_URL=$RERANK_URL" \
    "FAST_ALIASES=$FAST_ALIASES" \
    "KEEPALIVE_INTERVAL=$KEEPALIVE_INTERVAL" \
    bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/llm-router.service <<EOF
[Unit]
Description=LLM cluster router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=router
WorkingDirectory=/opt/llm-router
Environment="V620_URL=${V620_URL}"
Environment="FAST_URL=${FAST_URL}"
Environment="EMBED_URL=${EMBED_URL}"
Environment="RERANK_URL=${RERANK_URL}"
Environment="FAST_ALIASES=${FAST_ALIASES}"
Environment="KEEPALIVE_INTERVAL=${KEEPALIVE_INTERVAL}"
ExecStart=/opt/llm-router/venv/bin/uvicorn app:app \\
    --host 0.0.0.0 --port 8000 \\
    --timeout-keep-alive 300 \\
    --workers 1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable llm-router
    # restart (not just `enable --now`) so changed env vars take effect on re-run
    systemctl restart llm-router
    systemctl status llm-router --no-pager || true
GUEST
}

main() {
  phase_7_1_create
  phase_7_2_python_env
  phase_7_3_deploy_app
  phase_7_4_systemd

  step "Phase 7 complete."
  local ip; ip="$(lxc_get_ip "$ROUTER_VMID" || true)"
  ok "Router ready at IP: ${ip:-unknown}"
  echo "  Smoke test:"
  echo "    curl -s http://${ip:-<router-ip>}:8000/healthz | jq"
  echo "    curl -s http://${ip:-<router-ip>}:8000/v1/models | jq"
}

main "$@"
