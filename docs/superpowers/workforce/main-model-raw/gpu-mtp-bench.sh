#!/usr/bin/env bash
# gpu-mtp-bench.sh <out> — inside LXC 151, GPU free. Per config: start llama-server on 127.0.0.1:8091, one
# throwaway warm-up, then each body /root/mtpb-*.json twice; one JSON line per request with predicted_n,
# gen t/s, prompt t/s, draft stats. Thinking off (enable_thinking false), layer split 1:1, ub 1024, f16 KV.
set -uo pipefail
OUT="${1:?out}"
BIN=/opt/llama.cpp/build/bin/llama-server; PORT=8091; UNIT=wf-gpu-mtpb
M6=/opt/models/qwen36-mtp/Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf
M4X=/opt/models/qwen36-mtp/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
BASE="--alias qwen3.6 --flash-attn auto --ctx-size 65536 --split-mode layer --tensor-split 1,1 --batch-size 2048 --ubatch-size 1024 --parallel 1 --n-gpu-layers all --jinja --host 127.0.0.1 --port $PORT"
BASE="$BASE --chat-template-kwargs {\"enable_thinking\":false} --min-p 0"
CFGS=(
 "q4km-nomtp|--hf-repo unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"
 "q6xl-nomtp|-m $M6"
 "q6xl-mtp1|-m $M6 --spec-type draft-mtp --spec-draft-n-max 1"
 "q6xl-mtp2|-m $M6 --spec-type draft-mtp --spec-draft-n-max 2"
 "q6xl-mtp3|-m $M6 --spec-type draft-mtp --spec-draft-n-max 3"
 "q4xl-nomtp-retry|-m $M4X"
)
stop() { systemctl stop "$UNIT" 2>/dev/null; systemctl reset-failed "$UNIT" 2>/dev/null; sleep 3; }
: > "$OUT"
for c in "${CFGS[@]}"; do
  name="${c%%|*}"; flags="${c#*|}"; stop
  logf="/var/log/wf-gpu-mtpb-$name.log"; : > "$logf"
  # shellcheck disable=SC2086
  systemd-run --quiet --unit="$UNIT" --collect -p StandardOutput="append:$logf" -p StandardError="append:$logf" \
    -E HSA_OVERRIDE_GFX_VERSION=10.3.0 -E HIP_FORCE_DEV_KERNARG=1 -E LLAMA_CACHE=/opt/models/.cache \
    "$BIN" $flags $BASE
  ok=0; for i in $(seq 1 120); do sleep 2; curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null && { ok=1; break; }; [ "$(systemctl is-active $UNIT)" = active ] || break; done
  if [ $ok != 1 ]; then echo "{\"config\":\"$name\",\"error\":\"no server\"}" >> "$OUT"; stop; continue; fi
  curl -s -m 600 -H 'Content-Type: application/json' --data @/root/mtpb-code-t07.json "http://127.0.0.1:$PORT/v1/chat/completions" >/dev/null
  for b in /root/mtpb-*.json; do
    for rep in 1 2; do
      r=$(curl -s -m 900 -H 'Content-Type: application/json' --data @"$b" "http://127.0.0.1:$PORT/v1/chat/completions")
      python3 -c '
import json, sys
name, body, rep, raw = sys.argv[1:5]
try:
    d = json.loads(raw); t = d.get("timings") or {}
    print(json.dumps({"config": name, "body": body, "rep": int(rep), "predicted_n": t.get("predicted_n"),
          "gen_tps": t.get("predicted_per_second"), "prompt_n": t.get("prompt_n"), "prompt_tps": t.get("prompt_per_second"),
          "draft_n": t.get("draft_n"), "draft_accepted": t.get("draft_n_accepted")}))
except Exception as e:
    print(json.dumps({"config": name, "body": body, "rep": int(rep), "error": f"{type(e).__name__}: {raw[:200]}"}))
' "$name" "$(basename "$b" .json)" "$rep" "$r" >> "$OUT"
    done
  done
  grep -q "illegal memory" "$logf" && echo "{\"config\":\"$name\",\"note\":\"ROCm illegal memory access in log\"}" >> "$OUT"
  stop
done
echo DONE >> "$OUT"
