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
# Same default as 72-vm-tester.sh's TESTER_IP, so an override there is picked
# up here too instead of silently DNATing to a stale address.
TESTER_IP="${TESTER_IP:-10.78.0.10/24}"
TESTER_GUEST_IP="${TESTER_GUEST_IP:-${TESTER_IP%%/*}}"
TESTER_LAN_CIDR="${TESTER_LAN_CIDR:-192.168.6.0/24}"
TESTER_GW="${TESTER_GW:-10.78.0.254}"
TESTER_SIBLING_CIDR="${TESTER_SIBLING_CIDR:-10.77.0.0/24}"
TESTER_FLEET_CIDR="${TESTER_FLEET_CIDR:-10.60.0.0/16}"
# Spec §8: the guest must not be L2-adjacent to the LAN. Same default as
# 72-vm-tester.sh's TESTER_BRIDGE — asserted below, not just assumed.
TESTER_BRIDGE="${TESTER_BRIDGE:-sdxguest}"
# The host-side interface the DNAT and the IPv6-forwarding check apply to.
TESTER_UPLINK="${TESTER_UPLINK:-vmbr0}"
# Probed after the change to prove host services still answer. Derived from
# cluster-monitor's own config (/etc/cluster-monitor.json) rather than
# hardcoded: cluster-monitor binds whatever "bind_host"/"bind_port" say --
# on this host that is the LAN IP, not loopback -- so a hardcoded
# 127.0.0.1 can never answer, and a hardcoded LAN IP would be wrong on any
# other host. Same idiom 63-cluster-monitor.sh already uses to read this
# file back. If the config is missing, there is nothing to derive a URL
# from and the probe is skipped (with a warning) rather than treated as
# fatal for a condition that was never checkable.
TESTER_HOST_PROBE_URL="${TESTER_HOST_PROBE_URL:-}"
TESTER_MONITOR_CONFIG="${TESTER_MONITOR_CONFIG:-/etc/cluster-monitor.json}"

require_cmd pve-firewall iptables systemctl qm python3

# Resolve the probe URL now (an explicit TESTER_HOST_PROBE_URL always wins).
if [[ -z "$TESTER_HOST_PROBE_URL" ]]; then
  if [[ -f "$TESTER_MONITOR_CONFIG" ]]; then
    TESTER_HOST_PROBE_URL="$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$TESTER_MONITOR_CONFIG'))
    bind_host = cfg['bind_host']  # no default: a missing key means the schema
                                  # drifted, and defaulting to 127.0.0.1 here
                                  # is exactly the D1 bug (a loopback URL that
                                  # can never answer) -- treat it the same as
                                  # an unparseable file, below.
    print('http://%s:%s/' % (bind_host, cfg.get('bind_port', 8888)))
except Exception:
    sys.exit(1)
" 2>/dev/null)" || TESTER_HOST_PROBE_URL=""
    if [[ -n "$TESTER_HOST_PROBE_URL" ]]; then
      ok "Derived host probe URL from $TESTER_MONITOR_CONFIG: $TESTER_HOST_PROBE_URL"
    else
      warn "$TESTER_MONITOR_CONFIG exists but could not be parsed for bind_host/bind_port -- step 7's host-service canary will be skipped"
    fi
  else
    warn "$TESTER_MONITOR_CONFIG not found -- cannot derive a host probe URL; step 7's host-service canary will be skipped"
  fi
fi

# If we die after the DNAT is installed, the port stays forwarded with no
# guidance -- print a rollback recipe instead of leaving a silent exposure.
# ERR fires first (see lib/common.sh) and this fires on any exit, including
# the explicit `exit 1` inside die().
dnat_installed=0
rollback_hint() {
  local status=$?
  if [[ $status -ne 0 && $dnat_installed -eq 1 ]]; then
    cat >&2 <<EOF

FAILED after the DNAT was already installed — port ${TESTER_SSH_PORT} may
still be forwarded to ${TESTER_GUEST_IP}:22. Roll back with:
  systemctl disable --now tester-vm-dnat.service
  rm -f /etc/systemd/system/tester-vm-dnat.service /usr/local/sbin/tester-vm-dnat.sh
  systemctl daemon-reload
  rm -f /etc/pve/firewall/${TESTER_VMID}.fw
  pve-firewall restart
EOF
  fi
}
trap rollback_hint EXIT

step "1 — preflight"
[[ -f "/etc/pve/qemu-server/${TESTER_VMID}.conf" ]] \
  || die "VM $TESTER_VMID does not exist — run 72-vm-tester.sh first."
nic_line="$(grep -E '^net[0-9]+:.*firewall=1' "/etc/pve/qemu-server/${TESTER_VMID}.conf" | head -1)"
[[ -n "$nic_line" ]] \
  || die "VM $TESTER_VMID has no NIC with firewall=1 — the policy below would never apply."
echo "$nic_line" | grep -q "bridge=${TESTER_BRIDGE}" \
  || die "VM $TESTER_VMID's filtered NIC is not on bridge=${TESTER_BRIDGE} (spec §8: 'do not move this guest to vmbr0' -- it must have no L2 path to the LAN): $nic_line"
ok "VM $TESTER_VMID exists and has a filtered NIC on bridge=${TESTER_BRIDGE}"

step "2 — assert IPv6 forwarding is off"
# The policy is IPv4-only. Checking net.ipv6.conf.all.forwarding alone is not
# enough: Linux still honours a per-interface conf.<if>.forwarding=1 even when
# the global knob is 0. Assert the "all" default plus the two interfaces that
# actually carry this guest's traffic -- the SDN bridge it sits on and the
# uplink the DNAT traverses.
for ifc in all "$TESTER_BRIDGE" "$TESTER_UPLINK"; do
  path="/proc/sys/net/ipv6/conf/${ifc}/forwarding"
  [[ -r "$path" ]] || continue
  [[ "$(cat "$path")" == "0" ]] \
    || die "net.ipv6.conf.${ifc}.forwarding is not 0 — this IPv4-only policy is incomplete. Stop."
done
ok "IPv6 forwarding is off (all, ${TESTER_BRIDGE}, ${TESTER_UPLINK})"

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

# The filter bridge only exists while the guest is running. If VM 172 is
# stopped (or not yet created), absence is expected and only a warning --
# mirrors 71-'s handling exactly. But if the guest IS running, absence means
# its NIC is genuinely unfiltered right now (the filter bridge is created at
# NIC attach, not at config write -- e.g. `firewall=1` was added to a running
# VM's NIC after the fact) and step 6 is about to make that unfiltered guest
# reachable from the internet. That is never acceptable, so it is fatal.
if ip -br link show type bridge 2>/dev/null | grep -q "fwbr${TESTER_VMID}i"; then
  ok "fwbr${TESTER_VMID}i0 present — VM $TESTER_VMID is being filtered"
elif [[ "$(qm status "$TESTER_VMID" 2>/dev/null | awk '{print $2}')" == "running" ]]; then
  die "VM $TESTER_VMID is running but no fwbr${TESTER_VMID}i* interface exists -- its NIC is NOT being filtered. Exposing it via the DNAT in step 6 would be unsafe. Restart the VM (or re-attach the NIC) so the filter bridge is created, then re-run this script."
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
Environment=TESTER_UPLINK=${TESTER_UPLINK}
ExecStart=/usr/local/sbin/tester-vm-dnat.sh add
ExecStop=/usr/local/sbin/tester-vm-dnat.sh del

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable tester-vm-dnat.service
# `enable --now` is a no-op if the unit is already active, so a changed port
# or guest IP would update the unit file but leave the OLD rule installed.
# `restart` re-runs ExecStop (del the old rule, if any) then ExecStart (add
# the current one) every time, active or not.
systemctl restart tester-vm-dnat.service
dnat_installed=1
iptables -t nat -C PREROUTING -i "$TESTER_UPLINK" -p tcp --dport "$TESTER_SSH_PORT" \
  -j DNAT --to-destination "${TESTER_GUEST_IP}:22" \
  || die "DNAT rule is not present after enabling the unit"
ok "DNAT ${TESTER_SSH_PORT} -> ${TESTER_GUEST_IP}:22 installed and enabled"

step "7 — final check: host services unaffected"
if [[ -n "$TESTER_HOST_PROBE_URL" ]]; then
  curl -sf -o /dev/null --max-time 5 "$TESTER_HOST_PROBE_URL" \
    || die "host service at $TESTER_HOST_PROBE_URL stopped answering — back this out"
  ok "policy active, DNAT present, host services unaffected ($TESTER_HOST_PROBE_URL)"
else
  warn "no host probe URL available -- skipping the host-service canary (see the warning above)"
  ok "policy active, DNAT present"
fi

echo
echo "Verify from the guest:  qm guest exec $TESTER_VMID -- /bin/bash -c 'curl -s -m5 https://deb.debian.org -o /dev/null; echo \$?'"
echo "Verify LAN is denied:   qm guest exec $TESTER_VMID -- /bin/bash -c 'curl -s -m5 http://192.168.6.153:8000/healthz -o /dev/null; echo \$?'"
