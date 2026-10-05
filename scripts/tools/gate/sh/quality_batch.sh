#!/usr/bin/env bash
# Phase-1 gate quality sub-gate: <runs> back-to-back Polyglot runs against one endpoint, on LXC 158.
#   quality_batch.sh <label> <alias> <api-base> <key-file> <runs> [first-index=1]
# Run names are gate-<label>-NN; each run's aider log goes to /root/gate/logs/gate-<label>-NN.log.
# Start it detached (setsid nohup ... &) -- a run takes ~1 h on the GPU and several hours on a CPU worker.
set -Eeuo pipefail
LABEL="${1:?label}"; ALIAS="${2:?alias}"; BASE="${3:?api-base}"; KEYFILE="${4:?key-file}"
RUNS="${5:?runs}"; FIRST="${6:-1}"
[[ "$LABEL" =~ ^[a-z0-9-]+$ ]] || { echo "label must be [a-z0-9-]+" >&2; exit 2; }
[[ "$RUNS" =~ ^[0-9]+$ && "$FIRST" =~ ^[0-9]+$ ]] || { echo "runs/first-index must be integers" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /root/gate/logs
for ((i = FIRST; i < FIRST + RUNS; i++)); do
  name="$(printf 'gate-%s-%02d' "$LABEL" "$i")"
  echo "$(date -u +%FT%TZ) start $name" >> /root/gate/logs/batch-"$LABEL".log
  if "$HERE/polyglot_run.sh" "$ALIAS" "$name" "$BASE" "$KEYFILE" > "/root/gate/logs/$name.log" 2>&1; then
    echo "$(date -u +%FT%TZ) done $name" >> /root/gate/logs/batch-"$LABEL".log
  else
    echo "$(date -u +%FT%TZ) FAILED $name rc=$?" >> /root/gate/logs/batch-"$LABEL".log
  fi
done
echo "$(date -u +%FT%TZ) batch $LABEL complete" >> /root/gate/logs/batch-"$LABEL".log
