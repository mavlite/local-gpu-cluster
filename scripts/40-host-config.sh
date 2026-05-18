#!/usr/bin/env bash
# 40-host-config.sh — Phase 4 of setup-runbook.md.
#
# Idempotent host configuration. Run on the Proxmox VE 9.x host as root,
# after Phase 3 (PVE installed, no-subscription repo enabled).
#
# V620-only build: NVIDIA driver install (4.6) and kernel pinning (4.1) removed.
# Sub-steps:
#   4.2  Enable IOMMU + load VFIO modules
#   4.3  Verify IOMMU groups (advisory)
#   4.4  Install AMD firmware
#   4.5  Verify host sees both V620s (advisory)
#   4.7  Identify device major numbers (advisory; AMD render + KFD only)
#   4.8  Create ZFS mirror pool 'tank' across the two data NVMes + datasets
#   4.9  Add ZFS to PVE storage
#   4.10 Download Ubuntu 24.04 LXC template
#
# NOTE: This script may reboot once (IOMMU). After reboot, re-run;
# it will detect completed steps and resume.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

# Defaults
PIN_KERNEL="${PIN_KERNEL:-}"   # V620-only: empty by default (no NVIDIA DKMS dependency)
DATA_NVME_A="${DATA_NVME_A:-}"
DATA_NVME_B="${DATA_NVME_B:-}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"

# ----------------------------------------------------------------------------
# 4.1 — Kernel pinning (V620-only: optional, no NVIDIA dependency).
# ----------------------------------------------------------------------------
phase_4_1_kernel_check() {
  step "4.1 — Kernel check (no pin required for V620-only)"
  local running; running="$(uname -r)"
  log "Running kernel: $running"

  if [[ -z "$PIN_KERNEL" ]]; then
    log "PIN_KERNEL is empty — no pin requested. AMDGPU is in-tree and supports gfx1030 on all PVE 9.x kernels."
    return 0
  fi

  # Optional defensive pin (e.g., to work around an AQC113 10 GbE regression on a newer kernel).
  if [[ "$running" == "$PIN_KERNEL"* ]]; then
    if [[ -f /etc/kernel/proxmox-boot-pin ]] && grep -q "$running" /etc/kernel/proxmox-boot-pin 2>/dev/null; then
      skip "Kernel $running is already pinned."
      return 0
    fi
  fi

  log "Pinning kernel: $PIN_KERNEL (defensive pin, V620-only build does not require this)"
  proxmox-boot-tool kernel pin "$PIN_KERNEL"
  if [[ "$running" != "$PIN_KERNEL"* ]]; then
    warn "Kernel pin set but not running. Reboot then re-run."
    read -r -p "Reboot now? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] && reboot || die "Reboot required."
  fi
  ok "Kernel pinned: $PIN_KERNEL"
}

# ----------------------------------------------------------------------------
# 4.2 — Enable IOMMU + VFIO modules.
# ----------------------------------------------------------------------------
phase_4_2_iommu() {
  step "4.2 — Enable IOMMU + VFIO modules"

  local changed=0
  if ! grep -q "amd_iommu=on" /etc/default/grub; then
    log "Updating /etc/default/grub for amd_iommu=on iommu=pt"
    sed -i 's|GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=on iommu=pt"|' \
      /etc/default/grub
    changed=1
  else
    skip "GRUB cmdline already has amd_iommu=on"
  fi

  for mod in vfio vfio_iommu_type1 vfio_pci; do
    if ! grep -qE "^${mod}$" /etc/modules; then
      log "Adding $mod to /etc/modules"
      echo "$mod" >> /etc/modules
      changed=1
    fi
  done

  if (( changed )); then
    update-grub
    proxmox-boot-tool refresh
    update-initramfs -u -k all
    warn "GRUB / initramfs updated. Reboot required for IOMMU to take effect."
    echo
    read -r -p "Reboot now? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      reboot
    else
      die "Reboot required before continuing."
    fi
  fi

  # Verify IOMMU active
  if dmesg 2>/dev/null | grep -qE "AMD-Vi: Interrupt remapping enabled|Detected AMD IOMMU"; then
    ok "IOMMU is active."
  else
    warn "IOMMU not detected in dmesg. Re-check BIOS settings (see runbook Phase 2)."
  fi
}

# ----------------------------------------------------------------------------
# 4.3 — IOMMU groups sanity check (advisory).
# ----------------------------------------------------------------------------
phase_4_3_iommu_groups() {
  step "4.3 — IOMMU groups (advisory)"
  for d in /sys/kernel/iommu_groups/*/devices/*; do
    n=${d#*/iommu_groups/}; n=${n%%/*}
    printf 'IOMMU Group %s ' "$n"
    lspci -nns "${d##*/}"
  done | sort -V | grep -iE "vga|3d|display controller|radeon|navi|amd.*audio" || true
  ok "Review groups above — each V620 + its audio function should be in its own group."
}

# ----------------------------------------------------------------------------
# 4.4 — AMD firmware.
# ----------------------------------------------------------------------------
phase_4_4_amd_firmware() {
  step "4.4 — AMD firmware + amdgpu autoload"
  # CRITICAL: do NOT install firmware-amd-graphics on Proxmox — it conflicts with
  # pve-firmware and APT will offer to remove proxmox-default-kernel + proxmox-ve.
  # pve-firmware ships with PVE by default and contains all AMD GPU firmware blobs.
  if dpkg -l pve-firmware >/dev/null 2>&1; then
    ok "pve-firmware is installed (provides all AMD GPU firmware blobs)"
  else
    warn "pve-firmware is missing — unusual on PVE. Reinstalling."
    apt install -y pve-firmware
  fi

  # Persist amdgpu autoload. Fresh PVE installs (especially on the 7.0.x kernel)
  # may not autoload amdgpu via udev for class-0380 "Display controller" V620 cards.
  if grep -q "^amdgpu" /etc/modules 2>/dev/null; then
    skip "amdgpu already in /etc/modules"
  else
    echo amdgpu >> /etc/modules
    update-initramfs -u >/dev/null 2>&1 || warn "update-initramfs failed; manual run may be required"
    ok "Added amdgpu to /etc/modules and refreshed initramfs"
  fi

  # Load amdgpu now for the current session (idempotent)
  if ! lsmod | grep -q "^amdgpu"; then
    if modprobe amdgpu 2>/dev/null; then
      ok "amdgpu loaded"
    else
      warn "modprobe amdgpu failed — check dmesg. A reboot is required if a kernel just updated."
    fi
  fi
}

# ----------------------------------------------------------------------------
# 4.5 — Verify host sees all GPUs (advisory).
# ----------------------------------------------------------------------------
phase_4_5_verify_gpus() {
  step "4.5 — Verify V620 GPUs visible to host"
  # V620 = AMD Navi 21 [1002:73a1] class 0380 (Display controller, headless server card).
  # Naive `vga|3d controller` grep misses them — use the device ID instead.
  local v620_count
  v620_count="$(lspci -nn | grep -iEc 'navi 21|radeon pro v620|\[1002:73a1\]')"
  if [[ "$v620_count" -ge 2 ]]; then
    ok "Detected $v620_count V620 entries:"
    lspci -nn | grep -iE 'navi 21|radeon pro v620|\[1002:73a1\]'
  else
    warn "Expected 2 V620 entries, found $v620_count. Full AMD/ATI device list:"
    lspci -nn | grep -iE 'amd|ati' || true
    warn "If V620s are absent: check PCIe 8-pin power, BIOS Above-4G Decoding + Resize BAR + CSM=Disabled, and re-seat cards."
  fi
  echo
  if [[ -d /dev/dri ]]; then
    ls -l /dev/dri/ | grep -E "card|render" || true
  fi
  if command -v nvidia-smi >/dev/null 2>&1; then
    warn "nvidia-smi binary is present on host — V620-only build expects NO NVIDIA driver. Remove with: apt purge -y nvidia-driver firmware-nvidia-graphics"
  fi
}

# ----------------------------------------------------------------------------
# 4.7 — Device majors (advisory; AMD only — V620-only build has no NVIDIA).
# ----------------------------------------------------------------------------
phase_4_7_majors() {
  step "4.7 — Device major numbers (record these)"
  echo "--- AMD render + KFD ---"
  ls -l /dev/dri/render* /dev/kfd 2>/dev/null || warn "No AMD render/KFD nodes yet."
}

# ----------------------------------------------------------------------------
# 4.8 — ZFS pool + datasets.
# ----------------------------------------------------------------------------
phase_4_8_zfs() {
  step "4.8 — Create ZFS mirror pool 'tank' + datasets"

  if zpool list -H tank >/dev/null 2>&1; then
    skip "ZFS pool 'tank' already exists."
  else
    [[ -n "$DATA_NVME_A" && -n "$DATA_NVME_B" ]] || die "DATA_NVME_A and DATA_NVME_B must both be set in config.env (use /dev/disk/by-id/nvme-... paths). ls -l /dev/disk/by-id/ | grep nvme | grep -v part"
    [[ -b "$DATA_NVME_A" || -L "$DATA_NVME_A" ]] || die "$DATA_NVME_A is not a block device or symlink."
    [[ -b "$DATA_NVME_B" || -L "$DATA_NVME_B" ]] || die "$DATA_NVME_B is not a block device or symlink."

    # Safety check: pool create is destructive.
    log "About to create ZFS mirror 'tank' across $DATA_NVME_A + $DATA_NVME_B (DESTRUCTIVE)."
    read -r -p "Type 'create mirror' to confirm: " ans
    [[ "$ans" == "create mirror" ]] || die "Aborted."

    zpool create -o ashift=12 \
        -O compression=lz4 \
        -O atime=off \
        -O xattr=sa \
        tank mirror "$DATA_NVME_A" "$DATA_NVME_B"
    ok "Created mirror pool 'tank' across $DATA_NVME_A + $DATA_NVME_B"
  fi

  for ds in tank/models tank/anythingllm tank/mcp tank/backups; do
    if zfs list -H "$ds" >/dev/null 2>&1; then
      skip "Dataset $ds already exists."
    else
      zfs create "$ds"
      ok "Created $ds"
    fi
  done

  zfs set recordsize=1M tank/models
}

# ----------------------------------------------------------------------------
# 4.9 — Add ZFS to PVE storage.
# ----------------------------------------------------------------------------
phase_4_9_pvesm() {
  step "4.9 — Register tank-backups with PVE storage"
  if pvesm status 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "tank-backups"; then
    skip "PVE storage 'tank-backups' already configured."
  else
    pvesm add dir tank-backups --path /tank/backups --content backup,iso,vztmpl
    ok "Added tank-backups."
  fi
}

# ----------------------------------------------------------------------------
# 4.10 — Ubuntu 24.04 template.
# ----------------------------------------------------------------------------
phase_4_10_template() {
  step "4.10 — Download Ubuntu 24.04 LXC template"
  pveam update >/dev/null
  if pveam list "$LXC_TEMPLATE_STORAGE" 2>/dev/null | grep -q "$LXC_TEMPLATE_NAME"; then
    skip "Template $LXC_TEMPLATE_NAME already present."
  else
    log "Downloading $LXC_TEMPLATE_NAME -> $LXC_TEMPLATE_STORAGE"
    pveam download "$LXC_TEMPLATE_STORAGE" "$LXC_TEMPLATE_NAME"
  fi
}

main() {
  phase_4_1_kernel_check
  phase_4_2_iommu
  phase_4_3_iommu_groups
  phase_4_4_amd_firmware
  phase_4_5_verify_gpus
  phase_4_7_majors
  phase_4_8_zfs
  phase_4_9_pvesm
  phase_4_10_template

  # Disable transparent hugepages (V620-only tuning — THP fragments KV allocation on large models)
  if [[ "$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | grep -oP '\[\K[^]]+')" != "never" ]]; then
    log "Disabling transparent hugepages (THP can fragment KV cache on 22 GB chat model)"
    echo never > /sys/kernel/mm/transparent_hugepage/enabled || warn "Could not disable THP at runtime"
    if ! grep -q "transparent_hugepage=never" /etc/default/grub; then
      sed -i 's|GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"|GRUB_CMDLINE_LINUX_DEFAULT="\1 transparent_hugepage=never"|' /etc/default/grub
      update-grub && proxmox-boot-tool refresh
      warn "GRUB updated to persist THP=never. Reboot to apply at boot."
    fi
  fi

  step "Phase 4 complete."
  ok "Host is ready for V620-only LXC provisioning (Phases 5-9). NVIDIA driver install skipped (V620-only)."
}

main "$@"
