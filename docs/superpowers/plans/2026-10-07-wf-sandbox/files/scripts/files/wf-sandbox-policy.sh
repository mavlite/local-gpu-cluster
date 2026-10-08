#!/usr/bin/env bash
# wf-sandbox-policy.sh build|locked -- print the PVE firewall policy (<vmid>.fw) for the workforce
# sandbox VM (workforce spec §5.2; Plan B).
#
#   build   provisioning only: the internet, and NO private space (apt, npm, docker pulls).
#   locked  measured runs: the router's chat port and the three CPU workers -- nothing else. No DNS,
#           no package proxy, no nested lab, no host, no LAN (decisions 2026-10-07).
#
# Inbound is closed in both modes: the harness is driven through the QEMU guest agent, not SSH.
# Endpoints come from WF_ROUTER / WF_ROUTER_PORT / WF_WORKERS / WF_WORKER_PORT and must be single
# IPv4 addresses and ports.
set -euo pipefail

mode="${1:-}"
router="${WF_ROUTER:-192.168.6.153}"
router_port="${WF_ROUTER_PORT:-8000}"
workers="${WF_WORKERS:-172.16.10.205 172.16.10.206 172.16.10.207}"
worker_port="${WF_WORKER_PORT:-8090}"

ipv4() { [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] &&
         (( BASH_REMATCH[1] <= 255 && BASH_REMATCH[2] <= 255 && BASH_REMATCH[3] <= 255 && BASH_REMATCH[4] <= 255 )); }
port() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( $1 >= 1 && $1 <= 65535 )); }
die() { echo "wf-sandbox-policy: $*" >&2; exit 2; }

ipv4 "$router" || die "WF_ROUTER '$router' is not a single IPv4 address"
port "$router_port" || die "WF_ROUTER_PORT '$router_port' is not a port"
port "$worker_port" || die "WF_WORKER_PORT '$worker_port' is not a port"
for w in $workers; do ipv4 "$w" || die "WF_WORKERS entry '$w' is not a single IPv4 address"; done

case "$mode" in
  locked)
    printf '[OPTIONS]\nenable: 1\npolicy_in: DROP\npolicy_out: DROP\n\n[RULES]\n'
    echo "OUT ACCEPT -p tcp -dest $router -dport $router_port # router chat API (scoped key)"
    for w in $workers; do
      echo "OUT ACCEPT -p tcp -dest $w -dport $worker_port # CPU worker (throwaway worker key)"
    done
    ;;
  build)
    printf '[OPTIONS]\nenable: 1\npolicy_in: DROP\npolicy_out: ACCEPT\n\n[RULES]\n'
    echo "OUT DROP -dest 10.0.0.0/8 # RFC1918: sandbox gateway (the host), other vnets"
    echo "OUT DROP -dest 172.16.0.0/12 # RFC1918: VCF lab, CPU workers"
    echo "OUT DROP -dest 192.168.0.0/16 # RFC1918: the LAN, the host, every LXC"
    echo "OUT DROP -dest 100.64.0.0/10 # CGNAT"
    echo "OUT DROP -dest 169.254.0.0/16 # link-local and metadata"
    ;;
  *) die "usage: wf-sandbox-policy.sh build|locked" ;;
esac
