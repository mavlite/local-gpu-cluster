#!/usr/bin/env bash
# 72-vm-tester.sh — create the external tester guest (Debian 13.5, VMID 172).
#
# WHY IT EXISTS: an external tester (a person, and later an autonomous agent)
# needs a machine to install software on and orchestrate workloads in. They get
# SSH and root on that machine and nothing else. See
# docs/tester-vm-requirements.md for the threat model and the two constraints
# that produced this design.
#
# WHY sdxguest AND NOT vmbr0: a guest on vmbr0 is L2-adjacent to this host and
# every inference container, so its isolation is only a firewall rule. This
# guest lives on the SDN vnet instead and has no interface on 192.168.6.0/24 at
# all. A rule failure degrades to "on a foreign subnet", not "on your network".
#
# WHY tank AND NOT local-lvm: local-lvm is thin and carries the root disks of
# LXC 151 and 153. A tester filling a thin volume there would take those
# filesystems read-only. tank is not sparse, so PVE gives the zvol a
# refreservation it cannot overcommit.
#
# THIS SCRIPT ONLY CREATES THE GUEST. It is unreachable from outside until
# 73-vm-tester-firewall.sh has run. That split is deliberate: see 71's header.
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

TESTER_VMID="${TESTER_VMID:-172}"
TESTER_NAME="${TESTER_NAME:-tester}"
TESTER_MEM_MB="${TESTER_MEM_MB:-65536}"
TESTER_CORES="${TESTER_CORES:-8}"
TESTER_DISK_GB="${TESTER_DISK_GB:-200}"
TESTER_STORAGE="${TESTER_STORAGE:-tank-lxc}"
TESTER_BRIDGE="${TESTER_BRIDGE:-sdxguest}"
TESTER_IP="${TESTER_IP:-10.78.0.10/24}"
TESTER_GW="${TESTER_GW:-10.78.0.254}"
TESTER_DNS="${TESTER_DNS:-9.9.9.9 1.1.1.1}"
# Lower than the default 100 so the tester loses CPU contention to inference.
TESTER_CPUUNITS="${TESTER_CPUUNITS:-50}"
TESTER_IMAGE_URL="${TESTER_IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/20260518-2482/debian-13-genericcloud-amd64-20260518-2482.qcow2}"
TESTER_IMAGE_SHA512="${TESTER_IMAGE_SHA512:-7752ad2adce1bc49dd964dae8300ed7a239d0bf3c13112f55953b111447fe642d2cc01afeead234aa6ebe3605513f2e7c0e7c56785d675c38ff40110d5c8332b}"

require_cmd qm pvesm wget sha512sum zfs

# These have no defaults on purpose: no key belongs in this repo, and a guest
# with no key is a guest nobody can use.
[[ -n "${TESTER_USER:-}" ]] \
  || die "TESTER_USER is not set. Add it to scripts/config.env (the tester's login name)."
[[ -n "${TESTER_SSH_PUBKEY:-}" ]] \
  || die "TESTER_SSH_PUBKEY is not set. Add the tester's PUBLIC key to scripts/config.env."

vm_exists() { qm status "$1" >/dev/null 2>&1; }

step "1 — preflight"
pvesm status | awk 'NR>1 {print $1}' | grep -qx "$TESTER_STORAGE" \
  || die "storage '$TESTER_STORAGE' not found — check 'pvesm status'"
ip -br link show "$TESTER_BRIDGE" >/dev/null 2>&1 \
  || die "bridge '$TESTER_BRIDGE' not found — is the SDN zone applied?"
ip -4 addr show "$TESTER_BRIDGE" | grep -q "${TESTER_GW}/" \
  || die "gateway $TESTER_GW is not on $TESTER_BRIDGE — apply the SDN config first"
ok "storage, bridge and gateway present"

step "2 — fetch and verify the image"
img="/var/lib/vz/template/$(basename "$TESTER_IMAGE_URL")"
if [[ -f "$img" ]] && echo "${TESTER_IMAGE_SHA512}  ${img}" | sha512sum -c - >/dev/null 2>&1; then
  skip "image already present and verified"
else
  wget -q -O "$img" "$TESTER_IMAGE_URL" || die "download failed: $TESTER_IMAGE_URL"
  echo "${TESTER_IMAGE_SHA512}  ${img}" | sha512sum -c - >/dev/null 2>&1 \
    || die "SHA512 MISMATCH on $img — refusing to use it. Delete it and retry."
  ok "downloaded and verified $(basename "$img")"
fi

step "3 — create VM $TESTER_VMID ($TESTER_NAME)"
if vm_exists "$TESTER_VMID"; then
  skip "VM $TESTER_VMID already exists"
else
  # seabios, not ovmf: the genericcloud image boots BIOS without an efidisk,
  # which removes a whole class of first-boot failure. serial0 gives console
  # access, which matters for a guest with no other path in.
  qm create "$TESTER_VMID" \
    --name "$TESTER_NAME" \
    --ostype l26 \
    --machine q35 \
    --memory "$TESTER_MEM_MB" \
    --balloon 0 \
    --cores "$TESTER_CORES" \
    --sockets 1 \
    --cpu host \
    --cpuunits "$TESTER_CPUUNITS" \
    --scsihw virtio-scsi-single \
    --net0 "virtio,bridge=${TESTER_BRIDGE},firewall=1" \
    --agent enabled=1 \
    --serial0 socket \
    --vga serial0 \
    --onboot 0 \
    || die "qm create failed"
  ok "created VM $TESTER_VMID"
fi

step "4 — import and attach the disk"
if qm config "$TESTER_VMID" | grep -q '^scsi0:'; then
  skip "scsi0 already attached"
else
  qm disk import "$TESTER_VMID" "$img" "$TESTER_STORAGE" >/dev/null \
    || die "qm disk import failed"
  # Read the volid back rather than assuming it is vm-<id>-disk-0. That holds
  # for an empty VM, but if any volume already exists the guess silently
  # attaches the wrong disk.
  volid="$(qm config "$TESTER_VMID" | awk -F': ' '/^unused[0-9]+:/{print $2; exit}')"
  [[ -n "$volid" ]] || die "imported disk did not appear as an unused volume on VM $TESTER_VMID"
  qm set "$TESTER_VMID" \
    --scsi0 "${volid},discard=on,iothread=1,ssd=1" >/dev/null \
    || die "failed to attach imported disk $volid"
  qm disk resize "$TESTER_VMID" scsi0 "${TESTER_DISK_GB}G" >/dev/null \
    || die "failed to resize scsi0 to ${TESTER_DISK_GB}G"
  qm set "$TESTER_VMID" --boot order=scsi0 >/dev/null || die "failed to set boot order"
  ok "disk imported, resized to ${TESTER_DISK_GB}G, boot order set"
fi

step "5 — cloud-init"
keyfile="$(mktemp)"; trap 'rm -f "$keyfile"' EXIT
printf '%s\n' "$TESTER_SSH_PUBKEY" > "$keyfile"
qm set "$TESTER_VMID" \
  --ide2 "${TESTER_STORAGE}:cloudinit" \
  --citype nocloud \
  --ciuser "$TESTER_USER" \
  --sshkeys "$keyfile" \
  --ipconfig0 "ip=${TESTER_IP},gw=${TESTER_GW}" \
  --nameserver "$TESTER_DNS" \
  --ciupgrade 1 >/dev/null \
  || die "cloud-init configuration failed"
ok "cloud-init drive attached for user '$TESTER_USER'"

step "6 — start and wait for the guest agent"
qm start "$TESTER_VMID" >/dev/null 2>&1 || skip "already running"
for _ in $(seq 1 60); do
  qm guest cmd "$TESTER_VMID" ping >/dev/null 2>&1 && break
  sleep 5
done
qm guest cmd "$TESTER_VMID" ping >/dev/null 2>&1 \
  || die "guest agent never answered — check 'qm terminal $TESTER_VMID'"
ok "VM $TESTER_VMID is up and the agent answers"

echo
echo "The guest is NOT yet reachable from outside, and NOT yet confined."
echo "Run next:  scripts/73-vm-tester-firewall.sh"
