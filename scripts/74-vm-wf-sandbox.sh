#!/usr/bin/env bash
# 74-vm-wf-sandbox.sh — create the workforce sandbox VM (Debian 13, VMID 176) and its network.
#
# WHAT IT IS: the machine every workforce agent action executes in (workforce spec §5.1, decision F).
# The harness (scripts/tools/workforce) runs here as root; agents run as <prefix>-impl-N / -lead in
# systemd scopes. Driven through the QEMU guest agent only: no SSH, no inbound port, no DNAT.
#
# NETWORK (spec §5.2, decisions 2026-10-07): a plain bridge vmbrwf (10.79.0.0/24, gateway 10.79.0.254)
# that is NOT masqueraded to the host address. Its traffic leaving vmbr0 is SNATed to 192.168.6.79
# (wf-sandbox-net.service): the router allowlists the host's own address for /metrics, so a
# host-SNATed sandbox could read it without a key (Plan A ruling). Which destinations are reachable is
# the PVE firewall's job (75-vm-wf-sandbox-firewall.sh): internet-only while provisioning, router +
# three workers only for measured runs.
#
# DISK: tank-lxc, thick. tank-lxc has drifted to `sparse 1` (memory: tester VM 172 rebuild), so this
# script sets refreservation=auto on the imported zvol and then ASSERTS it, as 72 does.
#
# THIS SCRIPT DOES NOT START THE VM. Order: 74 -> 75 build -> qm start 176 -> 76 provision ->
# 75 locked -> 76 proof. The VM is never running without a policy.
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"
require_root
require_pve_host
load_config

WF_VMID="${WF_VMID:-176}"
WF_NAME="${WF_NAME:-wf-sandbox}"
WF_MEM_MB="${WF_MEM_MB:-16384}"
WF_CORES="${WF_CORES:-6}"
WF_CPUUNITS="${WF_CPUUNITS:-50}"           # loses CPU contention to inference
WF_DISK_GB="${WF_DISK_GB:-48}"
WF_STORAGE="${WF_STORAGE:-tank-lxc}"
WF_BRIDGE="${WF_BRIDGE:-vmbrwf}"
WF_GW="${WF_GW:-10.79.0.254}"
WF_GUEST_IP="${WF_GUEST_IP:-10.79.0.10/24}"
WF_BUILD_DNS="${WF_BUILD_DNS:-9.9.9.9}"    # only usable in build mode; locked mode drops all DNS
WF_SNIPPET_STORAGE="${WF_SNIPPET_STORAGE:-local}"
WF_IMAGE_URL="${WF_IMAGE_URL:-${TESTER_IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/20260518-2482/debian-13-genericcloud-amd64-20260518-2482.qcow2}}"
WF_IMAGE_SHA512="${WF_IMAGE_SHA512:-${TESTER_IMAGE_SHA512:-7752ad2adce1bc49dd964dae8300ed7a239d0bf3c13112f55953b111447fe642d2cc01afeead234aa6ebe3605513f2e7c0e7c56785d675c38ff40110d5c8332b}}"
INTERFACES="${WF_INTERFACES_FILE:-/etc/network/interfaces}"

require_cmd qm pvesm pvesh zfs ip ifup wget sha512sum python3 systemctl iptables

step "1 — preflight"
pvesm status | awk 'NR>1 {print $1}' | grep -qx "$WF_STORAGE" || die "storage '$WF_STORAGE' not found"
[[ -x "$LGC_DIR/files/wf-sandbox-net.sh" || -r "$LGC_DIR/files/wf-sandbox-net.sh" ]] \
  || die "missing $LGC_DIR/files/wf-sandbox-net.sh"
# IPv6: ifupdown2 sets forwarding=1 on bridges at every boot (vmbr0 reads 1; memory: tester VM 172
# rebuild drift), so host-wide forwarding cannot be the guarantee. The guarantee is the sandbox's own
# bridge: IPv6 disabled and forwarding 0 on vmbrwf (step 2), so no IPv6 packet from the guest is ever
# processed or routed by the host. The guest disables IPv6 too.
ok "storage and helper present"

step "2 — bridge $WF_BRIDGE ($WF_GW)"
if ip -br link show "$WF_BRIDGE" >/dev/null 2>&1; then
  skip "$WF_BRIDGE exists"
else
  grep -qE "^iface ${WF_BRIDGE} " "$INTERFACES" || cat >> "$INTERFACES" <<EOF

# Workforce sandbox (scripts/74-vm-wf-sandbox.sh). Not masqueraded to the host address:
# wf-sandbox-net.service SNATs 10.79.0.0/24 to 192.168.6.79 on vmbr0.
auto ${WF_BRIDGE}
iface ${WF_BRIDGE} inet static
	address ${WF_GW}/24
	bridge-ports none
	bridge-stp off
	bridge-fd 0
	post-up sysctl -qw net.ipv6.conf.${WF_BRIDGE}.disable_ipv6=1
	post-up sysctl -qw net.ipv6.conf.${WF_BRIDGE}.forwarding=0
EOF
  ifup "$WF_BRIDGE" || die "ifup $WF_BRIDGE failed"
fi
ip -4 addr show "$WF_BRIDGE" | grep -q "${WF_GW}/" || die "$WF_GW is not on $WF_BRIDGE"
# No IPv6 on the sandbox bridge: in build mode the policy is IPv4-only, and link-local IPv6 would
# otherwise reach host services (security review MEDIUM).
sysctl -qw "net.ipv6.conf.${WF_BRIDGE}.disable_ipv6=1" "net.ipv6.conf.${WF_BRIDGE}.forwarding=0"
[[ "$(cat "/proc/sys/net/ipv6/conf/${WF_BRIDGE}/disable_ipv6")" == "1" ]] || die "IPv6 still enabled on $WF_BRIDGE"
[[ "$(cat "/proc/sys/net/ipv6/conf/${WF_BRIDGE}/forwarding")" == "0" ]] || die "IPv6 forwarding still on for $WF_BRIDGE"
ok "$WF_BRIDGE up with $WF_GW"

step "3 — dedicated LAN identity (192.168.6.79) via wf-sandbox-net.service"
install -m 0755 "$LGC_DIR/files/wf-sandbox-net.sh" /usr/local/sbin/wf-sandbox-net.sh
write_file_if_changed /etc/systemd/system/wf-sandbox-net.service 0644 <<'EOF'
[Unit]
Description=Workforce sandbox SNAT identity (10.79.0.0/24 -> 192.168.6.79 on vmbr0)
After=network-online.target pve-firewall.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/wf-sandbox-net.sh add
ExecStop=/usr/local/sbin/wf-sandbox-net.sh del

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable wf-sandbox-net.service >/dev/null
systemctl restart wf-sandbox-net.service
/usr/local/sbin/wf-sandbox-net.sh check || die "SNAT identity not in place after starting the unit"
ok "192.168.6.79 on vmbr0, SNAT for 10.79.0.0/24 installed"

step "4 — image"
img="/var/lib/vz/template/$(basename "$WF_IMAGE_URL")"
if [[ -f "$img" ]] && echo "${WF_IMAGE_SHA512}  ${img}" | sha512sum -c - >/dev/null 2>&1; then
  skip "image present and verified"
else
  wget -q -O "$img" "$WF_IMAGE_URL" || die "download failed: $WF_IMAGE_URL"
  echo "${WF_IMAGE_SHA512}  ${img}" | sha512sum -c - >/dev/null 2>&1 || die "SHA512 MISMATCH on $img"
  ok "downloaded and verified"
fi

step "5 — VM $WF_VMID ($WF_NAME)"
# A deny-all policy exists BEFORE the VM does, so VM 176 can never start unfiltered, even if 75 is
# skipped or fails (final review I2). 76 refuses to act on it (neither build nor locked).
if [[ ! -f "/etc/pve/firewall/${WF_VMID}.fw" ]]; then
  write_file_if_changed "/etc/pve/firewall/${WF_VMID}.fw" 0640 < <(bash "$LGC_DIR/files/wf-sandbox-policy.sh" closed)
fi
if qm status "$WF_VMID" >/dev/null 2>&1; then
  skip "VM $WF_VMID exists"
else
  qm create "$WF_VMID" --name "$WF_NAME" --ostype l26 --machine q35 --memory "$WF_MEM_MB" --balloon 0 \
    --cores "$WF_CORES" --sockets 1 --cpu host --cpuunits "$WF_CPUUNITS" --scsihw virtio-scsi-single \
    --net0 "virtio,bridge=${WF_BRIDGE},firewall=1" --agent enabled=1 --serial0 socket --vga serial0 \
    --onboot 0 || die "qm create failed"
  ok "created VM $WF_VMID"
fi

step "6 — disk"
if qm config "$WF_VMID" | grep -q '^scsi0:'; then
  skip "scsi0 attached"
else
  volid="$(qm config "$WF_VMID" | awk -F': ' '/^unused[0-9]+:/{print $2; exit}')"
  if [[ -z "$volid" ]]; then
    qm disk import "$WF_VMID" "$img" "$WF_STORAGE" >/dev/null || die "qm disk import failed"
    volid="$(qm config "$WF_VMID" | awk -F': ' '/^unused[0-9]+:/{print $2; exit}')"
    [[ -n "$volid" ]] || die "imported disk did not appear as an unused volume"
  fi
  qm set "$WF_VMID" --scsi0 "${volid},discard=on,iothread=1,ssd=1" >/dev/null || die "attach failed"
  qm disk resize "$WF_VMID" scsi0 "${WF_DISK_GB}G" >/dev/null || die "resize failed"
  qm set "$WF_VMID" --boot order=scsi0 >/dev/null || die "boot order failed"
  ok "disk attached, ${WF_DISK_GB}G"
fi

step "7 — thick provisioning (refreservation)"
scsi0="$(qm config "$WF_VMID" | awk -F': ' '/^scsi0:/{print $2; exit}' | cut -d, -f1)"
ds="$(pvesm path "$scsi0")"; ds="${ds#/dev/zvol/}"
zfs set refreservation=auto "$ds"
refres="$(zfs get -Hp -o value refreservation "$ds")"; volsize="$(zfs get -Hp -o value volsize "$ds")"
awk -v r="$refres" -v v="$volsize" 'BEGIN { d=(r>v)?r-v:v-r; exit !(r>0 && v>0 && d/v <= 0.05) }' \
  || die "zvol $ds refreservation=$refres not close to volsize=$volsize — the sandbox could starve tank"
ok "zvol $ds thick: refreservation=$refres volsize=$volsize"

step "8 — cloud-init (guest agent via vendor snippet, no login user)"
snip_path="$(pvesh get "/storage/${WF_SNIPPET_STORAGE}" --output-format json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("path",""))')"
[[ -n "$snip_path" ]] || die "storage '$WF_SNIPPET_STORAGE' has no path"
content="$(pvesh get "/storage/${WF_SNIPPET_STORAGE}" --output-format json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("content",""))')"
[[ ",$content," == *",snippets,"* ]] || pvesm set "$WF_SNIPPET_STORAGE" --content "${content},snippets"
mkdir -p "${snip_path}/snippets"
write_file_if_changed "${snip_path}/snippets/wf-sandbox-vendor.yaml" 0644 <<'EOF'
#cloud-config
package_update: true
packages:
  - qemu-guest-agent
write_files:
  - path: /etc/sysctl.d/90-wf-no-ipv6.conf
    content: |
      net.ipv6.conf.all.disable_ipv6 = 1
      net.ipv6.conf.default.disable_ipv6 = 1
runcmd:
  - [ sysctl, --system ]
  - [ systemctl, enable, --now, qemu-guest-agent ]
EOF
qm set "$WF_VMID" --ide2 "${WF_STORAGE}:cloudinit" --citype nocloud --ciuser wfadmin \
  --ipconfig0 "ip=${WF_GUEST_IP},gw=${WF_GW}" --nameserver "$WF_BUILD_DNS" --ciupgrade 1 \
  --cicustom "vendor=${WF_SNIPPET_STORAGE}:snippets/wf-sandbox-vendor.yaml" >/dev/null \
  || die "cloud-init configuration failed"
ok "cloud-init attached (no SSH keys, no password: the guest agent is the only way in)"

echo
echo "VM $WF_VMID created and NOT started. Next:"
echo "  scripts/75-vm-wf-sandbox-firewall.sh build && qm start $WF_VMID && scripts/76-vm-wf-sandbox.sh provision"
