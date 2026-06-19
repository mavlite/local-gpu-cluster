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
# V620s identify by PCI device id 1002:73a1 — they enumerate as "Display
# controller", not "VGA"/"3D controller", so the old vga|3d grep counted 0.
# The Ryzen 7600 iGPU adds an extra DRI render node, so don't assume a fixed
# renderD12x layout; rocminfo (group 3) is the authoritative "exactly 2 V620s".
v620_count="$(lspci -nn | grep -c '1002:73a1' || true)"
check "lspci shows 2 V620 GPUs (1002:73a1)"  [ "$v620_count" -eq 2 ]
check "No nvidia-smi binary on host"         bash -c '! command -v nvidia-smi'
rnode_count="$(ls /dev/dri/renderD* 2>/dev/null | wc -l)"
check "DRI render nodes present (>=2)"        [ "$rnode_count" -ge 2 ]

# ---------- Group 2: IOMMU + AMDGPU ----------
step "2. IOMMU active + AMDGPU loaded"
# Check sysfs (persistent) rather than dmesg, which rotates past the IOMMU init
# line on a long-lived host.
check "IOMMU active (sysfs groups present)" \
  bash -c '[ -d /sys/kernel/iommu_groups ] && [ "$(ls /sys/kernel/iommu_groups 2>/dev/null | wc -l)" -gt 0 ]'
check "amdgpu kernel module loaded"   bash -c 'lsmod | grep -q "^amdgpu"'

# ---------- Group 3: ROCm sees both V620s ----------
step "3. ROCm sees both V620s (LXC $AMD_VMID)"
if lxc_exists "$AMD_VMID" && lxc_running "$AMD_VMID"; then
  # Count gfx1030 agent Name lines, excluding the ISA line ("amdgcn-...-gfx1030").
  # Anchoring on $ is fragile (rocminfo pads the value with trailing spaces), so
  # filter out the amdgcn ISA lines instead — leaves one line per GPU agent.
  rocm_count="$(pct exec "$AMD_VMID" -- rocminfo 2>/dev/null \
                | grep gfx1030 | grep -vc amdgcn || true)"
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
  # llama.cpp keeps /health and /v1/models PUBLIC even with --api-key, so testing
  # those for 401 is wrong. Liveness uses /v1/models (200 w/ auth); the gate test
  # hits each port's actual GATED inference route (401 without auth).
  check "151:8080 up (200 /v1/models w/ auth)" \
    bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'Authorization: Bearer $LLAMACPP_KEY' http://localhost:8080/v1/models); [ \"\$code\" = '200' ]"
  check "151:8080 gate works (401 on /v1/chat/completions w/o auth)" \
    bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST -H 'Content-Type: application/json' -d '{\"messages\":[{\"role\":\"user\",\"content\":\"x\"}],\"max_tokens\":1}' http://localhost:8080/v1/chat/completions); [ \"\$code\" = '401' ] || [ \"\$code\" = '403' ]"
  check "151:8082 up (200 /v1/models w/ auth)" \
    bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'Authorization: Bearer $LLAMACPP_KEY' http://localhost:8082/v1/models); [ \"\$code\" = '200' ]"
  check "151:8082 gate works (401 on /v1/embeddings w/o auth)" \
    bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST -H 'Content-Type: application/json' -d '{\"input\":\"x\"}' http://localhost:8082/v1/embeddings); [ \"\$code\" = '401' ] || [ \"\$code\" = '403' ]"
  check "151:8083 up (200 /v1/models w/ auth)" \
    bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -H 'Authorization: Bearer $LLAMACPP_KEY' http://localhost:8083/v1/models); [ \"\$code\" = '200' ]"
  check "151:8083 gate works (401 on /v1/rerank w/o auth)" \
    bash -c "code=\$(pct exec $AMD_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST -H 'Content-Type: application/json' -d '{\"query\":\"x\",\"documents\":[\"y\"]}' http://localhost:8083/v1/rerank); [ \"\$code\" = '401' ] || [ \"\$code\" = '403' ]"
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
  # The rerank response is {"results":[{"index":N,"relevance_score":X},...]} sorted
  # best-first — no "document" field. Map results[0].index back to the input docs.
  top="$(pct exec "$AMD_VMID" -- bash -c "curl -fsS -m 10 -H 'Authorization: Bearer $LLAMACPP_KEY' http://localhost:8083/v1/rerank \
        -H 'Content-Type: application/json' \
        -d '{\"query\":\"capital of France\",\"documents\":[\"Paris is in France\",\"Berlin is in Germany\"]}' \
        | python3 -c 'import json,sys;docs=[\"Paris is in France\",\"Berlin is in Germany\"];d=json.load(sys.stdin);print(docs[d[\"results\"][0][\"index\"]])'" 2>/dev/null || true)"
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
      bash -c "code=\$(pct exec $ROUTER_VMID -- curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Authorization: Bearer $ROUTER_KEY' -X POST -H 'Content-Type: application/json' -d '{\"model\":\"rag-qwen3.6\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1}' http://localhost:8000/v1/chat/completions); [ \"\$code\" = '200' ]"
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

# ---------- Group 11: MCP server (mcp-sdg on :3004) ----------
# v2 consolidated the MCP layer into mcp-sdg.service (:3004). The v1 docker
# servers on :3002/:3003 (anythingllm-search, broadcom-techdocs) are OPTIONAL and
# only checked if their listeners actually exist.
step "11. MCP server (LXC $MCP_VMID): mcp-sdg on :3004"
if lxc_running "$MCP_VMID"; then
  check "mcp-sdg.service active" \
    bash -c "pct exec $MCP_VMID -- systemctl is-active mcp-sdg | grep -q '^active$'"
  check "mcp-sdg :3004 listening" \
    bash -c "pct exec $MCP_VMID -- bash -c 'timeout 3 bash -c \"echo > /dev/tcp/127.0.0.1/3004\" 2>/dev/null'"
  # Non-streaming probe: '/' returns 404 fast (the SSE endpoint is /sse); any HTTP
  # status (not 000) proves it's responding.
  check "mcp-sdg :3004 responds (HTTP, not refused)" \
    bash -c "code=\$(pct exec $MCP_VMID -- curl -s -o /dev/null -m 3 -w '%{http_code}' http://localhost:3004/); [ \"\$code\" != '000' ]"
  for port in 3002 3003; do
    if pct exec "$MCP_VMID" -- bash -c "timeout 2 bash -c 'echo > /dev/tcp/127.0.0.1/$port' 2>/dev/null"; then
      check "optional MCP :$port reachable" true
    else
      skip_test "optional MCP :$port not deployed (v1 docker stack)"
    fi
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
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://${MV_IP}:8000/" 2>/dev/null || true)"
  [[ "$code" =~ ^[2-4][0-9][0-9]$ ]] && ok "REST/dashboard listening (HTTP $code)" || warn "REST not reachable (code=${code:-none})"
  # 3. Streamable HTTP bridge: port listening + /mcp responds (307/400/406 = alive).
  if pct exec "$MV_VMID" -- bash -lc 'timeout 2 bash -c "echo > /dev/tcp/127.0.0.1/3005" 2>/dev/null'; then
    ok "MCP bridge listening on :3005"
  else
    warn "MCP bridge not listening on :3005"
  fi
  mcode="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://${MV_IP}:3005/mcp" 2>/dev/null || true)"
  [[ "$mcode" =~ ^[234][0-9][0-9]$ ]] && ok "MCP bridge /mcp responds (HTTP $mcode)" \
    || warn "MCP bridge /mcp not responding (code=${mcode:-none})"
  # 4. bridge service active
  pct exec "$MV_VMID" -- systemctl is-active --quiet memory-vault-bridge \
    && ok "memory-vault-bridge.service active" || warn "memory-vault-bridge.service not active"
fi

# ---------- Router Anthropic passthrough (LXC 153) ----------
step "Verify router /v1/messages route is registered"
# The router bearer-auths everything except /healthz, so fetch its key from LXC 153.
_RK="$(pct exec "${ROUTER_VMID:-153}" -- awk -F= '/^ROUTER_API_KEY=/{print $2}' /etc/router.env 2>/dev/null || true)"
if curl -s -H "Authorization: Bearer ${_RK}" "http://${ROUTER_IP:-192.168.6.153}:8000/openapi.json" | grep -q '/v1/messages'; then
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
