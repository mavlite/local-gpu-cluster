#!/bin/bash
# drive-backfill.sh <source-id> <expected-total> [max-runs]
#
# Drives a rag source to full coverage with --force tranches. --force bypasses
# refresh_interval; the nightly timer is unaffected.
#
# EXIT-CODE SEMANTICS (they are not all the same failure):
#   0        run applied cleanly -> continue
#   1        refresh HALTED on the deletion threshold. A human must review the
#            proposal with tools/verify-removals.py -> STOP.
#   >=128    killed by a signal. Observed 2026-08-24: trafilatura/lxml under 4
#            worker threads aborted with "corrupted size vs. prev_size" (glibc
#            heap corruption) at ~700/900 URLs, after six clean runs. Rare and
#            non-deterministic. Nothing is applied when this happens, so the
#            tranche is simply lost -> RETRY, up to RETRY_MAX consecutively.
#
# Do NOT wait on processes with `pgrep -f "<pattern>"` where the pattern also
# matches this script's own command line -- that self-match has deadlocked
# three watchers in this project. Wait on a marker file instead.
set -u
SRC="${1:?usage: drive-backfill.sh <source-id> <expected-total> [max-runs]}"
EXPECT="${2:?expected total url count}"
MAX="${3:-10}"
RETRY_MAX=2
BASE=/tank/vcf-docs/2026-08-refresh
DRIVER=$BASE/drive-$SRC.log
COV="python3 $BASE/coverage.py $SRC"
cd /root/local-gpu-cluster

echo "$(date -u +%FT%TZ) driver start for $SRC (expect ~$EXPECT, max $MAX runs)" >> "$DRIVER"
retries=0
for i in $(seq 1 "$MAX"); do
  read bt bf bp <<<"$($COV)"
  echo "$(date -u +%H:%M:%SZ) run $i/$MAX start: tracked=$bt fetched=$bf placeholders=$bp" >> "$DRIVER"
  LOG=$BASE/$SRC-run-$(date -u +%Y%m%dT%H%M%S).log
  ln -sfn "$LOG" $BASE/$SRC-latest.log
  # Run unpiped: piping through grep/tail masks the real exit status.
  /opt/vcf-scraper-venv/bin/python -u scripts/rag/refresh.py --source "$SRC" --force > "$LOG" 2>&1
  rc=$?
  read at af ap <<<"$($COV)"
  echo "$(date -u +%H:%M:%SZ) run $i rc=$rc: tracked=$at fetched=$af placeholders=$ap" >> "$DRIVER"

  if [ "$rc" -ge 128 ] 2>/dev/null; then
    retries=$((retries+1))
    echo "  CRASH (signal $((rc-128))) — nothing applied; retry $retries/$RETRY_MAX" >> "$DRIVER"
    [ "$retries" -ge "$RETRY_MAX" ] && { echo "  STOP: $RETRY_MAX consecutive crashes" >> "$DRIVER"; break; }
    continue
  fi
  retries=0
  if [ "$rc" != "0" ]; then
    # rc=1 covers two very different failures. Distinguish them by whether a
    # proposal was written, so the log does not misreport an embed timeout as a
    # deletion halt (it did on 2026-08-24).
    if ls /tank/rag-state/_proposals/${SRC}-*.json >/dev/null 2>&1; then
      echo "  STOP: threshold halt — review with tools/verify-removals.py" >> "$DRIVER"
    else
      echo "  STOP: refresh exited $rc, no proposal written — likely an embedding" >> "$DRIVER"
      echo "        failure. State is persisted BEFORE embedding, so documents may" >> "$DRIVER"
      echo "        be uploaded, tracked, and NOT attached. Check with:" >> "$DRIVER"
      echo "        tools/reconcile-workspace.py --workspace vcf-reference" >> "$DRIVER"
    fi
    break
  fi
  case "$af$ap" in ''|*[!0-9]*) echo "  STOP: counters unreadable" >> "$DRIVER"; break;; esac
  [ "$af" -le "$bf" ] && { echo "  STOP: fetched nothing new (converged or stuck)" >> "$DRIVER"; break; }
  if [ "$ap" -eq 0 ] && [ "$at" -ge "$EXPECT" ]; then
    echo "  DONE: full coverage, no placeholders" >> "$DRIVER"; break
  fi
done
echo "DRIVER-DONE $(date -u +%FT%TZ)" >> "$DRIVER"
