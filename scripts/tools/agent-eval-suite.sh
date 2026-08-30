#!/bin/bash
# run-suite.sh <model> <keyfile> [eval_url]
# Run every task set against one model and collect the results in one file.
#
# Turn caps differ by set on purpose: long-horizon tasks need room to actually
# be long, and a task that fails at the cap measures the cap rather than the
# model. Reps differ too -- 3 for the short sets so a single flake does not
# swing the rate, 1 for the long set which is slow and largely deterministic.
set -u
MODEL="${1:?model}"
KEYFILE="${2:?keyfile}"
URL="${3:-http://192.168.6.153:8000/v1/chat/completions}"
K=$(tr -d '\n\r' < "$KEYFILE")
OUT="/root/suite-${MODEL}.log"
: > "$OUT"
echo "===== SUITE $MODEL $(date -u +%FT%TZ) =====" >> "$OUT"
echo "url=$URL" >> "$OUT"

run_set () {
  local set_name="$1" reps="$2" turns="$3"
  echo "" >> "$OUT"
  echo "----- taskset=$set_name reps=$reps maxturns=$turns -----" >> "$OUT"
  TASKSET="$set_name" MAXTURNS="$turns" EVAL_URL="$URL" \
    python3 /root/agent-eval-v3.py "$MODEL" "$K" "$reps" >> "$OUT" 2>&1
}

run_set hidden 3 18
run_set open   3 18
run_set long   1 40
run_set hard   1 18

echo "" >> "$OUT"
echo "SUITE DONE $(date -u +%FT%TZ)" >> "$OUT"
