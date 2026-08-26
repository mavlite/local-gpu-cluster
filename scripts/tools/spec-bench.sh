#!/bin/bash
# spec-bench.sh — compare speculative-decoding configs on the live chat unit.
#
# RUN THIS INSIDE LXC 151 (it edits llamacpp-chat.service and restarts it):
#     pct push 151 scripts/tools/spec-bench.sh /root/spec-bench.sh
#     pct push 151 scripts/tools/apply-spec.py  /root/apply-spec.py
#     pct exec 151 -- cp /etc/systemd/system/llamacpp-chat.service /root/llamacpp-chat.service.PRE-SPECBENCH
#     pct exec 151 -- /root/spec-bench.sh
#
# Chat is unavailable while it runs (one restart per config, ~90s each). The unit
# is restored from PRE-SPECBENCH on exit AND on interrupt.
#
# As shipped it sweeps ngram-mod draft length (n-min/n-max) against the current
# draft-mtp baseline. Conclusion recorded in 51-lxc-amd.sh: rejected for RAG.
#
# From spec-bench2 (same build/quant, 4 distinct prompts):
#   A draft-mtp n-max 3            COLD 43.36  WARM 43.28   (flat: no pool)
#   B draft-mtp,ngram-mod 48/64    COLD 40.61  WARM 54.30
#   C ngram-mod only     48/64     COLD 22.07  WARM 64.36
# Hypothesis: the cold penalty is drafting 48-64 tokens into a cold hash pool --
# most drafts miss and the batched verify is paid anyway. Shorter drafts should
# cut that loss. Docs warn against a small n-MATCH (the 24-token lookup window),
# not against short drafts, so n-match stays 24 throughout.
#
# A is re-measured here as an in-session control: restarts, thermals and pool
# state must not be compared across separate runs.
#
# Validity note: the restart between configs really does clear the hash pool --
# in spec-bench2, C ran immediately after B with identical prompts and still
# reported its LOWEST number cold (22.07), which could not happen if B's pool
# had carried over.
set -u
UNIT=/etc/systemd/system/llamacpp-chat.service
BACKUP=/root/llamacpp-chat.service.PRE-SPECBENCH
KEY=$(sed -n 's/^LLAMACPP_API_KEY=//p' /etc/llamacpp.env | tr -d '"')
OUT=/root/spec-sweep-results.txt
NPRED=400

restore() {
  cp "$BACKUP" "$UNIT"; systemctl daemon-reload; systemctl restart llamacpp-chat
  echo "RESTORED baseline unit $(date -u +%FT%TZ)" | tee -a "$OUT"
}
trap 'echo "  interrupted - restoring"; restore; exit 130' INT TERM

wait_ready() {
  for i in $(seq 1 240); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
           -H "Authorization: Bearer $KEY" http://127.0.0.1:8080/health 2>/dev/null)
    [ "$code" = "200" ] && return 0
    sleep 5
  done
  echo "      TIMEOUT waiting for ready"; return 1
}

P1='VMware Cloud Foundation 9.1 upgrade prerequisites require all workload domains to report healthy state, the management domain upgraded first, and sufficient free vSAN capacity for the rolling upgrade. Quote those prerequisites verbatim, then restate each in your own words.'
P2='NSX Edge clusters in VCF 9.1 must be at a compatible version before SDDC Manager permits a workload domain upgrade. Edge node form factors determine throughput ceilings. Quote that constraint verbatim, then explain its operational impact.'
P3='vSAN storage policies in VCF 9.1 control failures-to-tolerate, stripe width, and space reservation per object. Changing a policy triggers a rolling resync. Quote that behaviour verbatim, then describe the capacity implications.'
P4='SDDC Manager backup in VCF 9.1 captures configuration state but not workload data; restore requires matching appliance versions. Quote that limitation verbatim, then outline a recovery runbook.'

gen() {
  local body
  body=$(python3 -c 'import json,sys; print(json.dumps({"prompt":sys.argv[1],"n_predict":int(sys.argv[2]),"temperature":0,"seed":42,"cache_prompt":False}))' "$1" "$NPRED")
  curl -s --max-time 300 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
       http://127.0.0.1:8080/completion -d "$body" \
  | python3 -c 'import json,sys
try:
    t=json.load(sys.stdin).get("timings",{})
    print("%.2f" % t.get("predicted_per_second",0))
except Exception:
    print("0")'
}

block() {
  local sum=0 n=0 tps
  for p in "$P1" "$P2" "$P3" "$P4"; do
    tps=$(gen "$p"); sum=$(python3 -c "print($sum+$tps)"); n=$((n+1))
  done
  python3 -c "print('      $1: %.2f t/s' % ($sum/$n))" | tee -a "$OUT"
}

echo "=== spec-sweep $(date -u +%FT%TZ) ===" > "$OUT"
/opt/llama.cpp/build/bin/llama-server --version 2>&1 | head -1 >> "$OUT"
echo "n-match fixed at 24. COLD = 4 distinct prompts on a fresh server; WARM = same 4 repeated." >> "$OUT"

run_cfg() {  # $1=label, rest=args
  local label="$1"; shift
  echo "" | tee -a "$OUT"; echo "  === $label ===" | tee -a "$OUT"
  cp "$BACKUP" "$UNIT"
  python3 "$(dirname "$0")/apply-spec.py" "$UNIT" "$@" || { echo "  apply failed"; return; }
  systemctl daemon-reload; systemctl restart llamacpp-chat; wait_ready || return
  curl -s --max-time 120 -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
       http://127.0.0.1:8080/completion -d '{"prompt":"warm","n_predict":16}' >/dev/null 2>&1
  block "COLD"; block "WARM"
}

run_cfg "A control: draft-mtp n-max 3" \
  "--spec-type draft-mtp" "--spec-draft-n-max 3"

for pair in "8 12" "16 24" "32 48" "48 64"; do
  set -- $pair
  run_cfg "B draft-mtp+ngram  n-min $1 n-max $2" \
    "--spec-type draft-mtp,ngram-mod" "--spec-draft-n-max 2" \
    "--spec-ngram-mod-n-match 24" "--spec-ngram-mod-n-min $1" "--spec-ngram-mod-n-max $2"
done

run_cfg "C ngram-only  n-min 16 n-max 24" \
  "--spec-type ngram-mod" "--spec-ngram-mod-n-match 24" \
  "--spec-ngram-mod-n-min 16" "--spec-ngram-mod-n-max 24"

restore; wait_ready
echo "SPECSWEEP-DONE $(date -u +%FT%TZ)" | tee -a "$OUT"
