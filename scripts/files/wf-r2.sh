#!/usr/bin/env bash
# wf-r2.sh — round 2 Task 11: the six pre-registered runs S G T T G S, on the Proxmox host
# (systemd-run), so nothing inside a run depends on the workstation. Derived from Plan D's wf-w3.sh.
# Preconditions (checked): window open, keys in /root/wf/keys (router.key; worker.key for T), frozen
# bundles pushed, workers up with C2 and locked, probe unit wf-probe active on the router.
# Per run: probe alive -> one untimed warm-up to the lead (and each worker for T; round 2 §5.2) ->
# stamp before -> 76 start-run -> wait -> stamp after -> harvest -> chat journal for exactly that
# window. Invalid runs are kept, never repeated (spec §9). Stops at WF_R2_STOP_H hours (default 14).
#   systemd-run --unit=wf-r2 --collect /bin/bash /root/local-gpu-cluster/scripts/files/wf-r2.sh
set -uo pipefail
cd /root/local-gpu-cluster || exit 1
LOG="${WF_R2_LOG:-/root/wf/r2.log}"
STAMPS="${WF_R2_STAMPS:-/root/wf/stamps.jsonl}"
PROBE_OUT="${WF_R2_PROBE_OUT:-/root/wf-probe-r2.jsonl}"
RUNS=(r2-1-s r2-2-g r2-3-t r2-4-t r2-5-g r2-6-s)
START=$SECONDS
STOP_AT=$(( ${WF_R2_STOP_H:-14} * 3600 ))
log() { echo "$(date -u +%T) $*" >> "$LOG"; }
abort() { log "ABORT: $*"; exit 1; }
journal_ts() {  # journal_ts <label>: the stamp's ts as "YYYY-MM-DD HH:MM:SS UTC"
  python3 - "$STAMPS" "$1" <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ts = [r["ts"] for r in recs if r["label"] == sys.argv[2]][-1]
print(ts.replace("T", " ").replace("Z", " UTC"))
PY
}

[ -f /root/wf/window/window.env ] || abort "window is not open"
[ -s /root/wf/keys/router.key ] && [ -s /root/wf/keys/worker.key ] || abort "keys missing"
log "R2 start: ${RUNS[*]}"
for id in "${RUNS[@]}"; do
  (( SECONDS - START < STOP_AT )) || abort "stop_at_h reached before $id"
  arm=$(echo "${id##*-}" | tr a-z A-Z)
  if [ "$(pct exec 153 -- systemctl is-active wf-probe)" != active ]; then
    log "probe not active before $id: restarting it"
    pct exec 153 -- systemd-run --unit=wf-probe --collect /usr/bin/python3 /root/wf-user-probe.py run \
      --out "$PROBE_OUT" --interval 60 >/dev/null 2>&1 || log "probe restart failed"
  fi
  # Untimed warm-ups: the lead through the router, and each worker for T. Recorded, outside the stamps.
  w=$(bash scripts/76-vm-wf-sandbox.sh warm "$arm" 2>&1 | grep "^warm " | tr '\n' ';')
  log "warm $id ($arm): $w"
  bash scripts/files/wf-window.sh stamp "$STAMPS" "before-$id" >/dev/null || abort "stamp before $id failed"
  out=$(bash scripts/76-vm-wf-sandbox.sh start-run "$id" "$arm" 2>&1 | tail -1)
  log "start $id ($arm): $out"
  while :; do
    sleep 60
    st=$(bash scripts/76-vm-wf-sandbox.sh run-status "$id" 2>/dev/null)
    [[ "$st" =~ unit:\ (inactive|failed) ]] && break
    (( SECONDS - START < STOP_AT )) || abort "stop_at_h reached during $id"
  done
  bash scripts/files/wf-window.sh stamp "$STAMPS" "after-$id" >/dev/null || log "stamp after $id failed"
  h=$(bash scripts/76-vm-wf-sandbox.sh harvest "$id" /root/wf/runs 2>&1 | tail -1)
  log "harvest $id: $h"
  pct exec 151 -- journalctl -u llamacpp-chat --since "$(journal_ts "before-$id")" \
    --until "$(journal_ts "after-$id")" -o cat > "/root/wf/runs/$id.journal" 2>/dev/null \
    || log "journal capture for $id failed"
  log "journal $id: $(grep -c print_timing "/root/wf/runs/$id.journal") timing records"
done
log "DONE in $(( (SECONDS - START) / 60 )) min"
