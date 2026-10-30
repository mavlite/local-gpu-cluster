#!/usr/bin/env bash
# SNAT only. The 192.168.6.78 address itself is persistent in /etc/network/interfaces (vmbr0 stanza).
set -euo pipefail
ip_addr=192.168.6.78; cidr=10.81.0.0/24; uplink=vmbr0
rule=(POSTROUTING -s "$cidr" -o "$uplink" -j SNAT --to-source "$ip_addr")
has_rule() { iptables -t nat -C "${rule[@]}" 2>/dev/null; }
case "${1:-}" in
  add) has_rule || iptables -t nat -I "${rule[0]}" 1 "${rule[@]:1}" ;;
  del) if has_rule; then iptables -t nat -D "${rule[@]}"; fi ;;
  check) ip -4 addr show dev "$uplink" | grep -q " ${ip_addr}/" && has_rule ;;
  *) echo "usage: agent-runner-net.sh add|del|check" >&2; exit 2 ;;
esac
