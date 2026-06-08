#!/usr/bin/env bash
# bootstrap.sh — orchestrator for the local-gpu-cluster deployment.
#
# Runs idempotent phase scripts in order. Each phase can be invoked
# standalone (./40-host-config.sh) — this just sequences them.
#
# Usage:
#   ./bootstrap.sh                       # run all phases
#   ./bootstrap.sh --from 51             # resume from phase 51 onward
#   ./bootstrap.sh --only 51             # run a single phase
#   ./bootstrap.sh --phases 40,51,52     # run a specific list
#   ./bootstrap.sh --list                # show available phases
#   ./bootstrap.sh --dry-run             # print the plan, don't execute
#
# All phases assume:
#   - You're running as root on a Proxmox VE 9.x host.
#   - Phases 1-3 of setup-runbook.md are complete (hardware, BIOS, PVE installed).
#   - You've copied config.env.example to config.env and reviewed it.

set -Eeuo pipefail

LGC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LGC_DIR

# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

# Phase script registry. The numeric prefix is the "phase number" the user passes.
# V620-only build — Phase 52 (3060 LXC) has been removed in the pivot. The embed and
# rerank services are now provisioned by 51-lxc-amd.sh as additional systemd units
# inside LXC 151 (the V620 LXC).
PHASES=(
  "40:Host configuration (IOMMU, firmware, ZFS mirror, template; no NVIDIA driver, no kernel pin):40-host-config.sh"
  "51:V620 LXC + ROCm + llama.cpp HIP + three systemd units (chat, embed, rerank) + API key + SSH harden:51-lxc-amd.sh"
  "52:Swap-webhook service on host (auto-swap profiles via router; wires SWAP_WEBHOOK_URL into LXC 153):52-swap-webhook.sh"
  "53:Router LXC (FastAPI auth + admission + Prometheus + rate-limit + API key):53-lxc-router.sh"
  "54:AnythingLLM LXC (Docker + compose stack; ALLM_LLM_TOKEN_LIMIT=131072):54-lxc-anythingllm.sh"
  "55:MCP stack LXC (Docker + remote MCP servers, with hardcoded-IP audit):55-lxc-mcp.sh"
  "56:V620 fan-control bridge (host hwmon PWM driven by GPU temp):56-fan-control.sh"
  "57:AnythingLLM workspace tuning via REST API (needs ALLM_API_KEY):57-configure-anythingllm.sh"
  "58:RAG refresh systemd timer + Prometheus textfile metrics:58-rag-refresh-timer.sh"
  "60:Smoke tests / final verification (auth gates, embed dim, rerank, concurrent VRAM, RAG E2E):60-verify.sh"
)

usage() {
  sed -n '2,/^set -E/p' "$0" | sed 's/^# \{0,1\}//; /^set -E/d'
  exit "${1:-0}"
}

list_phases() {
  printf "Available phases:\n\n"
  for entry in "${PHASES[@]}"; do
    IFS=':' read -r num desc script <<<"$entry"
    printf "  %-4s %-65s (%s)\n" "$num" "$desc" "$script"
  done
}

parse_args() {
  FROM="" ONLY="" PHASES_LIST="" DRY=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --from)    FROM="$2"; shift 2 ;;
      --only)    ONLY="$2"; shift 2 ;;
      --phases)  PHASES_LIST="$2"; shift 2 ;;
      --list)    list_phases; exit 0 ;;
      --dry-run) DRY=1; shift ;;
      -h|--help) usage 0 ;;
      *) err "Unknown arg: $1"; usage 1 ;;
    esac
  done
}

selected_phases() {
  local -a out=()
  for entry in "${PHASES[@]}"; do
    IFS=':' read -r num desc script <<<"$entry"
    if [[ -n "$ONLY" ]]; then
      [[ "$num" == "$ONLY" ]] && out+=("$entry")
    elif [[ -n "$PHASES_LIST" ]]; then
      [[ ",${PHASES_LIST}," == *",${num},"* ]] && out+=("$entry")
    elif [[ -n "$FROM" ]]; then
      [[ "$num" -ge "$FROM" ]] && out+=("$entry")
    else
      out+=("$entry")
    fi
  done
  printf '%s\n' "${out[@]}"
}

main() {
  parse_args "$@"
  require_root
  require_pve_host

  step "Plan"
  local -a plan
  mapfile -t plan < <(selected_phases)
  if [[ ${#plan[@]} -eq 0 ]]; then
    die "No phases selected. Try --list."
  fi
  for entry in "${plan[@]}"; do
    IFS=':' read -r num desc script <<<"$entry"
    printf "  - %s  %s\n" "$num" "$desc"
  done

  if [[ "$DRY" -eq 1 ]]; then
    ok "Dry run complete. Re-run without --dry-run to execute."
    exit 0
  fi

  for entry in "${plan[@]}"; do
    IFS=':' read -r num desc script <<<"$entry"
    step "Phase $num — $desc"
    local path="$LGC_DIR/$script"
    [[ -x "$path" ]] || chmod +x "$path"
    "$path"
    ok "Phase $num complete."
  done

  step "All selected phases completed successfully."
}

main "$@"
