#!/usr/bin/env bash
# 71-vm-se-qa-firewall.sh — the PVE firewall policy confining the SE QA guest (VMID 170).
#
# WHY THIS IS SEPARATE FROM 70-vm-se-qa.sh: creating a guest affects one guest.
# Enabling the datacenter firewall flips a CLUSTER-WIDE switch that, if any
# other guest ever gains firewall=1 on a NIC, starts filtering that guest too.
# Those two risks do not belong under one confirmation. Read this script on its
# own before running it.
#
# WHY IT EXISTS AT ALL: docs/se-qa-vm-requirements.md section 5 asked whether
# the guest could run with zero egress. It could not be settled from the host:
# proving a local multiplayer join needs the game CLIENT to render, the guest
# has no GPU, and the null-renderer that would remove that requirement is
# SE-DX2.0Code's tooling, not this repo's. Rather than leave the guest open
# while that question stands, section 5's documented fallback shipped: keep it
# on vmbr0 and deny it the things that matter.
#
# THE POLICY, and why each rule is the shape it is:
#   - default OUT ACCEPT      -- Steam must work, or the guest cannot be built
#                                or kept current.
#   - ACCEPT to the gateway, udp/67 -- DHCP renewal. Without this the lease
#                                expires and the guest silently loses its
#                                address, because the next rule would drop the
#                                renewal along with the rest of the subnet.
#   - DROP all private space, ordered AFTER the DHCP accept:
#       10.0.0.0/8     -- the production fleet VPN (10.60.0.0/16; this guest
#                         exists to provoke crashes and must never reach a live
#                         game server), plus the host-only lab and SDN vnets.
#       172.16.0.0/12  -- the VCF lab management network.
#       192.168.0.0/16 -- the LAN: this PVE host and every inference container.
#       100.64.0.0/10  -- CGNAT / carrier space.
#       169.254.0.0/16 -- link-local.
#     The first policy denied only 192.168.6.0/24 and 10.60.0.0/16 by name.
#     The VCF lab (172.16.0.0/12) came later and was never denied: the LAN
#     router does not route to it, but one `route add ... 192.168.6.11` inside
#     the guest did, and the firewall matches destination, not next hop.
#     Verified 2026-10-02 from a LAN LXC; same flaw VM 172 had on 2026-09-25.
#     Denying all of RFC1918 does not go stale when a network is added.
#
# KNOWN LIMIT: these are IPv4 rules. The LAN and fleet ranges in the
# requirements doc are IPv4, and the guest gets no IPv6 address today, but this
# is a real gap and not a claim of completeness.
#
# THIS IS THE WEAKER OF THE TWO POSTURES. The end state is the isolated bridge
# with no uplink at all. When the client-side team proves or disproves the
# offline join, move the NIC and this policy becomes belt-and-braces:
#   qm set 170 --net0 virtio,bridge=vmbrseqa,firewall=1
set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

SEQA_VMID="${SEQA_VMID:-170}"
SEQA_GATEWAY="${SEQA_GATEWAY:-192.168.6.1}"
# The tester VM (73-vm-tester-firewall.sh) is the one other guest expected to be filtered.
TESTER_VMID="${TESTER_VMID:-172}"
# Probed after the change to prove host services still answer. Derived from the
# monitor's own config, as 73-vm-tester-firewall.sh does: cluster-monitor binds
# the LAN address only, so the old 127.0.0.1 default could never answer and
# warned on every run. An explicit SEQA_HOST_PROBE_URL always wins.
SEQA_HOST_PROBE_URL="${SEQA_HOST_PROBE_URL:-}"
SEQA_MONITOR_CONFIG="${SEQA_MONITOR_CONFIG:-/etc/cluster-monitor.json}"
if [[ -z "$SEQA_HOST_PROBE_URL" && -f "$SEQA_MONITOR_CONFIG" ]]; then
  SEQA_HOST_PROBE_URL="$(python3 -c "
import json, sys
try:
    cfg = json.load(open('$SEQA_MONITOR_CONFIG'))
    print('http://%s:%s/' % (cfg['bind_host'], cfg.get('bind_port', 8888)))
except Exception:
    sys.exit(1)
" 2>/dev/null)" || SEQA_HOST_PROBE_URL=""
fi

step "1 — preflight"
require_cmd pve-firewall
[[ -f "/etc/pve/qemu-server/${SEQA_VMID}.conf" ]] \
  || die "VM $SEQA_VMID does not exist — run 70-vm-se-qa.sh and install it first."
grep -qE '^net[0-9]+:.*firewall=1' "/etc/pve/qemu-server/${SEQA_VMID}.conf" \
  || die "VM $SEQA_VMID has no NIC with firewall=1 — the policy below would never be applied.
        Fix with: qm set $SEQA_VMID --net0 virtio,bridge=<bridge>,firewall=1"
ok "VM $SEQA_VMID exists and has a filtered NIC"

step "2 — blast-radius guard"
# The datacenter switch only filters interfaces that opt in with firewall=1.
# Assert nothing ELSE has opted in, so enabling it cannot disturb the inference
# stack. Checked every run, not just the first: a GUI click can add the flag,
# and the container-side default is off only while the key is absent.
others=""
for conf in /etc/pve/lxc/*.conf /etc/pve/qemu-server/*.conf; do
  [[ -e "$conf" ]] || continue
  id="$(basename "$conf" .conf)"
  [[ "$id" == "$SEQA_VMID" || "$id" == "$TESTER_VMID" ]] && continue
  if grep -qE '^net[0-9]+:.*firewall=1' "$conf"; then
    others="${others} ${id}"
  fi
done
if [[ -n "$others" ]]; then
  die "refusing to proceed: guest(s)${others} also have firewall=1 and would start being filtered.
        Only VM $SEQA_VMID and VM $TESTER_VMID are expected to be filtered. Investigate before re-running."
fi
ok "only VM $SEQA_VMID and VM $TESTER_VMID opt into filtering — the datacenter switch touches nothing else"

step "3 — datacenter switch"
write_file_if_changed /etc/pve/firewall/cluster.fw 0640 <<'EOF'
[OPTIONS]
enable: 1
EOF

step "4 — keep this host open"
# Deliberate. The host runs cluster-monitor, node metrics, SSH and the web UI,
# and nothing about confining a guest requires filtering the host itself.
# Removing this file, or flipping IN to DROP, will cut your own management
# access to a headless machine.
write_file_if_changed "/etc/pve/nodes/$(hostname)/host.fw" 0640 <<'EOF'
[OPTIONS]
enable: 1

[RULES]
IN ACCEPT # host stays open as before; datacenter FW filters only VM 170 (net firewall=1)
EOF

step "5 — guest policy"
write_file_if_changed "/etc/pve/firewall/${SEQA_VMID}.fw" 0640 <<EOF
[OPTIONS]
enable: 1
policy_in: ACCEPT
policy_out: ACCEPT

[RULES]
OUT ACCEPT -dest ${SEQA_GATEWAY} -p udp -dport 67 # DHCP renew to gateway; must precede the 192.168 deny
OUT DROP -dest 10.0.0.0/8 # RFC1918: production fleet VPN 10.60.0.0/16, host-only lab and SDN vnets
OUT DROP -dest 172.16.0.0/12 # RFC1918: the VCF lab management network
OUT DROP -dest 192.168.0.0/16 # RFC1918: the LAN - PVE host + all inference LXCs
OUT DROP -dest 100.64.0.0/10 # CGNAT / carrier space
OUT DROP -dest 169.254.0.0/16 # link-local
EOF

step "6 — apply and verify"
pve-firewall compile >/dev/null 2>&1 || die "pve-firewall compile failed — rules not applied; fix the files above"
pve-firewall restart >/dev/null 2>&1 || die "pve-firewall restart failed"
sleep 2
pve-firewall status 2>&1 | grep -q "enabled/running" \
  || die "pve-firewall is not enabled/running after restart"
ok "pve-firewall enabled/running"

# The filter bridge is the ground truth for WHICH interfaces are filtered --
# stronger evidence than reading config flags back.
if ip -br link show type bridge 2>/dev/null | grep -q "fwbr${SEQA_VMID}i"; then
  ok "fwbr${SEQA_VMID}i0 present — VM $SEQA_VMID is being filtered"
else
  warn "no fwbr${SEQA_VMID}i* interface — expected if VM $SEQA_VMID is stopped; re-check once it is running"
fi
stray="$(ip -br link show type bridge 2>/dev/null | awk '{print $1}' | grep -E '^fwbr' | grep -v "^fwbr${SEQA_VMID}i" | grep -v "^fwbr${TESTER_VMID}i" || true)"
[[ -z "$stray" ]] || die "unexpected filter bridges present: $stray — something other than VM $SEQA_VMID or VM $TESTER_VMID is being filtered"

if [[ -z "$SEQA_HOST_PROBE_URL" ]]; then
  warn "no host probe URL ($SEQA_MONITOR_CONFIG missing or unparseable) — check host services by hand"
else
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$SEQA_HOST_PROBE_URL" || true)"
  [[ "$code" == "200" ]] \
    && ok "host services still answering ($SEQA_HOST_PROBE_URL -> $code)" \
    || warn "host probe $SEQA_HOST_PROBE_URL returned '$code' — check cluster-monitor before walking away"
fi

step "done"
cat <<EOF

  VM ${SEQA_VMID} is on vmbr0 and confined by /etc/pve/firewall/${SEQA_VMID}.fw:
    allowed  : everything else, including Steam
    denied   : 10/8, 172.16/12, 192.168/16 (all RFC1918: LAN, fleet, VCF lab)
    denied   : 100.64/10 (CGNAT), 169.254/16 (link-local)
    excepted : udp/67 to ${SEQA_GATEWAY}, so the DHCP lease can renew

  Verify from inside the guest, not from here:
    qm guest exec ${SEQA_VMID} -- powershell -Command "Test-NetConnection 1.1.1.1 -Port 443"
    qm guest exec ${SEQA_VMID} -- powershell -Command "Test-NetConnection 192.168.6.175 -Port 8006"
  Expect the first to succeed and the second to fail. The VCF lab is not on the
  guest's default route, so prove the deny with the route a guest admin could add:
    qm guest exec ${SEQA_VMID} -- powershell -Command "route add 172.16.10.133 mask 255.255.255.255 192.168.6.11; Test-NetConnection 172.16.10.133 -Port 443; route delete 172.16.10.133"
  Expect TcpTestSucceeded False.

  This is the fallback posture, not the target. When the client-side team
  settles the offline-join question, move to the isolated bridge:
    qm set ${SEQA_VMID} --net0 virtio,bridge=vmbrseqa,firewall=1

  Rollback (restores the previous state: firewall fully off):
    rm -f /etc/pve/firewall/${SEQA_VMID}.fw /etc/pve/firewall/cluster.fw
    rm -f /etc/pve/nodes/$(hostname)/host.fw
    pve-firewall restart
EOF
