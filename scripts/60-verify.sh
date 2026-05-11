#!/usr/bin/env bash
# 60-verify.sh — Appendix C smoke tests from setup-runbook.md.
#
# Runs the verification tests in order. Each prints PASS/FAIL/SKIP.
# Designed to be safe to re-run; pure-read with timeouts.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

AMD_VMID="${AMD_VMID:-151}"
NV_VMID="${NV_VMID:-152}"
ROUTER_VMID="${ROUTER_VMID:-153}"
ALLM_VMID="${ANYTHINGLLM_VMID:-154}"
MCP_VMID="${MCP_VMID:-155}"

PASS=0; FAIL=0; SKIPPED=0
results=()

check() {
  local name="$1"; shift
  printf "  [ .. ] %s" "$name"
  if "$@" >/dev/null 2>&1; then
    printf "\r  [%bPASS%b] %s\n" "${__LGC_GRN}" "${__LGC_NC}" "$name"
    PASS=$((PASS+1))
  else
    printf "\r  [%bFAIL%b] %s\n" "${__LGC_RED}" "${__LGC_NC}" "$name"
    FAIL=$((FAIL+1))
    results+=("FAIL: $name")
  fi
}

skip_test() {
  printf "  [%bSKIP%b] %s\n" "${__LGC_YLW}" "${__LGC_NC}" "$1"
  SKIPPED=$((SKIPPED+1))
}

# ---------- 1. Host-level GPU enumeration ----------
step "1. Host sees 3 GPUs"
gpu_count="$(lspci -nn | grep -ciE 'vga|3d controller' || true)"
check "lspci shows ≥3 GPU entries"        [ "$gpu_count" -ge 3 ]

# ---------- 2. IOMMU active ----------
step "2. IOMMU active on host"
check "dmesg shows AMD-Vi enabled" \
  bash -c 'dmesg | grep -qE "AMD-Vi: Interrupt remapping enabled|Detected AMD IOMMU"'

# ---------- 3. ROCm sees both V620s ----------
step "3. ROCm sees both V620s (LXC $AMD_VMID)"
if lxc_exists "$AMD_VMID" && lxc_running "$AMD_VMID"; then
  rocm_count="$(pct exec "$AMD_VMID" -- rocminfo 2>/dev/null \
                | grep -c 'Name:.*gfx1030' || true)"
  check "rocminfo shows 2 gfx1030 agents" [ "$rocm_count" -eq 2 ]
else
  skip_test "LXC $AMD_VMID not running"
fi

# ---------- 4. CUDA sees the 3060 ----------
step "4. CUDA sees the 3060 (LXC $NV_VMID)"
if lxc_exists "$NV_VMID" && lxc_running "$NV_VMID"; then
  check "nvidia-smi succeeds in LXC" \
    pct exec "$NV_VMID" -- nvidia-smi
  check "RTX 3060 detected" \
    bash -c "pct exec $NV_VMID -- nvidia-smi --query-gpu=name --format=csv,noheader | grep -qi '3060'"
else
  skip_test "LXC $NV_VMID not running"
fi

# ---------- 5. V620 llama-server responding ----------
step "5. V620 llama-server responds"
if lxc_running "$AMD_VMID"; then
  check "GET /v1/models on V620 stack" \
    pct exec "$AMD_VMID" -- timeout 5 curl -fsS http://localhost:8080/v1/models
else
  skip_test "LXC $AMD_VMID not running"
fi

# ---------- 6. 3060 services responding ----------
step "6. 3060 embedder + reranker respond"
if lxc_running "$NV_VMID"; then
  check "GET /v1/models on embedder (8082)" \
    pct exec "$NV_VMID" -- timeout 5 curl -fsS http://localhost:8082/v1/models
  check "GET /v1/models on reranker (8083)" \
    pct exec "$NV_VMID" -- timeout 5 curl -fsS http://localhost:8083/v1/models
else
  skip_test "LXC $NV_VMID not running"
fi

# ---------- 7. Router healthy + aggregates models ----------
step "7. Router healthy"
if lxc_running "$ROUTER_VMID"; then
  check "GET /healthz returns ok=true" \
    bash -c "pct exec $ROUTER_VMID -- timeout 5 curl -fsS http://localhost:8000/healthz | grep -q '\"ok\":true'"
  check "GET /v1/models aggregates ≥1 model" \
    bash -c "pct exec $ROUTER_VMID -- timeout 5 curl -fsS http://localhost:8000/v1/models | grep -q '\"data\"'"
else
  skip_test "Router LXC $ROUTER_VMID not running"
fi

# ---------- 8. Embedding dimension is 1024 ----------
step "8. Embedding dimension"
if lxc_running "$NV_VMID"; then
  dim="$(pct exec "$NV_VMID" -- bash -c "curl -fsS http://localhost:8082/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{\"model\":\"qwen3-embedding\",\"input\":\"probe\"}' \
        | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d[\"data\"][0][\"embedding\"]))'" 2>/dev/null || echo 0)"
  check "Embedding returns 1024 dimensions" [ "$dim" = "1024" ]
else
  skip_test "LXC $NV_VMID not running"
fi

# ---------- 9. AnythingLLM API reachable ----------
step "9. AnythingLLM responding"
if lxc_running "$ALLM_VMID"; then
  check "GET / on AnythingLLM port 3001" \
    pct exec "$ALLM_VMID" -- timeout 5 curl -fsSL -o /dev/null http://localhost:3001/
else
  skip_test "AnythingLLM LXC $ALLM_VMID not running"
fi

# ---------- 10. MCP SSE endpoints listening ----------
step "10. MCP SSE endpoints"
if lxc_running "$MCP_VMID"; then
  for port in 3002 3003 3004; do
    check "MCP port $port reachable" \
      pct exec "$MCP_VMID" -- bash -c "timeout 3 curl -fsS http://localhost:$port/sse >/dev/null 2>&1 || \
                                       timeout 3 curl -fsS http://localhost:$port/ >/dev/null 2>&1"
  done
else
  skip_test "MCP LXC $MCP_VMID not running"
fi

# ---------- 11. Speculative decoding active (advisory) ----------
step "11. Spec decode acceptance (advisory)"
if lxc_running "$AMD_VMID"; then
  # Only meaningful if traffic has flowed; treat as advisory.
  if pct exec "$AMD_VMID" -- journalctl -u llama-server --no-pager 2>/dev/null \
     | grep -q 'spec_decode'; then
    printf "  [%bPASS%b] spec_decode lines present in llama-server logs\n" "${__LGC_GRN}" "${__LGC_NC}"
    PASS=$((PASS+1))
  else
    skip_test "No spec_decode telemetry yet (need a generation first)"
  fi
fi

# ---------- summary ----------
step "Summary"
printf "  Pass: %d   Fail: %d   Skipped: %d\n" "$PASS" "$FAIL" "$SKIPPED"
if (( FAIL > 0 )); then
  echo
  err "Failures:"
  for r in "${results[@]}"; do printf "  - %s\n" "$r"; done
  exit 1
fi
ok "All verifiable checks passed (skipped checks need their LXCs running)."
