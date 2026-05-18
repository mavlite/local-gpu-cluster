#!/usr/bin/env bash
# 51-lxc-amd.sh — Phase 5 of setup-runbook.md.
#
# Provision LXC 151 (llamacpp-amd):
#   - Create unprivileged LXC with V620 passthrough (modern dev0: syntax)
#   - Install ROCm (latest, no DKMS — host kernel module already loaded)
#   - Build llama.cpp with HIP backend targeting gfx1030
#   - Install production systemd unit
#   - Install in-LXC V620 temperature publisher (for fan control bridge)
#
# Idempotent: skips create if VMID exists, skips builds if outputs present.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

AMD_VMID="${AMD_VMID:-151}"
AMD_HOSTNAME="${AMD_HOSTNAME:-llamacpp-amd}"
AMD_CORES="${AMD_CORES:-8}"
AMD_MEMORY="${AMD_MEMORY:-32768}"
AMD_SWAP="${AMD_SWAP:-8192}"
AMD_ROOTFS_SIZE="${AMD_ROOTFS_SIZE:-64}"
LXC_STORAGE="${LXC_STORAGE:-local-lvm}"   # V620-only: ext4 + LVM-thin (was local-zfs)
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"
MODELS_DIR="${MODELS_DIR:-/tank/models}"
AMD_GPU_TARGET="${AMD_GPU_TARGET:-gfx1030}"
ROCM_RELEASE="${ROCM_RELEASE:-latest}"

# ---------- llama-server tunables (overridable via config.env) ----------
# V620-only build: 2x V620 = 64 GB VRAM total. Chat unit ~22 GB weights + draft + KV cache.
# Verified model availability on HF (2026-05-17):
#   unsloth/Qwen3.6-35B-A3B-GGUF                Q file: Qwen3.6-35B-A3B-UD-Q4_K_M.gguf (~22 GB)
#   unsloth/Qwen3-0.6B-GGUF                     Q file: Qwen3-0.6B-Q4_K_M.gguf (~400 MB)
# Qwen3.6 reuses Qwen3 tokenizer/vocab → Qwen3-0.6B is a vocab-compatible draft for the 35B target.
LLAMA_HF_REPO="${LLAMA_HF_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
LLAMA_HF_QUANT="${LLAMA_HF_QUANT:-UD-Q4_K_M}"      # Unsloth Dynamic Q4_K_M (slightly better than vanilla at same size)
LLAMA_ALIAS="${LLAMA_ALIAS:-rag-qwen3.6}"          # matches AnythingLLM ALLM_LLM_MODEL
LLAMA_CTX="${LLAMA_CTX:-131072}"                   # 128K; AnythingLLM ALLM_LLM_TOKEN_LIMIT must equal this
LLAMA_KV_TYPE="${LLAMA_KV_TYPE:-q8_0}"             # q8_0 ~10-14GB @ 128K with --parallel 4
LLAMA_PARALLEL="${LLAMA_PARALLEL:-4}"              # 4 concurrent slots (was 2 pre-pivot)
LLAMA_CACHE_REUSE="${LLAMA_CACHE_REUSE:-1024}"     # prompt-prefix reuse window for RAG system prompts
LLAMA_TENSOR_SPLIT="${LLAMA_TENSOR_SPLIT:-1,1}"
LLAMA_THREADS="${LLAMA_THREADS:-8}"
LLAMA_FLASH_ATTN="${LLAMA_FLASH_ATTN:-auto}"        # set to 'off' if §5.10 benchmark shows FA hurts

# Speculative decoding (in-process draft on the V620 chat unit).
# Qwen3.6 small variants don't exist on HF; Qwen3-0.6B is tokenizer-compatible.
LLAMA_DRAFT_REPO="${LLAMA_DRAFT_REPO:-unsloth/Qwen3-0.6B-GGUF}"
LLAMA_DRAFT_QUANT="${LLAMA_DRAFT_QUANT:-Q4_K_M}"
LLAMA_SPEC_NMAX="${LLAMA_SPEC_NMAX:-16}"
LLAMA_SPEC_NMIN="${LLAMA_SPEC_NMIN:-0}"

# ---------- Embedder unit (V620 #1) ----------
EMBED_HF_REPO="${EMBED_HF_REPO:-Qwen/Qwen3-Embedding-0.6B-GGUF}"
EMBED_HF_QUANT="${EMBED_HF_QUANT:-Q8_0}"
EMBED_ALIAS="${EMBED_ALIAS:-qwen3-embed}"
EMBED_CTX="${EMBED_CTX:-8192}"
EMBED_PARALLEL="${EMBED_PARALLEL:-8}"
EMBED_POOLING="${EMBED_POOLING:-last}"   # CRITICAL: Qwen3-Embedding needs 'last', NOT 'cls'

# ---------- Reranker unit (V620 #2) ----------
RERANK_HF_REPO="${RERANK_HF_REPO:-Qwen/Qwen3-Reranker-0.6B-GGUF}"
RERANK_HF_QUANT="${RERANK_HF_QUANT:-Q8_0}"
RERANK_ALIAS="${RERANK_ALIAS:-bge-rerank}"
RERANK_CTX="${RERANK_CTX:-8192}"
RERANK_PARALLEL="${RERANK_PARALLEL:-4}"

# ----------------------------------------------------------------------------
# 5.1 — Create LXC
# ----------------------------------------------------------------------------
phase_5_1_create() {
  step "5.1 — Create LXC $AMD_VMID ($AMD_HOSTNAME)"
  if lxc_exists "$AMD_VMID"; then
    skip "LXC $AMD_VMID already exists."
    return 0
  fi
  pct create "$AMD_VMID" "$LXC_TEMPLATE_STORAGE:vztmpl/$LXC_TEMPLATE_NAME" \
    --hostname "$AMD_HOSTNAME" \
    --cores "$AMD_CORES" \
    --memory "$AMD_MEMORY" \
    --swap "$AMD_SWAP" \
    --rootfs "${LXC_STORAGE}:${AMD_ROOTFS_SIZE}" \
    --net0 "name=eth0,bridge=${BRIDGE},firewall=0,ip=dhcp,type=veth" \
    --features nesting=1 \
    --unprivileged 1 \
    --ostype ubuntu \
    --start 0
  ok "Created LXC $AMD_VMID."
}

# ----------------------------------------------------------------------------
# 5.2 — GPU passthrough via modern dev0: syntax
# ----------------------------------------------------------------------------
phase_5_2_passthrough() {
  step "5.2 — Configure GPU passthrough"

  local render_gid video_gid
  render_gid="$(getent group render | cut -d: -f3 || true)"
  video_gid="$(getent group video  | cut -d: -f3 || true)"
  [[ -n "$render_gid" ]] || die "Host has no 'render' group. Ensure pve-firmware is installed and amdgpu is loaded, then reboot."
  log "render_gid=$render_gid  video_gid=$video_gid"

  # The host has THREE GPUs bound to amdgpu: 2× V620 plus the Ryzen 7600's integrated
  # Raphael iGPU. Each gets a render node under /dev/dri/. We need to pass ONLY the
  # V620 render nodes to LXC 151 — pass-through the iGPU's node would be wrong.
  #
  # Map render nodes → PCI addresses, then keep only the V620 ones (device 1002:73a1).
  local amd_renders=()
  for r in /dev/dri/renderD*; do
    [[ -e "$r" ]] || continue
    local pci_addr device_id
    pci_addr="$(basename "$(readlink -f "/sys/class/drm/$(basename "$r")/device" 2>/dev/null)" 2>/dev/null)"
    [[ -n "$pci_addr" ]] || continue
    device_id="$(lspci -nn -s "$pci_addr" 2>/dev/null | grep -oE '\[1002:73a1\]' || true)"
    if [[ -n "$device_id" ]]; then
      amd_renders+=("$r")
      log "V620 render node: $r → $pci_addr"
    else
      log "Skipping non-V620 render node: $r → $pci_addr"
    fi
  done
  [[ ${#amd_renders[@]} -eq 2 ]] || die "Expected exactly 2 V620 render nodes (PCI ID 1002:73a1); found ${#amd_renders[@]}. Check 'lspci -nn | grep 73a1' and 'ls /dev/dri/' on the host."

  # Use pct_set_if_changed so re-runs don't churn a running LXC.
  # Bind mount is read-WRITE because llama-server uses /opt/models/.cache as the
  # HuggingFace download target (via LLAMA_CACHE env in the chat/embed/rerank units).
  # The model files themselves live on /tank/models on the host's ZFS mirror, so
  # they're redundant + checksummed regardless of how the LXC accesses them.
  pct_set_if_changed "$AMD_VMID" mp0 "$MODELS_DIR,mp=/opt/models"
  pct_set_if_changed "$AMD_VMID" dev0 "/dev/kfd,gid=$render_gid"
  local i=1
  for r in "${amd_renders[@]}"; do
    pct_set_if_changed "$AMD_VMID" "dev$i" "$r,gid=$render_gid"
    i=$((i + 1))
  done

  log "Resulting LXC config (dev/mp lines):"
  pct config "$AMD_VMID" | grep -E "^(dev|mp)" || true
}

# ----------------------------------------------------------------------------
# 5.3 — Start LXC + verify GPU visibility
# ----------------------------------------------------------------------------
phase_5_3_start() {
  step "5.3 — Start LXC and verify GPU visibility"
  ensure_lxc_started "$AMD_VMID"
  log "Devices visible inside LXC:"
  pct exec "$AMD_VMID" -- ls -l /dev/dri/ /dev/kfd || warn "Some devices missing — check /etc/pve/lxc/${AMD_VMID}.conf"
}

# ----------------------------------------------------------------------------
# 5.4 — Install ROCm inside the LXC (no DKMS)
# ----------------------------------------------------------------------------
phase_5_4_rocm() {
  step "5.4 — Install ROCm ($ROCM_RELEASE) in LXC $AMD_VMID"

  if pct exec "$AMD_VMID" -- bash -c 'command -v rocminfo >/dev/null 2>&1'; then
    skip "rocminfo already installed in LXC."
    return 0
  fi

  # Heredoc'd guest script. We pass ROCM_RELEASE via environment for the heredoc.
  pct exec "$AMD_VMID" -- env "ROCM_RELEASE=$ROCM_RELEASE" bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt update
    apt install -y wget gnupg2 build-essential cmake git curl

    # Pull the latest amdgpu-install .deb. The 'latest' URL alias serves
    # the current stable; pin via ROCM_RELEASE for a specific minor (e.g. "6.4").
    cd /tmp
    rm -f amdgpu-install_*.deb
    # repo.radeon.com lists multiple debs; grab the highest version match.
    INDEX="https://repo.radeon.com/amdgpu-install/${ROCM_RELEASE}/ubuntu/noble/"
    DEB_NAME="$(curl -s "$INDEX" \
                  | grep -oE 'amdgpu-install_[0-9.]+-[0-9]+_all\.deb' \
                  | sort -V | tail -1)"
    [ -n "$DEB_NAME" ] || { echo "Could not find amdgpu-install .deb at $INDEX"; exit 1; }
    echo "Downloading $DEB_NAME"
    wget -q "${INDEX}${DEB_NAME}" -O /tmp/amdgpu-install.deb
    apt install -y /tmp/amdgpu-install.deb

    # Install ROCm WITHOUT the kernel module — host already owns the kernel.
    amdgpu-install -y --usecase=rocm --no-dkms

    # Add root to render/video so the GPU device files are accessible.
    usermod -aG render,video root
GUEST

  log "Verifying ROCm sees both V620s"
  pct exec "$AMD_VMID" -- rocminfo | grep -c "Name:.*gfx1030" \
    | grep -q '^2$' \
    || warn "rocminfo did not show TWO gfx1030 agents. Inspect manually: pct exec $AMD_VMID -- rocminfo"
}

# ----------------------------------------------------------------------------
# 5.6 — Build llama.cpp with HIP
# ----------------------------------------------------------------------------
phase_5_6_build() {
  step "5.6 — Build llama.cpp with HIP (target $AMD_GPU_TARGET)"

  if pct exec "$AMD_VMID" -- test -x /opt/llama.cpp/build/bin/llama-server; then
    skip "llama-server binary already present in LXC."
    return 0
  fi

  pct exec "$AMD_VMID" -- env "AMD_GPU_TARGET=$AMD_GPU_TARGET" bash -se <<'GUEST'
    set -Eeuo pipefail
    cd /opt
    if [ ! -d llama.cpp ]; then
      git clone https://github.com/ggml-org/llama.cpp.git
    fi
    cd llama.cpp
    git pull --ff-only || true

    HIPCXX="$(hipconfig -l)/clang" HIP_PATH="$(hipconfig -R)" \
      cmake -S . -B build \
        -DGGML_HIP=ON \
        -DGPU_TARGETS="$AMD_GPU_TARGET" \
        -DCMAKE_BUILD_TYPE=Release \
        -DLLAMA_CURL=ON

    cmake --build build --config Release -j"$(nproc)"

    ./build/bin/llama-server --version
GUEST
}

# ----------------------------------------------------------------------------
# 5.11 — Install production systemd unit
# ----------------------------------------------------------------------------
phase_5_11_1_api_key() {
  step "5.11.1 — Generate LLAMACPP_API_KEY on LXC $AMD_VMID"
  pct exec "$AMD_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    mkdir -p /etc
    if ! grep -q "^LLAMACPP_API_KEY=" /etc/llamacpp.env 2>/dev/null; then
      apt install -y openssl >/dev/null 2>&1 || true
      echo "LLAMACPP_API_KEY=$(openssl rand -hex 32)" >> /etc/llamacpp.env
    fi
    chmod 600 /etc/llamacpp.env
    chown root:root /etc/llamacpp.env
GUEST
  ok "LLAMACPP_API_KEY persisted to /etc/llamacpp.env (mode 600)"
}

phase_5_11_2_warmup() {
  step "5.11.2 — Install warm-chat.sh ExecStartPost helper"
  pct exec "$AMD_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /usr/local/bin/warm-chat.sh <<'EOF'
#!/bin/bash
# Warm-up the chat unit after startup so the first user request doesn't pay cold-start.
sleep 5
. /etc/llamacpp.env
curl -s -m 30 http://localhost:8080/v1/chat/completions \
    -H "Authorization: Bearer ${LLAMACPP_API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"model":"x","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
    > /dev/null
EOF
    chmod +x /usr/local/bin/warm-chat.sh
GUEST
}

phase_5_11_3_chat_unit() {
  step "5.11.3 — Install llamacpp-chat.service (V620 tensor-split)"
  local draft_lines=""
  if [[ -n "$LLAMA_DRAFT_REPO" ]]; then
    draft_lines="    --hf-repo-draft ${LLAMA_DRAFT_REPO}:${LLAMA_DRAFT_QUANT} \\
    --n-gpu-layers-draft all \\
    --spec-draft-n-max ${LLAMA_SPEC_NMAX} \\
    --spec-draft-n-min ${LLAMA_SPEC_NMIN} \\"
  fi

  pct exec "$AMD_VMID" -- env \
    "LLAMA_HF_REPO=$LLAMA_HF_REPO" \
    "LLAMA_HF_QUANT=$LLAMA_HF_QUANT" \
    "LLAMA_ALIAS=$LLAMA_ALIAS" \
    "LLAMA_CTX=$LLAMA_CTX" \
    "LLAMA_KV_TYPE=$LLAMA_KV_TYPE" \
    "LLAMA_PARALLEL=$LLAMA_PARALLEL" \
    "LLAMA_CACHE_REUSE=$LLAMA_CACHE_REUSE" \
    "LLAMA_TENSOR_SPLIT=$LLAMA_TENSOR_SPLIT" \
    "LLAMA_THREADS=$LLAMA_THREADS" \
    "LLAMA_FLASH_ATTN=$LLAMA_FLASH_ATTN" \
    "DRAFT_LINES=$draft_lines" \
    bash -se <<'GUEST'
    set -Eeuo pipefail
    # Remove the pre-pivot unit name if present (clean migration)
    if [ -f /etc/systemd/system/llama-server.service ]; then
      systemctl stop llama-server 2>/dev/null || true
      systemctl disable llama-server 2>/dev/null || true
      rm -f /etc/systemd/system/llama-server.service
    fi

    cat > /etc/systemd/system/llamacpp-chat.service <<EOF
[Unit]
Description=llama.cpp chat (V620 ROCm tensor-split — ${LLAMA_HF_REPO}:${LLAMA_HF_QUANT} + draft)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
EnvironmentFile=/etc/llamacpp.env
Environment="HIP_VISIBLE_DEVICES=0,1"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="HIP_FORCE_DEV_KERNARG=1"
Environment="GPU_MAX_HW_QUEUES=2"
Environment="GGML_HIP_UMA=0"
Environment="LLAMA_CACHE=/opt/models/.cache"
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
    --hf-repo ${LLAMA_HF_REPO}:${LLAMA_HF_QUANT} \\
    --alias ${LLAMA_ALIAS} \\
    --host 0.0.0.0 --port 8080 \\
    --api-key \${LLAMACPP_API_KEY} \\
    --ctx-size ${LLAMA_CTX} \\
    --n-gpu-layers all \\
    --tensor-split ${LLAMA_TENSOR_SPLIT} \\
    --threads ${LLAMA_THREADS} \\
    --batch-size 2048 --ubatch-size 512 \\
    --cache-type-k ${LLAMA_KV_TYPE} --cache-type-v ${LLAMA_KV_TYPE} \\
    --cont-batching \\
    --parallel ${LLAMA_PARALLEL} \\
    --cache-reuse ${LLAMA_CACHE_REUSE} \\
${DRAFT_LINES}
    --flash-attn ${LLAMA_FLASH_ATTN} \\
    --reasoning-format deepseek \\
    --jinja \\
    --mlock \\
    --log-prefix \\
    --metrics
ExecStartPost=/usr/local/bin/warm-chat.sh
Restart=on-failure
RestartSec=10
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
EOF
    mkdir -p /opt/models/.cache
    systemctl daemon-reload
    systemctl enable llamacpp-chat
GUEST
  ok "llamacpp-chat.service installed (enabled, not started — first start downloads model)"
}

phase_5_11_4_embed_unit() {
  step "5.11.4 — Install llamacpp-embed.service (V620 #1, --pooling last)"
  pct exec "$AMD_VMID" -- env \
    "EMBED_HF_REPO=$EMBED_HF_REPO" \
    "EMBED_HF_QUANT=$EMBED_HF_QUANT" \
    "EMBED_ALIAS=$EMBED_ALIAS" \
    "EMBED_CTX=$EMBED_CTX" \
    "EMBED_PARALLEL=$EMBED_PARALLEL" \
    "EMBED_POOLING=$EMBED_POOLING" \
    bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/llamacpp-embed.service <<EOF
[Unit]
Description=llama.cpp embedder (V620 #1 — ${EMBED_HF_REPO}:${EMBED_HF_QUANT}, --pooling ${EMBED_POOLING})
After=network-online.target llamacpp-chat.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
EnvironmentFile=/etc/llamacpp.env
Environment="HIP_VISIBLE_DEVICES=0"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="HIP_FORCE_DEV_KERNARG=1"
Environment="LLAMA_CACHE=/opt/models/.cache"
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
    --hf-repo ${EMBED_HF_REPO}:${EMBED_HF_QUANT} \\
    --alias ${EMBED_ALIAS} \\
    --host 0.0.0.0 --port 8082 \\
    --api-key \${LLAMACPP_API_KEY} \\
    --main-gpu 0 \\
    --n-gpu-layers all \\
    --embeddings \\
    --pooling ${EMBED_POOLING} \\
    --ctx-size ${EMBED_CTX} \\
    --cont-batching \\
    --parallel ${EMBED_PARALLEL} \\
    --batch-size 2048 --ubatch-size 512 \\
    --flash-attn off \\
    --mlock \\
    --metrics
Restart=on-failure
RestartSec=10
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable llamacpp-embed
GUEST
  ok "llamacpp-embed.service installed (enabled, not started)"
}

phase_5_11_5_rerank_unit() {
  step "5.11.5 — Install llamacpp-rerank.service (V620 #2, --reranking)"
  pct exec "$AMD_VMID" -- env \
    "RERANK_HF_REPO=$RERANK_HF_REPO" \
    "RERANK_HF_QUANT=$RERANK_HF_QUANT" \
    "RERANK_ALIAS=$RERANK_ALIAS" \
    "RERANK_CTX=$RERANK_CTX" \
    "RERANK_PARALLEL=$RERANK_PARALLEL" \
    bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/llamacpp-rerank.service <<EOF
[Unit]
Description=llama.cpp reranker (V620 #2 — ${RERANK_HF_REPO}:${RERANK_HF_QUANT}, --reranking)
After=network-online.target llamacpp-chat.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
EnvironmentFile=/etc/llamacpp.env
Environment="HIP_VISIBLE_DEVICES=1"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="HIP_FORCE_DEV_KERNARG=1"
Environment="LLAMA_CACHE=/opt/models/.cache"
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
    --hf-repo ${RERANK_HF_REPO}:${RERANK_HF_QUANT} \\
    --alias ${RERANK_ALIAS} \\
    --host 0.0.0.0 --port 8083 \\
    --api-key \${LLAMACPP_API_KEY} \\
    --main-gpu 1 \\
    --n-gpu-layers all \\
    --embeddings --pooling rank \\
    --reranking \\
    --ctx-size ${RERANK_CTX} \\
    --cont-batching \\
    --parallel ${RERANK_PARALLEL} \\
    --flash-attn off \\
    --mlock \\
    --metrics
Restart=on-failure
RestartSec=10
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable llamacpp-rerank
GUEST
  ok "llamacpp-rerank.service installed (enabled, not started)"
}

phase_5_11_6_journald_ssh() {
  step "5.11.6 — journald retention + SSH hardening"
  pct exec "$AMD_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    mkdir -p /etc/systemd/journald.conf.d
    cat > /etc/systemd/journald.conf.d/retention.conf <<'EOF'
[Journal]
SystemMaxUse=500M
MaxRetentionSec=7day
EOF
    systemctl restart systemd-journald 2>/dev/null || true

    # SSH hardening — disable password + root password login.
    # Operator must push their public key to /root/.ssh/authorized_keys before this lands.
    apt install -y openssh-server >/dev/null 2>&1 || true
    sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
GUEST
}

phase_5_11_systemd() {
  phase_5_11_1_api_key
  phase_5_11_2_warmup
  phase_5_11_3_chat_unit
  phase_5_11_4_embed_unit
  phase_5_11_5_rerank_unit
  phase_5_11_6_journald_ssh
  warn "Three llama-server units installed (enabled, not started). First-start downloads:"
  warn "  - Chat target ~22 GB"
  warn "  - Draft ~600 MB"
  warn "  - Embedder ~1 GB"
  warn "  - Reranker ~1.5 GB"
  warn "Start with: pct exec $AMD_VMID -- systemctl start llamacpp-chat llamacpp-embed llamacpp-rerank"
  warn "Tail logs:  pct exec $AMD_VMID -- journalctl -u llamacpp-chat -f"
}

# ----------------------------------------------------------------------------
# 5.13 (publisher) — Install in-LXC V620 temperature publisher
# (Host fan bridge lives in 56-fan-control.sh; see runbook 5.13.4.)
# ----------------------------------------------------------------------------
phase_5_13_publisher() {
  step "5.13 — Install V620 temperature publisher inside LXC"

  # Set up bind-mount on host (idempotent).
  mkdir -p /var/lib/v620-temps
  chmod 755 /var/lib/v620-temps

  pct_set_if_changed "$AMD_VMID" mp1 "/var/lib/v620-temps,mp=/var/lib/v620-temps"
  ensure_lxc_started "$AMD_VMID"

  pct exec "$AMD_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /usr/local/bin/v620-temp-publish.sh <<'EOF'
#!/bin/bash
# Publish max V620 edge temp every 5 seconds. Read by host fan bridge.
mkdir -p /var/lib/v620-temps
while true; do
    T1=$(rocm-smi -d 0 --showtemp 2>/dev/null | awk '/Temperature.*edge/ {print int($NF); exit}')
    T2=$(rocm-smi -d 1 --showtemp 2>/dev/null | awk '/Temperature.*edge/ {print int($NF); exit}')
    [ -z "$T1" ] && T1=60
    [ -z "$T2" ] && T2=60
    MAX=$(( T1 > T2 ? T1 : T2 ))
    echo "$MAX" > /var/lib/v620-temps/current-temp.tmp
    mv /var/lib/v620-temps/current-temp.tmp /var/lib/v620-temps/current-temp
    sleep 5
done
EOF
    chmod +x /usr/local/bin/v620-temp-publish.sh

    cat > /etc/systemd/system/v620-temp-publish.service <<'EOF'
[Unit]
Description=V620 GPU temperature publisher
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/v620-temp-publish.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now v620-temp-publish.service
GUEST
}

main() {
  phase_5_1_create
  phase_5_2_passthrough
  phase_5_3_start
  phase_5_4_rocm
  phase_5_6_build
  phase_5_11_systemd
  phase_5_13_publisher

  step "Phase 5 complete."
  local ip; ip="$(lxc_get_ip "$AMD_VMID" || true)"
  ok "LXC $AMD_VMID ($AMD_HOSTNAME) ready at IP: ${ip:-unknown}"
  echo "  Start the model server when ready:"
  echo "    pct exec $AMD_VMID -- systemctl start llama-server"
}

main "$@"
