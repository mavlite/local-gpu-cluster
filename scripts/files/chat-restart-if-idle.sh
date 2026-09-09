#!/usr/bin/env bash
# chat-restart-if-idle.sh — idle-gated proactive restart of llamacpp-chat.
#
# Deployed to /usr/local/bin/ in the AMD LXC by 59-llamacpp-restart-timer.sh
# and invoked by llamacpp-chat-restart.service.
#
# Why this exists: the timer used to run `systemctl restart llamacpp-chat`
# unconditionally at two fixed times a day. On 2026-09-09 it fired at 16:10:05
# UTC into an active ~60K-token generation; llama.cpp would not drain, systemd
# SIGKILLed it 90 s later, and clients saw the router's fail-open frame
# (`service_degraded`) for ~107 s. Randomised delay does NOT mitigate this —
# it relocates the collision rather than reducing its probability. The only
# real fix is to ask whether the slot is in use before restarting.
#
# Decision table:
#   unit not active, or up < MIN_UPTIME   -> skip  (warm-up / already churning)
#   restarted < MIN_INTERVAL ago          -> skip  (rate limit)
#   /slots 200 and a slot is processing   -> skip  (never interrupt generation)
#   /slots 200, idle, no HEALTHZ_URL      -> skip  (cannot confirm session ended)
#   /slots 200, chat within IDLE_MIN      -> skip  (session still live; a restart
#                                                   here drops the KV cache and
#                                                   costs a full prefill — ~47 s
#                                                   on a 47K-token context)
#   /slots non-200 (401, 5xx, ...)        -> skip  (server is ALIVE and serving;
#                                                   e.g. a rotated API key. This
#                                                   must never restart, or key
#                                                   rotation kills a live request)
#   /slots no HTTP response after retry   -> RESTART (server wedged — the point)
#   otherwise                             -> RESTART
#
# The rate-limit stamp is written only after the unit is confirmed active again,
# so a failed restart does not suppress recovery for MIN_INTERVAL.
#
# Overrides come from /etc/llamacpp-chat-restart.env (written by the installer).

set -Eeuo pipefail

CHAT_UNIT="${CHAT_UNIT:-llamacpp-chat.service}"
SLOTS_URL="${SLOTS_URL:-http://127.0.0.1:8080/slots}"
HEALTHZ_URL="${HEALTHZ_URL:-}"
IDLE_MIN="${IDLE_MIN:-900}"
MIN_INTERVAL="${MIN_INTERVAL:-43200}"
MIN_UPTIME="${MIN_UPTIME:-120}"
STAMP="${STAMP:-/var/lib/llamacpp/last-proactive-restart}"

log() { echo "chat-restart-if-idle: $*"; }
skip() { log "SKIP — $*"; exit 0; }

now="$(date +%s)"

# --- rate limit ------------------------------------------------------------
if [ -f "$STAMP" ]; then
  last="$(cat "$STAMP" 2>/dev/null || echo 0)"
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  age=$(( now - last ))
  [ "$age" -lt "$MIN_INTERVAL" ] && skip "last proactive restart ${age}s ago (< ${MIN_INTERVAL}s)"
fi

# --- unit must be up and settled ------------------------------------------
state="$(systemctl show "$CHAT_UNIT" -p ActiveState --value 2>/dev/null || echo unknown)"
[ "$state" = "active" ] || skip "$CHAT_UNIT is '$state', not active"

# Use the REALTIME timestamp, not ActiveEnterTimestampMonotonic: inside an LXC
# lxcfs virtualizes /proc/uptime, so it is a different clock from systemd's
# monotonic origin and the subtraction yields nonsense (observed: -8s).
enter_ts="$(systemctl show "$CHAT_UNIT" -p ActiveEnterTimestamp --value 2>/dev/null || echo '')"
if [ -n "$enter_ts" ]; then
  enter_s="$(date -d "$enter_ts" +%s 2>/dev/null || echo 0)"
  case "$enter_s" in ''|*[!0-9]*) enter_s=0 ;; esac
  if [ "$enter_s" -gt 0 ]; then
    up=$(( now - enter_s ))
    [ "$up" -lt 0 ] && up=0
    [ "$up" -lt "$MIN_UPTIME" ] && skip "unit only up ${up}s (< ${MIN_UPTIME}s), still warming"
  fi
fi

# --- in-flight check -------------------------------------------------------
# A server that cannot answer at all is what this restart is for, so a
# transport-level failure means restart. But an HTTP *error* response (401 from
# a rotated key, 5xx under load) proves the process is alive and serving — that
# must NOT restart, or key rotation would kill an in-flight generation, which is
# the exact failure this guard exists to prevent.
reason=""
key=""
if [ -r /etc/llamacpp.env ]; then
  # cut, not `awk -F=`: a base64 key can contain '=' and awk would truncate it.
  # The tr set relies on tr's own backslash-escape parsing to strip CR.
  key="$(sed -n 's/^LLAMACPP_API_KEY=//p' /etc/llamacpp.env | tr -d '"'"'"'
')"
fi

body="$(mktemp)"
trap 'rm -f "$body"' EXIT

probe_slots() {
  # curl already prints 000 on a transport failure; it also exits non-zero, so
  # swallow the status rather than echoing a second value and concatenating.
  local c=""
  c="$(curl -s -o "$body" -w '%{http_code}' -m 10        -H "Authorization: Bearer ${key}" "$SLOTS_URL" 2>/dev/null)" || true
  case "$c" in ''|*[!0-9]*) c=000 ;; esac
  [ "${#c}" -eq 3 ] || c=000
  printf '%s' "$c"
}

code="$(probe_slots)"
# One retry: a single timeout under load is not proof of a hang.
[ "$code" = "000" ] && sleep 3 && code="$(probe_slots)"

case "$code" in
  200)
    busy="$(python3 -c       'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("parse-error"); raise SystemExit(0)
print("busy" if any(s.get("is_processing") for s in d) else "idle")' "$body" 2>/dev/null || echo parse-error)"

    case "$busy" in
      busy)        skip "a slot is processing" ;;
      parse-error) skip "could not parse /slots response" ;;
    esac

    # --- session-liveness check -------------------------------------------
    # Slot-idle is also true between two turns of a live conversation. Only the
    # router knows when the session itself went quiet, so without it we cannot
    # safely distinguish a lull from a pause — skip rather than guess.
    [ -n "$HEALTHZ_URL" ] || skip "no HEALTHZ_URL configured; cannot confirm the session is over"

    if health="$(curl -sf -m 10 "$HEALTHZ_URL" 2>/dev/null)"; then
      idle="$(printf '%s' "$health" | python3 -c         'import json,sys
try:
    print(int(float(json.load(sys.stdin).get("seconds_since_chat", 0))))
except Exception:
    print(-1)' 2>/dev/null || echo -1)"
      case "$idle" in ''|*[!0-9-]*) idle=-1 ;; esac
      [ "$idle" -lt 0 ] && skip "could not read seconds_since_chat"
      [ "$idle" -lt "$IDLE_MIN" ] && skip "last chat ${idle}s ago (< ${IDLE_MIN}s)"
      reason="idle ${idle}s, no slot processing"
    else
      skip "router healthz unreachable — cannot confirm the session is over"
    fi
    ;;
  000)
    reason="/slots gave no response after retry while unit active — server appears wedged"
    ;;
  *)
    # Alive and answering, just not with 200. Never restart on this.
    skip "/slots returned HTTP $code — server is alive, not wedged"
    ;;
esac

# --- restart ---------------------------------------------------------------
log "RESTART — $reason"
/bin/systemctl --no-block restart "$CHAT_UNIT"

# Confirm it actually came back before arming the rate limit. Stamping an
# unsuccessful restart would suppress recovery attempts for MIN_INTERVAL.
for _ in $(seq 1 "${CONFIRM_TIMEOUT:-60}"); do
  sleep 1
  [ "$(systemctl show "$CHAT_UNIT" -p ActiveState --value 2>/dev/null)" = "active" ] || continue
  sub="$(systemctl show "$CHAT_UNIT" -p SubState --value 2>/dev/null)"
  if [ "$sub" = "running" ]; then
    mkdir -p "$(dirname "$STAMP")"
    date +%s > "$STAMP"
    log "OK — $CHAT_UNIT is active/running again"
    exit 0
  fi
done

log "WARNING — $CHAT_UNIT did not return to active/running; rate limit NOT armed so the next tick can retry"
exit 1
