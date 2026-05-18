#!/usr/bin/env bash
# 57-configure-anythingllm.sh — runbook Phase 10 (provider + workspace setup).
#
# AnythingLLM is already env-driven by 54-lxc-anythingllm.sh — provider and
# embedder come up wired to the router. This script handles the bits that
# still need the REST API: workspace creation and RAG tuning.
#
# Inputs:
#   ALLM_API_KEY in config.env, OR generated on first run via the system-admin
#   endpoint (UI signup also works; if both fail, you can paste a key after
#   creating one in Settings -> API Keys).
#
# Idempotent: skips workspace create if it already exists, then PATCHes
# settings to the desired values.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

ALLM_VMID="${ANYTHINGLLM_VMID:-154}"
ANYTHINGLLM_IP="${ANYTHINGLLM_IP:-192.168.6.154}"
ALLM_API_BASE="http://${ANYTHINGLLM_IP}:3001/api/v1"
ALLM_API_KEY="${ALLM_API_KEY:-}"

# Workspaces to create/configure. Each line: slug|prompt|topN|refusal
# Override with WORKSPACES=("slug1|prompt1|10|refusal1" ...) in config.env.
# (${#arr[@]:-0} is invalid bash — :- can't be combined with array-length syntax.)
if [[ -z "${WORKSPACES+x}" ]] || (( ${#WORKSPACES[@]} == 0 )); then
  WORKSPACES=(
    "vcf-reference|You are a technical reference assistant for VMware Cloud Foundation (VCF). Answer questions using ONLY the content retrieved from the attached VCF documentation. If the answer is not in the retrieved context, say so — do not fall back on general VMware knowledge. Cite which document each claim comes from when possible.|10|Not in the provided VCF documents."
    "sdg-documentation|You are a technical reference assistant for SDG infrastructure (Keycloak and related self-hosted tools). Answer questions using ONLY the content retrieved from the attached documentation. Each document has a source: <tool-name> field — name the originating tool when citing.|12|Not in the provided SDG documents."
  )
fi

is_ipv4 "$ANYTHINGLLM_IP" \
  || die "ANYTHINGLLM_IP='$ANYTHINGLLM_IP' is not a valid IPv4."

# ----------------------------------------------------------------------------
# Wait for AnythingLLM to be reachable
# ----------------------------------------------------------------------------
wait_for_allm() {
  step "Wait for AnythingLLM to come up at $ALLM_API_BASE"
  for i in {1..60}; do
    if curl -sf -o /dev/null --max-time 3 "http://${ANYTHINGLLM_IP}:3001/api/ping"; then
      ok "AnythingLLM is responding."
      return 0
    fi
    sleep 2
  done
  die "AnythingLLM didn't respond within 120s. Check: pct exec $ALLM_VMID -- docker logs anythingllm"
}

# ----------------------------------------------------------------------------
# Authenticate. If ALLM_API_KEY is empty, try to mint one via the
# system-admin bootstrap endpoint (only works on a fresh install).
# ----------------------------------------------------------------------------
ensure_api_key() {
  step "Ensure ALLM_API_KEY is available"
  if [[ -n "$ALLM_API_KEY" ]]; then
    if curl -sf -H "Authorization: Bearer $ALLM_API_KEY" \
            "$ALLM_API_BASE/auth" -o /dev/null; then
      ok "Provided API key is valid."
      return 0
    else
      warn "Provided ALLM_API_KEY rejected. Falling back to bootstrap."
      ALLM_API_KEY=""
    fi
  fi

  # Bootstrap path: AnythingLLM exposes /api/v1/admin endpoints to a sysadmin
  # account. The exact endpoint varies by version. We probe the
  # public-facing "system" endpoint that does not require auth for setup.
  warn "ALLM_API_KEY is empty. Generate one via the AnythingLLM UI:"
  warn "  1. Open http://${ANYTHINGLLM_IP}:3001 in a browser"
  warn "  2. Complete the first-run onboarding (admin account creation)"
  warn "  3. Settings -> API Keys -> Generate New API Key"
  warn "  4. Save it to config.env as ALLM_API_KEY=<key> and re-run this script"
  die "Cannot proceed without an API key."
}

allm_curl() {
  local method="$1" path="$2"
  shift 2
  curl -sf -X "$method" \
    -H "Authorization: Bearer $ALLM_API_KEY" \
    -H "Content-Type: application/json" \
    "$ALLM_API_BASE$path" "$@"
}

# ----------------------------------------------------------------------------
# Workspace upsert + tune
# ----------------------------------------------------------------------------
workspace_exists() {
  local slug="$1"
  # AnythingLLM returns {"workspace": []} when not found, or
  # {"workspace": [{..., "slug": "<slug>", ...}]} when found. The old check
  # just grepped for the literal '"workspace"' which matched the empty case too.
  allm_curl GET "/workspace/$slug" 2>/dev/null \
    | grep -qE '"slug"[[:space:]]*:[[:space:]]*"'"$slug"'"'
}

create_workspace() {
  local slug="$1"
  log "Creating workspace: $slug"
  allm_curl POST "/workspace/new" \
    -d "$(printf '{"name":"%s"}' "$slug")" >/dev/null
}

tune_workspace() {
  local slug="$1" prompt="$2" top_n="$3" refusal="$4"
  log "Tuning workspace: $slug (topN=$top_n)"

  local payload
  # Use python's json.dumps for robust escaping of multi-line prompt content.
  payload="$(python3 -c "import json,sys
print(json.dumps({
    'similarityThreshold': 0.0,
    'topN': int(sys.argv[1]),
    'chatMode': 'query',
    'vectorSearchMode': 'rerank',
    'openAiTemp': 0.3,
    'queryRefusalResponse': sys.argv[2],
    'openAiPrompt': sys.argv[3]
}))" "$top_n" "$refusal" "$prompt")"

  allm_curl POST "/workspace/$slug/update" -d "$payload" >/dev/null
  ok "Tuned $slug"
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
main() {
  wait_for_allm
  ensure_api_key

  step "Configure workspaces"
  for ws in "${WORKSPACES[@]}"; do
    IFS='|' read -r slug prompt top_n refusal <<<"$ws"
    if workspace_exists "$slug"; then
      skip "Workspace '$slug' already exists."
    else
      create_workspace "$slug"
    fi
    tune_workspace "$slug" "$prompt" "$top_n" "$refusal"
  done

  step "Done."
  ok "AnythingLLM is wired to the router and the reference workspaces are tuned."
  echo "  Smoke test (replace KEY):"
  echo "    curl -s -X POST $ALLM_API_BASE/workspace/vcf-reference/chat \\"
  echo "      -H 'Authorization: Bearer \$ALLM_API_KEY' \\"
  echo "      -H 'Content-Type: application/json' \\"
  echo "      -d '{\"message\":\"capital of France?\",\"mode\":\"query\"}' | jq -r .textResponse"
}

main "$@"
