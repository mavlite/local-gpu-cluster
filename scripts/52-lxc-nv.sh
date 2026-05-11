#!/usr/bin/env bash
# 52-lxc-nv.sh — Phase 6 of setup-runbook.md.
#
# Provision LXC 152 (llamacpp-nv):
#   - Create unprivileged LXC with NVIDIA passthrough
#   - Install matching NVIDIA userspace driver (--no-kernel-module)
#   - Install CUDA toolkit
#   - Build llama.cpp with CUDA backend (sm_86 for RTX 3060)
#   - Install embedder (port 8082) and reranker (port 8083) systemd units
#
# Idempotent.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

NV_VMID="${NV_VMID:-152}"
NV_HOSTNAME="${NV_HOSTNAME:-llamacpp-nv}"
NV_CORES="${NV_CORES:-4}"
NV_MEMORY="${NV_MEMORY:-16384}"
NV_SWAP="${NV_SWAP:-4096}"
NV_ROOTFS_SIZE="${NV_ROOTFS_SIZE:-48}"
LXC_STORAGE="${LXC_STORAGE:-local-zfs}"
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"
MODELS_DIR="${MODELS_DIR:-/tank/models}"
NV_CUDA_ARCH="${NV_CUDA_ARCH:-86}"

# ---------- Embedder tuning ----------
# Qwen3-Embedding-0.6B: 1024-dim, supports MRL truncation 32-1024, 32K ctx,
# Q8_0 recommended (~639 MB). --pooling last per model card.
EMBED_HF_REPO="${EMBED_HF_REPO:-Qwen/Qwen3-Embedding-0.6B-GGUF}"
EMBED_HF_QUANT="${EMBED_HF_QUANT:-Q8_0}"
EMBED_CTX="${EMBED_CTX:-32768}"

# ---------- Reranker tuning ----------
RERANK_HF_REPO="${RERANK_HF_REPO:-gpustack/bge-reranker-v2-m3-GGUF}"
RERANK_HF_QUANT="${RERANK_HF_QUANT:-Q4_K_M}"
RERANK_CTX="${RERANK_CTX:-8192}"

# ---------- Fast-chat model (uses spare ~9 GB of 3060) ----------
# Small dense model for low-latency replies on short queries / agent loops.
# Router exposes it via /v1/models so clients can pick it by alias.
# Set FAST_HF_REPO="" to disable the fast-chat unit.
FAST_HF_REPO="${FAST_HF_REPO:-unsloth/Qwen3-4B-Instruct-2507-GGUF}"
FAST_HF_QUANT="${FAST_HF_QUANT:-Q4_K_M}"
FAST_ALIAS="${FAST_ALIAS:-qwen3-4b-fast}"
FAST_CTX="${FAST_CTX:-32768}"
FAST_PARALLEL="${FAST_PARALLEL:-4}"

# ----------------------------------------------------------------------------
# 6.1 — Create LXC
# ----------------------------------------------------------------------------
phase_6_1_create() {
  step "6.1 — Create LXC $NV_VMID ($NV_HOSTNAME)"
  if lxc_exists "$NV_VMID"; then
    skip "LXC $NV_VMID already exists."
    return 0
  fi
  pct create "$NV_VMID" "$LXC_TEMPLATE_STORAGE:vztmpl/$LXC_TEMPLATE_NAME" \
    --hostname "$NV_HOSTNAME" \
    --cores "$NV_CORES" \
    --memory "$NV_MEMORY" \
    --swap "$NV_SWAP" \
    --rootfs "${LXC_STORAGE}:${NV_ROOTFS_SIZE}" \
    --net0 "name=eth0,bridge=${BRIDGE},firewall=0,ip=dhcp,type=veth" \
    --features nesting=1 \
    --unprivileged 1 \
    --ostype ubuntu \
    --start 0
  ok "Created LXC $NV_VMID."
}

# ----------------------------------------------------------------------------
# 6.2 — NVIDIA passthrough
# ----------------------------------------------------------------------------
phase_6_2_passthrough() {
  step "6.2 — Configure NVIDIA passthrough"

  [[ -e /dev/nvidia0 ]] || die "/dev/nvidia0 missing. Install host driver (Phase 4.6) first."

  local nv_gid
  nv_gid="$(stat -c '%g' /dev/nvidia0)"
  log "nvidia GID on host: $nv_gid"

  pct_set_if_changed "$NV_VMID" mp0  "$MODELS_DIR,mp=/opt/models,ro=1"
  pct_set_if_changed "$NV_VMID" dev0 "/dev/nvidia0,gid=$nv_gid"
  pct_set_if_changed "$NV_VMID" dev1 "/dev/nvidiactl,gid=$nv_gid"
  pct_set_if_changed "$NV_VMID" dev2 "/dev/nvidia-uvm,gid=$nv_gid"
  pct_set_if_changed "$NV_VMID" dev3 "/dev/nvidia-uvm-tools,gid=$nv_gid"

  local idx=4
  if [[ -d /dev/nvidia-caps ]]; then
    for cap in /dev/nvidia-caps/nvidia-cap*; do
      [[ -e "$cap" ]] || continue
      pct_set_if_changed "$NV_VMID" "dev$idx" "$cap,gid=$nv_gid"
      idx=$((idx + 1))
    done
  fi
  if [[ -e /dev/nvidia-modeset ]]; then
    pct_set_if_changed "$NV_VMID" "dev$idx" "/dev/nvidia-modeset,gid=$nv_gid"
  fi

  log "Resulting LXC config:"
  pct config "$NV_VMID" | grep -E "^(dev|mp)" || true
}

# ----------------------------------------------------------------------------
# 6.3 — Start LXC, push NVIDIA installer matching host
# ----------------------------------------------------------------------------
phase_6_3_start_and_driver() {
  step "6.3 — Start LXC + install matching NVIDIA driver (userspace only)"
  ensure_lxc_started "$NV_VMID"

  if pct exec "$NV_VMID" -- bash -c 'command -v nvidia-smi >/dev/null 2>&1'; then
    skip "nvidia-smi already installed in LXC."
    return 0
  fi

  local host_ver
  host_ver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)"
  [[ -n "$host_ver" ]] || die "Host nvidia-smi failed — install host driver first (Phase 4.6)."
  log "Host NVIDIA driver version: $host_ver"

  local host_installer="/root/nvidia-installer/NVIDIA-Linux-x86_64-${host_ver}.run"
  if [[ ! -f "$host_installer" ]]; then
    log "Downloading installer for $host_ver"
    mkdir -p /root/nvidia-installer
    wget -O "$host_installer" \
      "https://us.download.nvidia.com/XFree86/Linux-x86_64/${host_ver}/NVIDIA-Linux-x86_64-${host_ver}.run"
    chmod +x "$host_installer"
  fi

  log "Pushing installer into LXC"
  pct push "$NV_VMID" "$host_installer" "/root/NVIDIA-Linux-x86_64-${host_ver}.run" --perms 0755

  pct exec "$NV_VMID" -- env "DRIVER=${host_ver}" bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt update
    apt install -y wget build-essential cmake git curl

    cd /root
    ./NVIDIA-Linux-x86_64-${DRIVER}.run --no-kernel-module --silent --no-questions
    nvidia-smi
GUEST
}

# ----------------------------------------------------------------------------
# 6.5 — CUDA toolkit
# ----------------------------------------------------------------------------
phase_6_5_cuda() {
  step "6.5 — Install CUDA toolkit in LXC"
  if pct exec "$NV_VMID" -- bash -c 'command -v nvcc >/dev/null 2>&1'; then
    skip "CUDA already installed."
    return 0
  fi

  pct exec "$NV_VMID" -- bash -se <<'GUEST'
    set -Eeuo pipefail
    export DEBIAN_FRONTEND=noninteractive
    cd /tmp
    wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O cuda-keyring.deb
    dpkg -i cuda-keyring.deb
    apt update
    apt install -y cuda-toolkit-12-6

    cat > /etc/profile.d/cuda.sh <<EOF
export PATH=/usr/local/cuda-12.6/bin:\$PATH
export LD_LIBRARY_PATH=/usr/local/cuda-12.6/lib64:\$LD_LIBRARY_PATH
EOF
    chmod 0644 /etc/profile.d/cuda.sh
GUEST
}

# ----------------------------------------------------------------------------
# 6.7 — Build llama.cpp with CUDA
# ----------------------------------------------------------------------------
phase_6_7_build() {
  step "6.7 — Build llama.cpp (CUDA, sm_${NV_CUDA_ARCH})"
  if pct exec "$NV_VMID" -- test -x /opt/llama.cpp/build/bin/llama-server; then
    skip "llama-server already built."
    return 0
  fi

  pct exec "$NV_VMID" -- env "ARCH=$NV_CUDA_ARCH" bash -se <<'GUEST'
    set -Eeuo pipefail
    source /etc/profile.d/cuda.sh
    cd /opt
    if [ ! -d llama.cpp ]; then
      git clone https://github.com/ggml-org/llama.cpp.git
    fi
    cd llama.cpp
    git pull --ff-only || true

    cmake -B build \
      -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES="$ARCH" \
      -DCMAKE_BUILD_TYPE=Release \
      -DLLAMA_CURL=ON

    cmake --build build --config Release -j"$(nproc)"
    ./build/bin/llama-server --version
GUEST
}

# ----------------------------------------------------------------------------
# 6.9 — Embedder + reranker systemd units
# ----------------------------------------------------------------------------
phase_6_9_systemd() {
  step "6.9 — Install embedder, reranker, and fast-chat systemd units"

  pct exec "$NV_VMID" -- env \
    "EMBED_HF_REPO=$EMBED_HF_REPO" \
    "EMBED_HF_QUANT=$EMBED_HF_QUANT" \
    "EMBED_CTX=$EMBED_CTX" \
    "RERANK_HF_REPO=$RERANK_HF_REPO" \
    "RERANK_HF_QUANT=$RERANK_HF_QUANT" \
    "RERANK_CTX=$RERANK_CTX" \
    bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/llama-embed.service <<EOF
[Unit]
Description=llama.cpp embedding server (${EMBED_HF_REPO}:${EMBED_HF_QUANT})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
Environment="CUDA_VISIBLE_DEVICES=0"
Environment="LLAMA_CACHE=/opt/models/.cache"
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
    --hf-repo ${EMBED_HF_REPO}:${EMBED_HF_QUANT} \\
    --alias qwen3-embedding \\
    --host 0.0.0.0 \\
    --port 8082 \\
    --embeddings \\
    --pooling last \\
    --ctx-size ${EMBED_CTX} \\
    --n-gpu-layers all \\
    --batch-size 512 \\
    --ubatch-size 512 \\
    --threads 4 \\
    --metrics
Restart=on-failure
RestartSec=10
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/llama-rerank.service <<EOF
[Unit]
Description=llama.cpp reranker (${RERANK_HF_REPO}:${RERANK_HF_QUANT})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
Environment="CUDA_VISIBLE_DEVICES=0"
Environment="LLAMA_CACHE=/opt/models/.cache"
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
    --hf-repo ${RERANK_HF_REPO}:${RERANK_HF_QUANT} \\
    --alias bge-reranker-v2-m3 \\
    --host 0.0.0.0 \\
    --port 8083 \\
    --embeddings \\
    --pooling rank \\
    --reranking \\
    --ctx-size ${RERANK_CTX} \\
    --n-gpu-layers all \\
    --threads 4 \\
    --metrics
Restart=on-failure
RestartSec=10
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF

    mkdir -p /opt/models/.cache
    systemctl daemon-reload
    systemctl enable llama-embed llama-rerank
GUEST

  # Optional fast-chat unit — runs alongside embedder + reranker on the same 3060.
  if [[ -n "$FAST_HF_REPO" ]]; then
    pct exec "$NV_VMID" -- env \
      "FAST_HF_REPO=$FAST_HF_REPO" \
      "FAST_HF_QUANT=$FAST_HF_QUANT" \
      "FAST_ALIAS=$FAST_ALIAS" \
      "FAST_CTX=$FAST_CTX" \
      "FAST_PARALLEL=$FAST_PARALLEL" \
      bash -se <<'GUEST'
      set -Eeuo pipefail
      cat > /etc/systemd/system/llama-chat-fast.service <<EOF
[Unit]
Description=llama.cpp fast-chat (${FAST_HF_REPO}:${FAST_HF_QUANT}) — small low-latency model on RTX 3060
After=network-online.target llama-embed.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
Environment="CUDA_VISIBLE_DEVICES=0"
Environment="LLAMA_CACHE=/opt/models/.cache"
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
    --hf-repo ${FAST_HF_REPO}:${FAST_HF_QUANT} \\
    --alias ${FAST_ALIAS} \\
    --host 0.0.0.0 \\
    --port 8081 \\
    --ctx-size ${FAST_CTX} \\
    --n-gpu-layers all \\
    --threads 4 \\
    --batch-size 2048 \\
    --ubatch-size 512 \\
    --cache-type-k q8_0 \\
    --cache-type-v q8_0 \\
    --cont-batching \\
    --parallel ${FAST_PARALLEL} \\
    --flash-attn auto \\
    --reasoning-format deepseek \\
    --jinja \\
    --metrics
Restart=on-failure
RestartSec=10
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
      systemctl daemon-reload
      systemctl enable llama-chat-fast
GUEST
    ok "Fast-chat unit enabled (port 8081, alias $FAST_ALIAS)."
  else
    skip "FAST_HF_REPO empty — skipping fast-chat unit."
  fi

  warn "Units enabled but not started — first run downloads models from HuggingFace."
  warn "Start: pct exec $NV_VMID -- systemctl start llama-embed llama-rerank${FAST_HF_REPO:+ llama-chat-fast}"
}

main() {
  phase_6_1_create
  phase_6_2_passthrough
  phase_6_3_start_and_driver
  phase_6_5_cuda
  phase_6_7_build
  phase_6_9_systemd

  step "Phase 6 complete."
  local ip; ip="$(lxc_get_ip "$NV_VMID" || true)"
  ok "LXC $NV_VMID ($NV_HOSTNAME) ready at IP: ${ip:-unknown}"
}

main "$@"
