#!/usr/bin/env bash
# 60-verify.sh — V620-only smoke tests (Appendix C / Phase 11.4 in setup-runbook.md).
#
# Runs verification tests in order. Each prints PASS/FAIL/SKIP.
# Pure-read with timeouts. Safe to re-run.
#
# V620-only build: LXC 152 (the old 3060 LXC) was destroyed. No NVIDIA checks.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

AMD_VMID="${AMD_VMID:-151}"
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

# Helper: fetch LLAMACPP_API_KEY from LXC 151
LLAMACPP_KEY="$(pct exec "$AMD_VMID" -- awk -F= '/^LLAMACPP_API_KEY=/{print $2}' /etc/llamacpp.env 2>/dev/null || true)"
ROUTER_KEY="$(pct exec "$ROUTER_VMID" -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env 2>/dev/null || true)"

# ---------- Group 1: Host (V620-only) ----------
step "1. Host: 2× V620, no NVIDIA"
gpu_count="$(lspci -nn | grep -ciE 'vga|3d controller' || true)"
check "lspci shows 2 GPU entries"          [ "$gpu_count" -eq 2 ]
check "No nvidia-smi binary on host"       bash -c '! command -v nvidia-smi'
check "Both V620 render nodes present"     bash -c '[ -e /dev/dri/renderD128 ] && [ -e /dev/dri/renderD129 ]'
check "No renderD130 (no 3rd GPU)"         bash -c '[ ! -e /dev/dri/renderD130 ]'

# ---------- Group 2: IOMMU + AMDGPU ----------
step "2. IOMMU active + AMDGPU loaded"
check "dmesg shows AMD IOMMU enabled" \
  bash -c 'dmesg | grep -qE "AMD-Vi: Interrupt remapping enabled|Detected AMD IOMMU"'
check "amdgpu kernel module loaded"   bash -c 'lsmod | grep -q "^amdgpu"'

# ---------- Group 3: ROCm sees both V620s ----------
step "3. ROCm sees both V620s (LXC $AMD_VMID)"
if lxc_exists "$AMD_VMID" && lxc_running "$AMD_VMID"; then
  rocm_count="$(pct exec "$AMD_VMID" -- rocminfo 2>/dev/null \
                | grep -c 'Name:.*gfx1030' || true)"
  check "rocminfo shows 2 gfx1030 agents" [ "$rocm_count" -eq 2 ]
else
  skip_test "LXC $AMD_VMID not running"
fi

# ---------- Group 4: Three llama-server units active ----------
step "4. V620 LXC has three llama-server units active"
if lxc_running "$AMD_VMID"; then
  for unit in llamacpp-chat llamacpp-embed llamacpp-rerank; do
    check "$unit.service active" \
      bash -c "pct exec $AMD_VMID -- systemctl is-active $unit | grep -q '^active$'"
  done
else
  skip_test "LXC $AMD_VMID not running — skipping unit checks"
fi

# ---------- Group 5: Direct endpoints (Bearer auth required) ----------
step "5. Direct endpoint health on LXC 151 (auth required)"
if [[ -z "$LLAMACPP_KEY" ]]; then
  skip_test "LLAMACPP_API_KEY not present in /etc/llamacpp.env — run 51-lxc-amd.sh first"
else
  for port in 8080 8082 8083; do
    check "151:$port returns 200 with Bearer auth" \
      bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'Authorization: Bearer $LLAMACPP_KEY' http://localhost:$port/v1/models); [ \"\$code\" = '200' ]"
    check "151:$port returns 401 WITHOUT auth (gate works)" \
      bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 http://localhost:$port/v1/models); [ \"\$code\" = '401' ]"
  done
fi

# ---------- Group 6: Embedding correctness ----------
step "6. Embedding correctness (--pooling last → dim 1024)"
if [[ -n "$LLAMACPP_KEY" ]] && lxc_running "$AMD_VMID"; then
  dim="$(pct exec "$AMD_VMID" -- bash -c "curl -fsS -m 10 -H 'Authorization: Bearer $LLAMACPP_KEY' http://localhost:8082/v1/embeddings \
        -H 'Content-Type: application/json' \
        -d '{\"input\":\"probe\"}' \
        | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d[\"data\"][0][\"embedding\"]))'" 2>/dev/null || echo 0)"
  check "Embedding dim is 1024" [ "$dim" = "1024" ]
else
  skip_test "Need LXC $AMD_VMID running + LLAMACPP_API_KEY"
fi

# ---------- Group 7: Rerank correctness ----------
step "7. Rerank correctness (Paris > Berlin for 'capital of France')"
if [[ -n "$LLAMACPP_KEY" ]] && lxc_running "$AMD_VMID"; then
  top="$(pct exec "$AMD_VMID" -- bash -c "curl -fsS -m 10 -H 'Authorization: Bearer $LLAMACPP_KEY' http://localhost:8083/v1/rerank \
        -H 'Content-Type: application/json' \
        -d '{\"query\":\"capital of France\",\"documents\":[\"Paris is in France\",\"Berlin is in Germany\"]}' \
        | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d[\"results\"][0].get(\"document\",\"\"))'" 2>/dev/null || true)"
  check "Top reranked doc mentions Paris" bash -c "[[ \"$top\" == *Paris* ]]"
else
  skip_test "Need LXC $AMD_VMID running + LLAMACPP_API_KEY"
fi

# ---------- Group 8: Router auth gate ----------
step "8. Router auth gate"
if lxc_running "$ROUTER_VMID"; then
  if [[ -n "$ROUTER_KEY" ]]; then
    check "Router unauthed → 403" \
      bash -c "code=\$(pct exec $ROUTER_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST -H 'Content-Type: application/json' -d '{\"messages\":[{\"role\":\"user\",\"content\":\"q\"}]}' http://localhost:8000/v1/chat/completions); [ \"\$code\" = '403' ] || [ \"\$code\" = '401' ]"
    check "Router authed → 200" \
      bash -c "code=\$(pct exec $ROUTER_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Authorization: Bearer $ROUTER_KEY' -X POST -H 'Content-Type: application/json' -d '{\"model\":\"rag-qwen3.5\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}' http://localhost:8000/v1/chat/completions); [ \"\$code\" = '200' ]"
  else
    skip_test "ROUTER_API_KEY not present — run 53-lxc-router.sh first"
  fi
else
  skip_test "Router LXC $ROUTER_VMID not running"
fi

# ---------- Group 9: Router /healthz ----------
step "9. Router /healthz"
if lxc_running "$ROUTER_VMID"; then
  check "GET /healthz returns ok=true" \
    bash -c "pct exec $ROUTER_VMID -- timeout 5 curl -fsS http://localhost:8000/healthz | grep -q '\"ok\":true'"
else
  skip_test "Router LXC $ROUTER_VMID not running"
fi

# ---------- Group 10: AnythingLLM API ----------
step "10. AnythingLLM responding"
if lxc_running "$ALLM_VMID"; then
  check "GET / on AnythingLLM port 3001" \
    pct exec "$ALLM_VMID" -- timeout 5 curl -fsSL -o /dev/null http://localhost:3001/
else
  skip_test "AnythingLLM LXC $ALLM_VMID not running"
fi

# ---------- Group 11: MCP SSE endpoints ----------
step "11. MCP SSE endpoints"
if lxc_running "$MCP_VMID"; then
  for port in 3002 3003 3004; do
    check "MCP port $port reachable" \
      pct exec "$MCP_VMID" -- bash -c "timeout 3 curl -fsS http://localhost:$port/sse >/dev/null 2>&1 || \
                                       timeout 3 curl -fsS http://localhost:$port/ >/dev/null 2>&1"
  done
else
  skip_test "MCP LXC $MCP_VMID not running"
fi

# ---------- Group 12: Spec decode acceptance (advisory) ----------
step "12. Spec decode acceptance (advisory)"
if lxc_running "$AMD_VMID"; then
  # Look for acceptance lines in the new chat unit. Pre-Phase-5-expansion clusters
  # used llama-server.service; check both.
  if pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat -u llama-server --no-pager 2>/dev/null \
     | grep -qiE 'accept|spec_decode'; then
    printf "  [%bPASS%b] spec-decode telemetry present in journal\n" "${__LGC_GRN}" "${__LGC_NC}"
    PASS=$((PASS+1))
  else
    skip_test "No spec-decode telemetry yet (fire a chat completion first)"
  fi
fi

# ---------- 13. No-NVIDIA-driver post-pivot sanity ----------
step "13. No NVIDIA driver post-pivot (host or LXC 151)"
check "Host has no /dev/nvidia* devices" bash -c '! ls /dev/nvidia* >/dev/null 2>&1'
if lxc_running "$AMD_VMID"; then
  check "LXC $AMD_VMID has no nvidia-smi" \
    bash -c "! pct exec $AMD_VMID -- command -v nvidia-smi >/dev/null 2>&1"
fi

# ---------- Memory Vault (LXC 156) ----------
MV_VMID="${MEMVAULT_VMID:-156}"
if lxc_exists "$MV_VMID"; then
  step "Verify Memory Vault stack + bridge"
  MV_IP="$(lxc_get_ip "$MV_VMID" || true)"
  # 1. docker compose services up
  pct exec "$MV_VMID" -- bash -lc 'cd /opt/memory-vault && docker compose ps --status running --format "{{.Service}}"' \
    | grep -q app && ok "memory-vault app container up" || warn "memory-vault app not running"
  # 2. dashboard/REST listening
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://${MV_IP}:8000/" || echo 000)"
  [[ "$code" != "000" ]] && ok "REST/dashboard listening (HTTP $code)" || warn "REST not reachable"
  # 3. SSE bridge handshake
  if curl -sN -m 5 "http://${MV_IP}:3005/sse" | head -1 | grep -qi event; then
    ok "MCP-SSE bridge handshake OK on :3005"
  else
    warn "MCP-SSE bridge not responding on :3005"
  fi
  # 4. bridge service active
  pct exec "$MV_VMID" -- systemctl is-active --quiet memory-vault-bridge \
    && ok "memory-vault-bridge.service active" || warn "memory-vault-bridge.service not active"
fi

# ---------- Router Anthropic passthrough (LXC 153) ----------
step "Verify router /v1/messages route is registered"
if curl -s "http://${ROUTER_IP:-192.168.6.153}:8000/openapi.json" | grep -q '/v1/messages'; then
  ok "router exposes /v1/messages"
else
  warn "router /v1/messages not found — redeploy 53-lxc-router.sh"
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
ok "All verifiable checks passed (skipped checks need their LXCs running + keys present)."
