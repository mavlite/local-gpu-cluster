#!/usr/bin/env bash
# Phase-1 gate: CPU worker llama-server control. Runs INSIDE an llmbenchNN VM as user `bench`
# (passwordless sudo), delivered and invoked through vSphere GuestOperations (gate_guest.ps1).
#   worker_serve.sh quiet               stop apt timers for the window; needrestart list-only
#   worker_serve.sh install-key <file>  move an uploaded throwaway key to /etc/gate/worker.key
#   worker_serve.sh learn               log (do not filter) new :8090 connections
#   worker_serve.sh sources             print the source IPs seen since `learn`
#   worker_serve.sh lock <ip>...        accept :8090 only from <ip>..., drop everyone else
#   worker_serve.sh start               launch llama-server with the frozen flags; print the log path
#   worker_serve.sh stop | status | teardown
# Spec: docs/superpowers/specs/2026-10-05-minisforum-cluster-integration-design.md §3, §5.0.3, §5.5, §5.8
set -Eeuo pipefail

BIN="${GATE_IK_BIN:-/opt/bench/src/ik/build/bin/llama-server}"
MODEL="${GATE_MODEL:-/models/Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf}"
PORT=8090
ETC=/etc/gate
KEY="${GATE_KEY_FILE:-$ETC/worker.key}"
LOGDIR="${GATE_LOGDIR:-/var/log/gate}"
PIDFILE="${GATE_PIDFILE:-/run/gate-llama.pid}"
UNIT=gate-llama
# Below the 32 GB worker VM (24 GB OOM-killed all three under Polyglot load, 2026-10-06) so an
# OOM, if one ever happens, kills only the server unit -- never VMware Tools or sshd.
MEMMAX="${GATE_MEMMAX:-30G}"
# Frozen worker flags (spec §3, shipped config) -- the gate commit pins this exact string.
WORKER_FLAGS="-c 65536 -t 12 -tb 16 -ub 1024 -b 2048 -fa on -ctk q8_0 -ctv q8_0 -rtr --spec-type mtp:n_max=3,p_min=0.5 -mtprot iq4_xs --min-p 0"
# Mandatory memory flags (ik defaults OOM), plus the Qwen non-thinking preset -- the same values the
# router injects for the GPU alias, presence penalty included (v2, 2026-10-07: workers without it looped).
WORKER_EXTRA="-cram 256 -ctx-ckpt 8 --temp 0.7 --top-p 0.8 --top-k 20 --presence-penalty 1.5"
APT_UNITS=(apt-daily.timer apt-daily-upgrade.timer)

die() { echo "[worker-serve] FATAL: $*" >&2; exit 1; }
log() { echo "[worker-serve] $*"; }

cmd_quiet() {
  sudo mkdir -p "$ETC"
  local u
  for u in "${APT_UNITS[@]}"; do
    echo "$u=$(systemctl is-active "$u" 2>/dev/null || true)"
  done | sudo tee "$ETC/apt.state" >/dev/null
  sudo systemctl stop "${APT_UNITS[@]}" 2>/dev/null || true
  sudo mkdir -p /etc/needrestart/conf.d
  echo "\$nrconf{restart} = 'l';" | sudo tee /etc/needrestart/conf.d/99-gate.conf >/dev/null
  log "apt timers stopped, needrestart list-only"
}

cmd_install_key() {
  local src="${1:?install-key <uploaded file>}"
  [ -s "$src" ] || die "$src is empty or missing"
  sudo mkdir -p "$ETC"
  sudo install -o root -g bench -m 0640 "$src" "$KEY"
  shred -u "$src" 2>/dev/null || rm -f "$src"
  log "key installed at $KEY (root:bench 0640)"
}

nft_table() {           # nft_table <rules...>: replace the gate table atomically
  {
    echo "table inet gate"
    echo "delete table inet gate"
    echo "table inet gate {"
    echo " chain input {"
    echo "  type filter hook input priority 0; policy accept;"
    printf '  %s\n' "$@"
    echo " }"           # each closing brace on its own line: "} }" is a syntax error to nft
    echo "}"
  } | sudo nft -f -
}

cmd_learn() {
  nft_table "tcp dport $PORT ct state new log prefix \"gate8090 \""
  log "logging new :$PORT connections; curl from every client, then run: sources"
}

cmd_sources() {
  sudo journalctl -k --no-pager -o cat | grep 'gate8090 ' | grep -o 'SRC=[0-9.]*' | sort -u | cut -d= -f2
}

cmd_lock() {
  [ "$#" -ge 1 ] || die "lock needs at least one client IP"
  local ip
  for ip in "$@"; do
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "not an IPv4 address: $ip"
  done
  local set
  set="$(IFS=,; echo "$*")"
  nft_table "tcp dport $PORT ip saddr { $set } accept" "tcp dport $PORT counter drop"
  log ":$PORT accepted only from: $*"
}

cmd_start() {
  [ -r "$KEY" ] || die "$KEY not readable -- run install-key first"
  systemctl is-active --quiet "$UNIT" && die "already running"
  sudo mkdir -p "$LOGDIR"; sudo chown bench:bench "$LOGDIR"
  local logf
  logf="$LOGDIR/server-$(date -u +%Y%m%dT%H%M%SZ).log"
  sudo systemctl reset-failed "$UNIT" 2>/dev/null || true
  # Own transient unit with a memory cap. Started from GuestOperations the server otherwise runs
  # in open-vm-tools.service, whose default OOMPolicy=stop took VMware Tools down with it when the
  # kernel OOM-killed llama-server (all 3 workers, 2026-10-06).
  # shellcheck disable=SC2086  # the flag strings are intentionally word-split
  sudo systemd-run --quiet --unit="$UNIT" --uid=bench --gid=bench -p MemoryMax="$MEMMAX" \
    -p StandardOutput="append:$logf" -p StandardError="append:$logf" \
    "$BIN" -m "$MODEL" $WORKER_FLAGS $WORKER_EXTRA \
    --jinja -np 1 --alias qwen3.6 --chat-template-kwargs '{"enable_thinking":false}' \
    --host 0.0.0.0 --port "$PORT" --api-key-file "$KEY" || die "systemd-run failed"
  systemctl show -p MainPID --value "$UNIT" | sudo tee "$PIDFILE" >/dev/null
  local i
  for i in $(seq 1 300); do
    curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null && { echo "$logf"; return 0; }
    systemctl is-active --quiet "$UNIT" || die "llama-server exited; tail: $(tail -n 5 "$logf")"
    sleep 2
  done
  die "llama-server not healthy after 600 s"
}

cmd_stop() {
  sudo systemctl stop "$UNIT" 2>/dev/null || true
  sudo systemctl reset-failed "$UNIT" 2>/dev/null || true
  sudo rm -f "$PIDFILE"
  log "stopped"
}

cmd_status() {
  echo "pid: $(cat "$PIDFILE" 2>/dev/null || echo none)"
  echo "health: $(curl -s -m 3 "http://127.0.0.1:$PORT/health" || echo down)"
  echo "key: $(stat -c '%U:%G %a' "$KEY" 2>/dev/null || echo absent)"
  echo "latest log: $(ls -1t "$LOGDIR"/server-*.log 2>/dev/null | head -1)"
  sudo nft list table inet gate 2>/dev/null || echo "nft: no gate table"
}

cmd_teardown() {
  cmd_stop
  [ -f "$KEY" ] && sudo shred -u "$KEY"
  sudo nft delete table inet gate 2>/dev/null || true
  sudo rm -f /etc/needrestart/conf.d/99-gate.conf
  if [ -f "$ETC/apt.state" ]; then
    local line u st
    while IFS= read -r line; do
      u="${line%%=*}"; st="${line#*=}"
      [ "$st" = "active" ] && sudo systemctl start "$u"
    done < "$ETC/apt.state"
  fi
  sudo rm -rf "$ETC"
  log "teardown complete: server stopped, key shredded, firewall table removed, apt timers restored"
}

case "${1:-}" in
  quiet) cmd_quiet ;;
  install-key) shift; cmd_install_key "$@" ;;
  learn) cmd_learn ;;
  sources) cmd_sources ;;
  lock) shift; cmd_lock "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  teardown) cmd_teardown ;;
  *) echo "usage: $0 quiet|install-key <f>|learn|sources|lock <ip>...|start|stop|status|teardown" >&2; exit 2 ;;
esac
