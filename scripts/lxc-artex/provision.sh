#!/usr/bin/env bash
# provision.sh — build LXC 158 "artex-redteam": ARTEX (artex-global fork) as an
# egress-locked autonomous-pentest box for AUTHORISED testing of the operator's
# own colo infra. Sibling to LXC 157 (RedteamAgent); mirrors its hard-scope model.
#
# Run FROM THE PROXMOX HOST. Idempotent-ish; re-running recreates nothing it finds.
# Secrets are never written here: the router key is copied from LXC 157 at runtime
# and the postgres password is generated. See env.example for the .env layout.
#
# Phases: create CT -> Docker -> build artex:local from the pinned commit ->
# compose up (+ model wiring) -> egress lock (install-egress-lock.sh is the file
# redteam-egress-lock.sh in this directory). The egress lock is applied LAST,
# because the build needs open internet (Go proxy, npm, apt, Playwright, GHCR bases).
set -euo pipefail

CTID=158
HOSTNAME=artex-redteam
IP=192.168.6.158/24
GW=192.168.6.1
BRIDGE=vmbr0
TEMPLATE=local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst
STORAGE=tank-lxc
CORES=6
MEM=12288
SWAP=2048
DISK=60
PINNED=133f3cf97f2d153227a1a95d3c4b7cd5b72020a6   # jiwoochris/artex-global, source-reviewed
ROUTER_SRC_CT=157                                  # copy the router key from here

echo "[1/5] create + start CT $CTID (privileged, nesting+keyctl, onboot=0)"
pct create "$CTID" "$TEMPLATE" \
  --hostname "$HOSTNAME" \
  --cores "$CORES" --memory "$MEM" --swap "$SWAP" \
  --features nesting=1,keyctl=1 \
  --nameserver "$GW" \
  --net0 "name=eth0,bridge=$BRIDGE,firewall=0,gw=$GW,ip=$IP,type=veth" \
  --onboot 0 --ostype debian \
  --rootfs "$STORAGE:$DISK" \
  --description "ARTEX artex-global @${PINNED:0:7} PRIVILEGED - authorised pentest of own colo. Egress-locked. Model via router 153."
pct start "$CTID"; sleep 8

echo "[2/5] Docker + base (IPv6 off, rsync for build.sh)"
pct exec "$CTID" -- bash -c '
  set -e; export DEBIAN_FRONTEND=noninteractive
  printf "net.ipv6.conf.all.disable_ipv6=1\nnet.ipv6.conf.default.disable_ipv6=1\n" >/etc/sysctl.d/99-disable-ipv6.conf
  sysctl -p /etc/sysctl.d/99-disable-ipv6.conf >/dev/null 2>&1 || true
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg git jq rsync file >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" >/etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null 2>&1'

echo "[3/5] build artex:local from pinned commit $PINNED (Go 1.26.3 + Node 20)"
pct exec "$CTID" -- bash -c "
  set -e; export DEBIAN_FRONTEND=noninteractive; cd /root
  if ! /usr/local/go/bin/go version 2>/dev/null | grep -q go1.26.3; then
    curl -fsSL https://go.dev/dl/go1.26.3.linux-amd64.tar.gz -o /tmp/go.tgz
    rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tgz && rm -f /tmp/go.tgz
  fi
  export PATH=/usr/local/go/bin:\$PATH
  if ! node -v 2>/dev/null | grep -q '^v20'; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1
    apt-get install -y -qq nodejs >/dev/null
  fi
  rm -rf artex-global
  git clone -q https://github.com/jiwoochris/artex-global.git
  cd artex-global && git checkout -q $PINNED
  [ \"\$(git rev-parse HEAD)\" = \"$PINNED\" ] || { echo PIN_MISMATCH; exit 1; }
  ARTEX_OUTPUT=dist/amd64/artex ARTEX_TARGET_OS=linux ARTEX_TARGET_ARCH=amd64 PATH=/usr/local/go/bin:\$PATH bash build.sh
  docker build -t artex:local .
  sed -i 's#image: ghcr.io/jiwoochris/artex-global:.*#image: artex:local#' docker-compose.yml"

echo "[4/5] .env (router key copied from CT $ROUTER_SRC_CT; pg password generated) + compose up"
KEY=$(pct exec "$ROUTER_SRC_CT" -- python3 -c 'import json;print(json.load(open("/root/redteam-agent/.opencode/opencode.json"))["provider"]["router"]["options"]["apiKey"])')
PGPW=$(openssl rand -hex 16)
pct exec "$CTID" -- bash -c 'umask 077; cat > /root/artex-global/.env' <<EOF
POSTGRES_USER=artex
POSTGRES_DB=artex
POSTGRES_PASSWORD=$PGPW
ARTEX_LLM_PROVIDER=openai
ARTEX_LLM_BASE_URL=http://192.168.6.153:8000/v1
ARTEX_LLM_MODEL=qwen3.8-redteam
ARTEX_LLM_STREAM=true
OPENAI_API_KEY=$KEY
ARTEX_LANG=en
ARTEX_BIND=0.0.0.0
EOF
unset KEY PGPW
pct exec "$CTID" -- chmod 600 /root/artex-global/.env
pct exec "$CTID" -- bash -c 'cd /root/artex-global && docker compose up -d'

echo "[5/5] egress lock (hard scope boundary) — install + enable + docker drop-in"
LOCK_DIR="$(cd "$(dirname "$0")" && pwd)"
pct push "$CTID" "$LOCK_DIR/redteam-egress-lock.sh" /usr/local/sbin/redteam-egress-lock.sh
pct exec "$CTID" -- chmod 0755 /usr/local/sbin/redteam-egress-lock.sh
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/redteam-egress-lock.service' <<'UNIT'
[Unit]
Description=Red-team egress allowlist (ARTEX; targets + model only; hard scope boundary)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/redteam-egress-lock.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
pct exec "$CTID" -- bash -c 'mkdir -p /etc/systemd/system/docker.service.d'
pct exec "$CTID" -- bash -c 'cat > /etc/systemd/system/docker.service.d/redteam-egress.conf' <<'CONF'
[Service]
ExecStartPost=/usr/local/sbin/redteam-egress-lock.sh
CONF
pct exec "$CTID" -- systemctl daemon-reload
pct exec "$CTID" -- systemctl enable --now redteam-egress-lock.service

echo "DONE. UI: http://192.168.6.158:8787 (setup token in: docker logs artex-global-artex-1)."
echo "Verify scope: from the artex container, router .153 reachable, internet + rest of cluster REJECTed."
