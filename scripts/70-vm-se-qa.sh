#!/usr/bin/env bash
# 70-vm-se-qa.sh — create the Space Engineers QA guest (Windows VM) on the PVE host.
#
# WHY IT EXISTS: the SE-DX2.0Code project needs to drive a real Space Engineers
# game client against a Torch dedicated server to finish the Expanse2.Fixes
# v2.3.0 rotor-guard test plan. The remaining test rows cannot be proved
# headlessly on the server alone. Running that client on a daily-driver desktop
# was rejected for two reasons: the QA tooling can execute arbitrary code inside
# the game process, and the whole point of the exercise is to provoke crashes.
# Both belong behind a hypervisor boundary.
#
# WHY A VM AND NOT AN LXC — this is the FIRST qemu guest on this host and a
# deliberate departure from the LXC-for-everything model in AGENTS.md. The
# workload is Windows; a container cannot host it. Everything else stays LXC.
#
# WHY THE GUEST HOLDS BOTH HALVES: the guest runs the Torch dedicated server AND
# the game client. Torch is Windows-native and needs no GPU, the two halves talk
# over the guest's own loopback, and the guest therefore needs no route to
# anything. That is why SEQA_BRIDGE defaults to an isolated bridge with no
# uplink and no gateway: nothing in this guest can reach the LAN, the Proxmox
# host's services, or the VPN to the production fleet. Do not "fix" that by
# moving it to vmbr0.
#
# HOW IT IS DRIVEN WITH NO NETWORK: the QEMU guest agent. `qm guest exec` runs
# commands inside the guest over the virtio serial channel. AGENTS.md's warning
# about nested quoting applies doubly here -- pass scripts in via base64, never
# inline heredocs.
#
# GPU: none by default, and that is not an oversight. The game validates
# graphics adapters at startup and rejects output-less software adapters, so a
# rendered client needs real hardware. We are first testing whether the client's
# built-in null renderer (MyProgram.ForceNullRender, declared but never assigned
# in shipped code) can be set by a Pulsar preloader, which removes the GPU
# requirement entirely. If that fails, set SEQA_HOSTPCI to a passed-through
# device. Do NOT hand it one of the V620s: both are loaded with model weights
# (measured 27.4 GB and 19.2 GB of 30.7 GB on 2026-09-07) and the chat profile
# is split across the pair, so removing either breaks inference.
#
# This script only CREATES and configures the guest. Installing Windows is a
# separate, manual step -- see the "done" block at the end.
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

# VMIDs 151-158 are the LXC estate; 170 keeps the qemu guest visibly separate.
SEQA_VMID="${SEQA_VMID:-170}"
SEQA_NAME="${SEQA_NAME:-se-qa}"
SEQA_CORES="${SEQA_CORES:-6}"
# The host has 128 GB with ~94 GB available (2026-09-07). 16 GB covers a Torch
# server on a small world plus a client in the same guest.
SEQA_MEMORY="${SEQA_MEMORY:-16384}"
# SE is ~85 GB installed, Torch + a world + logs on top, plus Windows itself.
SEQA_DISK_GB="${SEQA_DISK_GB:-200}"
SEQA_STORAGE="${SEQA_STORAGE:-local-lvm}"
SEQA_BRIDGE="${SEQA_BRIDGE:-vmbrseqa}"
SEQA_ISO="${SEQA_ISO:-local:iso/SERVER_EVAL_x64FRE_en-us.iso}"
SEQA_VIRTIO_ISO="${SEQA_VIRTIO_ISO:-local:iso/virtio-win-0.1.266.iso}"
# Empty = no passthrough. Set to e.g. "0000:7e:00.0,pcie=1" only after reading
# the GPU note in the header.
SEQA_HOSTPCI="${SEQA_HOSTPCI:-}"

vm_exists() { qm status "$1" >/dev/null 2>&1; }

step "1 — preflight"
require_cmd qm
require_cmd pvesm
pvesm status | awk 'NR>1 {print $1}' | grep -qx "$SEQA_STORAGE" \
  || die "storage '$SEQA_STORAGE' not found — check 'pvesm status'"
for iso in "$SEQA_ISO" "$SEQA_VIRTIO_ISO"; do
  # local:iso/foo.iso -> /var/lib/vz/template/iso/foo.iso
  path="/var/lib/vz/template/iso/${iso#*iso/}"
  [[ -r "$path" ]] || die "ISO not readable: $path (referenced as $iso)"
done
ok "storage '$SEQA_STORAGE' and both ISOs present"

step "2 — isolated bridge '$SEQA_BRIDGE'"
# An additive stanza with no bridge-ports and no address: a switch with nothing
# plugged into it but the guest. ifreload -a (ifupdown2, which PVE uses) applies
# it without disturbing vmbr0.
if ip link show "$SEQA_BRIDGE" >/dev/null 2>&1; then
  skip "bridge $SEQA_BRIDGE already exists"
elif grep -q "^iface ${SEQA_BRIDGE} " /etc/network/interfaces 2>/dev/null; then
  warn "bridge $SEQA_BRIDGE is configured but not up — run 'ifreload -a'"
else
  cp -a /etc/network/interfaces "/etc/network/interfaces.bak.$(date +%Y%m%d-%H%M%S)"
  cat >>/etc/network/interfaces <<EOF

auto ${SEQA_BRIDGE}
iface ${SEQA_BRIDGE} inet manual
	bridge-ports none
	bridge-stp off
	bridge-fd 0
#   Isolated by design: no uplink, no address, no gateway. The SE QA guest
#   (VMID ${SEQA_VMID}) runs both its Torch server and its game client, so it
#   needs no route off this bridge. Drive it with 'qm guest exec'.
EOF
  ifreload -a || die "ifreload failed — restore /etc/network/interfaces from the .bak just written"
  ok "created isolated bridge $SEQA_BRIDGE"
fi

step "3 — create VM $SEQA_VMID ($SEQA_NAME)"
if vm_exists "$SEQA_VMID"; then
  skip "VM $SEQA_VMID already exists"
else
  # q35 + OVMF: Server 2022 wants UEFI, and q35 is required for any later PCIe
  # passthrough. virtio-scsi-single + iothread is the standard PVE disk choice.
  # ostype win11 covers Server 2022 (same Hyper-V enlightenments).
  qm create "$SEQA_VMID" \
    --name "$SEQA_NAME" \
    --ostype win11 \
    --machine q35 \
    --bios ovmf \
    --efidisk0 "${SEQA_STORAGE}:1,efitype=4m,pre-enrolled-keys=1" \
    --cpu host \
    --cores "$SEQA_CORES" \
    --sockets 1 \
    --memory "$SEQA_MEMORY" \
    --balloon 0 \
    --scsihw virtio-scsi-single \
    --scsi0 "${SEQA_STORAGE}:${SEQA_DISK_GB},iothread=1,discard=on,ssd=1" \
    --net0 "virtio,bridge=${SEQA_BRIDGE},firewall=1" \
    --agent enabled=1 \
    --tablet 0 \
    --onboot 0 \
    || die "qm create failed"
  ok "created VM $SEQA_VMID"
fi

step "4 — install media"
# ide2 = Windows installer, ide0 = virtio driver disc. Windows Setup cannot see
# a virtio-scsi disk without loading the driver from ide0 at the disk-select
# screen (vioscsi\2k22\amd64).
qm set "$SEQA_VMID" --ide2 "${SEQA_ISO},media=cdrom" >/dev/null \
  || die "failed to attach install ISO"
qm set "$SEQA_VMID" --ide0 "${SEQA_VIRTIO_ISO},media=cdrom" >/dev/null \
  || die "failed to attach virtio ISO"
qm set "$SEQA_VMID" --boot "order=ide2;scsi0" >/dev/null \
  || die "failed to set boot order"
ok "install ISO on ide2, virtio drivers on ide0, booting from ide2 first"

step "5 — optional GPU passthrough"
if [[ -z "$SEQA_HOSTPCI" ]]; then
  skip "SEQA_HOSTPCI empty — no GPU attached (see the GPU note in this script's header)"
else
  case "$SEQA_HOSTPCI" in
    *03:00.0*|*07:00.0*)
      die "refusing to pass through a V620 ($SEQA_HOSTPCI) — both carry live model weights for LXC 151" ;;
  esac
  qm set "$SEQA_VMID" --hostpci0 "$SEQA_HOSTPCI" >/dev/null \
    || die "failed to attach hostpci0=$SEQA_HOSTPCI"
  ok "attached hostpci0=$SEQA_HOSTPCI"
fi

step "done"
cat <<EOF

  VM ${SEQA_VMID} (${SEQA_NAME}) is defined but NOT started, and Windows is not
  installed. It is deliberately on isolated bridge '${SEQA_BRIDGE}' with no
  uplink, so it has no LAN, no internet and no path to the production fleet.

  Next steps, in order:

    1. Start it and open the console:
         qm start ${SEQA_VMID}
         # then Console in the PVE web UI (https://192.168.6.175:8006)

    2. Install Windows Server with the *Desktop Experience* option. Server Core
       will not run a game client. At the disk-select screen choose "Load
       driver" and take vioscsi from the ide0 disc (vioscsi\\2k22\\amd64),
       otherwise the 200 GB disk is invisible.

    3. From the same virtio disc install the guest agent
       (guest-agent\\qemu-ga-x86_64.msi) and the NetKVM driver. The agent is
       how this guest is driven -- without it there is no way in, because there
       is no network.

    4. Verify the channel from the host:
         qm guest exec ${SEQA_VMID} -- cmd.exe /c echo ok

  Getting software in with no network: attach an ISO you build on the host, or
  use 'qm guest exec' with base64-encoded payloads. Do NOT reach for a bridge
  change -- the isolation is the feature.

  Undo everything this script did:
    qm stop ${SEQA_VMID} ; qm destroy ${SEQA_VMID} --purge
    # then remove the '${SEQA_BRIDGE}' stanza from /etc/network/interfaces
    # (a timestamped .bak was written before it was added) and 'ifreload -a'
EOF
