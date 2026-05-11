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
LXC_STORAGE="${LXC_STORAGE:-local-zfs}"
BRIDGE="${BRIDGE:-vmbr0}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"
MODELS_DIR="${MODELS_DIR:-/tank/models}"
AMD_GPU_TARGET="${AMD_GPU_TARGET:-gfx1030}"
ROCM_RELEASE="${ROCM_RELEASE:-latest}"

# ---------- llama-server tunables (overridable via config.env) ----------
# Hardware: 2x V620 = 64 GB VRAM total. Model ~22 GB + KV cache fits 256K @ q8_0.
LLAMA_HF_REPO="${LLAMA_HF_REPO:-unsloth/Qwen3.6-35B-A3B-GGUF}"
LLAMA_HF_QUANT="${LLAMA_HF_QUANT:-UD-Q4_K_M}"
LLAMA_ALIAS="${LLAMA_ALIAS:-qwen3.6-coder}"
LLAMA_CTX="${LLAMA_CTX:-262144}"             # 256K native; set 131072 for 128K
LLAMA_KV_TYPE="${LLAMA_KV_TYPE:-q8_0}"       # q8_0 ~16GB @ 128K, ~32GB @ 256K. Use q4_0 to halve.
LLAMA_PARALLEL="${LLAMA_PARALLEL:-2}"        # concurrent slots (each gets ctx-size/parallel tokens)
LLAMA_TENSOR_SPLIT="${LLAMA_TENSOR_SPLIT:-1,1}"  # even split across both V620s
LLAMA_THREADS="${LLAMA_THREADS:-8}"

# Speculative decoding: draft model runs in same HIP process as target.
# Qwen3-0.6B shares the Qwen3 tokenizer family — should be vocab-compatible.
# Set LLAMA_DRAFT_REPO="" to disable spec decode.
LLAMA_DRAFT_REPO="${LLAMA_DRAFT_REPO:-unsloth/Qwen3-0.6B-GGUF}"
LLAMA_DRAFT_QUANT="${LLAMA_DRAFT_QUANT:-Q4_K_M}"
LLAMA_SPEC_NMAX="${LLAMA_SPEC_NMAX:-16}"
LLAMA_SPEC_NMIN="${LLAMA_SPEC_NMIN:-0}"

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
  [[ -n "$render_gid" ]] || die "Host has no 'render' group. Install firmware-amd-graphics and reboot."
  log "render_gid=$render_gid  video_gid=$video_gid"

  # Detect render nodes for V620s — runbook says renderD128, renderD129.
  # We pass through whichever first two render nodes are AMD by checking /sys.
  local amd_renders=()
  for r in /dev/dri/renderD128 /dev/dri/renderD129; do
    [[ -e "$r" ]] && amd_renders+=("$r")
  done
  [[ ${#amd_renders[@]} -ge 2 ]] || warn "Expected 2 AMD render nodes; found ${#amd_renders[@]}. Continuing with what we have."

  # Use pct_set_if_changed so re-runs don't churn a running LXC.
  pct_set_if_changed "$AMD_VMID" mp0 "$MODELS_DIR,mp=/opt/models,ro=1"
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
phase_5_11_systemd() {
  step "5.11 — Install llama-server systemd unit"

  # Build the optional draft / spec-decode flag block on the host so we can
  # inject conditional `--hf-repo-draft ...` lines without bash-in-heredoc hell.
  local draft_lines=""
  if [[ -n "$LLAMA_DRAFT_REPO" ]]; then
    draft_lines="    --hf-repo-draft ${LLAMA_DRAFT_REPO}:${LLAMA_DRAFT_QUANT} \\
    --n-gpu-layers-draft all \\
    --spec-draft-n-max ${LLAMA_SPEC_NMAX} \\
    --spec-draft-n-min ${LLAMA_SPEC_NMIN} \\"
  fi

  # Pass tunables through to the heredoc as env vars.
  pct exec "$AMD_VMID" -- env \
    "LLAMA_HF_REPO=$LLAMA_HF_REPO" \
    "LLAMA_HF_QUANT=$LLAMA_HF_QUANT" \
    "LLAMA_ALIAS=$LLAMA_ALIAS" \
    "LLAMA_CTX=$LLAMA_CTX" \
    "LLAMA_KV_TYPE=$LLAMA_KV_TYPE" \
    "LLAMA_PARALLEL=$LLAMA_PARALLEL" \
    "LLAMA_TENSOR_SPLIT=$LLAMA_TENSOR_SPLIT" \
    "LLAMA_THREADS=$LLAMA_THREADS" \
    "DRAFT_LINES=$draft_lines" \
    bash -se <<'GUEST'
    set -Eeuo pipefail
    cat > /etc/systemd/system/llama-server.service <<EOF
[Unit]
Description=llama.cpp server (V620 ROCm) — ${LLAMA_HF_REPO}:${LLAMA_HF_QUANT} @ ${LLAMA_CTX} ctx
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/llama.cpp
# V620 = gfx1030. HSA override pins LLVM target. UMA off forces dedicated VRAM use.
Environment="HIP_VISIBLE_DEVICES=0,1"
Environment="HSA_OVERRIDE_GFX_VERSION=10.3.0"
Environment="GGML_HIP_UMA=0"
Environment="LLAMA_CACHE=/opt/models/.cache"
ExecStart=/opt/llama.cpp/build/bin/llama-server \\
    --hf-repo ${LLAMA_HF_REPO}:${LLAMA_HF_QUANT} \\
    --alias ${LLAMA_ALIAS} \\
    --host 0.0.0.0 \\
    --port 8080 \\
    --ctx-size ${LLAMA_CTX} \\
    --n-gpu-layers all \\
    --tensor-split ${LLAMA_TENSOR_SPLIT} \\
    --threads ${LLAMA_THREADS} \\
    --batch-size 2048 \\
    --ubatch-size 512 \\
    --cache-type-k ${LLAMA_KV_TYPE} \\
    --cache-type-v ${LLAMA_KV_TYPE} \\
    --cont-batching \\
    --parallel ${LLAMA_PARALLEL} \\
${DRAFT_LINES}
    --flash-attn auto \\
    --reasoning-format deepseek \\
    --jinja \\
    --mlock \\
    --metrics
Restart=on-failure
RestartSec=10
# Allow ample time for first-time HF download (~22 GB) and model load.
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
EOF

    mkdir -p /opt/models/.cache
    systemctl daemon-reload
    systemctl enable llama-server
GUEST
  warn "llama-server is enabled but not started — first start downloads ~22 GB."
  warn "Start manually when ready: pct exec $AMD_VMID -- systemctl start llama-server"
  warn "Tail logs:               pct exec $AMD_VMID -- journalctl -u llama-server -f"
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
