#!/usr/bin/env bash
# Phase-1 gate environment control. Runs ON the Proxmox host as root.
#   gate_env.sh preflight            refuse if redteam mode is active or rag-refresh is due soon
#   gate_env.sh pin                  save prior state, stop perturbing units, RATE_LIMIT_CHAT=1000/minute
#   gate_env.sh arm-a                3 x 128K chat (redteam-mode-enter) with its keep-warm/revert units stopped
#   gate_env.sh arm-b                1-slot chat (exit redteam mode if we entered it), same units stopped
#   gate_env.sh record <file>        append the environment record (JSON lines) to <file>
#   gate_env.sh restore              undo pin/arm-a exactly, from the saved state
# Spec: docs/superpowers/specs/2026-10-05-minisforum-cluster-integration-design.md §5.0.5-6, §5.4, §5.8
set -Eeuo pipefail

AMD="${GATE_AMD_CT:-151}"
ROUTER_CT="${GATE_ROUTER_CT:-153}"
ROUTER_URL="${GATE_ROUTER_URL:-http://192.168.6.153:8000}"
STATE_DIR="${GATE_STATE_DIR:-/root/gate}"
STATE="$STATE_DIR/state.env"
SBIN="${GATE_SBIN:-/usr/local/sbin}"
RT_STATE="${GATE_REDTEAM_STATE:-/run/redteam-mode.state}"
RATE="${GATE_RATE_LIMIT_CHAT:-1000/minute}"
HOST_UNITS=(redteam-mode-watch.service redteam-mode-idle.timer redteam-mode-precreate.service)
AMD_UNITS=(llamacpp-chat-restart.timer llamacpp-embed.service llamacpp-rerank.service llamacpp-fast.service)

die() { echo "[gate-env] FATAL: $*" >&2; exit 1; }
log() { echo "[gate-env] $*"; }

amd_active() { pct exec "$AMD" -- systemctl is-active "$1" 2>/dev/null || true; }
host_active() { systemctl is-active "$1" 2>/dev/null || true; }
redteam_active() { [ "$(cat "$RT_STATE" 2>/dev/null || true)" = "active" ]; }
key_of() { echo "$1" | tr -c 'A-Za-z0-9\n' '_'; }

healthz() { curl -fsS -m 10 "$ROUTER_URL/healthz"; }
capacity() {
  healthz | python3 -c 'import json,sys; print(json.load(sys.stdin)["chat_admission"]["capacity"])'
}
wait_capacity() {               # wait_capacity <n>: up to 10 min for the chat slot count to settle
  local want="$1" i got=""
  for i in $(seq 1 120); do
    got="$(capacity 2>/dev/null || true)"
    [ "$got" = "$want" ] && { log "chat_admission.capacity=$got"; return 0; }
    sleep 5
  done
  die "chat_admission.capacity is '$got', wanted $want"
}

stop_perturbers() {
  local u
  for u in "${HOST_UNITS[@]}"; do systemctl stop "$u" 2>/dev/null || true; done
  for u in "${AMD_UNITS[@]}"; do pct exec "$AMD" -- systemctl stop "$u" 2>/dev/null || true; done
}

cmd_preflight() {
  redteam_active && die "redteam mode is active -- an engagement may be running; not starting the gate"
  [ -f "$STATE" ] && die "$STATE exists -- a gate window is already pinned (run restore first)"
  systemctl list-timers --no-legend rag-refresh.timer || true
  log "preflight OK (check the rag-refresh time above is outside the run window)"
}

cmd_pin() {
  [ -f "$STATE" ] && die "$STATE exists -- already pinned"
  redteam_active && die "redteam mode is active -- refusing to pin"
  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  local u old tmp
  tmp="$(mktemp "$STATE_DIR/.state.XXXXXX")"
  old="$(pct exec "$ROUTER_CT" -- grep -m1 '^RATE_LIMIT_CHAT=' /etc/router.env | cut -d= -f2- || true)"
  { echo "PRIOR_RATE_LIMIT_CHAT='${old}'"
    for u in "${HOST_UNITS[@]}"; do echo "HOST_$(key_of "$u")='$(host_active "$u")'"; done
    for u in "${AMD_UNITS[@]}"; do echo "AMD_$(key_of "$u")='$(amd_active "$u")'"; done
    echo "PINNED_AT='$(date -u +%FT%TZ)'"
  } > "$tmp"
  mv "$tmp" "$STATE"
  log "saved prior state to $STATE"
  stop_perturbers
  if [ -n "$old" ]; then
    pct exec "$ROUTER_CT" -- sed -i "s|^RATE_LIMIT_CHAT=.*|RATE_LIMIT_CHAT=$RATE|" /etc/router.env
  else
    pct exec "$ROUTER_CT" -- sh -c "echo RATE_LIMIT_CHAT=$RATE >> /etc/router.env"
  fi
  pct exec "$ROUTER_CT" -- systemctl restart llm-router
  wait_capacity 1
  log "pinned: perturbing units stopped, embed/rerank stopped (both arms), RATE_LIMIT_CHAT=$RATE"
}

cmd_arm_a() {
  [ -f "$STATE" ] || die "not pinned"
  "$SBIN/redteam-mode-enter.sh"
  stop_perturbers                   # enter starts idle/watch/fast; arm A must not revert or share VRAM
  wait_capacity 3
}

cmd_arm_b() {
  [ -f "$STATE" ] || die "not pinned"
  if redteam_active; then "$SBIN/redteam-mode-exit.sh"; fi
  stop_perturbers                   # exit restarts the restart timer and embed/rerank
  wait_capacity 1
}

cmd_record() {
  local out="${1:?record <file>}"
  python3 - "$out" <<PY
import json, subprocess, sys, time
def sh(*a):
    r = subprocess.run(a, capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()
rec = {
  "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
  "host_repo_head": sh("git", "-C", "/root/local-gpu-cluster", "rev-parse", "HEAD"),
  "router_app_sha256": (sh("pct", "exec", "$ROUTER_CT", "--", "sha256sum", "/opt/llm-router/app.py").split()[:1] or [""])[0],
  "rate_limit_chat": sh("pct", "exec", "$ROUTER_CT", "--", "grep", "-m1", "^RATE_LIMIT_CHAT=", "/etc/router.env"),
  "llama_server_version": sh("pct", "exec", "$AMD", "--", "/opt/llama.cpp/build/bin/llama-server", "--version"),
  "chat_unit": sh("pct", "exec", "$AMD", "--", "systemctl", "cat", "llamacpp-chat"),
  "healthz": json.loads(sh("curl", "-fsS", "-m", "10", "$ROUTER_URL/healthz") or "null"),
  "redteam_state": sh("cat", "$RT_STATE"),
  "units": {u: sh("systemctl", "is-active", u) for u in "${HOST_UNITS[*]}".split()},
  "amd_units": {u: sh("pct", "exec", "$AMD", "--", "systemctl", "is-active", u) for u in "${AMD_UNITS[*]}".split()},
}
with open(sys.argv[1], "a") as f:
    f.write(json.dumps(rec) + "\n")
print("[gate-env] recorded", rec["ts"], "capacity", (rec["healthz"] or {}).get("chat_admission"))
PY
}

cmd_restore() {
  [ -f "$STATE" ] || die "no saved state at $STATE -- nothing to restore"
  # shellcheck disable=SC1090
  . "$STATE"
  if redteam_active; then "$SBIN/redteam-mode-exit.sh"; fi
  if [ -n "${PRIOR_RATE_LIMIT_CHAT:-}" ]; then
    pct exec "$ROUTER_CT" -- sed -i "s|^RATE_LIMIT_CHAT=.*|RATE_LIMIT_CHAT=$PRIOR_RATE_LIMIT_CHAT|" /etc/router.env
  else
    pct exec "$ROUTER_CT" -- sed -i '/^RATE_LIMIT_CHAT=/d' /etc/router.env
  fi
  pct exec "$ROUTER_CT" -- systemctl restart llm-router
  local u var
  for u in "${AMD_UNITS[@]}"; do
    var="AMD_$(key_of "$u")"
    if [ "${!var:-}" = "active" ]; then pct exec "$AMD" -- systemctl start "$u"
    else pct exec "$AMD" -- systemctl stop "$u" 2>/dev/null || true; fi
  done
  for u in "${HOST_UNITS[@]}"; do
    var="HOST_$(key_of "$u")"
    if [ "${!var:-}" = "active" ]; then systemctl start "$u"; fi
  done
  wait_capacity 1
  mv "$STATE" "$STATE.restored.$(date -u +%Y%m%dT%H%M%SZ)"
  log "restored to the state saved at ${PINNED_AT:-?}"
}

case "${1:-}" in
  preflight) cmd_preflight ;;
  pin) cmd_pin ;;
  arm-a) cmd_arm_a ;;
  arm-b) cmd_arm_b ;;
  record) shift; cmd_record "$@" ;;
  restore) cmd_restore ;;
  healthz) healthz; echo ;;
  *) echo "usage: $0 preflight|pin|arm-a|arm-b|record <file>|restore|healthz" >&2; exit 2 ;;
esac
