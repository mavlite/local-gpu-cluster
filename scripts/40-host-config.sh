#!/usr/bin/env bash
# 40-host-config.sh — Phase 4 of setup-runbook.md.
#
# Idempotent host configuration. Run on the Proxmox VE 9.x host as root,
# after Phase 3 (PVE installed, no-subscription repo enabled).
#
# Sub-steps:
#   4.1  Pin kernel to 6.14 (PVE 9.1 default is 6.17, which breaks NVIDIA DKMS)
#   4.2  Enable IOMMU + load VFIO modules
#   4.3  Verify IOMMU groups (advisory)
#   4.4  Install AMD firmware
#   4.5  Verify host sees all GPUs (advisory)
#   4.6  Install NVIDIA driver
#   4.7  Identify device major numbers (advisory)
#   4.8  Create ZFS pool + datasets for shared models
#   4.9  Add ZFS to PVE storage
#   4.10 Download Ubuntu 24.04 LXC template
#
# NOTE: This script may reboot twice (kernel pin, then IOMMU). After each
# reboot, re-run; it will detect completed steps and resume.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

# Defaults
PIN_KERNEL="${PIN_KERNEL:-auto}"
NVIDIA_DRIVER_VERSION="${NVIDIA_DRIVER_VERSION:-580.105.06}"
SECONDARY_NVME="${SECONDARY_NVME:-}"
LXC_TEMPLATE_NAME="${LXC_TEMPLATE_NAME:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
LXC_TEMPLATE_STORAGE="${LXC_TEMPLATE_STORAGE:-local}"

# ----------------------------------------------------------------------------
# 4.1 — Pin kernel to 6.14 series.
# ----------------------------------------------------------------------------
phase_4_1_pin_kernel() {
  step "4.1 — Pin kernel to 6.14"
  local running; running="$(uname -r)"
  log "Running kernel: $running"

  if [[ -z "$PIN_KERNEL" ]]; then
    skip "PIN_KERNEL is empty — skipping kernel pin."
    return 0
  fi

  # Already on 6.14 and pinned? proxmox-boot-tool writes the pin to
  # /etc/kernel/proxmox-boot-pin (the authoritative source).
  if [[ "$running" == 6.14.* ]]; then
    if [[ -f /etc/kernel/proxmox-boot-pin ]] && \
       grep -q "$running" /etc/kernel/proxmox-boot-pin 2>/dev/null; then
      skip "Kernel $running is already pinned."
      return 0
    fi
  fi

  apt_install_if_missing proxmox-kernel-6.14 proxmox-headers-6.14

  local pin="$PIN_KERNEL"
  if [[ "$pin" == "auto" ]]; then
    pin="$(proxmox-boot-tool kernel list 2>/dev/null \
            | grep -oE '6\.14\.[0-9]+-[0-9]+-pve' | sort -V | tail -1 || true)"
    [[ -n "$pin" ]] || die "Could not auto-detect a 6.14.x-pve kernel. Install one or set PIN_KERNEL explicitly."
  fi
  log "Pinning kernel: $pin"
  proxmox-boot-tool kernel pin "$pin"

  if [[ "$running" != "$pin" ]]; then
    warn "Kernel pin set to $pin but running $running. Reboot then re-run this script."
    echo
    read -r -p "Reboot now? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      reboot
    else
      die "Reboot required before continuing."
    fi
  fi
  ok "Kernel pinned: $pin"
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
  done | sort -V | grep -iE "vga|3d|audio.*nvidia|amd.*audio" || true
  ok "Review groups above — each GPU + its audio function should be in its own group."
}

# ----------------------------------------------------------------------------
# 4.4 — AMD firmware.
# ----------------------------------------------------------------------------
phase_4_4_amd_firmware() {
  step "4.4 — Install AMD firmware"
  apt_install_if_missing firmware-amd-graphics
}

# ----------------------------------------------------------------------------
# 4.5 — Verify host sees all GPUs (advisory).
# ----------------------------------------------------------------------------
phase_4_5_verify_gpus() {
  step "4.5 — Verify GPUs"
  lspci -nn | grep -iE "vga|3d controller|audio.*nvidia" || warn "No GPU entries found via lspci."
  echo
  if [[ -d /dev/dri ]]; then
    ls -l /dev/dri/ | grep -E "card|render" || true
  fi
}

# ----------------------------------------------------------------------------
# 4.6 — NVIDIA driver (.run installer).
# ----------------------------------------------------------------------------
phase_4_6_nvidia() {
  step "4.6 — Install NVIDIA driver $NVIDIA_DRIVER_VERSION"

  if command -v nvidia-smi >/dev/null 2>&1; then
    local cur; cur="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)"
    if [[ "$cur" == "$NVIDIA_DRIVER_VERSION" ]]; then
      skip "NVIDIA driver $cur already installed and matches target."
      return 0
    elif [[ -n "$cur" ]]; then
      warn "NVIDIA driver $cur is installed, target is $NVIDIA_DRIVER_VERSION. Re-installing."
    fi
  fi

  apt_install_if_missing "proxmox-headers-$(uname -r)" wget pkg-config

  local installer_dir=/root/nvidia-installer
  local installer="$installer_dir/NVIDIA-Linux-x86_64-${NVIDIA_DRIVER_VERSION}.run"
  mkdir -p "$installer_dir"

  if [[ ! -f "$installer" ]]; then
    local url="https://us.download.nvidia.com/XFree86/Linux-x86_64/${NVIDIA_DRIVER_VERSION}/NVIDIA-Linux-x86_64-${NVIDIA_DRIVER_VERSION}.run"
    log "Downloading NVIDIA installer: $url"
    wget -O "$installer" "$url"
    chmod +x "$installer"
  fi

  log "Running NVIDIA installer (silent, DKMS). This rebuilds modules on kernel updates."
  "$installer" --silent --dkms --no-questions

  if ! lsmod | grep -q nvidia; then
    warn "nvidia kernel module not yet loaded. A reboot may be required."
  fi

  log "Verifying nvidia-smi"
  nvidia-smi || warn "nvidia-smi failed — reboot and re-run to verify."

  echo
  log "NVIDIA installer saved at $installer (needed by LXC 152 with the SAME version)."
}

# ----------------------------------------------------------------------------
# 4.7 — Device majors (advisory).
# ----------------------------------------------------------------------------
phase_4_7_majors() {
  step "4.7 — Device major numbers (record these)"
  echo "--- AMD render + KFD ---"
  ls -l /dev/dri/render* /dev/kfd 2>/dev/null || warn "No AMD render/KFD nodes yet."
  echo "--- NVIDIA char devices ---"
  ls -l /dev/nvidia* 2>/dev/null || warn "No NVIDIA char devices yet."
}

# ----------------------------------------------------------------------------
# 4.8 — ZFS pool + datasets.
# ----------------------------------------------------------------------------
phase_4_8_zfs() {
  step "4.8 — Create ZFS pool 'tank' + datasets"

  if zpool list -H tank >/dev/null 2>&1; then
    skip "ZFS pool 'tank' already exists."
  else
    [[ -n "$SECONDARY_NVME" ]] || die "SECONDARY_NVME is empty. Set it in config.env to the device for tank (e.g. /dev/nvme1n1). lsblk -d -o NAME,SIZE,MODEL"
    [[ -b "$SECONDARY_NVME" ]] || die "$SECONDARY_NVME is not a block device."

    # Safety check: pool create is destructive.
    log "About to create ZFS pool 'tank' on $SECONDARY_NVME (DESTRUCTIVE — wipes the device)."
    read -r -p "Type 'create' to confirm: " ans
    [[ "$ans" == "create" ]] || die "Aborted."

    zpool create -o ashift=12 tank "$SECONDARY_NVME"
    ok "Created pool 'tank' on $SECONDARY_NVME"
  fi

  for ds in tank/models tank/anythingllm tank/mcp tank/backups; do
    if zfs list -H "$ds" >/dev/null 2>&1; then
      skip "Dataset $ds already exists."
    else
      zfs create "$ds"
      ok "Created $ds"
    fi
  done

  zfs set compression=lz4 tank/models
  zfs set atime=off tank/models
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
  phase_4_1_pin_kernel
  phase_4_2_iommu
  phase_4_3_iommu_groups
  phase_4_4_amd_firmware
  phase_4_5_verify_gpus
  phase_4_6_nvidia
  phase_4_7_majors
  phase_4_8_zfs
  phase_4_9_pvesm
  phase_4_10_template

  step "Phase 4 complete."
  ok "Host is ready for LXC provisioning (Phases 5-9)."
}

main "$@"
