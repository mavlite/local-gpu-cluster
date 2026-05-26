#!/usr/bin/env bash
# swap-chat-model.sh — switch between deployed chat-model profiles.
#
# The cluster's 64 GB VRAM pool can hold ONE chat model at a time (see
# day-2-ops.md § 4.4 for the math). This script handles the swap as a
# single atomic operation:
#
#   1. Update LLAMA_HF_REPO + LLAMA_HF_QUANT + LLAMA_ALIAS in
#      scripts/config.env to the target profile's values.
#   2. Re-run scripts/51-lxc-amd.sh (regenerates the systemd unit file
#      inside LXC 151 — idempotent; skips download/build/install work
#      that's already done).
#   3. Restart llamacpp-chat (~5-15s on warm cache; ~7-15 min on first
#      use of a new model due to HF download).
#   4. Wait for the unit to reach active state.
#   5. Verify and report.
#
# Embed (port 8082) and rerank (port 8083) units are NOT touched. Only
# the chat unit on port 8080 swaps.
#
# Profiles (extend by adding to the case in get_profile() below):
#
#   qwen3.6    Qwen3.6-35B-A3B UD-Q4_K_M  → alias rag-qwen3.6
#              RAG / general / research / tool use. The default.
#
#   coder      Qwen3-Coder-Next UD-IQ4_XS → alias qwen3-coder
#              Coding-specific. 80B / 3B-A MoE; native tool use.
#              First use downloads ~38 GB.
#
# Usage:
#   ./swap-chat-model.sh qwen3.6        # switch to RAG/general model
#   ./swap-chat-model.sh coder          # switch to coding model
#   ./swap-chat-model.sh --status       # show what's currently loaded
#   ./swap-chat-model.sh --force coder  # re-run even if already on coder
#
# Run from the Proxmox host as root. Idempotent — re-running with the
# already-loaded profile is a no-op unless --force is passed.

set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
CONFIG_ENV="${LGC_DIR}/config.env"
PROVISION_SCRIPT="${LGC_DIR}/51-lxc-amd.sh"
AMD_VMID="${AMD_VMID:-151}"
ROUTER_VMID="${ROUTER_VMID:-153}"

# ─── profile definitions ─────────────────────────────────────────────────────
# Returns "REPO QUANT ALIAS TENSOR_SPLIT" on stdout, rc=0; rc=1 on unknown name.
#
# TENSOR_SPLIT (comma-separated, no spaces) is profile-specific because the
# embedder pins to GPU 0 and the reranker pins to GPU 1, AND llama.cpp places
# non-split tensors (output layer, embedding table, some buffers) entirely on
# --main-gpu (default 0). For Coder-Next those non-split layers are bigger
# than for Qwen3.6, so a 1,1 split leaves GPU 0 ~10 GB heavier than GPU 1.
# Coder uses 1,1.5 to push more *split* weight onto GPU 1 to compensate.
# Tune empirically: target ~3 GB free on each card per day-2-ops.md § 4.4.
get_profile() {
  case "$1" in
    qwen3.6)
      echo "unsloth/Qwen3.6-35B-A3B-GGUF UD-Q4_K_M rag-qwen3.6 1,1"
      ;;
    coder)
      echo "unsloth/Qwen3-Coder-Next-GGUF UD-IQ4_XS qwen3-coder 1,1.5"
      ;;
    *)
      return 1
      ;;
  esac
}

PROFILE_NAMES=(qwen3.6 coder)

# ─── helpers ─────────────────────────────────────────────────────────────────

usage() {
  cat <<EOF
Usage: $(basename "$0") [--force] {${PROFILE_NAMES[*]/ /|}|--status}

Profiles:
  qwen3.6  — Qwen3.6-35B-A3B UD-Q4_K_M (RAG / general — default)
  coder    — Qwen3-Coder-Next UD-IQ4_XS (coding-specific)

Flags:
  --status  Show currently-loaded profile (no changes)
  --force   Re-run even if already on target profile
EOF
  exit "${1:-0}"
}

# Extract the --hf-repo value from the current chat unit. Returns "" if not
# found or the unit doesn't exist yet. Format example:
#   unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M
detect_current_hfrepo() {
  pct exec "$AMD_VMID" -- bash -c '
    awk "/--hf-repo / {
      for (i=1; i<=NF; i++) {
        if (\$i ~ /[A-Za-z0-9._-]+\\/[A-Za-z0-9._-]+:[A-Za-z0-9_-]+/) {
          gsub(/\"/, \"\", \$i)
          print \$i
          exit
        }
      }
    }" /etc/systemd/system/llamacpp-chat.service
  ' 2>/dev/null || echo ""
}

# Match a detected --hf-repo string against the known profiles.
# Echoes the matching profile name (e.g., "qwen3.6") or "unknown".
identify_profile() {
  local hfrepo="$1"
  local repo="${hfrepo%%:*}"  # strip :QUANT
  for name in "${PROFILE_NAMES[@]}"; do
    local profile_repo
    profile_repo=$(get_profile "$name" | awk '{print $1}')
    if [[ "$repo" == "$profile_repo" ]]; then
      echo "$name"
      return 0
    fi
  done
  echo "unknown"
}

# Rewrite the four LLAMA_* keys in config.env atomically. Uses Python so
# special characters in values can never corrupt the file via shell quoting.
write_config_env() {
  local repo="$1" quant="$2" alias="$3" tensor_split="$4"
  python3 - "$CONFIG_ENV" "$repo" "$quant" "$alias" "$tensor_split" <<'PY'
import os, sys, tempfile

path, repo, quant, alias, tensor_split = sys.argv[1:6]
targets = {
    "LLAMA_HF_REPO": repo,
    "LLAMA_HF_QUANT": quant,
    "LLAMA_ALIAS": alias,
    "LLAMA_TENSOR_SPLIT": tensor_split,
}

try:
    with open(path) as f:
        lines = f.readlines()
except FileNotFoundError:
    lines = []

written = {k: False for k in targets}
out = []
for line in lines:
    stripped = line.lstrip()
    matched = None
    for k in targets:
        # Match both live (KEY=) and commented (# KEY=) lines so we don't
        # leave a stale commented example next to the new live value.
        if stripped.startswith(f"{k}=") or stripped.startswith(f"# {k}=") or stripped.startswith(f"#{k}="):
            matched = k
            break
    if matched:
        if not written[matched]:
            out.append(f"{matched}={targets[matched]}\n")
            written[matched] = True
        # subsequent matches (live + commented) drop out
    else:
        out.append(line)

missing = [k for k in targets if not written[k]]
if missing:
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    out.append("\n# Written by scripts/swap-chat-model.sh\n")
    for k in missing:
        out.append(f"{k}={targets[k]}\n")

d = os.path.dirname(path) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.env.swap.")
try:
    with os.fdopen(fd, "w") as f:
        f.writelines(out)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}

# ─── parse args ──────────────────────────────────────────────────────────────

FORCE=false
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)  FORCE=true; shift ;;
    --status) TARGET="--status"; shift ;;
    -h|--help) usage 0 ;;
    --*)
      echo "unknown flag: $1" >&2; usage 2 ;;
    *)
      [[ -z "$TARGET" ]] || { echo "extra positional arg: $1" >&2; usage 2; }
      TARGET="$1"; shift ;;
  esac
done

[[ -n "$TARGET" ]] || usage 2

# ─── --status branch ─────────────────────────────────────────────────────────

current_hfrepo=$(detect_current_hfrepo)
current_profile=$(identify_profile "$current_hfrepo")

if [[ "$TARGET" == "--status" ]]; then
  systemd_state=$(pct exec "$AMD_VMID" -- systemctl is-active llamacpp-chat 2>/dev/null || echo unknown)
  printf "currently loaded profile: %s\n" "$current_profile"
  printf "current --hf-repo:        %s\n" "${current_hfrepo:-<not detected>}"
  printf "llamacpp-chat state:      %s\n" "$systemd_state"
  exit 0
fi

# ─── validate target ─────────────────────────────────────────────────────────

profile_line=$(get_profile "$TARGET") || { echo "unknown profile: $TARGET" >&2; usage 2; }
read -r TARGET_REPO TARGET_QUANT TARGET_ALIAS TARGET_SPLIT <<<"$profile_line"

# ─── idempotency check ───────────────────────────────────────────────────────

if ! $FORCE && [[ "$current_hfrepo" == "${TARGET_REPO}:${TARGET_QUANT}" ]]; then
  echo "already loaded: $TARGET ($current_hfrepo)"
  echo "use --force to re-run anyway"
  exit 0
fi

# ─── preconditions ───────────────────────────────────────────────────────────

[[ -f "$CONFIG_ENV" ]] || { echo "config.env not found at $CONFIG_ENV" >&2; exit 1; }
[[ -x "$PROVISION_SCRIPT" ]] || { echo "$PROVISION_SCRIPT not executable" >&2; exit 1; }

# ─── do the swap ─────────────────────────────────────────────────────────────

started=$(date +%s)
echo "[swap-chat-model] $(date -u +%FT%TZ) — ${current_profile} → ${TARGET}"
echo "  updating config.env (LLAMA_HF_REPO=${TARGET_REPO} QUANT=${TARGET_QUANT} ALIAS=${TARGET_ALIAS} TENSOR_SPLIT=${TARGET_SPLIT})"

write_config_env "$TARGET_REPO" "$TARGET_QUANT" "$TARGET_ALIAS" "$TARGET_SPLIT"

echo "  re-running $PROVISION_SCRIPT (idempotent: regenerates unit + reloads systemd)"
"$PROVISION_SCRIPT" >/tmp/swap-chat-model.51-lxc-amd.log 2>&1 \
  || { echo "  ERROR: 51-lxc-amd.sh failed; see /tmp/swap-chat-model.51-lxc-amd.log" >&2; exit 1; }

echo "  restarting llamacpp-chat in LXC $AMD_VMID"
pct exec "$AMD_VMID" -- systemctl daemon-reload
pct exec "$AMD_VMID" -- systemctl restart llamacpp-chat

# Poll for active. First-time use of a new model triggers a ~38 GB HF
# download inside the unit (chat unit's TimeoutStartSec=1800s = 30 min).
# A warm-cache load is typically 5-15s; allow generous timeout.
echo -n "  waiting for active "
deadline=$(( $(date +%s) + 1800 ))
while :; do
  state=$(pct exec "$AMD_VMID" -- systemctl is-active llamacpp-chat 2>/dev/null || echo unknown)
  if [[ "$state" == "active" ]]; then
    echo " OK"
    break
  fi
  if [[ "$state" == "failed" ]]; then
    echo
    echo "  ERROR: llamacpp-chat reached failed state" >&2
    echo "  Inspect: pct exec $AMD_VMID -- journalctl -u llamacpp-chat -n 80 --no-pager" >&2
    exit 1
  fi
  if (( $(date +%s) > deadline )); then
    echo
    echo "  ERROR: timeout waiting for llamacpp-chat active (last state=$state)" >&2
    echo "  Inspect: pct exec $AMD_VMID -- journalctl -u llamacpp-chat -n 80 --no-pager" >&2
    exit 1
  fi
  echo -n "."
  sleep 5
done

elapsed=$(( $(date +%s) - started ))
new_hfrepo=$(detect_current_hfrepo)

echo
echo "============================================================"
echo " swapped to ${TARGET} in ${elapsed}s"
echo " --hf-repo:       ${new_hfrepo}"
echo " --alias:         ${TARGET_ALIAS}"
echo " --tensor-split:  ${TARGET_SPLIT}"
echo "============================================================"
echo
echo "Smoke test through the router:"
echo "  ROUTER_KEY=\$(pct exec ${ROUTER_VMID} -- awk -F= '/^ROUTER_API_KEY=/{print \$2}' /etc/router.env)"
echo "  curl -sf -H \"Authorization: Bearer \$ROUTER_KEY\" http://192.168.6.${ROUTER_VMID}:8000/v1/models | jq '.data[].id'"
