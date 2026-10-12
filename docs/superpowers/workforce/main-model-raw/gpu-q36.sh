#!/usr/bin/env bash
# gpu-q36.sh start <nothink|think> | stop | status — TRANSIENT best-config Qwen3.6-35B-A3B server on the V620s
# for the main-model research (inside LXC 151). Config from the 2026-10-12 MTP bench: Unsloth MTP GGUF
# UD-Q6_K_XL, in-GGUF MTP (draft-mtp, n-max 3), layer split 1,1, ub 1024, f16 KV, 1 slot x 128K.
# Sampling = the model card's preset for the mode (non-thinking 0.7/0.8/20, presence 1.5 per card is NOT
# used server-side so a client's own sampling (aider sends temperature 0) is what counts; thinking-coding
# 0.6/0.95/20). Same key file as the reviewer server; stop does NOT shred it (the final restore does).
# --n-predict 32768: a temperature-0 repetition loop (Polyglot forth, 52K+ tokens) fails fast instead of
# stalling to the 131K context; no real answer comes near it, so scores are unchanged.
set -euo pipefail
UNIT=wf-gpu-q36; PORT=8090; KEY=/root/wf-gpu-reviewer.key; LOG=/var/log/wf-gpu-q36.log
M=/opt/models/qwen36-mtp/Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf
case "${1:-}" in
  start)
    mode="${2:?nothink|think}"
    case "$mode" in
      nothink) kw='{"enable_thinking":false}'; samp=(--temp 0.7 --top-p 0.8 --top-k 20) ;;
      think)   kw='{"enable_thinking":true}';  samp=(--temp 0.6 --top-p 0.95 --top-k 20) ;;
      *) echo "mode must be nothink or think" >&2; exit 2 ;;
    esac
    [ -s "$KEY" ] || { echo "no $KEY" >&2; exit 1; }
    systemctl is-active --quiet wf-gpu-reviewer && { echo "wf-gpu-reviewer still running" >&2; exit 1; }
    systemctl stop "$UNIT" 2>/dev/null || true; systemctl reset-failed "$UNIT" 2>/dev/null || true
    : > "$LOG"
    systemd-run --quiet --unit="$UNIT" --collect \
      -p StandardOutput="append:$LOG" -p StandardError="append:$LOG" \
      -E HSA_OVERRIDE_GFX_VERSION=10.3.0 -E HIP_FORCE_DEV_KERNARG=1 -E LLAMA_CACHE=/opt/models/.cache \
      /opt/llama.cpp/build/bin/llama-server -m "$M" --alias qwen3.6 \
        --host 0.0.0.0 --port "$PORT" --api-key-file "$KEY" \
        --ctx-size 131072 --parallel 1 --n-gpu-layers all --split-mode layer --tensor-split 1,1 \
        --flash-attn auto --batch-size 2048 --ubatch-size 1024 \
        --spec-type draft-mtp --spec-draft-n-max 3 --n-predict 32768 \
        --jinja --chat-template-kwargs "$kw" "${samp[@]}" --presence-penalty 0 --min-p 0 \
        --reasoning-format deepseek --metrics --slots
    for i in $(seq 1 180); do
      sleep 2
      curl -sf -m 2 "http://127.0.0.1:$PORT/health" >/dev/null && { echo "up ($mode) after $((i * 2)) s"; exit 0; }
      systemctl is-active --quiet "$UNIT" || { echo "unit died; tail:" >&2; tail -n 5 "$LOG" >&2; exit 1; }
    done
    echo "no health in 360 s" >&2; exit 1 ;;
  stop) systemctl stop "$UNIT" 2>/dev/null || true; systemctl reset-failed "$UNIT" 2>/dev/null || true; echo stopped ;;
  status) echo "unit: $(systemctl is-active "$UNIT" 2>/dev/null || true)"; curl -s -m 3 "http://127.0.0.1:$PORT/health" || true; echo ;;
  *) echo "usage: $0 start <nothink|think> | stop | status" >&2; exit 2 ;;
esac
