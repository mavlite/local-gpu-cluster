#!/usr/bin/env bash
# 59-llamacpp-restart-timer.sh — Proactive restart of llamacpp-chat.service.
#
# Why: llama.cpp's HIP backend on gfx1030 (V620) has a known stability issue
# in the KV-cache checkpoint code path used by --cache-reuse on long contexts.
# Symptom is `hipMemcpyAsync ... ROCm error: an illegal memory access was
# encountered` during multi-turn conversation continuity. Failures recur
# roughly every few hours under sustained OpenCode load (observed: 6 events
# in 24 h on this cluster).
#
# Mitigation strategy:
#   1. Router emits OpenAI-shaped errors (fixed separately)
#   2. OpenCode chatMaxRetries=3 catches transient failures
#   3. THIS SCRIPT — proactive twice-daily restart to flush GPU memory
#      fragmentation before it causes a fault during a user session.
#
# Picks 04:00 and 16:00 UTC by default (~ midnight and noon CT for the
# operator) — outside expected interactive use windows. Each restart takes
# 20-60 s (model weights re-mmap'd from /opt/models cache). Embed and
# rerank services are NOT touched.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

AMD_VMID="${AMD_VMID:-151}"
# Restart times (systemd OnCalendar syntax). Override in config.env if you
# want a different cadence — e.g., "OnCalendar=*-*-* 02:00:00" for once-daily.
LLAMA_RESTART_SCHEDULE="${LLAMA_RESTART_SCHEDULE:-04:00 16:00}"

if ! pct status "$AMD_VMID" >/dev/null 2>&1; then
  die "LXC $AMD_VMID does not exist. Run scripts/51-lxc-amd.sh first."
fi

step "Install proactive restart timer for llamacpp-chat.service in LXC $AMD_VMID"

# Build the OnCalendar lines from the schedule. Each token in
# LLAMA_RESTART_SCHEDULE becomes a separate OnCalendar= line; systemd will
# fire the timer at the union of all of them.
on_calendar_lines=""
for t in $LLAMA_RESTART_SCHEDULE; do
  on_calendar_lines+="OnCalendar=*-*-* ${t}:00"$'\n'
done

pct exec "$AMD_VMID" -- env \
  "ON_CALENDAR_LINES=$on_calendar_lines" \
  bash -se <<'GUEST'
  set -Eeuo pipefail

  cat > /etc/systemd/system/llamacpp-chat-restart.service <<'EOF'
[Unit]
Description=Proactive restart of llamacpp-chat to flush GPU memory fragmentation
# See https://github.com/ggerganov/llama.cpp/issues for the cache-reuse
# hipMemcpyAsync fault on gfx1030 that motivates this proactive restart.
Documentation=https://github.com/ggerganov/llama.cpp/issues
# NOTE: do NOT use Requires=llamacpp-chat.service here. The ExecStart issues
# `systemctl restart llamacpp-chat`, which transiently stops chat. With
# Requires=, systemd kills this in-flight restart command with SIGTERM the
# moment chat goes down — leaving both units in a broken state and triggering
# a tight ~5s restart loop. Use no dependency at all; the restart command
# will start chat back up itself even if it was already stopped.

[Service]
Type=oneshot
# --no-block returns immediately so this oneshot doesn't sit holding the dbus
# transaction while chat tears down + comes back up. The actual restart
# proceeds asynchronously and is observable via `systemctl status llamacpp-chat`.
ExecStart=/bin/systemctl --no-block restart llamacpp-chat.service
SuccessExitStatus=0 1
EOF

  cat > /etc/systemd/system/llamacpp-chat-restart.timer <<EOF
[Unit]
Description=Schedule for proactive llamacpp-chat restart
Documentation=See llamacpp-chat-restart.service

[Timer]
${ON_CALENDAR_LINES}# Skew up to 15 minutes so multiple cluster nodes (if ever) don't restart
# simultaneously. Also softens the impact if the timer happens to fire just
# as a user is sending a request.
RandomizedDelaySec=15min
# Persistent=true catches up if the host was off when a timer should have
# fired — runs a single restart on boot if a window was missed.
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now llamacpp-chat-restart.timer

  echo "--- Timer status ---"
  systemctl list-timers llamacpp-chat-restart.timer --no-pager
GUEST

ok "Restart timer installed and active in LXC $AMD_VMID."
echo
echo "Next scheduled restart times:"
pct exec "$AMD_VMID" -- systemctl list-timers llamacpp-chat-restart.timer --no-pager \
  | tail -n +2 | head -n 3
echo
echo "Manual trigger if you want to test now:"
echo "  pct exec $AMD_VMID -- systemctl start llamacpp-chat-restart.service"
echo
echo "Monitor crashes vs. proactive restarts (last 24h breakdown):"
echo "  pct exec $AMD_VMID -- journalctl -u llamacpp-chat.service --since '24 hours ago' | grep -cE 'ROCm error|Restarting'"
