#!/bin/sh
# i226-no-aspm.sh -- disable PCIe ASPM L1 on every Intel I226 NIC (8086:125c), matched by ID, not PCI address.
# On 2026-10-13 the host's I226-V (igc) logged "PCIe link lost, device now detached" with ASPM L1 enabled; the host
# stayed up for 6.5 h with no network. Run at boot by i226-no-aspm.service (installed by 40-host-config.sh 4.11).
# SYSFS_PCI_DEVICES overrides the sysfs root for tests (scripts/files/tests/test_i226_no_aspm.py).
base="${SYSFS_PCI_DEVICES:-/sys/bus/pci/devices}"
found=0
for d in "$base"/*; do
  [ "$(cat "$d/vendor" 2>/dev/null)" = "0x8086" ] && [ "$(cat "$d/device" 2>/dev/null)" = "0x125c" ] || continue
  found=1
  if [ -w "$d/link/l1_aspm" ]; then
    echo 0 > "$d/link/l1_aspm" && logger -t i226-no-aspm "$(basename "$d"): l1_aspm=$(cat "$d/link/l1_aspm")"
  else
    logger -t i226-no-aspm "$(basename "$d"): no writable link/l1_aspm"
  fi
done
[ "$found" = 1 ] || logger -t i226-no-aspm "no I226 found"
exit 0
