#!/usr/bin/env bash
# wf-run-control.sh start <run-id> <T|G|S> [--w1] | start-replay <run-id> <src-run-id> [opts] | warm <T|G|S> |
#                   status <run-id> | pack <run-id> | clear-keys
#
# Runs INSIDE the workforce sandbox VM as root (installed as /usr/local/sbin/wf-run-control by
# 76-vm-wf-sandbox.sh); the host drives it through the guest agent. Plan D.
#
#   start   read the keys from stdin (WF_ROUTER_KEY=..., and WF_WORKER_KEY=... for arm T; any other
#           line is refused), write them to a root-only file, and start the harness detached as the
#           transient unit wf-run-<id>. A W3 run lasts hours: longer than any guest-exec timeout.
#   _exec   (the unit's process) load the keys into the environment, DELETE the file, then exec the
#           harness: from here on the keys live only in the harness's process environment.
#   start-replay  like start, for the offline reviewer replay (round 2 §5.1): WF_ROUTER_KEY= only on
#           stdin (a worker key line is refused), source /srv/wf/replay/<src-run-id> (pushed by the
#           host's 76 push-run), output /srv/wf/runs/<run-id> under the same unit name, so status
#           and pack work unchanged.
#   warm    one 16-token request to the lead (through the router) and, for arm T, to each worker, with
#           the keys from stdin (round 2 §5.2: untimed warm-ups before a run's clock starts). Prints
#           `warm <endpoint> <http code> <seconds>` per endpoint; exit 1 if any is not 200. The keys
#           reach curl through a -K config on stdin, never argv.
#   status  the unit state, whether run.json exists, and the log's tail.
#   pack    once the run has finished (run.json, or replay.json for a replay), /srv/wf/runs/<id> + its log
#           into /root/wf-out/<id>.tgz;
#           prints the size and sha256 so the host can verify its copy.
#   clear-keys  remove every leftover key file (window teardown).
# Run ids are single-use. Keys are never on a command line and never printed.
set -Eeuo pipefail

RUNS="${WF_RC_RUNS:-/srv/wf/runs}"
KEYS="${WF_RC_KEYS:-/run/wf}"
OUT="${WF_RC_OUT:-/root/wf-out}"
WF_RUN="${WF_RC_BIN:-/usr/local/sbin/wf-run}"
BUNDLES="${WF_RC_BUNDLES:-/srv/wf/bundles}"
REPLAY="${WF_RC_REPLAY:-/srv/wf/replay}"
ROUTER_URL="${WF_RC_ROUTER:-http://192.168.6.153:8000/v1}"
LEAD_ALIAS="${WF_RC_LEAD_ALIAS:-qwen3.8-nothink}"
WORKER_ALIAS="${WF_RC_WORKER_ALIAS:-qwen3.6}"
WORKERS="${WF_RC_WORKERS:-172.16.10.205 172.16.10.206 172.16.10.207}"
WORKER_PORT=8090
GRADER=docker:wf-grader:1
PREFIX=wf
SELF="$(readlink -f "$0")"

die() { echo "[wf-run-control] FATAL: $*" >&2; exit 1; }
log() { echo "[wf-run-control] $*"; }

check_id() { [[ "${1:-}" =~ ^[a-z0-9][a-z0-9._-]{0,40}$ ]] || die "run id must match [a-z0-9][a-z0-9._-]{0,40}"; }
check_arm() { [[ "${1:-}" == T || "${1:-}" == G || "${1:-}" == S ]] || die "arm must be T, G or S"; }
check_opts() {
  local o
  for o in "$@"; do
    [[ "$o" == --w1 || "$o" == --read-first || "$o" =~ ^--lead-url=https?://[A-Za-z0-9.:/_-]+$ \
       || "$o" =~ ^--lead-alias=[A-Za-z0-9._-]+$ || "$o" =~ ^--review-steps=[0-9]{1,3}$ \
       || "$o" == --enforce-read || "$o" =~ ^--reviewer-temperature=[0-9](\.[0-9]{1,2})?$ || "$o" == --defects-only ]] \
      || die "unknown option '$o' (only --w1, --lead-url=<url>, --lead-alias=<name>, --review-steps=<n>, --read-first)"
  done
}
has_lead_opt() { [[ " $* " == *" --lead-url="* ]]; }
safe_key() { [[ "$1" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; }
# start-replay options (reviewer-model trial): --reviewer-model=<provider/model> and --worker=<url>.
check_replay_opts() {
  local o
  for o in "$@"; do
    [[ "$o" =~ ^--reviewer-model=[A-Za-z0-9._/-]+$ || "$o" =~ ^--worker=https?://[A-Za-z0-9.:/_-]+$ \
       || "$o" =~ ^--worker-alias=[A-Za-z0-9._-]+$ || "$o" =~ ^--review-steps=[0-9]{1,3}$ || "$o" == --read-first \
       || "$o" == --enforce-read || "$o" =~ ^--reviewer-temperature=[0-9](\.[0-9]{1,2})?$ || "$o" == --defects-only ]] \
      || die "unknown option '$o' (only --reviewer-model=<provider/model>, --worker=<url>, --worker-alias=<name>, --review-steps=<n>, --read-first)"
  done
}
has_worker_opt() { [[ " $* " == *" --worker="* ]]; }

# Read the keys from stdin into KEY_ROUTER / KEY_WORKER, refusing any other line; `no-worker` refuses
# a worker key too. Nothing is written before every line has been checked.
read_keys() {
  local mode="${1:-}" line
  KEY_ROUTER="" KEY_WORKER=""
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    case "$line" in
      WF_ROUTER_KEY=?*) KEY_ROUTER="${line#WF_ROUTER_KEY=}" ;;
      WF_WORKER_KEY=?*)
        if [ "$mode" = no-worker ]; then die "unexpected stdin line (only WF_ROUTER_KEY= is accepted here)"; fi
        KEY_WORKER="${line#WF_WORKER_KEY=}" ;;
      *) die "unexpected stdin line (only WF_ROUTER_KEY= and WF_WORKER_KEY= are accepted)" ;;
    esac
  done
  [ -n "$KEY_ROUTER" ] || die "WF_ROUTER_KEY missing on stdin"
  safe_key "$KEY_ROUTER" || die "WF_ROUTER_KEY has unexpected characters"
  [ -z "$KEY_WORKER" ] || safe_key "$KEY_WORKER" || die "WF_WORKER_KEY has unexpected characters"
}

write_keys() {
  local id="$1"
  mkdir -p "$KEYS" "$RUNS"
  chmod 0700 "$KEYS"
  (
    umask 077
    echo "WF_ROUTER_KEY=$KEY_ROUTER" > "$KEYS/$id.env"
    if [ -n "$KEY_WORKER" ]; then echo "WF_WORKER_KEY=$KEY_WORKER" >> "$KEYS/$id.env"; fi
  )
}

start_unit() {
  local id="$1"
  shift
  systemd-run --unit="wf-run-$id" --collect \
    -p StandardOutput="append:$RUNS/$id.log" -p StandardError="append:$RUNS/$id.log" \
    /bin/bash "$SELF" "$@"
}

cmd_start() {
  local id="${1:-}" arm="${2:-}"
  shift 2 || true
  check_id "$id"
  check_arm "$arm"
  check_opts "$@"
  [ -e "$RUNS/$id" ] && die "run $id exists -- run ids are single-use"
  read_keys
  [ "$arm" != T ] || [ -n "$KEY_WORKER" ] || die "arm T needs WF_WORKER_KEY on stdin"
  ! has_lead_opt "$@" || [ -n "$KEY_WORKER" ] || die "a --lead-url lead needs WF_WORKER_KEY on stdin"
  write_keys "$id"
  start_unit "$id" _exec "$id" "$arm" "$@"
  log "started wf-run-$id (arm $arm${*:+ $*}); log $RUNS/$id.log"
}

cmd_start_replay() {
  local id="${1:-}" src="${2:-}"
  shift 2 || true
  check_id "$id"
  check_id "$src"
  check_replay_opts "$@"
  [ -d "$REPLAY/$src" ] || die "no such replay source $REPLAY/$src (76 push-run first)"
  [ -e "$RUNS/$id" ] && die "run $id exists -- run ids are single-use"
  if has_worker_opt "$@"; then read_keys; else read_keys no-worker; fi
  ! has_worker_opt "$@" || [ -n "$KEY_WORKER" ] || die "a --worker reviewer needs WF_WORKER_KEY on stdin"
  write_keys "$id"
  start_unit "$id" _exec_replay "$id" "$src" "$@"
  log "started wf-run-$id (replay of $src${*:+ $*}); log $RUNS/$id.log"
}

cmd_exec_replay() {
  local id="${1:-}" src="${2:-}" o
  shift 2 || true
  check_id "$id"
  check_id "$src"
  check_replay_opts "$@"
  local extra=()
  for o in "$@"; do
    case "$o" in
      --reviewer-model=*) extra+=(--reviewer-model "${o#--reviewer-model=}") ;;
      --worker=*) extra+=(--worker "${o#--worker=}") ;;
      --read-first) extra+=(--read-first) ;;
      --worker-alias=*) extra+=(--worker-alias "${o#--worker-alias=}") ;;
      --review-steps=*) extra+=(--review-steps "${o#--review-steps=}") ;;
      --enforce-read) extra+=(--enforce-read) ;;
      --defects-only) extra+=(--defects-only) ;;
      --reviewer-temperature=*) extra+=(--reviewer-temperature "${o#--reviewer-temperature=}") ;;
    esac
  done
  local f="$KEYS/$id.env"
  [ -r "$f" ] || die "no keys for $id"
  set -a
  # shellcheck disable=SC1090
  . "$f"
  set +a
  rm -f "$f"
  exec "$WF_RUN" replay-reviews --run "$REPLAY/$src" --bundles "$BUNDLES" --out "$RUNS/$id" \
    --router "$ROUTER_URL" --grader "$GRADER" --agent-user-prefix "$PREFIX" "${extra[@]}"
}

cmd_exec() {
  local id="${1:-}" arm="${2:-}"
  shift 2 || true
  check_id "$id"
  check_arm "$arm"
  check_opts "$@"
  local f="$KEYS/$id.env"
  [ -r "$f" ] || die "no keys for $id"
  set -a
  # shellcheck disable=SC1090
  . "$f"
  set +a
  rm -f "$f"
  local w args=(run --arm "$arm" --bundles "$BUNDLES" --out "$RUNS/$id" --router "$ROUTER_URL"
                --grader "$GRADER" --agent-user-prefix "$PREFIX")
  if [ "$arm" = T ]; then
    for w in $WORKERS; do args+=(--worker "http://$w:$WORKER_PORT/v1"); done
  fi
  local o
  for o in "$@"; do
    case "$o" in
      --lead-url=*) args+=(--lead-url "${o#--lead-url=}") ;;
      --lead-alias=*) args+=(--lead-alias "${o#--lead-alias=}") ;;
      --review-steps=*) args+=(--review-steps "${o#--review-steps=}") ;;
      --reviewer-temperature=*) args+=(--reviewer-temperature "${o#--reviewer-temperature=}") ;;
      *) args+=("$o") ;;
    esac
  done
  exec "$WF_RUN" "${args[@]}"
}

# warm_one <label> <url> <key> <model>: one short completion; prints "warm <label> <code> <seconds>".
warm_one() {
  local label="$1" url="$2" key="$3" model="$4" body res
  body=$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply with the single word ready."}],"max_tokens":16,"temperature":0}' "$model")
  res=$(printf 'header = "Authorization: Bearer %s"\n' "$key" \
        | curl -s -o /dev/null -w "%{http_code} %{time_total}" --max-time 900 -K - \
            -H "Content-Type: application/json" -X POST --data "$body" "$url/chat/completions" || echo "000 0")
  echo "warm $label $res"
  [ "${res%% *}" = 200 ]
}

cmd_warm() {
  local arm="${1:-}" w ok=0
  [[ "$arm" == T || "$arm" == G || "$arm" == S ]] || die "arm must be T, G or S"
  read_keys
  [ "$arm" != T ] || [ -n "$KEY_WORKER" ] || die "arm T needs WF_WORKER_KEY on stdin"
  warm_one lead "$ROUTER_URL" "$KEY_ROUTER" "$LEAD_ALIAS" || ok=1
  if [ "$arm" = T ]; then
    for w in $WORKERS; do warm_one "$w" "http://$w:$WORKER_PORT/v1" "$KEY_WORKER" "$WORKER_ALIAS" || ok=1; done
  fi
  return $ok
}

cmd_status() {
  local id="${1:-}"
  check_id "$id"
  echo "unit: $(systemctl is-active "wf-run-$id" 2>/dev/null || true)"
  if [ -f "$RUNS/$id/run.json" ] || [ -f "$RUNS/$id/replay.json" ]; then echo "record: present"; else echo "record: absent"; fi
  if [ -f "$RUNS/$id.log" ]; then tail -n 3 "$RUNS/$id.log"; fi
}

cmd_pack() {
  local id="${1:-}"
  check_id "$id"
  [ "$(systemctl is-active "wf-run-$id" 2>/dev/null || true)" = active ] && die "wf-run-$id is still running"
  [ -f "$RUNS/$id/run.json" ] || [ -f "$RUNS/$id/replay.json" ] || die "no $RUNS/$id/run.json (or replay.json) -- the run did not finish"
  mkdir -p "$OUT"
  local items=("$id")
  [ -f "$RUNS/$id.log" ] && items+=("$id.log")
  tar --force-local -C "$RUNS" -czf "$OUT/$id.tgz" "${items[@]}"
  echo "$OUT/$id.tgz $(stat -c %s "$OUT/$id.tgz") $(sha256sum "$OUT/$id.tgz" | cut -d' ' -f1)"
}

case "${1:-}" in
  start) shift; cmd_start "$@" ;;
  start-replay) shift; cmd_start_replay "$@" ;;
  _exec) shift; cmd_exec "$@" ;;
  _exec_replay) shift; cmd_exec_replay "$@" ;;
  warm) shift; cmd_warm "$@" ;;
  status) shift; cmd_status "$@" ;;
  pack) shift; cmd_pack "$@" ;;
  clear-keys) rm -f "$KEYS"/*.env; log "key files removed" ;;
  *) echo "usage: $0 start <run-id> <T|G|S> [--w1] | start-replay <run-id> <src-run-id> | warm <T|G|S> | status <run-id> | pack <run-id> | clear-keys" >&2; exit 2 ;;
esac
