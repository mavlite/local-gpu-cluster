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
LXC_STORAGE="${LXC_STORAGE:-local-lvm}"   # V620-only: ext4 + LVM-thin (was local-zfs)
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"

# V620-only: all upstream URLs target LXC 151 (the V620 LXC) on different ports.
# FAST_URL + FAST_ALIASES removed (no more 3060 fast-chat tier).
AMD_IP="${AMD_IP:-192.168.6.151}"
V620_URL="${V620_URL:-http://${AMD_IP}:8080}"
EMBED_URL="${EMBED_URL:-http://${AMD_IP}:8082}"
RERANK_URL="${RERANK_URL:-http://${AMD_IP}:8083}"
KEEPALIVE_INTERVAL="${KEEPALIVE_INTERVAL:-12}"

# Model warm-up keepalive timer (Phase 7.5) — distinct from KEEPALIVE_INTERVAL
# above (which is the SSE ping cadence during streaming). Default OFF: the
# periodic warm-up ping keeps the chat model hot in VRAM, but on an otherwise
# idle single-user cluster it wakes the GPU and cycles the fans every few
# minutes. Set ROUTER_KEEPALIVE=on to restore warm models / snappy cold starts.
ROUTER_KEEPALIVE="${ROUTER_KEEPALIVE:-off}"
ROUTER_KEEPALIVE_INTERVAL="${ROUTER_KEEPALIVE_INTERVAL:-5min}"

# Admission control + rate limit (consumed by router-app.py rewrite).
# CHAT_CONCURRENCY=1 matches the upstream chat unit's --parallel 1, which is
# set that way to give every request the full Qwen3.6 trained context window
# (n_ctx_train=262144 = 256K). With parallel=1, allowing CHAT_CONCURRENCY > 1
# would just queue inside llama-server with worse SSE/keepalive behavior.
# Sub-agent calls from OpenCode/Cline queue at the router instead, which is
# the cleaner spot to hold them (router can emit keepalives while waiting).
CHAT_CONCURRENCY="${CHAT_CONCURRENCY:-1}"
EMBED_CONCURRENCY="${EMBED_CONCURRENCY:-4}"
# MAX_CHAT_INPUT_TOKENS sized at ~200K to use most of the model's 256K window
# while reserving ~56K for output + thinking. opencode.json's per-model
# limit.context should be set to this value (or limit.context - limit.output
# should equal this) so OpenCode's compaction triggers before the router 413s.
MAX_CHAT_INPUT_TOKENS="${MAX_CHAT_INPUT_TOKENS:-200000}"
MAX_EMBED_INPUT_TOKENS="${MAX_EMBED_INPUT_TOKENS:-16384}"  # must match EMBED_CTX/EMBED_PARALLEL per-slot ctx on LXC 151
RATE_LIMIT_CHAT="${RATE_LIMIT_CHAT:-60/minute}"
RATE_LIMIT_EMBED="${RATE_LIMIT_EMBED:-200/minute}"
RATE_LIMIT_TAVILY="${RATE_LIMIT_TAVILY:-30/minute}"
# Tavily Search API key. Set in config.env (NOT committed). When empty the
# /v1/tavily/search endpoint and any Tavily-based tools (tavily_search,
# tavily_extract, tavily_crawl, tavily_map) return an error envelope.
# Get a key at https://tavily.com. Free tier = 1k searches/month.
TAVILY_API_KEY="${TAVILY_API_KEY:-}"

# Server-side tool execution. When a chat-completion request includes
# `"tool_execution": "server"`, the router runs the OpenAI tools/tool_calls
# multi-turn loop internally (executes Tavily / web_fetch / etc. server-side,
# feeds results back into the conversation) instead of returning tool_calls
# to the client. Default "client" preserves the legacy pass-through behavior
# expected by OpenCode / Cline / Continue. MAX_TOOL_ITERATIONS caps the
# multi-turn loop to prevent runaways (each iteration is one upstream chat
# completion + N tool executions).
MAX_TOOL_ITERATIONS="${MAX_TOOL_ITERATIONS:-10}"
TOOL_EXECUTION_DEFAULT="${TOOL_EXECUTION_DEFAULT:-client}"

# web_fetch tool safety. Caps response body size and request timeout to
# prevent runaway downloads / hung requests. SSRF guards (loopback + private
# range deny list) are hardcoded in router-app.py.
WEB_FETCH_MAX_SIZE_KB="${WEB_FETCH_MAX_SIZE_KB:-1024}"
WEB_FETCH_TIMEOUT_SECONDS="${WEB_FETCH_TIMEOUT_SECONDS:-15}"
PROXMOX_HOST_IP="${PROXMOX_HOST_IP:-192.168.6.150}"
METRICS_ALLOWED_IPS="${METRICS_ALLOWED_IPS:-127.0.0.1,${PROXMOX_HOST_IP}}"
# CORS allow-origins. Needed when HTML pages loaded from file:// (Origin: null)
# or arbitrary LAN origins call this router via fetch(). Default "*" is OK
# because (a) this LXC sits on a LAN-only IP behind your firewall, and
# (b) every protected endpoint already requires Bearer auth. Tighten to an
# explicit comma-separated origin list (e.g., "http://192.168.6.150,https://app.lan")
# once the legitimate caller set is known.
CORS_ALLOW_ORIGINS="${CORS_ALLOW_ORIGINS:-*}"

# AMD_VMID needed to fetch LLAMACPP_API_KEY from LXC 151 in phase_7_1_5
AMD_VMID="${AMD_VMID:-151}"

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
    --onboot 1 \
    --startup order=2,up=5 \
    --start 1
  sleep 5
}

phase_7_1_5_api_keys() {
  step "7.1.5 — Generate ROUTER_API_KEY + fetch LLAMACPP_API_KEY from LXC 151"
  pct exec "$ROUTER_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    mkdir -p /etc
    # Idempotent: only generate if not present
    if ! grep -q "^ROUTER_API_KEY=" /etc/router.env 2>/dev/null; then
      echo "ROUTER_API_KEY=$(openssl rand -hex 32)" >> /etc/router.env
    fi
    chmod 600 /etc/router.env
    chown root:root /etc/router.env
GUEST

  # Pull LLAMACPP_API_KEY from LXC 151 (must have been generated by 51-lxc-amd.sh).
  # Strip any whitespace/CR — awk output can carry a trailing newline that breaks sed.
  local llamacpp_key
  llamacpp_key="$(pct exec "$AMD_VMID" -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ -z "$llamacpp_key" ]]; then
    warn "LLAMACPP_API_KEY not found on LXC $AMD_VMID. The router will fail upstream auth until 51-lxc-amd.sh has run and generated it. Continuing anyway."
  else
    pct exec "$ROUTER_VMID" -- env "LLAMACPP_KEY=$llamacpp_key" bash -se <<'GUEST'
      set -Eeuo pipefail
      : "${LLAMACPP_KEY:?LLAMACPP_KEY not propagated from host}"
      if grep -q "^LLAMACPP_API_KEY=" /etc/router.env 2>/dev/null; then
        sed -i "s|^LLAMACPP_API_KEY=.*|LLAMACPP_API_KEY=${LLAMACPP_KEY}|" /etc/router.env
      else
        echo "LLAMACPP_API_KEY=${LLAMACPP_KEY}" >> /etc/router.env
      fi
      chmod 600 /etc/router.env
GUEST
  fi
  ok "API keys persisted to /etc/router.env on LXC $ROUTER_VMID"
}

phase_7_2_python_env() {
  step "7.2 — Python venv + FastAPI + slowapi + Prometheus instrumentator"
  ensure_lxc_started "$ROUTER_VMID"

  pct exec "$ROUTER_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt update
    # curl is required by the keepalive script (/usr/local/bin/llm-router-keepalive.sh).
    # Without it, the timer fails silently every 5-6 minutes — keepalive doesn't
    # do harm, but the model gradually pages out of VRAM and first requests after
    # idle pay a cold-start tax.
    apt install -y python3 python3-venv python3-pip openssh-server openssl curl

    id -u router >/dev/null 2>&1 || useradd -r -m -d /opt/llm-router -s /usr/sbin/nologin router
    mkdir -p /opt/llm-router
    chown router:router /opt/llm-router

    if [ ! -x /opt/llm-router/venv/bin/uvicorn ]; then
      sudo -u router bash -c '
        python3 -m venv /opt/llm-router/venv
        /opt/llm-router/venv/bin/pip install --upgrade pip
        /opt/llm-router/venv/bin/pip install \
            fastapi "uvicorn[standard]" httpx \
            slowapi prometheus-fastapi-instrumentator
      '
    fi

    # SSH hardening: disable password auth + root password login.
    # Operator must push their public key into /root/.ssh/authorized_keys before this lands.
    sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
GUEST
}

phase_7_3_deploy_app() {
  step "7.3 — Deploy router app.py"
  pct push "$ROUTER_VMID" "$APP_SRC" /opt/llm-router/app.py --perms 0644
  pct exec "$ROUTER_VMID" -- chown router:router /opt/llm-router/app.py
}

phase_7_4_systemd() {
  step "7.4 — Install systemd unit (V620-only, EnvironmentFile loads API keys + admission)"
  pct exec "$ROUTER_VMID" -- env \
    "V620_URL=$V620_URL" \
    "EMBED_URL=$EMBED_URL" \
    "RERANK_URL=$RERANK_URL" \
    "KEEPALIVE_INTERVAL=$KEEPALIVE_INTERVAL" \
    "CHAT_CONCURRENCY=$CHAT_CONCURRENCY" \
    "EMBED_CONCURRENCY=$EMBED_CONCURRENCY" \
    "MAX_CHAT_INPUT_TOKENS=$MAX_CHAT_INPUT_TOKENS" \
    "MAX_EMBED_INPUT_TOKENS=$MAX_EMBED_INPUT_TOKENS" \
    "RATE_LIMIT_CHAT=$RATE_LIMIT_CHAT" \
    "RATE_LIMIT_EMBED=$RATE_LIMIT_EMBED" \
    "RATE_LIMIT_TAVILY=$RATE_LIMIT_TAVILY" \
    "TAVILY_API_KEY=$TAVILY_API_KEY" \
    "MAX_TOOL_ITERATIONS=$MAX_TOOL_ITERATIONS" \
    "TOOL_EXECUTION_DEFAULT=$TOOL_EXECUTION_DEFAULT" \
    "WEB_FETCH_MAX_SIZE_KB=$WEB_FETCH_MAX_SIZE_KB" \
    "WEB_FETCH_TIMEOUT_SECONDS=$WEB_FETCH_TIMEOUT_SECONDS" \
    "METRICS_ALLOWED_IPS=$METRICS_ALLOWED_IPS" \
    "CORS_ALLOW_ORIGINS=$CORS_ALLOW_ORIGINS" \
    bash -se <<'GUEST'
    set -Eeuo pipefail

    # Persist admission/rate-limit/metrics config to /etc/router.env so EnvironmentFile picks them up.
    # API keys are already in /etc/router.env from phase 7.1.5.
    #
    # IMPORTANT: This function uses a Python rewrite (not `sed`) because values may
    # contain characters that `sed` treats specially: `|` (delimiter), `&`
    # (backreference), `\` (escape), or backreferences `\1`. A value like
    # `RATE_LIMIT_CHAT="120/minute # commented"` could otherwise corrupt the file.
    # We also `chmod 600` inside the function so callers don't have to remember to.
    upsert_env() {
      local key="$1" val="$2"
      python3 - "$key" "$val" /etc/router.env <<'PY'
import os, sys, tempfile
key, val, path = sys.argv[1], sys.argv[2], sys.argv[3]
prefix = key + "="
lines = []
found = False
try:
    with open(path) as f:
        for line in f:
            if line.startswith(prefix):
                lines.append(prefix + val + "\n")
                found = True
            else:
                lines.append(line)
except FileNotFoundError:
    pass
if not found:
    lines.append(prefix + val + "\n")
# Atomic write
d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".router.env.")
try:
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except Exception:
    os.unlink(tmp)
    raise
PY
    }
    upsert_env V620_URL "$V620_URL"
    upsert_env EMBED_URL "$EMBED_URL"
    upsert_env RERANK_URL "$RERANK_URL"
    upsert_env KEEPALIVE_INTERVAL "$KEEPALIVE_INTERVAL"
    upsert_env CHAT_CONCURRENCY "$CHAT_CONCURRENCY"
    upsert_env EMBED_CONCURRENCY "$EMBED_CONCURRENCY"
    upsert_env MAX_CHAT_INPUT_TOKENS "$MAX_CHAT_INPUT_TOKENS"
    upsert_env MAX_EMBED_INPUT_TOKENS "$MAX_EMBED_INPUT_TOKENS"
    upsert_env RATE_LIMIT_CHAT "$RATE_LIMIT_CHAT"
    upsert_env RATE_LIMIT_EMBED "$RATE_LIMIT_EMBED"
    upsert_env RATE_LIMIT_TAVILY "$RATE_LIMIT_TAVILY"
    upsert_env TAVILY_API_KEY "$TAVILY_API_KEY"
    upsert_env MAX_TOOL_ITERATIONS "$MAX_TOOL_ITERATIONS"
    upsert_env TOOL_EXECUTION_DEFAULT "$TOOL_EXECUTION_DEFAULT"
    upsert_env WEB_FETCH_MAX_SIZE_KB "$WEB_FETCH_MAX_SIZE_KB"
    upsert_env WEB_FETCH_TIMEOUT_SECONDS "$WEB_FETCH_TIMEOUT_SECONDS"
    upsert_env METRICS_ALLOWED_IPS "$METRICS_ALLOWED_IPS"
    upsert_env CORS_ALLOW_ORIGINS "$CORS_ALLOW_ORIGINS"
    chmod 600 /etc/router.env

    cat > /etc/systemd/system/llm-router.service <<EOF
[Unit]
Description=LLM cluster router (V620-only — Bearer auth, admission control, Prometheus)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=router
WorkingDirectory=/opt/llm-router
EnvironmentFile=/etc/router.env
ExecStart=/opt/llm-router/venv/bin/uvicorn app:app \\
    --host 0.0.0.0 --port 8000 \\
    --timeout-keep-alive 300 \\
    --workers 1
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    # Provision the access-log directory (writeable by the router user)
    mkdir -p /var/log/llm-router
    chown router:router /var/log/llm-router

    systemctl daemon-reload
    systemctl enable llm-router
    # restart so updated /etc/router.env values take effect on re-run
    systemctl restart llm-router
    systemctl status llm-router --no-pager || true
GUEST
}

phase_7_5_keepalive_timer() {
  step "7.5 — Configure model-keepalive timer (ROUTER_KEEPALIVE=$ROUTER_KEEPALIVE)"
  pct exec "$ROUTER_VMID" -- env \
    "ROUTER_KEEPALIVE=$ROUTER_KEEPALIVE" \
    "KA_INTERVAL=$ROUTER_KEEPALIVE_INTERVAL" \
    bash -se <<'GUEST'
    set -Eeuo pipefail

    # Default OFF: the warm-up ping keeps the chat model hot, but on an idle
    # single-user cluster it wakes the GPU and cycles the fans every few minutes.
    # When off, proactively remove any previously-installed units so re-runs
    # converge (this is what makes `ROUTER_KEEPALIVE=off` persistent).
    if [[ "${ROUTER_KEEPALIVE,,}" != "on" ]]; then
      systemctl disable --now llm-router-keepalive.timer 2>/dev/null || true
      rm -f /etc/systemd/system/llm-router-keepalive.timer \
            /etc/systemd/system/llm-router-keepalive.service \
            /usr/local/bin/llm-router-keepalive.sh
      systemctl daemon-reload
      echo "keepalive disabled (ROUTER_KEEPALIVE=off) — model warms on first request"
      exit 0
    fi

    # Tiny periodic ping to the chat model so its weights stay hot in VRAM. The
    # llamacpp-chat unit doesn't unload weights but the first request after a
    # long idle period eats a 2-5s warm-up. Hitting it periodically pre-warms
    # the cache and keeps p99 latency consistent.
    cat > /usr/local/bin/llm-router-keepalive.sh <<'SH'
#!/bin/bash
# Pings the chat model with a 1-token request. Fails silently if the router
# is unreachable (we don't want a paging dependency).
#
# The Authorization header is passed via curl's -H @<file> form (reading from
# a file) rather than -H "Bearer $KEY" so ROUTER_API_KEY does NOT appear in
# /proc/<pid>/cmdline / ps output during the curl call. The header file is
# created with mode 600 in a private tmpdir and removed on exit.
set -e
. /etc/router.env  # source ROUTER_API_KEY

tmpdir=$(mktemp -d -t llm-router-keepalive.XXXXXX)
trap 'rm -rf "$tmpdir"' EXIT
umask 077
printf 'Authorization: Bearer %s\n' "$ROUTER_API_KEY" > "$tmpdir/auth"

curl -s -m 30 -o /dev/null \
    -H "@$tmpdir/auth" \
    -H "Content-Type: application/json" \
    -d '{"model":"rag-qwen3.6","messages":[{"role":"user","content":"."}],"max_tokens":1}' \
    http://127.0.0.1:8000/v1/chat/completions || true
SH
    chmod 0755 /usr/local/bin/llm-router-keepalive.sh
    chown root:root /usr/local/bin/llm-router-keepalive.sh

    cat > /etc/systemd/system/llm-router-keepalive.service <<'EOF'
[Unit]
Description=Periodic warm-up ping to the LLM chat model
After=llm-router.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/llm-router-keepalive.sh
EOF

    cat > /etc/systemd/system/llm-router-keepalive.timer <<EOF
[Unit]
Description=Periodic warm-up ping (interval ${KA_INTERVAL})
After=llm-router.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=${KA_INTERVAL}
RandomizedDelaySec=20s

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now llm-router-keepalive.timer
    systemctl status llm-router-keepalive.timer --no-pager || true
GUEST
}

main() {
  phase_7_1_create
  phase_7_1_5_api_keys
  phase_7_2_python_env
  phase_7_3_deploy_app
  phase_7_4_systemd
  phase_7_5_keepalive_timer

  step "Phase 7 complete."
  local ip; ip="$(lxc_get_ip "$ROUTER_VMID" || true)"
  ok "Router ready at IP: ${ip:-unknown}"
  echo "  Smoke test:"
  echo "    curl -s http://${ip:-<router-ip>}:8000/healthz | jq"
  echo "    curl -s http://${ip:-<router-ip>}:8000/v1/models | jq"
}

main "$@"
