#!/usr/bin/env bash
# wf-sandbox-net.sh add|del|check -- the workforce sandbox's dedicated LAN identity (Plan B).
#
# The sandbox bridge (vmbrwf, 10.79.0.0/24) is NOT masqueraded to the host address: the router's
# /metrics allowlist contains the host (192.168.6.175), so a host-SNATed sandbox could read it
# without a key. Its traffic leaving vmbr0 is SNATed to a dedicated address instead, which the host
# also answers ARP for. Idempotent; installed as wf-sandbox-net.service.
set -euo pipefail

ip_addr="${WF_SNAT_IP:-192.168.6.79}"
prefix="${WF_SNAT_PREFIX:-24}"
cidr="${WF_SANDBOX_CIDR:-10.79.0.0/24}"
uplink="${WF_UPLINK:-vmbr0}"
rule=(POSTROUTING -s "$cidr" -o "$uplink" -j SNAT --to-source "$ip_addr")

has_addr() { ip addr show dev "$uplink" | grep -q " ${ip_addr}/${prefix} "; }
has_rule() { iptables -t nat -C "${rule[@]}" 2>/dev/null; }

case "${1:-}" in
  add)
    has_addr || ip addr add "${ip_addr}/${prefix}" dev "$uplink"
    has_rule || iptables -t nat -I "${rule[0]}" 1 "${rule[@]:1}"
    ;;
  del)
    if has_rule; then iptables -t nat -D "${rule[@]}"; fi
    if has_addr; then ip addr del "${ip_addr}/${prefix}" dev "$uplink"; fi
    ;;
  check)
    has_addr && has_rule
    ;;
  *) echo "usage: wf-sandbox-net.sh add|del|check" >&2; exit 2 ;;
esac
