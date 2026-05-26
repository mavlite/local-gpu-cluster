#!/usr/bin/env bash
# stability-test-coder.sh — exercise the coder profile under growing KV pressure
# and report on OOM, VRAM drift, latency, and unit errors.
#
# What it does:
#   1. Snapshots rocm-smi + journalctl cursor as baseline.
#   2. Sends four chat-completions requests through the router:
#        T1  — ~2K prompt tokens (warmup)
#        T2  — ~16K prompt tokens (medium)
#        T3a — ~90K prompt tokens (heavy, first occurrence)
#        T3b — ~90K prompt tokens (heavy, repeat — runaway-drift detector)
#      The T3a/T3b pair distinguishes "first-time activation buffer
#      allocation" (normal, bounded) from "buffer grows per heavy
#      request" (real fragmentation).
#   3. After each, snapshots rocm-smi + measures latency.
#   4. Post-settle remeasure (30s wait) to catch buffers that release
#      after the request completes.
#   5. Tails journalctl from the baseline cursor to surface any errors
#      (ERROR / OOM / hsa / failed) and notable warnings (cache_reuse
#      auto-disable, etc.).
#   6. Reports VRAM peak, drift breakdown, and a pass/fail summary.
#
# Pre-reqs:
#   - Run from the Proxmox host as root.
#   - Coder profile must already be loaded (./scripts/swap-chat-model.sh coder).
#   - Router (LXC 153) reachable on 192.168.6.153:8000.
#   - The repo's scripts/ directory is the source of test content — concat'd
#     files of growing size make for a realistic OpenCode-style workload.
#
# Usage:
#   ./scripts/tools/stability-test-coder.sh
#   ./scripts/tools/stability-test-coder.sh --keep-tmp   # leave context files for inspection
#
# Approx. duration: 3-8 min depending on coder's throughput.

set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
REPO_DIR="${LGC_DIR%/scripts}"
AMD_VMID="${AMD_VMID:-151}"
ROUTER_VMID="${ROUTER_VMID:-153}"
ROUTER_HOST="${ROUTER_HOST:-192.168.6.${ROUTER_VMID}}"
ROUTER_PORT="${ROUTER_PORT:-8000}"

# MODEL_ALIAS is auto-detected from the chat unit's --alias arg below if not
# specified. Override via --model or the MODEL_ALIAS env var if you want to
# exercise a specific router-side alias (e.g., to test thinking-mode behavior
# separately from non-thinking) rather than whatever the chat unit reports.
MODEL_ALIAS="${MODEL_ALIAS:-}"

KEEP_TMP=false
FOLLOW=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep-tmp)   KEEP_TMP=true; shift ;;
    --follow|-f)  FOLLOW=true; shift ;;
    --model)      MODEL_ALIAS="$2"; shift 2 ;;
    -h|--help)
      cat <<EOF
Usage: $(basename "$0") [--follow] [--keep-tmp] [--model ALIAS]

Stability-tests whichever chat profile is currently loaded (auto-detected
from the chat unit's --alias arg). Sends three escalating chat-completions
through the router (~2K → ~30K → ~100K prompt tokens), snapshots rocm-smi
between each, scans journalctl for errors, and reports:

  - peak VRAM per GPU + drift baseline → peak
  - post-settle VRAM (30s after last request) → distinguishes transient
    activation buffers from permanent fragmentation
  - latency + throughput per round
  - notable llama.cpp warnings (cache_reuse auto-disable, etc.)
  - pass/fail verdict

Flags:
  --follow, -f      Stream chat-unit journal in the background during the
                    test. Each line prefixed with "  | ".
  --keep-tmp        Leave the generated prompt files in TMP_DIR for inspection.
  --model ALIAS     Override the router-side model alias. Default: auto-
                    detected from the chat unit's --alias arg. Useful for
                    testing a thinking-mode alias separately (e.g., to
                    test qwen3.6-think while qwen3.6 is loaded).
EOF
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

TMP_DIR=$(mktemp -d -t coder-stability.XXXXXX)
JOURNAL_PID=""

cleanup() {
  $KEEP_TMP || rm -rf "$TMP_DIR"
  if [[ -n "$JOURNAL_PID" ]]; then
    kill "$JOURNAL_PID" 2>/dev/null || true
    wait "$JOURNAL_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ─── helpers ─────────────────────────────────────────────────────────────────

vram_snapshot() {
  # Returns "G0_pct G1_pct" on one line.
  pct exec "$AMD_VMID" -- rocm-smi --showmemuse -d 0 1 2>/dev/null \
    | awk '
        /GPU\[0\].*VRAM%/  { for(i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) { g0=$i } }
        /GPU\[1\].*VRAM%/  { for(i=1;i<=NF;i++) if ($i ~ /^[0-9]+$/) { g1=$i } }
        END { printf "%s %s\n", g0, g1 }'
}

# pct exec with the router key resolved on the router LXC, posts a chat-completions
# request, prints "tokens_in tokens_out elapsed_s" on stdout.
send_chat() {
  local prompt_file="$1" max_tokens="$2"
  local start end elapsed body resp
  start=$(date +%s.%N)
  # Build the JSON body on the router LXC so the prompt file is readable there
  # via /tank (or we can pipe it). Simpler: cat the prompt locally on the host
  # and let the host curl directly to the router endpoint.
  resp=$(jq -Rs --arg model "$MODEL_ALIAS" --argjson max "$max_tokens" \
    '{model:$model, max_tokens:$max, temperature:0.2, messages:[{role:"user", content:.}]}' \
    < "$prompt_file" \
    | curl -sf -X POST \
        -H "Authorization: Bearer ${ROUTER_KEY}" \
        -H "Content-Type: application/json" \
        --data-binary @- \
        "http://${ROUTER_HOST}:${ROUTER_PORT}/v1/chat/completions") \
    || { echo "  ERROR: curl failed" >&2; return 1; }
  end=$(date +%s.%N)
  elapsed=$(awk -v s="$start" -v e="$end" 'BEGIN {printf "%.1f", e-s}')

  local in_tok out_tok
  in_tok=$(jq -r '.usage.prompt_tokens // 0' <<<"$resp")
  out_tok=$(jq -r '.usage.completion_tokens // 0' <<<"$resp")
  echo "$in_tok $out_tok $elapsed"
}

# ─── pre-flight ──────────────────────────────────────────────────────────────

echo "[stability-test-coder] starting at $(date -u +%FT%TZ)"
echo

# Resolve router key (matches the smoke-test pattern in swap-chat-model.sh)
ROUTER_KEY=$(pct exec "$ROUTER_VMID" -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env 2>/dev/null) \
  || { echo "ERROR: couldn't read ROUTER_API_KEY from LXC $ROUTER_VMID:/etc/router.env" >&2; exit 1; }
[[ -n "$ROUTER_KEY" ]] || { echo "ERROR: ROUTER_API_KEY is empty" >&2; exit 1; }

# Confirm SOME chat profile is loaded (was coder-only; now profile-agnostic).
# Chat unit's systemd ExecStart never lies about what model is mmap'd into VRAM.
chat_hf=$(pct exec "$AMD_VMID" -- bash -c "grep -oE '[A-Za-z0-9_-]+/[A-Za-z0-9._-]+:[A-Za-z0-9_-]+' /etc/systemd/system/llamacpp-chat.service | head -1" 2>/dev/null || echo "")
echo "  chat unit --hf-repo: ${chat_hf:-<not detected>}"
if [[ -z "$chat_hf" ]]; then
  echo "ERROR: no --hf-repo detected in chat unit. Is llamacpp-chat installed?" >&2
  echo "       Check: pct exec $AMD_VMID -- systemctl status llamacpp-chat" >&2
  exit 1
fi

# Auto-detect MODEL_ALIAS from the chat unit's --alias arg unless overridden.
if [[ -z "$MODEL_ALIAS" ]]; then
  MODEL_ALIAS=$(pct exec "$AMD_VMID" -- bash -c "grep -oE -- '--alias \"[^\"]+\"' /etc/systemd/system/llamacpp-chat.service | head -1 | sed 's/.*\"\([^\"]*\)\".*/\1/'" 2>/dev/null || echo "")
fi
if [[ -z "$MODEL_ALIAS" ]]; then
  echo "ERROR: couldn't determine MODEL_ALIAS. Pass --model <alias> explicitly." >&2
  exit 1
fi
echo "  target model alias:  $MODEL_ALIAS"

# Informational only: the router /v1/models list.
loaded=$(curl -sf -H "Authorization: Bearer $ROUTER_KEY" \
  "http://${ROUTER_HOST}:${ROUTER_PORT}/v1/models" 2>/dev/null \
  | jq -r '.data[].id' 2>/dev/null | tr '\n' ' ' || echo "")
echo "  router /v1/models reports: ${loaded:-<unreachable>}"
if ! echo "$loaded" | grep -qw "$MODEL_ALIAS"; then
  cat >&2 <<WARN
  WARN: router does not advertise '$MODEL_ALIAS' in /v1/models.
        Either the router-app.py is stale (redeploy with scripts/53-lxc-router.sh)
        or this alias maps to a backend that doesn't match the loaded profile.
        Test will proceed and rely on ALIAS_MAP passthrough — request will
        likely still route correctly.
WARN
fi

# Mark the journal cursor BEFORE the test so we can scan only test-window log entries
journal_cursor=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat -n0 --show-cursor 2>/dev/null \
  | awk '/^-- cursor:/ {print $3}')
echo "  journal cursor at start: ${journal_cursor:0:32}…"

baseline=$(vram_snapshot)
echo "  baseline VRAM: GPU0=${baseline% *}%  GPU1=${baseline##* }%"
echo

# ─── build the three test prompts ────────────────────────────────────────────

# T1: small warmup — ~1-2K tokens (a single substantial coding question)
cat > "$TMP_DIR/t1.txt" <<'P1'
Please carefully review this short Python script and identify:
1. Any bugs or edge cases it handles incorrectly.
2. Suggest two readability improvements with explanations.
3. Propose one performance optimization with an estimated impact.

```python
import os, json, hashlib

def load_users(path):
    with open(path) as f:
        return json.load(f)

def hash_password(p):
    return hashlib.md5(p.encode()).hexdigest()

def authenticate(users, name, pw):
    for u in users:
        if u['name'] == name:
            if u['password'] == hash_password(pw):
                return True
    return False

def main():
    users = load_users('users.json')
    while True:
        name = input('user: ')
        pw = input('pw: ')
        if authenticate(users, name, pw):
            print('OK')
            os.system(f'echo "welcome {name}"')
            break

if __name__ == '__main__':
    main()
```

Be specific and quote line ranges in your review.
P1

# T2: medium ~30K tokens — concatenate a handful of repo scripts with context
{
  echo "Here are several scripts from a homelab GPU-cluster repo. Please:"
  echo "1. Identify the operational pattern that ties them together."
  echo "2. Spot the three most subtle bugs OR foot-guns across these files."
  echo "3. Propose a refactor that would reduce duplication, with a code sketch."
  echo
  for f in \
      "$LGC_DIR/swap-chat-model.sh" \
      "$LGC_DIR/51-lxc-amd.sh" \
      "$LGC_DIR/lib/common.sh" \
      "$LGC_DIR/52-lxc-router.sh" \
      "$LGC_DIR/53-lxc-anythingllm.sh"; do
    [[ -f "$f" ]] || continue
    echo "### $(basename "$f") ###"
    cat "$f"
    echo
  done
} > "$TMP_DIR/t2.txt"

# T3: heavy ~80-120K tokens — many scripts + the router app + day-2-ops
{
  echo "Here is a substantial portion of a homelab GPU-cluster repo. Please:"
  echo "1. Summarize the system architecture in ~200 words."
  echo "2. List the top FIVE risks/foot-guns you can spot across the codebase."
  echo "3. For each risk, point to a specific file and lines if you can."
  echo
  for f in \
      "$LGC_DIR"/*.sh \
      "$LGC_DIR/lib/"*.sh \
      "$LGC_DIR/files/router-app.py" \
      "$LGC_DIR/files/router.env.example" \
      "$REPO_DIR/day-2-ops.md"; do
    [[ -f "$f" ]] || continue
    echo "### ${f#$REPO_DIR/} ###"
    cat "$f"
    echo
  done
} > "$TMP_DIR/t3.txt"

t1_bytes=$(wc -c < "$TMP_DIR/t1.txt")
t2_bytes=$(wc -c < "$TMP_DIR/t2.txt")
t3_bytes=$(wc -c < "$TMP_DIR/t3.txt")
echo "  test prompts built (bytes): T1=$t1_bytes  T2=$t2_bytes  T3=$t3_bytes"
echo "  (approx tokens: divide bytes by 3.5)"
echo

# ─── --follow: spawn background journal poller for the test duration ─────────
# Polls every 3s via --since "@EPOCH" + line-count tracking (cursor-based
# polling proved fragile through pct exec arg-passing). Killing the subshell
# PID terminates cleanly because no `pct exec ... -f` blocks across iterations.
if $FOLLOW; then
  (
    since="@$(date +%s)"
    seen=0
    while sleep 3; do
      all=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat --since "$since" --no-pager -o cat 2>/dev/null || true)
      [[ -z "$all" ]] && continue
      new_count=$(printf '%s\n' "$all" | wc -l)
      if (( new_count > seen )); then
        printf '%s\n' "$all" | tail -n +$((seen + 1)) | sed 's/^/  | /'
        seen=$new_count
      fi
    done
  ) &
  JOURNAL_PID=$!
  echo "  --follow: streaming chat-unit journal (poll 3s, PID=$JOURNAL_PID)"
  echo
fi

# ─── run the tests ───────────────────────────────────────────────────────────

declare -a results=()
declare -a vram_after=()

# T3a + T3b are both the same heavy prompt. Runaway-buffer detection compares
# T3b to T3a: if the second heavy request grows VRAM further on top of the
# first, the activation buffer is unbounded (real fragmentation). If it stays
# at T3a's peak, the buffer is bounded (one-time allocation — normal on this
# llama.cpp build). Without two consecutive heavies, we can't distinguish
# "first allocation" from "runaway", because T2's medium prompt is below the
# threshold that triggers the heavy-prefill buffer in the first place.
for round in 1 2 3 4; do
  case $round in
    1) prompt="$TMP_DIR/t1.txt"; max=600  ; label="T1 (warmup)"          ;;
    2) prompt="$TMP_DIR/t2.txt"; max=2000 ; label="T2 (medium)"          ;;
    3) prompt="$TMP_DIR/t3.txt"; max=3000 ; label="T3a (heavy — first)"  ;;
    4) prompt="$TMP_DIR/t3.txt"; max=3000 ; label="T3b (heavy — repeat)" ;;
  esac
  echo "── round $round: $label ──"
  result=$(send_chat "$prompt" "$max") || {
    echo "  FAILED — stopping test"
    exit 1
  }
  read -r r_in r_out r_lat <<<"$result"
  echo "  result: in=${r_in} tok  out=${r_out} tok  latency=${r_lat}s"
  vram=$(vram_snapshot)
  echo "  VRAM after: GPU0=${vram% *}%  GPU1=${vram##* }%"
  results+=("$result")
  vram_after+=("$vram")
  echo
done

# ─── post-settle remeasure ───────────────────────────────────────────────────
# Activation buffers from large prefills may release after the request completes
# (transient — no fragmentation concern) OR they may stick around (real drift,
# compounds over a long session). Wait + remeasure to distinguish.

echo "── post-settle (waiting 30s for activation buffers to release) ──"
sleep 30
post_settle=$(vram_snapshot)
echo "  post-settle VRAM: GPU0=${post_settle% *}%  GPU1=${post_settle##* }%"
echo

# ─── post-test analysis ──────────────────────────────────────────────────────

echo "── post-test journal scan (errors since baseline) ──"
if [[ -n "$journal_cursor" ]]; then
  errors=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat \
    --after-cursor "$journal_cursor" --no-pager 2>/dev/null \
    | grep -Ei 'error|oom|hsa.*fail|cuda.*fail|failed to|abort' \
    | grep -v 'warning' \
    || true)
  # Surface notable llama.cpp warnings (cache_reuse auto-disable, etc.) for awareness
  warnings=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat \
    --after-cursor "$journal_cursor" --no-pager 2>/dev/null \
    | grep -iE 'cache_reuse.*not supported|control-looking token|n_ctx_seq.*<.*n_ctx_train' \
    | sort -u || true)
else
  errors=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat -n 500 --no-pager 2>/dev/null \
    | grep -Ei 'error|oom|hsa.*fail|cuda.*fail|failed to|abort' \
    | grep -v 'warning' \
    || true)
  warnings=""
fi
if [[ -z "$errors" ]]; then
  echo "  (no error-level lines in journal during test window)"
else
  echo "$errors"
fi
if [[ -n "$warnings" ]]; then
  echo
  echo "  notable llama.cpp warnings (informational):"
  echo "$warnings" | sed 's/^/    /'
fi
echo

echo "── summary ──"
printf "  %-15s %12s %12s %10s\n" "round" "in tokens" "out tokens" "latency(s)"
labels=("T1" "T2" "T3a" "T3b")
for i in 0 1 2 3; do
  read -r in_t out_t lat <<<"${results[$i]}"
  printf "  %-15s %12s %12s %10s\n" "${labels[$i]}" "$in_t" "$out_t" "$lat"
done
echo
printf "  %-15s  GPU0  GPU1\n" "snapshot"
printf "  %-15s  %4s  %4s\n" "baseline" "${baseline% *}" "${baseline##* }"
for i in 0 1 2 3; do
  v="${vram_after[$i]}"
  printf "  %-15s  %4s  %4s\n" "after-${labels[$i]}" "${v% *}" "${v##* }"
done
printf "  %-15s  %4s  %4s\n" "post-settle" "${post_settle% *}" "${post_settle##* }"
echo

# Peak = max across all per-round snapshots.
peak_g0=${baseline% *}
peak_g1=${baseline##* }
for v in "${vram_after[@]}"; do
  vg0=${v% *}; vg1=${v##* }
  (( vg0 > peak_g0 )) && peak_g0=$vg0
  (( vg1 > peak_g1 )) && peak_g1=$vg1
done

# Two drift numbers — peak drift is "worst transient" (could include
# activation buffers that release); settle drift is "what's still held"
# (real fragmentation that compounds across sessions).
peak_drift_g0=$(( peak_g0 - ${baseline% *} ))
peak_drift_g1=$(( peak_g1 - ${baseline##* } ))
settle_drift_g0=$(( ${post_settle% *} - ${baseline% *} ))
settle_drift_g1=$(( ${post_settle##* } - ${baseline##* } ))

echo "  peak VRAM:        GPU0=${peak_g0}%   GPU1=${peak_g1}%"
echo "  peak drift:       GPU0=+${peak_drift_g0}pp  GPU1=+${peak_drift_g1}pp  (worst transient)"
echo "  post-settle drift: GPU0=+${settle_drift_g0}pp  GPU1=+${settle_drift_g1}pp  (held buffers — real fragmentation)"
echo

# ─── pass/fail verdict ───────────────────────────────────────────────────────

# Runaway-growth detector: compares T3b to T3a (both 90K prefills). If the
# second heavy request bumps VRAM further on top of the first, the activation
# buffer is unbounded — real fragmentation that will compound across a long
# session. If T3b stays at T3a's level, the buffer is bounded (one-time
# allocation that survives across requests — normal on this llama.cpp build).
# T2 vs T3 doesn't work for this because T2 is below the threshold that
# triggers the heavy-prefill buffer in the first place.
v_after_t3a=${vram_after[2]}
v_after_t3b=${vram_after[3]}
runaway_g0=$(( ${v_after_t3b% *} - ${v_after_t3a% *} ))
runaway_g1=$(( ${v_after_t3b##* } - ${v_after_t3a##* } ))

pass=true
[[ -n "$errors" ]] && { echo "  ❌ FAIL: errors in journal"; pass=false; }
(( peak_g0 >= 98 )) && { echo "  ⚠️  FAIL: GPU0 peak >= 98% (near-OOM territory)"; pass=false; }
(( peak_g1 >= 98 )) && { echo "  ⚠️  FAIL: GPU1 peak >= 98% (near-OOM territory)"; pass=false; }
(( runaway_g0 > 5 )) && { echo "  ⚠️  FAIL: GPU0 runaway drift — T3b added +${runaway_g0}pp on top of T3a (buffer not bounded across consecutive heavy prefills)"; pass=false; }
(( runaway_g1 > 5 )) && { echo "  ⚠️  FAIL: GPU1 runaway drift — T3b added +${runaway_g1}pp on top of T3a (buffer not bounded across consecutive heavy prefills)"; pass=false; }
# Informational: one-time activation buffer is normal — verified bounded if
# runaway delta is small.
if (( settle_drift_g0 > 5 || settle_drift_g1 > 5 )); then
  if (( runaway_g0 <= 5 && runaway_g1 <= 5 )); then
    echo "  ℹ️   note: post-settle drift +${settle_drift_g0}pp/+${settle_drift_g1}pp — one-time activation buffer, verified bounded (T3b vs T3a delta = +${runaway_g0}pp/+${runaway_g1}pp)"
  fi
fi

# Latency-degradation check: T3a should not be more than ~5x slower than T2
# PER TOKEN. Attention is O(n²) so 5-6× input growth naturally produces ~3-6×
# per-token slowdown. Compare T2→T3a (not T3b) because T3b benefits from the
# prompt-cache hit on the identical T3a prompt and isn't representative of
# cold-context throughput.
read -r _ t2_out t2_lat <<<"${results[1]}"
read -r _ t3a_out t3a_lat <<<"${results[2]}"
read -r _ t3b_out t3b_lat <<<"${results[3]}"
if (( t2_out > 0 && t3a_out > 0 )); then
  t2_tps=$(awk -v t="$t2_lat" -v n="$t2_out" 'BEGIN {printf "%.2f", n/t}')
  t3a_tps=$(awk -v t="$t3a_lat" -v n="$t3a_out" 'BEGIN {printf "%.2f", n/t}')
  t3b_tps=$(awk -v t="$t3b_lat" -v n="$t3b_out" 'BEGIN {printf "%.2f", n/t}')
  echo "  throughput:  T2=${t2_tps} tok/s   T3a=${t3a_tps} tok/s   T3b=${t3b_tps} tok/s (cache hit)"
  ratio=$(awk -v a="$t2_tps" -v b="$t3a_tps" 'BEGIN {if (b>0) printf "%.2f", a/b; else print "inf"}')
  echo "  slowdown:    T2/T3a = ${ratio}x  (>5x suggests context-length sensitivity beyond expected quadratic scaling)"
fi

echo
if $pass; then
  echo "  ✅ PASS: no errors, no near-OOM, no runaway drift across consecutive heavy prefills — $MODEL_ALIAS profile stable under tested load"
else
  echo "  ❌ FAIL: see warnings above"
  exit 1
fi
