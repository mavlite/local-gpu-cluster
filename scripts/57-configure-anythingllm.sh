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

# Retrieval tuning. These match what the live cluster runs -- an earlier version
# of this script shipped topN=10 / similarityThreshold=0.0 and would silently
# have regressed the hand-tuned workspaces on any re-run.
ALLM_SIMILARITY_THRESHOLD="${ALLM_SIMILARITY_THRESHOLD:-0.4}"
ALLM_TEMP="${ALLM_TEMP:-0.3}"

# Canonical system prompts, kept as heredocs because they are multi-line, which
# the slug|prompt|topN|refusal array cannot carry (`read` stops at the first
# newline).
#
# The VCF prompt's Currency footer is load-bearing: the vcf-reference corpus is
# refreshed unevenly (release notes weekly via the vcf-release-notes source;
# everything else is the 2026-05-19 bulk ingest), and without the footer a stale
# answer is indistinguishable from a fresh one. It is phrased as an
# unconditional line on purpose. Two earlier versions failed verification:
# a conditional "add this note when X" never fired on the case it existed for,
# and a two-way branch placed at the END was truncated away on long answers --
# exactly when a reader most needs to know how current the material is. It now
# leads the answer and states one refresh date for the whole corpus, which the
# 2026-08-24 backfill made true. Update the date after any bulk re-ingest.
read -r -d '' VCF_PROMPT <<'VCF_PROMPT_EOF' || true
You are a technical reference assistant for VMware Cloud Foundation (VCF).

# Currency line (MANDATORY, and it goes FIRST)

Begin every substantive answer with exactly this line, then a blank line, then
the answer:

Currency: VCF corpus refreshed 2026-08-24 — release notes weekly, other pages monthly.

It goes at the TOP, not the end. A trailing disclaimer is the first thing lost
when an answer hits the token limit, which is precisely when a reader most
needs to know how current the material is. Omit it only when your entire reply
is the refusal sentence from rule 2.

# Answering rules

1. Answer using ONLY the content retrieved from the attached VCF documentation. Do not invent facts.
2. If the retrieved context does not contain the answer, respond exactly: "Not in the provided VCF documents."
3. **Be comprehensive.** When the retrieved context covers multiple sub-topics, addresses both VCF 9.0 and 9.1 differences, or contains step-by-step procedures, include all relevant material. Prefer structured responses with headings and numbered steps over short summaries.
4. Cite source URLs directly, taken verbatim from the `source:` / `Source:` line in the retrieved chunk. Do not reconstruct a URL from a filename. NEVER include the literal strings "[CONTEXT N]", "[Context 0]", "(Context 0, 1)" or any chunk-number reference.

WRONG: Click Apply to save [CONTEXT 1] [CONTEXT 3].
RIGHT: Click Apply to save (source: https://techdocs.broadcom.com/.../some-page.html).

5. Some chunks carry no source URL — they are the second or later chunk of a
longer page. Cite the URL from a sibling chunk of the same document rather than
omitting the citation or inventing one.
VCF_PROMPT_EOF

read -r -d '' SDG_PROMPT <<'SDG_PROMPT_EOF' || true
You are a technical reference assistant for SDG self-hosted infrastructure (currently: OPNsense firewall/routing, OpenZFS, TrueNAS; future: Keycloak and other tools).

# Answering rules

1. Answer using ONLY the content retrieved from the attached documentation. Do not invent facts.
2. If the retrieved context does not contain the answer, respond exactly: "Not in the provided SDG documents." Do not summarize what topics the context covers; just refuse.
3. Identify which tool a citation belongs to (OPNsense, OpenZFS, TrueNAS, Keycloak, etc.) when relevant.

# Citation format

Cite source URLs directly. NEVER include the literal strings "[CONTEXT N]", "[Context 0]", "(Context 0, 1)" or any chunk-number reference in your answer.
SDG_PROMPT_EOF

# Default prompt for a slug when its WORKSPACES entry leaves the field empty.
default_prompt() {
  case "$1" in
    vcf-reference)     printf '%s' "$VCF_PROMPT" ;;
    sdg-documentation) printf '%s' "$SDG_PROMPT" ;;
    *)                 printf '%s' "" ;;
  esac
}

# Workspaces to create/configure. Each line: slug|prompt|topN|refusal
# An EMPTY prompt field means "use default_prompt for this slug" -- that is how
# the multi-line canonical prompts above get applied. Override via
# WORKSPACES=("slug|inline prompt|10|refusal" ...) in config.env; an inline
# override must be single-line.
# (${#arr[@]:-0} is invalid bash — :- can't be combined with array-length syntax.)
if [[ -z "${WORKSPACES+x}" ]] || (( ${#WORKSPACES[@]} == 0 )); then
  WORKSPACES=(
    "vcf-reference||12|Not in the provided VCF documents."
    "sdg-documentation||12|Not in the provided SDG documents."
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
  # Use python json.dumps for robust escaping (matches the pattern in
  # tune_workspace below). printf "%s" would mangle slugs containing
  # double-quotes, backslashes, or non-ASCII characters.
  local payload
  payload="$(python3 -c "import json,sys; print(json.dumps({'name': sys.argv[1]}))" "$slug")"
  allm_curl POST "/workspace/new" -d "$payload" >/dev/null
}

tune_workspace() {
  local slug="$1" prompt="$2" top_n="$3" refusal="$4"
  log "Tuning workspace: $slug (topN=$top_n)"

  local payload
  # Use python's json.dumps for robust escaping of multi-line prompt content.
  payload="$(python3 -c "import json,sys
print(json.dumps({
    'similarityThreshold': float(sys.argv[4]),
    'topN': int(sys.argv[1]),
    'chatMode': 'query',
    'vectorSearchMode': 'rerank',
    'openAiTemp': float(sys.argv[5]),
    'queryRefusalResponse': sys.argv[2],
    'openAiPrompt': sys.argv[3]
}))" "$top_n" "$refusal" "$prompt" "$ALLM_SIMILARITY_THRESHOLD" "$ALLM_TEMP")"

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
    [[ -z "$prompt" ]] && prompt="$(default_prompt "$slug")"
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
