#!/usr/bin/env bash
# 55-lxc-mcp.sh — Phase 9 of setup-runbook.md.
#
# Provision LXC 155 (mcp-stack):
#   - Unprivileged LXC with nesting=1,keyctl=1 for Docker
#   - Install Docker CE
#   - Optionally rsync MCP source from a previous host (set OLD_HOST in config.env)
#   - Build and start the MCP containers
#
# MCPs assumed to live under /opt and expose ports 3002/3003/3004.
# Override the MCPS list in config.env if your set differs.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

MCP_VMID="${MCP_VMID:-155}"
MCP_HOSTNAME="${MCP_HOSTNAME:-mcp-stack}"
MCP_CORES="${MCP_CORES:-2}"
MCP_MEMORY="${MCP_MEMORY:-4096}"
MCP_ROOTFS_SIZE="${MCP_ROOTFS_SIZE:-16}"
LXC_STORAGE="${LXC_STORAGE:-local-lvm}"   # V620-only: ext4 + LVM-thin (was local-zfs)
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"
OLD_HOST="${OLD_HOST:-}"
ANYTHINGLLM_IP="${ANYTHINGLLM_IP:-192.168.6.154}"

# Default MCP set from the runbook. Override in config.env (MCPS=(a b c)).
# Note: declaring MCPS unconditionally would shadow any value from config.env,
# so we test for unset-or-empty without tripping `set -u`. (${#arr[@]:-0} is
# invalid bash — :- can't be combined with the array-length syntax.)
if [[ -z "${MCPS+x}" ]] || (( ${#MCPS[@]} == 0 )); then
  MCPS=(anythingllm-mcp broadcom-techdocs-mcp sdg-mcp)
fi

is_ipv4 "$ANYTHINGLLM_IP" \
  || die "ANYTHINGLLM_IP='$ANYTHINGLLM_IP' is not a valid IPv4. Fix config.env."

phase_9_1_create() {
  step "9.1 — Create LXC $MCP_VMID ($MCP_HOSTNAME)"
  if lxc_exists "$MCP_VMID"; then
    skip "LXC $MCP_VMID already exists."
    return 0
  fi
  pct create "$MCP_VMID" "$LXC_TEMPLATE_STORAGE:vztmpl/$LXC_TEMPLATE_NAME" \
    --hostname "$MCP_HOSTNAME" \
    --cores "$MCP_CORES" \
    --memory "$MCP_MEMORY" \
    --rootfs "${LXC_STORAGE}:${MCP_ROOTFS_SIZE}" \
    --net0 "name=eth0,bridge=${BRIDGE},firewall=0,ip=dhcp,type=veth" \
    --features "nesting=1,keyctl=1" \
    --unprivileged 1 \
    --ostype ubuntu \
    --start 1
  sleep 5
}

phase_9_2_docker() {
  step "9.2 — Install Docker CE"
  if pct exec "$MCP_VMID" -- bash -c 'command -v docker >/dev/null 2>&1'; then
    skip "Docker already installed."
    return 0
  fi
  pct exec "$MCP_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt update
    apt install -y ca-certificates curl gnupg lsb-release

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

    apt update
    apt install -y docker-ce docker-ce-cli containerd.io \
                   docker-buildx-plugin docker-compose-plugin

    systemctl enable --now docker
GUEST
}

phase_9_3_migrate() {
  step "9.3 — Migrate MCP source from previous host (optional)"
  if [[ -z "$OLD_HOST" ]]; then
    skip "OLD_HOST not set in config.env — skipping rsync. Provide your MCP source under /opt/<name>/ manually."
    return 0
  fi

  # Build the source list as a shell-safe array of paths.
  local paths=()
  for mcp in "${MCPS[@]}"; do
    paths+=("/opt/$mcp")
  done

  log "Pulling ${paths[*]} from $OLD_HOST"
  local quoted_paths
  quoted_paths="$(printf '%q ' "${paths[@]}")"
  # shellcheck disable=SC2029
  ssh "$OLD_HOST" "tar czf - $quoted_paths" \
    | pct exec "$MCP_VMID" -- tar xzf - -C /
}

# MCP slug must be a simple identifier — no shell metachars, no path traversal.
# This is enforced before any value is interpolated into a guest-side command.
_validate_mcp_slug() {
  local slug="$1"
  if [[ ! "$slug" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
    die "Invalid MCP slug '$slug': must match [A-Za-z0-9][A-Za-z0-9_.-]*"
  fi
}

phase_9_4_rewrite_urls() {
  step "9.4 — Rewrite MCP configs to point at new AnythingLLM IP ($ANYTHINGLLM_IP)"
  for mcp in "${MCPS[@]}"; do
    _validate_mcp_slug "$mcp"
    if pct exec "$MCP_VMID" -- test -d "/opt/$mcp"; then
      # Pass values via `env VAR=val` so they're available inside the quoted
      # heredoc without unsafe interpolation. ANYTHINGLLM_IP is already
      # validated by is_ipv4() up top; MCP_SLUG is validated above.
      pct exec "$MCP_VMID" -- env "MCP_SLUG=$mcp" "ALLM_IP=$ANYTHINGLLM_IP" bash -se <<'GUEST'
        set -Eeuo pipefail
        cd "/opt/$MCP_SLUG"
        if [ -f docker-compose.yml ]; then
          sed -i "s|http://[^:]*:3001|http://${ALLM_IP}:3001|g" docker-compose.yml
        fi
        if [ -f .env ]; then
          sed -i "s|ANYTHINGLLM_BASE_URL=.*|ANYTHINGLLM_BASE_URL=http://${ALLM_IP}:3001|" .env
        fi
GUEST
      ok "Updated $mcp"
    else
      warn "/opt/$mcp not found in LXC — supply it before building."
    fi
  done
}

phase_9_5_build_and_start() {
  step "9.5 — docker compose up -d for each MCP"
  for mcp in "${MCPS[@]}"; do
    _validate_mcp_slug "$mcp"
    if pct exec "$MCP_VMID" -- test -f "/opt/$mcp/docker-compose.yml"; then
      log "Bringing up $mcp"
      pct exec "$MCP_VMID" -- env "MCP_SLUG=$mcp" bash -se <<'GUEST'
        set -Eeuo pipefail
        cd "/opt/$MCP_SLUG"
        docker compose down 2>/dev/null || true
        docker compose up -d --build
GUEST
    else
      warn "Skipping $mcp — no docker-compose.yml at /opt/$mcp/"
    fi
  done
}

main() {
  phase_9_1_create
  phase_9_2_docker
  phase_9_3_migrate
  phase_9_4_rewrite_urls
  phase_9_5_build_and_start

  step "Phase 9 complete."
  local ip; ip="$(lxc_get_ip "$MCP_VMID" || true)"
  ok "MCP stack at IP: ${ip:-unknown}"
  echo "  Probe SSE endpoints (default ports 3002/3003/3004):"
  echo "    pct exec $MCP_VMID -- bash -c 'for p in 3002 3003 3004; do"
  echo "      printf \"port \$p: \"; timeout 3 curl -sN -H Accept:text/event-stream"
  echo "        http://localhost:\$p/sse 2>&1 | head -1; done'"
}

main "$@"
