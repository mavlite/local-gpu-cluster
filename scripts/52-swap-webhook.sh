#!/usr/bin/env bash
# 52-swap-webhook.sh — install the swap-webhook service on the Proxmox host.
#
# The swap-webhook allows the LLM router (LXC 153) to trigger model profile
# swaps automatically, eliminating the need to SSH into the host when switching
# models. The router detects a cross-profile request, calls this webhook, waits
# for the swap to complete (with SSE keepalive pings to the client), then
# forwards the original request.
#
# What this script does:
#   1. Detects the vmbr0 bridge IP (the address LXC containers see the host at).
#   2. Generates a SWAP_WEBHOOK_KEY (or keeps the existing one on re-run).
#   3. Writes /etc/swap-webhook.env (mode 600, root:root).
#   4. Installs scripts/files/swap-webhook.py to /usr/local/sbin/.
#   5. Installs + enables the swap-webhook systemd unit on the host.
#   6. Injects SWAP_WEBHOOK_URL + SWAP_WEBHOOK_KEY into LXC 153's /etc/router.env
#      via targeted upsert — ROUTER_API_KEY and all other keys are untouched.
#   7. Restarts llm-router in LXC 153 so it picks up the new env vars.
#
# Safe to re-run (idempotent):
#   - SWAP_WEBHOOK_KEY is preserved on re-run (no rotation).
#   - Does NOT re-run 53-lxc-router.sh (ROUTER_API_KEY is never touched).
#
# Run from the repo root on the Proxmox host as root:
#   ./scripts/52-swap-webhook.sh

set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

ROUTER_VMID="${ROUTER_VMID:-153}"
SWAP_PORT="${SWAP_PORT:-9100}"
WEBHOOK_ENV="/etc/swap-webhook.env"
SWAP_BIN="/usr/local/sbin/swap-webhook.py"
SWAP_SRC="${LGC_DIR}/files/swap-webhook.py"
# Point at the in-repo script (not a copy) so it resolves config.env and
# 51-lxc-amd.sh relative to its own location. See swap-chat-model.sh:52-54.
SWAP_SCRIPT="${LGC_DIR}/swap-chat-model.sh"

[[ -f "$SWAP_SRC" ]]    || die "swap-webhook.py not found at $SWAP_SRC"
[[ -f "$SWAP_SCRIPT" ]] || die "swap-chat-model.sh not found at $SWAP_SCRIPT"

# ── 1. Detect vmbr0 bridge IP ────────────────────────────────────────────────
step "1 — Detect vmbr0 bridge IP"
HOST_IP=$(ip -4 -o addr show vmbr0 2>/dev/null \
  | awk '{gsub(/\/.*/,"",$4); print $4; exit}')
[[ -n "$HOST_IP" ]] || die "Could not detect vmbr0 IP. Check: ip addr show vmbr0"
ok "vmbr0 IP: $HOST_IP"

# ── 2. Generate or preserve SWAP_WEBHOOK_KEY ─────────────────────────────────
step "2 — SWAP_WEBHOOK_KEY"
EXISTING_KEY=""
if [[ -f "$WEBHOOK_ENV" ]]; then
  EXISTING_KEY=$(awk -F= '/^SWAP_WEBHOOK_KEY=/{print $2}' "$WEBHOOK_ENV" \
    | tr -d '[:space:]') || true
fi
if [[ -n "$EXISTING_KEY" ]]; then
  SWAP_WEBHOOK_KEY="$EXISTING_KEY"
  ok "Keeping existing SWAP_WEBHOOK_KEY"
else
  SWAP_WEBHOOK_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  ok "Generated new SWAP_WEBHOOK_KEY"
fi

# ── 3. Write /etc/swap-webhook.env ───────────────────────────────────────────
step "3 — Write $WEBHOOK_ENV"
install -m 600 /dev/null "$WEBHOOK_ENV"
cat > "$WEBHOOK_ENV" <<EOF
SWAP_WEBHOOK_KEY=${SWAP_WEBHOOK_KEY}
SWAP_SCRIPT=${SWAP_SCRIPT}
SWAP_WEBHOOK_PORT=${SWAP_PORT}
SWAP_WEBHOOK_BIND=${HOST_IP}
EOF
ok "Written $WEBHOOK_ENV (mode 600, bind=${HOST_IP}:${SWAP_PORT})"

# ── 4. Install swap-webhook.py ────────────────────────────────────────────────
step "4 — Install swap-webhook.py"
install -m 755 "$SWAP_SRC" "$SWAP_BIN"
ok "Installed $SWAP_BIN"

# ── 5. Install + enable systemd unit ─────────────────────────────────────────
step "5 — swap-webhook.service"
cat > /etc/systemd/system/swap-webhook.service <<EOF
[Unit]
Description=LLM profile swap webhook (triggers swap-chat-model.sh via HTTP)
After=network.target pve-guests.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${WEBHOOK_ENV}
ExecStart=/usr/bin/python3 ${SWAP_BIN}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=swap-webhook

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable swap-webhook
systemctl restart swap-webhook
ok "swap-webhook service enabled and started"

# ── 6. Inject into LXC 153's /etc/router.env ─────────────────────────────────
step "6 — Wire SWAP_WEBHOOK_URL + SWAP_WEBHOOK_KEY into LXC $ROUTER_VMID /etc/router.env"
lxc_running "$ROUTER_VMID" \
  || die "LXC $ROUTER_VMID is not running. Start it first: pct start $ROUTER_VMID"

SWAP_WEBHOOK_URL="http://${HOST_IP}:${SWAP_PORT}"

# Targeted upsert: only touches SWAP_WEBHOOK_URL and SWAP_WEBHOOK_KEY.
# ROUTER_API_KEY and all other existing keys are preserved exactly.
# Same atomic Python rewrite pattern as 53-lxc-router.sh upsert_env.
pct exec "$ROUTER_VMID" -- python3 - \
  "SWAP_WEBHOOK_URL" "$SWAP_WEBHOOK_URL" \
  "SWAP_WEBHOOK_KEY" "$SWAP_WEBHOOK_KEY" \
  /etc/router.env <<'PY'
import os, sys, tempfile

args = sys.argv[1:]
# Layout: key1 val1 key2 val2 ... path
path = args[-1]
pairs = [(args[i], args[i + 1]) for i in range(0, len(args) - 1, 2)]
targets = dict(pairs)

try:
    with open(path) as f:
        lines = f.readlines()
except FileNotFoundError:
    lines = []

written = {k: False for k in targets}
out = []
for line in lines:
    matched = None
    for k in targets:
        if line.startswith(k + "="):
            matched = k
            break
    if matched:
        if not written[matched]:
            out.append(f"{matched}={targets[matched]}\n")
            written[matched] = True
        # Extra duplicates are dropped so re-runs stay clean.
    else:
        out.append(line)

missing = [k for k in targets if not written[k]]
if missing:
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    out.append("\n# Added by scripts/52-swap-webhook.sh\n")
    for k in missing:
        out.append(f"{k}={targets[k]}\n")

d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".router.env.swap.")
try:
    with os.fdopen(fd, "w") as f:
        f.writelines(out)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
ok "Injected SWAP_WEBHOOK_URL=${SWAP_WEBHOOK_URL} into LXC ${ROUTER_VMID} /etc/router.env"

# ── 7. Restart llm-router to pick up new env vars ────────────────────────────
step "7 — Restart llm-router in LXC $ROUTER_VMID"
pct exec "$ROUTER_VMID" -- systemctl restart llm-router
ok "llm-router restarted"

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "============================================================"
echo " swap-webhook deployed"
echo " Webhook URL : ${SWAP_WEBHOOK_URL}"
echo " healthz     : curl http://${HOST_IP}:${SWAP_PORT}/healthz"
echo "============================================================"
echo
echo "Smoke tests:"
echo "  curl -sf http://${HOST_IP}:${SWAP_PORT}/healthz | python3 -m json.tool"
echo
echo "  ROUTER_KEY=\$(pct exec ${ROUTER_VMID} -- awk -F= '/^ROUTER_API_KEY=/{print \$2}' /etc/router.env)"
echo "  curl -sf -H \"Authorization: Bearer \$ROUTER_KEY\" \\"
echo "    http://192.168.6.${ROUTER_VMID}:8000/healthz | python3 -m json.tool"
