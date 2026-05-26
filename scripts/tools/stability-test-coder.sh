#!/usr/bin/env bash
# stability-test-coder.sh — exercise the coder profile under growing KV pressure
# and report on OOM, VRAM drift, latency, and unit errors.
#
# What it does:
#   1. Snapshots rocm-smi + journalctl cursor as baseline.
#   2. Sends three progressively heavier chat-completions requests through
#      the router (~2K → ~30K → ~100K prompt tokens), each generating
#      ~500-3000 tokens of output.
#   3. After each, snapshots rocm-smi + measures latency.
#   4. Tails journalctl from the baseline cursor to surface any errors
#      (ERROR / OOM / hsa / failed) emitted during the test.
#   5. Reports VRAM drift (peak - baseline) and a pass/fail summary.
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
MODEL_ALIAS="${MODEL_ALIAS:-qwen3-coder}"

KEEP_TMP=false
[[ "${1:-}" == "--keep-tmp" ]] && KEEP_TMP=true

TMP_DIR=$(mktemp -d -t coder-stability.XXXXXX)
trap '$KEEP_TMP || rm -rf "$TMP_DIR"' EXIT

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

# Confirm coder is loaded
loaded=$(curl -sf -H "Authorization: Bearer $ROUTER_KEY" \
  "http://${ROUTER_HOST}:${ROUTER_PORT}/v1/models" | jq -r '.data[].id' | tr '\n' ' ')
echo "  router /v1/models reports: $loaded"
if ! echo "$loaded" | grep -qw "$MODEL_ALIAS"; then
  echo "ERROR: $MODEL_ALIAS not listed by router. Run: ./scripts/swap-chat-model.sh coder" >&2
  exit 1
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

# ─── run the tests ───────────────────────────────────────────────────────────

declare -a results=()
declare -a vram_after=()

for round in 1 2 3; do
  case $round in
    1) prompt="$TMP_DIR/t1.txt"; max=600  ; label="T1 (warmup)" ;;
    2) prompt="$TMP_DIR/t2.txt"; max=2000 ; label="T2 (medium)" ;;
    3) prompt="$TMP_DIR/t3.txt"; max=3000 ; label="T3 (heavy)"  ;;
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

# ─── post-test analysis ──────────────────────────────────────────────────────

echo "── post-test journal scan (errors since baseline) ──"
if [[ -n "$journal_cursor" ]]; then
  errors=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat \
    --after-cursor "$journal_cursor" --no-pager 2>/dev/null \
    | grep -Ei 'error|oom|hsa.*fail|cuda.*fail|failed to|abort' \
    | grep -v 'warning' \
    || true)
else
  errors=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat -n 200 --no-pager 2>/dev/null \
    | grep -Ei 'error|oom|hsa.*fail|cuda.*fail|failed to|abort' \
    | grep -v 'warning' \
    || true)
fi
if [[ -z "$errors" ]]; then
  echo "  (no error-level lines in journal during test window)"
else
  echo "$errors"
fi
echo

echo "── summary ──"
printf "  %-15s %12s %12s %10s\n" "round" "in tokens" "out tokens" "latency(s)"
for i in 0 1 2; do
  read -r in_t out_t lat <<<"${results[$i]}"
  printf "  %-15s %12s %12s %10s\n" "$((i+1))" "$in_t" "$out_t" "$lat"
done
echo
printf "  %-15s  GPU0  GPU1\n" "snapshot"
printf "  %-15s  %4s  %4s\n" "baseline" "${baseline% *}" "${baseline##* }"
for i in 0 1 2; do
  v="${vram_after[$i]}"
  printf "  %-15s  %4s  %4s\n" "after-T$((i+1))" "${v% *}" "${v##* }"
done
echo

# Drift = peak - baseline
peak_g0=${baseline% *}
peak_g1=${baseline##* }
for v in "${vram_after[@]}"; do
  vg0=${v% *}; vg1=${v##* }
  (( vg0 > peak_g0 )) && peak_g0=$vg0
  (( vg1 > peak_g1 )) && peak_g1=$vg1
done
drift_g0=$(( peak_g0 - ${baseline% *} ))
drift_g1=$(( peak_g1 - ${baseline##* } ))

echo "  peak VRAM:   GPU0=${peak_g0}%   GPU1=${peak_g1}%"
echo "  drift:       GPU0=+${drift_g0}pp  GPU1=+${drift_g1}pp"
echo

# ─── pass/fail verdict ───────────────────────────────────────────────────────

pass=true
[[ -n "$errors" ]] && { echo "  ❌ FAIL: errors in journal"; pass=false; }
(( peak_g0 >= 98 )) && { echo "  ⚠️  WARN: GPU0 peak >= 98% (near-OOM territory)"; pass=false; }
(( peak_g1 >= 98 )) && { echo "  ⚠️  WARN: GPU1 peak >= 98% (near-OOM territory)"; pass=false; }
(( drift_g0 > 5 )) && { echo "  ⚠️  WARN: GPU0 drift >5pp (possible fragmentation)"; }
(( drift_g1 > 5 )) && { echo "  ⚠️  WARN: GPU1 drift >5pp (possible fragmentation)"; }

# Latency-degradation check: T3 should not be more than ~3x slower than T2 PER TOKEN
read -r _ t2_out t2_lat <<<"${results[1]}"
read -r _ t3_out t3_lat <<<"${results[2]}"
if (( t2_out > 0 && t3_out > 0 )); then
  t2_tps=$(awk -v t="$t2_lat" -v n="$t2_out" 'BEGIN {printf "%.2f", n/t}')
  t3_tps=$(awk -v t="$t3_lat" -v n="$t3_out" 'BEGIN {printf "%.2f", n/t}')
  echo "  throughput:  T2=${t2_tps} tok/s   T3=${t3_tps} tok/s"
  ratio=$(awk -v a="$t2_tps" -v b="$t3_tps" 'BEGIN {if (b>0) printf "%.2f", a/b; else print "inf"}')
  echo "  slowdown:    T2/T3 = ${ratio}x  (>3x suggests context-length sensitivity)"
fi

echo
if $pass; then
  echo "  ✅ PASS: no errors, no near-OOM, coder profile stable under tested load"
else
  echo "  ❌ FAIL: see warnings above"
  exit 1
fi
