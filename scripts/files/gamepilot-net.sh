#!/usr/bin/env bash
# GamePilot conductor (CT 160, bridge vmbrgp 10.82.0.0/24): SNAT to the host address, so the conductor's only
# allowed peer, the proxy on 192.168.6.155:3129, sees 192.168.6.175. The CT firewall (160.fw) allows nothing else.
set -euo pipefail
ip_addr=192.168.6.175; cidr=10.82.0.0/24; uplink=vmbr0
rule=(POSTROUTING -s "$cidr" -o "$uplink" -j SNAT --to-source "$ip_addr")
has_rule() { iptables -t nat -C "${rule[@]}" 2>/dev/null; }
case "${1:-}" in
  add) has_rule || iptables -t nat -I "${rule[0]}" 1 "${rule[@]:1}" ;;
  del) if has_rule; then iptables -t nat -D "${rule[@]}"; fi ;;
  check) has_rule ;;
  *) echo "usage: gamepilot-net.sh add|del|check" >&2; exit 2 ;;
esac
