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
#   qwen3.6        Qwen3.6-35B-A3B UD-Q6_K    → alias rag-qwen3.6
#                  RAG / general / research / tool use. The default.
#                  ~29 GB weights — near-lossless precision (Q6_K
#                  captures ~99% of Q8 quality). Pivoted from UD-Q4_K_M
#                  on 2026-05-27; see chat-quant-pivot-q6k memory.
#
#   qwen3.6-fast   Qwen3.6-35B-A3B UD-Q4_K_M  → alias rag-qwen3.6-fast
#                  Throughput-prioritized alternative. ~22 GB weights.
#                  Use when raw t/s matters more than precision (bulk
#                  batch workloads, low-stakes drafting). Replaces the
#                  prior qwen3.6-hi (UD-Q5_K_M) profile — Q6_K being
#                  the new default makes Q5 a strictly-dominated middle
#                  ground; Q4 survives as the genuine speed alternative.
#
#   coder          Qwen3-Coder-Next UD-IQ4_XS → alias qwen3-coder
#                  Coding-specific. 80B / 3B-A MoE; native tool use.
#                  First use downloads ~38 GB (pre-fetch with download-coder.sh).
#                  Architecture: hybrid Gated DeltaNet + MoE (same class as
#                  Qwen3.6-35B-A3B — b9547 fix covers the seq_rm abort).
#                  ROCm note: cache-reuse=0 disables prompt-cache (bug #19908
#                  causes GPU stall on cached-prompt replay with hybrid models).
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
# Returns "REPO QUANT ALIAS TENSOR_SPLIT CTX CACHE_REUSE" on stdout, rc=0;
# rc=1 on unknown name.
#
# TENSOR_SPLIT (comma-separated, no spaces) is profile-specific because the
# embedder pins to GPU 0 and the reranker pins to GPU 1, AND llama.cpp places
# non-split tensors (output layer, embedding table, some buffers) entirely on
# --main-gpu (default 0). For Coder-Next those non-split layers are bigger
# than for Qwen3.6, so a 1,1 split leaves GPU 0 ~10 GB heavier than GPU 1.
# Coder uses 1,1.5 to push more *split* weight onto GPU 1 to compensate.
# Tune empirically: target ~3 GB free on each card per day-2-ops.md § 4.4.
#
# CTX (--ctx-size, total context window pre-allocated as KV cache) is also
# profile-specific. Qwen3.6 with q8_0 KV at 256K uses ~6 GB total, fits with
# headroom on the 1,1 split. Coder-Next is heavier (more non-split layers,
# bigger activation buffers during prefill) and 256K leaves GPU 0 at 98%
# post-prefill — activation scratch isn't freed and the next request OOMs.
# Coder drops to 128K to halve the KV reservation, freeing ~5-7 GB of
# activation headroom. 128K is well above typical coding-session usage.
#
# CACHE_REUSE (--cache-reuse, prompt-prefix reuse window) is per-profile
# because a llama.cpp bug aborts the unit when an exact-match cached prompt
# replays (n_past gets set to len-1 and llama_memory_seq_rm fails with
# "failed to remove sequence ... p1=-1"). Qwen3.6 has not triggered this
# in practice and benefits from prefix reuse on long RAG system prompts.
# Coder-Next does trigger it, so we disable cache-reuse for that profile.
# Set to 0 to disable, >0 to enable with that token window.
get_profile() {
  case "$1" in
    qwen3.6)
      # Default profile, pivoted to UD-Q6_K on 2026-05-27. ~29 GB weights
      # (vs ~22 GB at Q4) — captures ~99% of Q8 precision quality, costs
      # ~3.5 GB of additional weight per card at a 1,1 tensor-split.
      #
      # Tensor-split: 1,1.5 (not 1,1).
      #   At 1,1 the asymmetry from ~10 GB non-split tensor mass on
      #   --main-gpu 0 plus +3.5 GB extra weight share would push GPU 0
      #   peak to ~95% under heavy prefill (Q4_K_M at 1,1 already hit
      #   84% peak / ~5 GB free; +3.5 GB makes it 1.5 GB free — too close
      #   to OOM). 1,1.5 mirrors the empirical tuning the now-retired
      #   qwen3.6-hi profile arrived at for Q5_K_M, which has the same
      #   asymmetry character.
      #
      # The numbers above are predicted. Run scripts/tools/stability-
      # test-coder.sh (or equivalent prefill stress) and update the
      # measurements in day-2-ops.md § 4.4 + get_profile_vram_estimate()
      # below after the first redeploy of this profile.
      echo "unsloth/Qwen3.6-35B-A3B-GGUF UD-Q6_K rag-qwen3.6 1,1.5 262144 1024 q8_0"
      ;;
    qwen3.6-fast)
      # Throughput-prioritized alternative — UD-Q4_K_M, ~22 GB weights.
      # Replaces the prior qwen3.6-hi (UD-Q5_K_M) profile: with Q6_K now
      # the default, Q5 is a strictly-dominated middle ground; Q4 lives
      # on as the genuine speed alternative for bulk-batch or low-stakes
      # drafting workloads where raw t/s matters more than precision.
      #
      # Tensor-split: 1,1 — this is the empirically-validated split for
      # Q4_K_M weights from the 2026-05-26 measurements (peak 84%/52%
      # under 90K prefill, ~5.1 GB free on GPU 0). Don't change to 1,1.5
      # — Q4's smaller weight share keeps the asymmetry tolerable.
      #
      # CACHE_REUSE=1024 is set but llama.cpp emits "cache_reuse is not
      # supported by this context, it will be disabled" at load (Q*_K_M
      # + q8_0 KV behavior). Effective is CACHE_REUSE=0. Harmless.
      echo "unsloth/Qwen3.6-35B-A3B-GGUF UD-Q4_K_M rag-qwen3.6-fast 1,1 262144 1024 q8_0"
      ;;
    coder)
      # Qwen3-Coder-Next UD-IQ4_XS — 80B total / 3B active MoE.
      # UD-IQ4_XS (~38 GB) is the right quant for this hardware: Q4_K_M
      # (~48.5 GB) would push GPU1 to ~97% idle at the 1,1.5 split (OOM
      # under prefill). IQ4_XS is minimal loss for a pure agentic workflow.
      #
      # KV_TYPE=q8_0: at 128K ctx, q8_0 KV costs ~4-5 GB — well within
      # the ~4.9 GB idle headroom. Do NOT use q4_0 here (unlike devstral).
      # If LLAMA_KV_TYPE is q4_0 in config.env from a prior devstral run,
      # this profile overwrites it back to q8_0.
      #
      # CACHE_REUSE=1024: originally set to 0 as workaround for llama.cpp
      # #19908 (ROCm GPU stall on cached DeltaNet prompt replay). Confirmed
      # safe on b9584 — stability test 2026-06-09 showed T3b (99K repeat)
      # completing in 61s vs 157s first-pass (2.56× speedup, sim=1.000,
      # f_keep=0.984). The b9582 regression that caused the stall is fixed.
      echo "unsloth/Qwen3-Coder-Next-GGUF UD-IQ4_XS qwen3-coder 1,1.5 131072 1024 q8_0"
      ;;
    devstral)
      # Devstral Small 2 24B Q8_0 — Mistral-architecture code model.
      # ~25 GB weights (Q8_0 ≈ 1 byte/param × 24B).
      #
      # Tensor split 1,1: Mistral's embedding table is ~500 MB (vs Qwen3.6's
      # ~2.7 GB), so GPU 0 non-split overhead is modest; symmetric split works.
      #
      # n_ctx_train = 393216 (384K) — confirmed from GGUF on first deploy 2026-06-08.
      # CTX 262144 (256K) is well within training range.
      # KV_TYPE=q4_0: required — GPU 0 has ~8.5 GB fixed overhead (ROCm +
      # embedder) leaving ~9.2 GB for KV. q8_0 at 256K needs 11.4 GB (OOM);
      # q4_0 needs ~4.6 GB. This KV_TYPE is DEVSTRAL-SPECIFIC. Swapping from
      # devstral back to any other profile resets KV_TYPE to q8_0 automatically.
      # CACHE_REUSE=0: conservative default for new Mistral architecture.
      echo "unsloth/Devstral-Small-2-24B-Instruct-2512-GGUF Q8_0 devstral 1,1.5 262144 0 q4_0"
      ;;
    devstral-large)
      # Devstral 2 (123B) UD-IQ2_M — full-size Mistral-architecture code model.
      # ~43.5 GB weights. SWE-bench 72.2% vs Small 2's 68.0%.
      #
      # Architecture: 88 layers, 8 KV heads (GQA), 128 head_dim, 256K n_ctx_train,
      # no sliding window. Pure dense transformer — same ROCm code path as devstral.
      # model_type=ministral3.
      #
      # VRAM constraint: GPU 0 carries ~8.5 GB fixed overhead (ROCm + embedder).
      # At 1,1.5 split: GPU 0 → 17.4 GB model + 8.5 = 25.9 GB (4.8 GB free),
      # GPU 1 → 26.1 GB model + 0.4 = 26.5 GB (4.2 GB free).
      # UD-IQ3_XXS (49 GB) was evaluated and rejected: GPU 1 only has 0.9 GB
      # remaining — insufficient for any practical KV cache.
      #
      # CTX 65536 (64K): q4_0 KV at 64K costs 2.95 GB total (88 layers × 8 heads
      # × 128 dim × 0.5 bytes × 65536). GPU 1 share (60%) = 1.77 GB, leaving
      # 2.43 GB for compute buffers. Comfortable but not lavish.
      #
      # KV_TYPE=q4_0: flash attn enabled (Mistral, no DeltaNet), V cache needs FA.
      # Same reasoning as devstral. CACHE_REUSE=0: conservative.
      echo "unsloth/Devstral-2-123B-Instruct-2512-GGUF UD-IQ2_M devstral-large 1,1.5 65536 0 q4_0"
      ;;
    *)
      return 1
      ;;
  esac
}

PROFILE_NAMES=(qwen3.6 qwen3.6-fast coder devstral devstral-large)

# Profile metadata for human display (--status, --help, swap header).
# Kept separate from get_profile so the operational config stays terse.
get_profile_description() {
  case "$1" in
    qwen3.6)      echo "Qwen3.6-35B-A3B UD-Q6_K — RAG / general (default; near-lossless precision)" ;;
    qwen3.6-fast) echo "Qwen3.6-35B-A3B UD-Q4_K_M — throughput-prioritized alternative" ;;
    coder)        echo "Qwen3-Coder-Next 80B/3B-A UD-IQ4_XS — coding-specific (agentic, no thinking mode)" ;;
    devstral)        echo "Devstral Small 2 24B Q8_0 — Mistral-architecture code model (~25 GB)" ;;
    devstral-large)  echo "Devstral 2 123B UD-IQ2_M — full-size Mistral code model (~43.5 GB, 64K ctx)" ;;
    *)               echo "" ;;
  esac
}

# Measured idle VRAM (and under-load peak where stability-tested). Printed
# before each swap so operators know what they're committing to. Update
# when day-2-ops.md § 4.4 table is updated.
get_profile_vram_estimate() {
  case "$1" in
    qwen3.6)      echo "(Q6_K pivot 2026-05-27; predicted idle ~76% / ~63%, peak ~85% / ~73% at 1,1.5 — RE-MEASURE after first deploy and update this string + day-2-ops § 4.4)" ;;
    qwen3.6-fast) echo "idle 75% / 44%; peak 84% / 52% under 90K prefill; ~5.1 GB free GPU 0 at peak; bounded across repeated heavy prefills (validated 2026-05-26 as Q4_K_M default; carries over since profile is the same GGUF)" ;;
    coder)        echo "idle 83% / 77%; peak 83% / 77% (zero drift at 99K ctx + 1600 tok gen — validated 2026-06-09 on b9584; decode 56 t/s@short / 25 t/s@99K; prefill ~1600 t/s; cache-reuse 2.56× speedup on repeat)" ;;
    devstral)        echo "idle 83% / 75%; ~5.1 GB free GPU 0, ~7.4 GB free GPU 1 (256K ctx, q4_0 KV, 1,1.5 split — validated 2026-06-08; peak under heavy prefill not yet measured)" ;;
    devstral-large)  echo "(not yet measured — predicted: ~25.9 GB GPU 0 model+overhead, ~26.5 GB GPU 1 model; 2.2 GB GPU 1 compute headroom at 64K ctx q4_0 KV. MEASURE after first deploy.)" ;;
    *)               echo "(no VRAM data — run stability-test after swap to characterize)" ;;
  esac
}

# ─── helpers ─────────────────────────────────────────────────────────────────

usage() {
  cat <<EOF
Usage: $(basename "$0") [--force] [--follow] {${PROFILE_NAMES[*]/ /|}|--status}

Profiles:
  qwen3.6       — Qwen3.6-35B-A3B UD-Q6_K (RAG / general — default; near-lossless precision)
  qwen3.6-fast  — Qwen3.6-35B-A3B UD-Q4_K_M (throughput-prioritized alternative)
  coder         — Qwen3-Coder-Next UD-IQ4_XS (coding-specific)
  devstral        — Devstral Small 2 24B Q8_0 (Mistral-architecture code model)
  devstral-large  — Devstral 2 123B UD-IQ2_M (full-size, 64K ctx)

Flags:
  --status  Show currently-loaded profile (no changes)
  --force   Re-run even if already on target profile
  --follow  Stream chat-unit journal during the wait loop (suppresses dots).
            Useful when waiting feels stuck — surfaces HF download / mmap /
            ROCm init progress in real-ish time (5s poll).
EOF
  exit "${1:-0}"
}

# Extract the --hf-repo value from the current chat unit. Returns "" if not
# found or the unit doesn't exist yet. Format example:
#   unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q6_K
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

# Match a detected --hf-repo string against the known profiles. Matches on
# the full "repo:quant" pair because multiple profiles can share a repo
# (e.g., qwen3.6 and qwen3.6-fast both use unsloth/Qwen3.6-35B-A3B-GGUF,
# just different quants). Echoes the matching profile name or "unknown".
identify_profile() {
  local hfrepo="$1"  # e.g., "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q5_K_M"
  for name in "${PROFILE_NAMES[@]}"; do
    local profile_repo profile_quant
    read -r profile_repo profile_quant _ <<<"$(get_profile "$name")"
    if [[ "$hfrepo" == "${profile_repo}:${profile_quant}" ]]; then
      echo "$name"
      return 0
    fi
  done
  echo "unknown"
}

# Rewrite the six LLAMA_* keys in config.env atomically. Uses Python so
# special characters in values can never corrupt the file via shell quoting.
write_config_env() {
  local repo="$1" quant="$2" alias="$3" tensor_split="$4" ctx="$5" cache_reuse="$6" kv_type="$7"
  python3 - "$CONFIG_ENV" "$repo" "$quant" "$alias" "$tensor_split" "$ctx" "$cache_reuse" "$kv_type" <<'PY'
import os, sys, tempfile

path, repo, quant, alias, tensor_split, ctx, cache_reuse, kv_type = sys.argv[1:9]
targets = {
    "LLAMA_HF_REPO": repo,
    "LLAMA_HF_QUANT": quant,
    "LLAMA_ALIAS": alias,
    "LLAMA_TENSOR_SPLIT": tensor_split,
    "LLAMA_CTX": ctx,
    "LLAMA_CACHE_REUSE": cache_reuse,
    "LLAMA_KV_TYPE": kv_type,
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
FOLLOW=false
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)   FORCE=true; shift ;;
    --follow)  FOLLOW=true; shift ;;
    --status)  TARGET="--status"; shift ;;
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
  desc=$(get_profile_description "$current_profile")
  [[ -n "$desc" ]] && printf "description:              %s\n" "$desc"
  printf "current --hf-repo:        %s\n" "${current_hfrepo:-<not detected>}"
  printf "llamacpp-chat state:      %s\n" "$systemd_state"
  vram=$(get_profile_vram_estimate "$current_profile")
  [[ -n "$vram" ]] && printf "VRAM characterization:    %s\n" "$vram"
  exit 0
fi

# ─── validate target ─────────────────────────────────────────────────────────

profile_line=$(get_profile "$TARGET") || { echo "unknown profile: $TARGET" >&2; usage 2; }
read -r TARGET_REPO TARGET_QUANT TARGET_ALIAS TARGET_SPLIT TARGET_CTX TARGET_CACHE_REUSE TARGET_KV_TYPE <<<"$profile_line"

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
target_desc=$(get_profile_description "$TARGET")
[[ -n "$target_desc" ]] && echo "  target:        $target_desc"
target_vram=$(get_profile_vram_estimate "$TARGET")
[[ -n "$target_vram" ]] && echo "  expected VRAM: $target_vram"
echo "  updating config.env (LLAMA_HF_REPO=${TARGET_REPO} QUANT=${TARGET_QUANT} ALIAS=${TARGET_ALIAS} TENSOR_SPLIT=${TARGET_SPLIT} CTX=${TARGET_CTX} CACHE_REUSE=${TARGET_CACHE_REUSE} KV_TYPE=${TARGET_KV_TYPE})"

write_config_env "$TARGET_REPO" "$TARGET_QUANT" "$TARGET_ALIAS" "$TARGET_SPLIT" "$TARGET_CTX" "$TARGET_CACHE_REUSE" "$TARGET_KV_TYPE"

echo "  re-running $PROVISION_SCRIPT (idempotent: regenerates unit + reloads systemd)"
"$PROVISION_SCRIPT" >/tmp/swap-chat-model.51-lxc-amd.log 2>&1 \
  || { echo "  ERROR: 51-lxc-amd.sh failed; see /tmp/swap-chat-model.51-lxc-amd.log" >&2; exit 1; }

echo "  restarting llamacpp-chat in LXC $AMD_VMID"

# Always capture a "since" anchor BEFORE the restart. Used by --follow to
# stream live output AND by the post-swap warning scan (--cache_reuse not
# supported, control-looking token, etc.) so operators don't have to enable
# --follow just to see them. Unix epoch ('@SECONDS') is unambiguous regardless
# of LXC timezone.
journal_since="@$(date +%s)"
journal_lines_seen=0

pct exec "$AMD_VMID" -- systemctl daemon-reload
pct exec "$AMD_VMID" -- systemctl restart llamacpp-chat

# Helper: print any chat-unit journal lines added since $journal_since that
# haven't been printed yet (tracked via $journal_lines_seen line count).
print_new_journal() {
  local all new_count
  all=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat --since "$journal_since" --no-pager -o cat 2>/dev/null || true)
  [[ -z "$all" ]] && return 0
  new_count=$(printf '%s\n' "$all" | wc -l)
  if (( new_count > journal_lines_seen )); then
    printf '%s\n' "$all" | tail -n +$((journal_lines_seen + 1)) | sed 's/^/  | /'
    journal_lines_seen=$new_count
  fi
}

# Poll for active. First-time use of a new model triggers a ~38 GB HF
# download inside the unit (chat unit's TimeoutStartSec=1800s = 30 min).
# A warm-cache load is typically 5-15s; allow generous timeout.
if $FOLLOW; then
  echo "  waiting for active — streaming chat-unit journal (5s poll):"
else
  echo -n "  waiting for active "
fi
deadline=$(( $(date +%s) + 1800 ))
while :; do
  state=$(pct exec "$AMD_VMID" -- systemctl is-active llamacpp-chat 2>/dev/null || echo unknown)
  if [[ "$state" == "active" ]]; then
    if $FOLLOW; then
      print_new_journal   # flush any final lines (incl. "server is listening")
      echo "  → active"
    else
      echo " OK"
    fi
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
  if $FOLLOW; then
    print_new_journal
  else
    echo -n "."
  fi
  sleep 5
done

elapsed=$(( $(date +%s) - started ))
new_hfrepo=$(detect_current_hfrepo)

# Surface notable llama.cpp warnings from the load journal so operators see
# things like "cache_reuse is not supported by this context, it will be
# disabled" without needing --follow. Filtered to known-meaningful patterns;
# silent if nothing matches.
notable_warnings=$(pct exec "$AMD_VMID" -- journalctl -u llamacpp-chat \
  --since "$journal_since" --no-pager -o cat 2>/dev/null \
  | grep -iE 'cache_reuse.*not supported|control-looking token|n_ctx_seq.*<.*n_ctx_train|will be disabled' \
  | sort -u || true)

echo
echo "============================================================"
echo " swapped to ${TARGET} in ${elapsed}s"
echo " --hf-repo:       ${new_hfrepo}"
echo " --alias:         ${TARGET_ALIAS}"
echo " --tensor-split:  ${TARGET_SPLIT}"
echo " --ctx-size:      ${TARGET_CTX}"
echo " --cache-reuse:   ${TARGET_CACHE_REUSE}"
echo " --cache-type-k/v ${TARGET_KV_TYPE}"
echo "============================================================"

if [[ -n "$notable_warnings" ]]; then
  echo
  echo "Notable llama.cpp warnings during this load (informational, not errors):"
  echo "$notable_warnings" | sed 's/^/  | /'
fi
echo
echo "Smoke test through the router:"
echo "  ROUTER_KEY=\$(pct exec ${ROUTER_VMID} -- awk -F= '/^ROUTER_API_KEY=/{print \$2}' /etc/router.env)"
echo "  curl -sf -H \"Authorization: Bearer \$ROUTER_KEY\" http://192.168.6.${ROUTER_VMID}:8000/v1/models | jq '.data[].id'"

# Profile-specific post-swap guidance
case "$TARGET" in
  coder)
    echo
    echo "OpenCode config for this profile (~/.opencode/config.json or provider settings):"
    echo "  \"model\":        \"router/qwen3-coder\""
    echo "  \"small_model\":  \"router/qwen3-coder\""
    echo "  \"context\":      131072"
    echo "  \"max_tokens\":   32768"
    echo
    echo "Validated VRAM (2026-06-09, b9584): idle 83%/77%, zero drift at 99K ctx + 1600 tok gen."
    echo "  decode: 56 t/s@short / 25 t/s@99K  |  prefill: ~1600 t/s  |  cache-reuse: 2.56x speedup"
    echo
    echo "Architecture notes:"
    echo "  - No thinking mode (enable_thinking=None in router ALIAS_MAP). Commit-first behavior."
    echo "  - cache-reuse=1024 (confirmed safe on b9584 — ROCm bug #19908 resolved)."
    echo "  - If you hit 'hipErrorInvalidDeviceFunction' in logs, the ROCm kernel wasn't compiled"
    echo "    for gfx908. Workaround: rebuild llama.cpp (51-lxc-amd.sh) or switch to Vulkan backend."
    ;;
  devstral)
    echo
    echo "OpenCode config for this profile:"
    echo "  \"model\":        \"router/devstral\""
    echo "  \"small_model\":  \"router/devstral\""
    echo "  \"context\":      262144"
    echo "  \"max_tokens\":   32768"
    echo "  NOTE: KV_TYPE set to q4_0 (required for devstral at 256K ctx on this hardware)."
    ;;
  devstral-large)
    echo
    echo "OpenCode config for this profile:"
    echo "  \"model\":        \"router/devstral-large\""
    echo "  \"small_model\":  \"router/devstral-large\""
    echo "  \"context\":      65536"
    echo "  \"max_tokens\":   16384"
    echo
    echo "  NOTE: Context is 64K (hardware limit at UD-IQ2_M quant on 64 GB VRAM)."
    echo "  NOTE: KV_TYPE set to q4_0 (same constraint as devstral — flash_attn + Mistral arch)."
    echo "  NOTE: VRAM is tight on GPU 1 (~2.2 GB compute headroom). If OOM, reduce ctx further."
    ;;
  qwen3.6|qwen3.6-fast)
    echo
    echo "OpenCode config for this profile:"
    echo "  \"model\":        \"router/qwen3.6\""
    echo "  \"small_model\":  \"router/qwen3.6\""
    echo "  \"context\":      262144"
    echo "  \"max_tokens\":   32768"
    ;;
esac
