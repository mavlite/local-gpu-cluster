#!/usr/bin/env bash
# wf-run-control.sh start <run-id> <T|G> [--w1] | status <run-id> | pack <run-id> | clear-keys
#
# Runs INSIDE the workforce sandbox VM as root (installed as /usr/local/sbin/wf-run-control by
# 76-vm-wf-sandbox.sh); the host drives it through the guest agent. Plan D.
#
#   start   read the keys from stdin (WF_ROUTER_KEY=..., and WF_WORKER_KEY=... for arm T; any other
#           line is refused), write them to a root-only file, and start the harness detached as the
#           transient unit wf-run-<id>. A W3 run lasts hours: longer than any guest-exec timeout.
#   _exec   (the unit's process) load the keys into the environment, DELETE the file, then exec the
#           harness: from here on the keys live only in the harness's process environment.
#   status  the unit state, whether run.json exists, and the log's tail.
#   pack    once the run has finished, /srv/wf/runs/<id> + its log into /root/wf-out/<id>.tgz;
#           prints the size and sha256 so the host can verify its copy.
#   clear-keys  remove every leftover key file (window teardown).
# Run ids are single-use. Keys are never on a command line and never printed.
set -Eeuo pipefail

RUNS="${WF_RC_RUNS:-/srv/wf/runs}"
KEYS="${WF_RC_KEYS:-/run/wf}"
OUT="${WF_RC_OUT:-/root/wf-out}"
WF_RUN="${WF_RC_BIN:-/usr/local/sbin/wf-run}"
BUNDLES="${WF_RC_BUNDLES:-/srv/wf/bundles}"
ROUTER_URL="${WF_RC_ROUTER:-http://192.168.6.153:8000/v1}"
WORKERS="${WF_RC_WORKERS:-172.16.10.205 172.16.10.206 172.16.10.207}"
WORKER_PORT=8090
GRADER=docker:wf-grader:1
PREFIX=wf
SELF="$(readlink -f "$0")"

die() { echo "[wf-run-control] FATAL: $*" >&2; exit 1; }
log() { echo "[wf-run-control] $*"; }

check_id() { [[ "${1:-}" =~ ^[a-z0-9][a-z0-9._-]{0,40}$ ]] || die "run id must match [a-z0-9][a-z0-9._-]{0,40}"; }
check_arm() { [[ "${1:-}" == T || "${1:-}" == G ]] || die "arm must be T or G"; }
check_opts() {
  local o
  for o in "$@"; do [[ "$o" == --w1 ]] || die "unknown option '$o' (only --w1)"; done
}
safe_key() { [[ "$1" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; }

cmd_start() {
  local id="${1:-}" arm="${2:-}"
  shift 2 || true
  check_id "$id"
  check_arm "$arm"
  check_opts "$@"
  [ -e "$RUNS/$id" ] && die "run $id exists -- run ids are single-use"
  local line router="" worker=""
  while IFS= read -r line || [ -n "$line" ]; do
    [ -z "$line" ] && continue
    case "$line" in
      WF_ROUTER_KEY=?*) router="${line#WF_ROUTER_KEY=}" ;;
      WF_WORKER_KEY=?*) worker="${line#WF_WORKER_KEY=}" ;;
      *) die "unexpected stdin line (only WF_ROUTER_KEY= and WF_WORKER_KEY= are accepted)" ;;
    esac
  done
  [ -n "$router" ] || die "WF_ROUTER_KEY missing on stdin"
  [ "$arm" = G ] || [ -n "$worker" ] || die "arm T needs WF_WORKER_KEY on stdin"
  safe_key "$router" || die "WF_ROUTER_KEY has unexpected characters"
  [ -z "$worker" ] || safe_key "$worker" || die "WF_WORKER_KEY has unexpected characters"
  mkdir -p "$KEYS" "$RUNS"
  chmod 0700 "$KEYS"
  (
    umask 077
    echo "WF_ROUTER_KEY=$router" > "$KEYS/$id.env"
    if [ -n "$worker" ]; then echo "WF_WORKER_KEY=$worker" >> "$KEYS/$id.env"; fi
  )
  systemd-run --unit="wf-run-$id" --collect \
    -p StandardOutput="append:$RUNS/$id.log" -p StandardError="append:$RUNS/$id.log" \
    /bin/bash "$SELF" _exec "$id" "$arm" "$@"
  log "started wf-run-$id (arm $arm${*:+ $*}); log $RUNS/$id.log"
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
  exec "$WF_RUN" "${args[@]}" "$@"
}

cmd_status() {
  local id="${1:-}"
  check_id "$id"
  echo "unit: $(systemctl is-active "wf-run-$id" 2>/dev/null || true)"
  if [ -f "$RUNS/$id/run.json" ]; then echo "record: present"; else echo "record: absent"; fi
  if [ -f "$RUNS/$id.log" ]; then tail -n 3 "$RUNS/$id.log"; fi
}

cmd_pack() {
  local id="${1:-}"
  check_id "$id"
  [ "$(systemctl is-active "wf-run-$id" 2>/dev/null || true)" = active ] && die "wf-run-$id is still running"
  [ -f "$RUNS/$id/run.json" ] || die "no $RUNS/$id/run.json -- the run did not finish"
  mkdir -p "$OUT"
  local items=("$id")
  [ -f "$RUNS/$id.log" ] && items+=("$id.log")
  tar --force-local -C "$RUNS" -czf "$OUT/$id.tgz" "${items[@]}"
  echo "$OUT/$id.tgz $(stat -c %s "$OUT/$id.tgz") $(sha256sum "$OUT/$id.tgz" | cut -d' ' -f1)"
}

case "${1:-}" in
  start) shift; cmd_start "$@" ;;
  _exec) shift; cmd_exec "$@" ;;
  status) shift; cmd_status "$@" ;;
  pack) shift; cmd_pack "$@" ;;
  clear-keys) rm -f "$KEYS"/*.env; log "key files removed" ;;
  *) echo "usage: $0 start <run-id> <T|G> [--w1] | status <run-id> | pack <run-id> | clear-keys" >&2; exit 2 ;;
esac
