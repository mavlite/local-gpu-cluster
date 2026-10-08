#!/usr/bin/env bash
# wf-window.sh open|close|stamp <file> <label>|status — the host side of a workforce measurement window
# (docs/superpowers/specs/2026-10-07-distributed-workforce-design.md §9-§10; Plan D). Runs ON the
# Proxmox host as root, from the repo checkout: `bash scripts/files/wf-window.sh open`.
#
#   open    3 x 128K chat (redteam-mode-enter.sh) with embed + rerank loaded again on top -- decision H
#           measured this to fit on 2026-10-08 -- and every unit that could restart chat or swap its
#           profile stopped: the chat restart timer, the redteam watcher/idle revert, the fast server,
#           rag-refresh and the swap webhook. Saves what it found first.
#   close   put back exactly what `open` found, then wait for the normal 1-slot layout.
#   stamp   append one JSON line: boot id, router and chat-server invocation ids, chat capacity.
#           A run is valid only if its before/after stamps agree (spec §9 validity tests).
#   status  window state and the router's /healthz.
set -Eeuo pipefail

AMD="${WF_WIN_AMD_CT:-151}"
ROUTER="${WF_WIN_ROUTER_CT:-153}"
STATE_DIR="${WF_WIN_STATE_DIR:-/root/wf/window}"
STATE="$STATE_DIR/window.env"
SBIN="${WF_WIN_SBIN:-/usr/local/sbin}"
RT_STATE="${WF_WIN_RT_STATE:-/run/redteam-mode.state}"
BOOT_ID="${WF_WIN_BOOT_ID:-/proc/sys/kernel/random/boot_id}"
WAIT_S="${WF_WIN_WAIT_S:-600}"
HOST_UNITS=(redteam-mode-watch.service redteam-mode-idle.timer redteam-mode-precreate.service
            rag-refresh.timer swap-webhook.service)
AMD_UNITS=(llamacpp-chat-restart.timer llamacpp-fast.service)
RAG_UNITS=(llamacpp-embed.service llamacpp-rerank.service)

die() { echo "[wf-window] FATAL: $*" >&2; exit 1; }
log() { echo "[wf-window] $*"; }

key_of() { echo "$1" | tr -c 'A-Za-z0-9\n' '_'; }
host_active() { systemctl is-active "$1" 2>/dev/null || true; }
amd_active() { pct exec "$AMD" -- systemctl is-active "$1" 2>/dev/null || true; }
redteam_active() { [ "$(cat "$RT_STATE" 2>/dev/null || true)" = "active" ]; }
healthz() { pct exec "$ROUTER" -- curl -s -m 5 http://127.0.0.1:8000/healthz 2>/dev/null || true; }
capacity() { healthz | grep -oE '"capacity": ?[0-9]+' | grep -oE '[0-9]+$' || true; }
rag_ok() {
  local h; h="$(healthz)"
  [[ "$h" =~ \"embed\":\ ?\"ok\" && "$h" =~ \"rerank\":\ ?\"ok\" ]]
}

wait_layout() {        # wait_layout <capacity> <rag:yes|any>
  local deadline=$((SECONDS + WAIT_S))
  while (( SECONDS < deadline )); do
    if [ "$(capacity)" = "$1" ] && { [ "$2" = any ] || rag_ok; }; then return 0; fi
    sleep 2
  done
  die "layout did not reach capacity $1 (rag: $2) within ${WAIT_S}s"
}

stop_perturbers() {
  systemctl stop "${HOST_UNITS[@]}" 2>/dev/null || true
  pct exec "$AMD" -- systemctl stop "${AMD_UNITS[@]}" 2>/dev/null || true
}

cmd_open() {
  [ -f "$STATE" ] && die "window already open ($STATE) -- run close first"
  redteam_active && die "redteam mode is active -- refusing to open a window over an engagement"
  [ "$(capacity)" = 1 ] || die "chat is not in the normal 1-slot layout; refusing to start from an unknown state"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  local u tmp
  tmp="$(mktemp "$STATE_DIR/.window.XXXXXX")"
  { for u in "${HOST_UNITS[@]}"; do echo "HOST_$(key_of "$u")='$(host_active "$u")'"; done
    for u in "${AMD_UNITS[@]}" "${RAG_UNITS[@]}"; do echo "AMD_$(key_of "$u")='$(amd_active "$u")'"; done
    echo "OPENED_AT='$(date -u +%FT%TZ)'"
  } > "$tmp"
  mv "$tmp" "$STATE"
  log "saved the prior state to $STATE (close restores it, even after a failed open)"
  "$SBIN/redteam-mode-enter.sh"
  stop_perturbers                  # enter starts the watcher, idle revert and fast server
  pct exec "$AMD" -- systemctl start "${RAG_UNITS[@]}"
  wait_layout 3 yes
  log "open: 3 x 128K chat, embed + rerank loaded, perturbers stopped"
}

restore_unit() {       # restore_unit host|amd <unit> <saved state>
  local verb=stop
  [ "$3" = active ] && verb=start
  if [ "$1" = host ]; then systemctl "$verb" "$2" 2>/dev/null || [ "$verb" = stop ]
  else pct exec "$AMD" -- systemctl "$verb" "$2" 2>/dev/null || [ "$verb" = stop ]; fi
}

cmd_close() {
  [ -f "$STATE" ] || die "window not open (no $STATE)"
  # shellcheck disable=SC1090
  . "$STATE"
  if redteam_active; then "$SBIN/redteam-mode-exit.sh"; fi
  local u var
  for u in "${AMD_UNITS[@]}" "${RAG_UNITS[@]}"; do
    var="AMD_$(key_of "$u")"
    restore_unit amd "$u" "${!var:-inactive}"
  done
  for u in "${HOST_UNITS[@]}"; do
    var="HOST_$(key_of "$u")"
    restore_unit host "$u" "${!var:-inactive}"
  done
  wait_layout 1 any
  mv "$STATE" "$STATE.closed.$(date -u +%Y%m%dT%H%M%SZ)"
  log "closed: restored the state saved at ${OPENED_AT:-?}"
}

cmd_stamp() {
  local out="${1:?stamp <file> <label>}" label="${2:?stamp <file> <label>}"
  [[ "$label" =~ ^[A-Za-z0-9._-]+$ ]] || die "label must match [A-Za-z0-9._-]+"
  local boot router chat cap open=false
  boot="$(tr -d '[:space:]' < "$BOOT_ID")"
  router="$(pct exec "$ROUTER" -- systemctl show -p InvocationID --value llm-router 2>/dev/null || true)"
  chat="$(pct exec "$AMD" -- systemctl show -p InvocationID --value llamacpp-chat 2>/dev/null || true)"
  cap="$(capacity)"
  [ -f "$STATE" ] && open=true
  printf '{"ts":"%s","label":"%s","boot_id":"%s","router_invocation":"%s","chat_invocation":"%s","capacity":%s,"window_open":%s}\n' \
    "$(date -u +%FT%TZ)" "$label" "$boot" "$router" "$chat" "${cap:-null}" "$open" >> "$out"
  log "stamped $label"
}

cmd_status() {
  if [ -f "$STATE" ]; then echo "window: open since $(grep -m1 '^OPENED_AT=' "$STATE" | cut -d"'" -f2)"
  else echo "window: closed"; fi
  echo "redteam state: $(cat "$RT_STATE" 2>/dev/null || echo none)"
  echo "healthz: $(healthz)"
}

case "${1:-}" in
  open) cmd_open ;;
  close) cmd_close ;;
  stamp) shift; cmd_stamp "$@" ;;
  status) cmd_status ;;
  *) echo "usage: $0 open|close|stamp <file> <label>|status" >&2; exit 2 ;;
esac
