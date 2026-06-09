#!/usr/bin/env bash
# download-devstral-large.sh — Pre-download Devstral-2-123B UD-IQ2_M into LXC 151's
# HuggingFace model cache BEFORE the first `swap-chat-model.sh devstral-large` run.
#
# Why: swap-chat-model.sh restarts llamacpp-chat.service, which downloads the model
# lazily on first start. At ~43.5 GB the download takes 10-50 min depending on
# network; systemd's TimeoutStartSec=1800 gives 30 min before it gives up. Running
# this script first moves the download outside systemd and means the swap completes
# in ~90s (warm-cache start) instead of racing the service timer.
#
# Uses hf_transfer (multi-threaded HF download — up to 8× faster than urllib).
# Idempotent: if the file is already in the HF cache it prints the path and exits.
#
# Usage (from Proxmox host as root):
#   ./scripts/tools/download-devstral-large.sh
#   ./scripts/tools/download-devstral-large.sh --dry-run   # check cache state only
#   HF_TOKEN=hf_... ./scripts/tools/download-devstral-large.sh   # if repo needs auth
#
# After success:
#   ./scripts/swap-chat-model.sh devstral-large --follow

set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host

AMD_VMID="${AMD_VMID:-151}"
HF_REPO="${HF_REPO:-unsloth/Devstral-2-123B-Instruct-2512-GGUF}"
HF_FILENAME="${HF_FILENAME:-Devstral-2-123B-Instruct-2512-UD-IQ2_M.gguf}"
CACHE_DIR="${CACHE_DIR:-/opt/models/.cache}"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--dry-run]"
      echo "  --dry-run  check cache state without downloading"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

log "Checking LXC ${AMD_VMID} for ${HF_REPO}/${HF_FILENAME}"
log "Cache dir: LXC ${AMD_VMID}:${CACHE_DIR}"

# ── check current state ────────────────────────────────────────────────────────

CACHED=$(pct exec "$AMD_VMID" -- python3 - <<PYEOF 2>/dev/null || true
import sys
try:
    from huggingface_hub import try_to_load_from_cache, constants
    result = try_to_load_from_cache("${HF_REPO}", "${HF_FILENAME}", cache_dir="${CACHE_DIR}")
    if result and result != getattr(constants, "HUGGINGFACE_HUB_REVISION_NOT_EXIST", "_NOT_FOUND_ON_HUB"):
        print(result)
except Exception:
    pass
PYEOF
)

if [[ -n "$CACHED" ]]; then
  SIZE=$(pct exec "$AMD_VMID" -- du -sh "$CACHED" 2>/dev/null | cut -f1 || echo "unknown")
  ok "Already cached: ${CACHED} (${SIZE})"
  log "No download needed. Run: ./scripts/swap-chat-model.sh devstral-large --follow"
  exit 0
fi

if $DRY_RUN; then
  log "Not cached. Run without --dry-run to download."
  exit 0
fi

# ── install deps inside LXC if needed ─────────────────────────────────────────

log "Installing huggingface-hub + hf_transfer in LXC ${AMD_VMID} (idempotent)..."
pct exec "$AMD_VMID" -- bash -se <<'GUEST'
  set -Eeuo pipefail
  if ! python3 -c "import huggingface_hub, hf_transfer" 2>/dev/null; then
    pip3 install -q --break-system-packages --ignore-installed huggingface-hub hf_transfer 2>&1 | tail -5
  fi
GUEST

# ── run the download ───────────────────────────────────────────────────────────

log "Downloading ${HF_FILENAME} (~43.5 GB) into LXC ${AMD_VMID}:${CACHE_DIR} ..."
log "hf_transfer enabled — multi-threaded; expect 5-20 min on gigabit LAN."
echo

pct exec "$AMD_VMID" -- env \
  "HF_REPO=${HF_REPO}" \
  "HF_FILENAME=${HF_FILENAME}" \
  "CACHE_DIR=${CACHE_DIR}" \
  ${HF_TOKEN:+"HF_TOKEN=${HF_TOKEN}"} \
  bash -se <<'GUEST'
  set -Eeuo pipefail
  export HF_HUB_ENABLE_HF_TRANSFER=1
  export PATH="/usr/local/bin:$PATH"

  huggingface-cli download "$HF_REPO" "$HF_FILENAME" \
    --cache-dir "$CACHE_DIR" \
    --quiet \
    && echo "huggingface-cli: download complete"
GUEST

echo

# ── verify ─────────────────────────────────────────────────────────────────────

CACHED=$(pct exec "$AMD_VMID" -- python3 - <<PYEOF 2>/dev/null || true
try:
    from huggingface_hub import try_to_load_from_cache, constants
    result = try_to_load_from_cache("${HF_REPO}", "${HF_FILENAME}", cache_dir="${CACHE_DIR}")
    if result and result != getattr(constants, "HUGGINGFACE_HUB_REVISION_NOT_EXIST", "_NOT_FOUND_ON_HUB"):
        print(result)
except Exception:
    pass
PYEOF
)

if [[ -z "$CACHED" ]]; then
  die "Download appeared to succeed but file not found in HF cache at ${CACHE_DIR}. Inspect: pct exec ${AMD_VMID} -- ls -lh ${CACHE_DIR}/models--unsloth--Devstral-2-123B-Instruct-2512-GGUF/"
fi

SIZE=$(pct exec "$AMD_VMID" -- du -sh "$CACHED" 2>/dev/null | cut -f1 || echo "unknown")
ok "${HF_FILENAME} cached: ${CACHED} (${SIZE})"
echo
echo "Next step — swap to devstral-large and follow the load:"
echo "  ./scripts/swap-chat-model.sh devstral-large --follow"
