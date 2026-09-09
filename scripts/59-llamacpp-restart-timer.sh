#!/usr/bin/env bash
# 59-llamacpp-restart-timer.sh — Idle-gated proactive restart of llamacpp-chat.
#
# Why: llama.cpp's HIP backend on gfx1030 (V620) has a stability issue in the
# KV-cache checkpoint path used by --cache-reuse on long contexts, surfacing as
# `hipMemcpyAsync ... ROCm error: an illegal memory access was encountered`.
# When this timer was written that fired ~6 times/24 h under sustained OpenCode
# load, and a blind twice-daily restart was a reasonable trade.
#
# That trade no longer holds unconditionally:
#   - The current qwen3.8-27B profile logs `cache_reuse is not supported by this
#     context, it will be disabled` on every start (the MTP draft context
#     disables it), so the faulting feature is not active.
#   - The unconditional restart caused a real outage. On 2026-09-09 it fired at
#     16:10:05 UTC into an active ~60K-token generation. llama.cpp would not
#     drain, systemd SIGKILLed it after TimeoutStopSec=90s, and clients saw the
#     router's fail-open `service_degraded` frame for ~107 s — a failure mode
#     indistinguishable from an upstream provider outage.
#
# So the restart is kept (it still recovers a wedged server) but is now gated:
#   1. Timer fires often (hourly) instead of at two exact times.
#   2. chat-restart-if-idle.sh decides whether to act — it skips while a slot is
#      processing and while the session is still live, and rate-limits itself to
#      one restart per MIN_INTERVAL. Net effect is ~2 restarts/day as before,
#      but always in a genuine lull.
#
# NOTE on RandomizedDelaySec: jitter does NOT protect an in-flight request. It
# relocates the collision rather than reducing its probability. It is retained
# only to spread load, and is no longer load-bearing for safety.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

AMD_VMID="${AMD_VMID:-151}"
ROUTER_VMID="${ROUTER_VMID:-153}"

# How often the timer wakes up to *consider* a restart. The guard, not the
# calendar, decides whether one actually happens.
LLAMA_RESTART_ONCALENDAR="${LLAMA_RESTART_ONCALENDAR:-hourly}"
# Seconds of no chat traffic before the slot counts as free.
LLAMA_RESTART_IDLE_MIN="${LLAMA_RESTART_IDLE_MIN:-900}"
# Minimum seconds between two proactive restarts (12 h => ~2/day).
LLAMA_RESTART_MIN_INTERVAL="${LLAMA_RESTART_MIN_INTERVAL:-43200}"
# Seconds the unit must have been up before it is eligible (avoids restarting
# during model load / warm-up).
LLAMA_RESTART_MIN_UPTIME="${LLAMA_RESTART_MIN_UPTIME:-120}"
# Bound the drain so a stuck shutdown cannot stretch a ~15 s restart into ~107 s.
LLAMA_CHAT_STOP_TIMEOUT="${LLAMA_CHAT_STOP_TIMEOUT:-20s}"

if [ -n "${LLAMA_RESTART_SCHEDULE:-}" ]; then
  warn "LLAMA_RESTART_SCHEDULE ('$LLAMA_RESTART_SCHEDULE') is deprecated and ignored."
  warn "The timer now fires on '$LLAMA_RESTART_ONCALENDAR' and chat-restart-if-idle.sh"
  warn "picks the moment. Use LLAMA_RESTART_MIN_INTERVAL to control the rate."
fi

if ! pct status "$AMD_VMID" >/dev/null 2>&1; then
  die "LXC $AMD_VMID does not exist. Run scripts/51-lxc-amd.sh first."
fi

GUARD_SRC="$LGC_DIR/files/chat-restart-if-idle.sh"
[ -f "$GUARD_SRC" ] || die "Missing $GUARD_SRC"

step "Resolve router healthz URL for the idle gate"
router_ip="$(lxc_get_ip "$ROUTER_VMID" 2>/dev/null || true)"
if [ -z "$router_ip" ]; then
  warn "Could not resolve LXC $ROUTER_VMID IP; the guard will fall back to the"
  warn "slot check alone (it will still never interrupt an active generation)."
  HEALTHZ_URL=""
else
  HEALTHZ_URL="http://${router_ip}:8000/healthz"
  ok "Router healthz: $HEALTHZ_URL"
fi

step "Install idle-gated restart timer for llamacpp-chat.service in LXC $AMD_VMID"

pct push "$AMD_VMID" "$GUARD_SRC" /usr/local/bin/chat-restart-if-idle.sh --perms 0755

pct exec "$AMD_VMID" -- env \
  "ON_CALENDAR=$LLAMA_RESTART_ONCALENDAR" \
  "HEALTHZ_URL=$HEALTHZ_URL" \
  "IDLE_MIN=$LLAMA_RESTART_IDLE_MIN" \
  "MIN_INTERVAL=$LLAMA_RESTART_MIN_INTERVAL" \
  "MIN_UPTIME=$LLAMA_RESTART_MIN_UPTIME" \
  "STOP_TIMEOUT=$LLAMA_CHAT_STOP_TIMEOUT" \
  bash -se <<'GUEST'
  set -Eeuo pipefail

  install -d -m 0755 /var/lib/llamacpp

  umask 022
  cat > /etc/llamacpp-chat-restart.env <<EOF
# Written by 59-llamacpp-restart-timer.sh — tunables for chat-restart-if-idle.sh
HEALTHZ_URL=${HEALTHZ_URL}
IDLE_MIN=${IDLE_MIN}
MIN_INTERVAL=${MIN_INTERVAL}
MIN_UPTIME=${MIN_UPTIME}
EOF

  cat > /etc/systemd/system/llamacpp-chat-restart.service <<'EOF'
[Unit]
Description=Idle-gated proactive restart of llamacpp-chat
# Context: /usr/local/bin/chat-restart-if-idle.sh documents the decision table.
Documentation=https://github.com/ggerganov/llama.cpp/issues
# NOTE: do NOT use Requires=llamacpp-chat.service here. The guard issues
# `systemctl restart llamacpp-chat`, which transiently stops chat. With
# Requires=, systemd kills this in-flight restart command with SIGTERM the
# moment chat goes down — leaving both units in a broken state and triggering
# a tight ~5s restart loop. Use no dependency at all; the restart command
# will start chat back up itself even if it was already stopped.

[Service]
Type=oneshot
EnvironmentFile=-/etc/llamacpp-chat-restart.env
# The guard exits 0 whether it restarts or skips; a skip is a normal outcome.
ExecStart=/usr/local/bin/chat-restart-if-idle.sh
# The guard polls for up to ~60s after a restart to confirm the unit came back
# before arming its rate limit, plus up to ~13s of probing. Give it headroom
# over the 90s oneshot default.
TimeoutStartSec=180
EOF

  # Bound the drain. Without this the unit inherits the 90s default, which is
  # what turned a ~15s restart into a ~107s outage on 2026-09-09.
  install -d -m 0755 /etc/systemd/system/llamacpp-chat.service.d
  cat > /etc/systemd/system/llamacpp-chat.service.d/10-stop-timeout.conf <<EOF
# Managed by 59-llamacpp-restart-timer.sh
[Service]
TimeoutStopSec=${STOP_TIMEOUT}
EOF

  cat > /etc/systemd/system/llamacpp-chat-restart.timer <<EOF
[Unit]
Description=Schedule for idle-gated llamacpp-chat restart
# See llamacpp-chat-restart.service for context; Documentation= requires a
# URL-shaped value so we omit it on the timer rather than carry a comment
# that systemd will misparse as URLs.

[Timer]
# Fires often; the guard decides whether to act. This is what lets the restart
# land in a real lull instead of at a fixed wall-clock time that may be busy.
OnCalendar=${ON_CALENDAR}
# Spreads load only. Jitter is NOT a safety mechanism for in-flight requests —
# the idle gate in the service is.
RandomizedDelaySec=5min
# Persistent=true catches up if the host was off when a window was missed.
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now llamacpp-chat-restart.timer

  echo "--- Timer status ---"
  systemctl list-timers llamacpp-chat-restart.timer --no-pager
  echo "--- Effective stop timeout ---"
  systemctl show llamacpp-chat -p TimeoutStopUSec
GUEST

ok "Idle-gated restart timer installed and active in LXC $AMD_VMID."
echo
echo "Dry-run the guard now (it will skip if the slot is in use):"
echo "  pct exec $AMD_VMID -- /usr/local/bin/chat-restart-if-idle.sh"
echo
echo "Force a restart regardless of the gate:"
echo "  pct exec $AMD_VMID -- systemctl restart llamacpp-chat"
echo
echo "See what the guard decided:"
echo "  pct exec $AMD_VMID -- journalctl -u llamacpp-chat-restart.service --since '24 hours ago'"
