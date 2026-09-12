#!/usr/bin/env bash
# 73-vm-tester-firewall.sh — confine VM 172 and expose exactly one port to it.
#
# WHY SEPARATE FROM 72-: creating a guest affects one guest. This script writes
# cluster-wide NAT state and relies on the datacenter firewall switch. Those are
# different blast radii and do not belong under one confirmation. Same reasoning
# as 71-vm-se-qa-firewall.sh.
#
# THE POLICY, and why each rule is the shape it is:
#   policy_in DROP      -- default closed. The guest offers one service.
#   IN ACCEPT tcp/22    -- the only way in, reached via the host's DNAT.
#   OUT DROP 10.78.0.254 -- the PVE host itself. Blocks host services while
#                          still permitting routing THROUGH it, because
#                          forwarded traffic is addressed to the internet, not
#                          to the gateway. This is why DNS must be public.
#   OUT DROP 192.168.6.0/24 -- the LAN: this host and every inference LXC.
#   OUT DROP 10.77.0.0/24   -- the sibling SDN vnet.
#   OUT DROP 10.60.0.0/16   -- the production fleet VPN, same reason as VM 170.
#   OUT DROP 169.254.0.0/16 -- link-local and cloud metadata.
#
# KNOWN LIMIT: these are IPv4 rules. That is only acceptable because host IPv6
# forwarding is off, which this script ASSERTS rather than assumes.
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

TESTER_VMID="${TESTER_VMID:-172}"
TESTER_SSH_PORT="${TESTER_SSH_PORT:-2222}"
TESTER_GUEST_IP="${TESTER_GUEST_IP:-10.78.0.10}"
TESTER_LAN_CIDR="${TESTER_LAN_CIDR:-192.168.6.0/24}"
TESTER_GW="${TESTER_GW:-10.78.0.254}"
TESTER_SIBLING_CIDR="${TESTER_SIBLING_CIDR:-10.77.0.0/24}"
TESTER_FLEET_CIDR="${TESTER_FLEET_CIDR:-10.60.0.0/16}"
# Probed after the change to prove host services still answer.
TESTER_HOST_PROBE_URL="${TESTER_HOST_PROBE_URL:-http://127.0.0.1:8888/}"

require_cmd pve-firewall iptables systemctl

step "1 — preflight"
[[ -f "/etc/pve/qemu-server/${TESTER_VMID}.conf" ]] \
  || die "VM $TESTER_VMID does not exist — run 72-vm-tester.sh first."
grep -qE '^net[0-9]+:.*firewall=1' "/etc/pve/qemu-server/${TESTER_VMID}.conf" \
  || die "VM $TESTER_VMID has no NIC with firewall=1 — the policy below would never apply."
ok "VM $TESTER_VMID exists and has a filtered NIC"

step "2 — assert IPv6 forwarding is off"
# The policy is IPv4-only. If the host ever routes v6, this policy is incomplete
# and the guest could reach the LAN over it.
[[ "$(sysctl -n net.ipv6.conf.all.forwarding)" == "0" ]] \
  || die "net.ipv6.conf.all.forwarding is not 0 — this IPv4-only policy is incomplete. Stop."
ok "IPv6 forwarding is off"

step "3 — blast-radius guard"
# The datacenter switch filters only NICs that opt in with firewall=1. VM 170
# already opts in by design (see 71-). Assert nothing ELSE has, so enabling or
# reloading the firewall cannot start filtering the inference stack.
others=""
for conf in /etc/pve/lxc/*.conf /etc/pve/qemu-server/*.conf; do
  [[ -e "$conf" ]] || continue
  id="$(basename "$conf" .conf)"
  [[ "$id" == "$TESTER_VMID" || "$id" == "170" ]] && continue
  grep -qE '^(net[0-9]+):.*firewall=1' "$conf" && others+=" $id"
done
[[ -z "$others" ]] \
  || die "these guests also have firewall=1 and would start being filtered:$others"
ok "only VM 170 and VM $TESTER_VMID have filtered NICs"

step "4 — write the guest policy"
write_file_if_changed "/etc/pve/firewall/${TESTER_VMID}.fw" 0640 <<EOF
[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -p tcp -dport 22 # the only way in, via the host DNAT
OUT DROP -dest ${TESTER_GW} # the PVE host itself - no host services
OUT DROP -dest ${TESTER_LAN_CIDR} # the LAN: this host + every inference LXC
OUT DROP -dest ${TESTER_SIBLING_CIDR} # sibling SDN vnet
OUT DROP -dest ${TESTER_FLEET_CIDR} # production fleet VPN
OUT DROP -dest 169.254.0.0/16 # link-local and cloud metadata
EOF

step "5 — reload the firewall and verify enforcement is genuinely live"
# Confinement must be PROVEN before the guest is made reachable in step 6.
# `compile` is syntax-only and `restart` succeeds even if the datacenter
# switch (cluster.fw) is off — neither proves per-guest filtering is active.
# Ground truth is the same pair 71-vm-se-qa-firewall.sh uses: the datacenter
# firewall's own status, and the fwbr* filter bridge for this guest's NIC.
pve-firewall compile >/dev/null || die "pve-firewall failed to compile the new policy"
pve-firewall restart
sleep 2
pve-firewall status 2>&1 | grep -q "enabled/running" \
  || die "pve-firewall is not enabled/running after restart — the datacenter switch (cluster.fw) is likely off, so VM $TESTER_VMID would be completely unconfined"
ok "pve-firewall enabled/running"

# The filter bridge only exists while the guest is running, and VM 172 may
# legitimately be stopped (or not yet created) at this point — so absence is
# a warning, never fatal. Mirrors 71-'s handling exactly.
if ip -br link show type bridge 2>/dev/null | grep -q "fwbr${TESTER_VMID}i"; then
  ok "fwbr${TESTER_VMID}i0 present — VM $TESTER_VMID is being filtered"
else
  warn "no fwbr${TESTER_VMID}i* interface — expected if VM $TESTER_VMID is stopped; re-check once it is running"
fi
stray="$(ip -br link show type bridge 2>/dev/null | awk '{print $1}' | grep -E '^fwbr' | grep -v "^fwbr${TESTER_VMID}i" | grep -v '^fwbr170i' || true)"
[[ -z "$stray" ]] || die "unexpected filter bridges present: $stray — something other than VM 170 or VM $TESTER_VMID is being filtered"

step "6 — install the DNAT unit (only now that confinement is confirmed)"
install -m 0755 "$LGC_DIR/files/tester-vm-dnat.sh" /usr/local/sbin/tester-vm-dnat.sh
write_file_if_changed /etc/systemd/system/tester-vm-dnat.service 0644 <<EOF
[Unit]
Description=Inbound SSH port-forward to the external tester guest (VM ${TESTER_VMID})
After=network-online.target pve-firewall.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=TESTER_SSH_PORT=${TESTER_SSH_PORT}
Environment=TESTER_DEST=${TESTER_GUEST_IP}:22
ExecStart=/usr/local/sbin/tester-vm-dnat.sh add
ExecStop=/usr/local/sbin/tester-vm-dnat.sh del

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now tester-vm-dnat.service
iptables -t nat -C PREROUTING -i vmbr0 -p tcp --dport "$TESTER_SSH_PORT" \
  -j DNAT --to-destination "${TESTER_GUEST_IP}:22" \
  || die "DNAT rule is not present after enabling the unit"
ok "DNAT ${TESTER_SSH_PORT} -> ${TESTER_GUEST_IP}:22 installed and enabled"

step "7 — final check: host services unaffected"
curl -sf -o /dev/null --max-time 5 "$TESTER_HOST_PROBE_URL" \
  || die "host service at $TESTER_HOST_PROBE_URL stopped answering — back this out"
ok "policy active, DNAT present, host services unaffected"

echo
echo "Verify from the guest:  qm guest exec $TESTER_VMID -- /bin/bash -c 'curl -s -m5 https://deb.debian.org -o /dev/null; echo \$?'"
echo "Verify LAN is denied:   qm guest exec $TESTER_VMID -- /bin/bash -c 'curl -s -m5 http://192.168.6.153:8000/healthz -o /dev/null; echo \$?'"
