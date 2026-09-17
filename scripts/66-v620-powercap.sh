#!/usr/bin/env bash
# 66-v620-powercap.sh — boot-time V620 power cap below the VBIOS 250 W floor.
#
# Installs /usr/local/sbin/v620-powercap-apply.sh and a oneshot unit that runs
# it before pve-guests.service starts LXC 151, while the cards are still idle.
# The script writes a soft PowerPlay table (RAM only, reverts on reboot) that
# unlocks power1_cap down to V620_PPT_FLOOR_PCT below 250 W, then sets
# V620_POWER_CAP_W. See the header of files/v620-powercap-apply.sh for the
# byte-level change and its guards.
#
# Also requires GFXOFF disabled on the kernel command line
# (amdgpu.ppfeaturemask=0xfff73fff): pp_table writes that race GFXOFF have
# hard-locked Navi 21 hosts (drm/amd#2060), and Secure Boot lockdown blocks
# the runtime debugfs toggle. Idle cost is ~+5 W per card.
#
# Measured 2026-09-17 (Qwen3.8-27B tensor-split, 6.9k-token prefill loop,
# both cards cooled to 50 C before each cap):
#   cap   pp t/s   tg t/s  junction max (card0/card1)  fan pwm avg
#   250    574      47        101 / 96 C                 255
#   200    550      42*        89 / 85 C                 255
#   180    534      48         86 / 81 C                 232
#   160    513      43*        81 / 77 C                 204
#   (*tg is MTP noise: 34-58 t/s per request at every cap)
# 180 W costs 7% prefill, no generation loss, and keeps the junction ~15 C
# below the 100 C throttle point that 250 W now crosses.

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

AMD_VMID="${AMD_VMID:-151}"
V620_POWER_CAP_W="${V620_POWER_CAP_W:-180}"
V620_PPT_FLOOR_PCT="${V620_PPT_FLOOR_PCT:-40}"
GFXOFF_OFF_MASK="0xfff73fff"   # stock 0xfff7bfff with PP_GFXOFF_MASK (0x8000) cleared

[[ "$V620_POWER_CAP_W" =~ ^[0-9]+$ ]] || die "V620_POWER_CAP_W must be an integer (got '$V620_POWER_CAP_W')"
[[ "$V620_PPT_FLOOR_PCT" =~ ^[0-9]+$ ]] || die "V620_PPT_FLOOR_PCT must be an integer (got '$V620_PPT_FLOOR_PCT')"
(( V620_PPT_FLOOR_PCT >= 7 && V620_PPT_FLOOR_PCT <= 40 )) || die "V620_PPT_FLOOR_PCT must be 7-40"
floor_w=$(( 250 * (100 - V620_PPT_FLOOR_PCT) / 100 ))
(( V620_POWER_CAP_W >= floor_w && V620_POWER_CAP_W <= 250 )) \
  || die "V620_POWER_CAP_W must be ${floor_w}-250 for a ${V620_PPT_FLOOR_PCT}% floor"

APPLY_SRC="$LGC_DIR/files/v620-powercap-apply.sh"
[ -f "$APPLY_SRC" ] || die "Missing $APPLY_SRC"

step "Kernel command line: disable GFXOFF (amdgpu.ppfeaturemask=$GFXOFF_OFF_MASK)"
reboot_needed=0
if grep -q "amdgpu.ppfeaturemask=$GFXOFF_OFF_MASK" /etc/default/grub; then
  skip "GRUB already has amdgpu.ppfeaturemask=$GFXOFF_OFF_MASK"
else
  grep -q "amdgpu.ppfeaturemask=" /etc/default/grub \
    && die "/etc/default/grub sets a different amdgpu.ppfeaturemask; reconcile it by hand"
  cp /etc/default/grub "/etc/default/grub.bak-$(date +%Y%m%d%H%M%S)"
  sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=\"\(.*\)\"|GRUB_CMDLINE_LINUX_DEFAULT=\"\1 amdgpu.ppfeaturemask=$GFXOFF_OFF_MASK\"|" \
    /etc/default/grub
  grep -q "amdgpu.ppfeaturemask=$GFXOFF_OFF_MASK" /etc/default/grub     || die "GRUB edit did not take — check the GRUB_CMDLINE_LINUX_DEFAULT line in /etc/default/grub"
  update-grub
  ok "GRUB updated"
fi
if ! grep -q "amdgpu.ppfeaturemask=$GFXOFF_OFF_MASK" /proc/cmdline; then
  reboot_needed=1
  warn "Running kernel does not have GFXOFF disabled yet — the cap applies after a reboot."
fi

step "Install v620-powercap apply script, config and unit (cap ${V620_POWER_CAP_W} W, floor ${floor_w} W)"
write_file_if_changed /usr/local/sbin/v620-powercap-apply.sh 0755 < "$APPLY_SRC"

write_file_if_changed /etc/default/v620-powercap 0644 <<EOF
# Managed by scripts/66-v620-powercap.sh — edit config.env and re-run instead.
# Runtime change: edit here, then: systemctl restart v620-powercap
# (a cap-only change is safe while the GPUs are busy; a floor change needs a reboot).
V620_POWER_CAP_W=$V620_POWER_CAP_W
V620_PPT_FLOOR_PCT=$V620_PPT_FLOOR_PCT
EOF

write_file_if_changed /etc/systemd/system/v620-powercap.service 0644 <<'EOF'
[Unit]
Description=V620 soft PowerPlay table + power cap (below the 250 W VBIOS floor)
Documentation=file:///usr/local/sbin/v620-powercap-apply.sh
# The pp_table write re-initialises the SMU, so it must happen while the GPUs are
# idle: before pve-guests.service starts LXC 151 and its llama.cpp servers.
After=systemd-modules-load.service local-fs.target pve-cluster.service
Before=pve-guests.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/v620-powercap-apply.sh
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable v620-powercap.service >/dev/null
ok "v620-powercap.service enabled"

if (( reboot_needed )); then
  warn "Reboot to disable GFXOFF; v620-powercap.service applies the cap on that boot."
  exit 0
fi

step "Apply now"
# Idempotent: cards that already carry the soft table only get their cap set.
# A card still on the stock table needs an idle GPU; the script refuses otherwise.
if systemctl restart v620-powercap.service; then
  journalctl -u v620-powercap.service -b --no-pager -n 10 -o cat
  ok "V620 power cap active"
else
  journalctl -u v620-powercap.service -b --no-pager -n 10 -o cat
  warn "Not applied now (see above). It will apply on the next boot, before LXC $AMD_VMID starts."
fi
