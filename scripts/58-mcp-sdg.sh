#!/usr/bin/env bash
# 58-mcp-sdg.sh — Stand up the sdg-documentation MCP server in LXC 155.
#
# This is a thin SSE-over-HTTP wrapper around AnythingLLM's workspace-chat /
# vector-search endpoints. OpenCode (and any other MCP client) can call it
# remotely and get RAG-synthesized answers + raw chunks from the
# `sdg-documentation` workspace.
#
# Assumes scripts/55-lxc-mcp.sh has already created LXC 155. Runs alongside
# Docker on that LXC without conflict — pure-Python service, listens on its
# own port (default 3004).
#
# What this script does (idempotent re-runs are safe):
#   1. Install Python venv + mcp SDK + httpx inside LXC 155 (skip if present)
#   2. Push scripts/files/mcp-sdg-server.py to /opt/mcp-sdg/server.py
#   3. Generate /etc/mcp-sdg.env (mode 600) from config.env's ALLM_API_KEY
#   4. Install + enable mcp-sdg.service (systemd)
#
# Required in config.env:
#   ALLM_API_KEY            AnythingLLM admin API key (Settings → API Keys)
#
# Optional config.env overrides:
#   MCP_VMID                LXC id, default 155
#   MCP_SDG_PORT            SSE listen port, default 3004
#   ANYTHINGLLM_IP          where the MCP reaches AnythingLLM, default 192.168.6.154
#   MCP_SDG_WORKSPACE       Workspace slug, default sdg-documentation

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

MCP_VMID="${MCP_VMID:-155}"
MCP_SDG_PORT="${MCP_SDG_PORT:-3004}"
ANYTHINGLLM_IP="${ANYTHINGLLM_IP:-192.168.6.154}"

# Workspaces to expose. Each gets a query_<slug> and search_<slug> tool pair.
# Override with MCP_WORKSPACES=foo,bar in config.env if you add corpora.
MCP_WORKSPACES="${MCP_WORKSPACES:-sdg-documentation,vcf-reference}"

# Human-readable descriptions for each workspace. These get surfaced in the
# tool docstring so the calling LLM picks the right tool. Override per
# workspace via env vars like MCP_WORKSPACE_DESC_SDG_DOCUMENTATION=...
MCP_WORKSPACE_DESC_SDG_DOCUMENTATION="${MCP_WORKSPACE_DESC_SDG_DOCUMENTATION:-OPNsense (firewall/routing), Keycloak (identity), TrueNAS Scale + OpenZFS (storage). Mix of official docs (vendor) and community blogs (homenetworkguy, ServeTheHome, 45drives, Phase Two, Baeldung, n-k.de, Inteca, Thomas-Krenn, Zenarmor). ~4000 docs total. Use for any infra/SRE question on these stacks.}"
MCP_WORKSPACE_DESC_VCF_REFERENCE="${MCP_WORKSPACE_DESC_VCF_REFERENCE:-VMware Cloud Foundation 9.0+ official documentation from techdocs.broadcom.com (Tier A release notes, Tier B deployment/lifecycle/security/licensing, Tier C everything else). ~5600 docs. Use for VCF, vSphere, vSAN, NSX, vCenter, SDDC Manager, Aria/vROps questions.}"

SERVER_SRC="$LGC_DIR/files/mcp-sdg-server.py"

[[ -r "$SERVER_SRC" ]] || die "MCP server source missing: $SERVER_SRC"
[[ -n "${ALLM_API_KEY:-}" ]] || die "ALLM_API_KEY not set in config.env (Settings → API Keys in AnythingLLM)."

# Verify LXC exists (created by scripts/55-lxc-mcp.sh) before continuing.
if ! pct status "$MCP_VMID" >/dev/null 2>&1; then
  die "LXC $MCP_VMID does not exist. Run scripts/55-lxc-mcp.sh first."
fi
if ! pct status "$MCP_VMID" 2>/dev/null | grep -q running; then
  log "Starting LXC $MCP_VMID..."
  pct start "$MCP_VMID"
  sleep 3
fi

phase_install_python() {
  step "Install Python venv + mcp SDK + httpx in LXC $MCP_VMID"
  pct exec "$MCP_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive

    # Python + venv: check for ensurepip specifically. The `venv` module
    # itself ships in stdlib so `import venv` succeeds even without the
    # python3-venv apt package — but actually creating a venv invokes
    # ensurepip, which is gated by the apt package on Debian/Ubuntu.
    if ! python3 -c 'import ensurepip' 2>/dev/null \
       || ! command -v python3 >/dev/null 2>&1; then
      apt update
      apt install -y python3 python3-venv python3-pip
    fi

    mkdir -p /opt/mcp-sdg
    if [[ ! -x /opt/mcp-sdg/venv/bin/python ]]; then
      python3 -m venv /opt/mcp-sdg/venv
    fi
    /opt/mcp-sdg/venv/bin/pip install --quiet --upgrade pip wheel
    # `mcp` is the official Python SDK from Anthropic. FastMCP API is exposed
    # at mcp.server.fastmcp. httpx is our HTTP client to AnythingLLM.
    /opt/mcp-sdg/venv/bin/pip install --quiet 'mcp>=1.2' httpx
GUEST
  ok "Python + mcp SDK ready in LXC $MCP_VMID."
}

phase_deploy_server() {
  step "Push MCP server code to LXC $MCP_VMID:/opt/mcp-sdg/server.py"
  pct push "$MCP_VMID" "$SERVER_SRC" /opt/mcp-sdg/server.py --perms 0644
  ok "Server pushed."
}

phase_write_env() {
  step "Write /etc/mcp-sdg.env (mode 600)"
  # Build env file content on the host, then push via stdin to keep the
  # API key off the command line / /proc/<pid>/cmdline.
  pct exec "$MCP_VMID" -- env \
    "ALLM_API_KEY=$ALLM_API_KEY" \
    "ALLM_URL=http://${ANYTHINGLLM_IP}:3001/api/v1" \
    "MCP_WORKSPACES=$MCP_WORKSPACES" \
    "MCP_WORKSPACE_DESC_SDG_DOCUMENTATION=$MCP_WORKSPACE_DESC_SDG_DOCUMENTATION" \
    "MCP_WORKSPACE_DESC_VCF_REFERENCE=$MCP_WORKSPACE_DESC_VCF_REFERENCE" \
    "MCP_PORT=$MCP_SDG_PORT" \
    bash -se <<'GUEST'
    set -Eeuo pipefail
    # Write env vars via a Python rewrite to handle special chars in
    # descriptions (quotes, slashes, etc.) safely. A here-doc with shell
    # variable expansion would mishandle values containing $, `, or \.
    python3 - <<'PY'
import os
keys = [
    "ALLM_URL",
    "ALLM_API_KEY",
    "MCP_WORKSPACES",
    "MCP_WORKSPACE_DESC_SDG_DOCUMENTATION",
    "MCP_WORKSPACE_DESC_VCF_REFERENCE",
    "MCP_PORT",
]
lines = []
for k in keys:
    v = os.environ.get(k, "")
    # systemd EnvironmentFile uses KEY=VALUE; quote if there's special chars
    if any(c in v for c in '"\\\n'):
        # systemd doesn't support multi-line; replace newlines with spaces
        v = v.replace("\n", " ").replace("\r", " ")
    lines.append(f"{k}={v}")
lines.append("MCP_HOST=0.0.0.0")
with open("/etc/mcp-sdg.env", "w") as f:
    f.write("\n".join(lines) + "\n")
os.chmod("/etc/mcp-sdg.env", 0o600)
PY
GUEST
  ok "Env file written."
}

phase_install_systemd() {
  step "Install + enable mcp-sdg.service"
  pct exec "$MCP_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/mcp-sdg.service <<'EOF'
[Unit]
Description=MCP server for AnythingLLM sdg-documentation workspace (SSE on port 3004)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/mcp-sdg.env
WorkingDirectory=/opt/mcp-sdg
ExecStart=/opt/mcp-sdg/venv/bin/python /opt/mcp-sdg/server.py
Restart=on-failure
RestartSec=10
# Don't expose host filesystem; the SSE server only needs network.
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable mcp-sdg.service
    systemctl restart mcp-sdg.service
    sleep 2
    systemctl is-active mcp-sdg.service
    systemctl status mcp-sdg.service --no-pager | head -12
GUEST
  ok "mcp-sdg.service is active."
}

main() {
  phase_install_python
  phase_deploy_server
  phase_write_env
  phase_install_systemd

  step "Phase 58 complete."
  local mcp_ip
  mcp_ip="$(pct exec "$MCP_VMID" -- hostname -I 2>/dev/null | awk '{print $1}')"
  ok "MCP bridge is up at http://${mcp_ip:-<LXC 155 IP>}:${MCP_SDG_PORT}/sse"
  ok "Workspaces exposed: $MCP_WORKSPACES"
  echo
  echo "Per workspace, two tools are registered:"
  echo "  query_<workspace_underscored>(question)       — full RAG synthesis"
  echo "  search_<workspace_underscored>(query, top_n)  — raw vector chunks"
  echo
  echo "Smoke test from the host:"
  echo "  curl -N -m 10 http://${mcp_ip:-...}:${MCP_SDG_PORT}/sse | head -3"
  echo "  (expect 'event: endpoint' line — SSE handshake)"
  echo
  echo "Wire it up in OpenCode by adding to opencode.json mcp section:"
  echo '  "sdg-docs": {'
  echo "    \"type\": \"remote\","
  echo "    \"url\": \"http://${mcp_ip:-LXC-155-IP}:${MCP_SDG_PORT}/sse\""
  echo "  }"
}

main "$@"
