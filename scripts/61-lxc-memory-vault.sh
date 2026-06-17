#!/usr/bin/env bash
# 61-lxc-memory-vault.sh — provision LXC 156 (memory-vault).
#
#   - Unprivileged LXC with nesting=1,keyctl=1 for Docker
#   - ZFS dataset tank/memory-vault, bind-mounted at /opt/memory-vault-data
#   - Install Docker CE (DEB822 sources, docker.asc keyring)
#   - Clone memory-vault, generate DB password, deploy via docker compose
#     with scripts/files/memory-vault-compose.override.yml
#
# The MCP-over-SSE bridge is deployed separately by 62-memory-vault-bridge.sh.
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

MV_VMID="${MEMVAULT_VMID:-156}"
MV_HOSTNAME="${MEMVAULT_HOSTNAME:-memory-vault}"
MV_CORES="${MEMVAULT_CORES:-4}"
MV_MEMORY="${MEMVAULT_MEMORY:-8192}"
MV_ROOTFS_SIZE="${MEMVAULT_ROOTFS_SIZE:-32}"
MV_STORAGE_MOUNT="${MEMVAULT_STORAGE_MOUNT:-/tank/memory-vault}"
MV_IMAGE="${MEMVAULT_IMAGE:-ghcr.io/mihaibuilds/memory-vault:1.0.1}"
LXC_STORAGE="${LXC_STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"
MV_REPO_URL="${MEMVAULT_REPO_URL:-https://github.com/MihaiBuilds/memory-vault.git}"
OVERRIDE_SRC="$LGC_DIR/files/memory-vault-compose.override.yml"

[[ -r "$OVERRIDE_SRC" ]] || die "Missing compose override: $OVERRIDE_SRC"

phase_create() {
  step "Create LXC $MV_VMID ($MV_HOSTNAME)"
  if lxc_exists "$MV_VMID"; then
    skip "LXC $MV_VMID already exists."
    return 0
  fi
  pct create "$MV_VMID" "$LXC_TEMPLATE_STORAGE:vztmpl/$LXC_TEMPLATE_NAME" \
    --hostname "$MV_HOSTNAME" \
    --cores "$MV_CORES" \
    --memory "$MV_MEMORY" \
    --swap 2048 \
    --rootfs "${LXC_STORAGE}:${MV_ROOTFS_SIZE}" \
    --net0 "name=eth0,bridge=${BRIDGE},firewall=0,ip=dhcp,type=veth" \
    --features "nesting=1,keyctl=1" \
    --unprivileged 1 \
    --ostype ubuntu \
    --onboot 1 \
    --startup order=5 \
    --start 0
}

phase_bind_mount() {
  step "ZFS dataset + bind-mount $MV_STORAGE_MOUNT"
  if ! zfs list "tank/memory-vault" >/dev/null 2>&1; then
    zfs create "tank/memory-vault"
  else
    skip "Dataset tank/memory-vault exists."
  fi
  mkdir -p "$MV_STORAGE_MOUNT"
  # Unprivileged LXC: container-root maps to host UID 100000.
  if [[ "$(stat -c '%u:%g' "$MV_STORAGE_MOUNT")" != "100000:100000" ]]; then
    chown -R 100000:100000 "$MV_STORAGE_MOUNT"
  fi
  pct_set_if_changed "$MV_VMID" mp0 "$MV_STORAGE_MOUNT,mp=/opt/memory-vault-data"
  ensure_lxc_started "$MV_VMID"
}

phase_docker() {
  step "Install Docker CE in LXC $MV_VMID"
  if pct exec "$MV_VMID" -- bash -c 'command -v docker >/dev/null 2>&1'; then
    skip "Docker already installed."
    return 0
  fi
  pct exec "$MV_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt update
    apt install -y ca-certificates curl gnupg lsb-release git
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
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
    apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
GUEST
}

phase_deploy() {
  step "Clone + deploy Memory Vault stack"
  # Push the override file into the LXC.
  pct push "$MV_VMID" "$OVERRIDE_SRC" /opt/memory-vault-override.yml --perms 0644

  pct exec "$MV_VMID" -- env "MV_REPO_URL=$MV_REPO_URL" "MV_IMAGE=$MV_IMAGE" bash -se <<'GUEST'
    set -Eeuo pipefail
    # Prepare bind-mounted PGDATA dir, owned by the postgres uid (999) inside the
    # pgvector/postgres image so initdb can write it.
    mkdir -p /opt/memory-vault-data/pgdata
    chown -R 999:999 /opt/memory-vault-data/pgdata

    if [[ ! -d /opt/memory-vault/.git ]]; then
      git clone --depth 1 "$MV_REPO_URL" /opt/memory-vault
    fi
    cd /opt/memory-vault
    install -m 0644 /opt/memory-vault-override.yml docker-compose.override.yml

    # Generate a strong DB password once; persist in .env for compose substitution.
    if [[ ! -f /opt/memory-vault/.env ]]; then
      umask 077
      printf 'MEMVAULT_DB_PASSWORD=%s\n' "$(openssl rand -hex 24)" > /opt/memory-vault/.env
    fi
    chmod 600 /opt/memory-vault/.env

    # Pin the app image tag from config (override base if it floats).
    docker compose pull
    docker compose up -d
GUEST
}

main() {
  phase_create
  phase_bind_mount
  phase_docker
  phase_deploy
  step "LXC 156 provisioned."
  local ip; ip="$(lxc_get_ip "$MV_VMID" || true)"
  ok "Memory Vault stack at: http://${ip:-<lxc-156-ip>}:${MEMVAULT_API_PORT:-8000}"
  echo "  Next: scripts/62-memory-vault-bridge.sh (mint token + deploy MCP bridge)"
}

main "$@"
