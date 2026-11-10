#!/usr/bin/env bash
# redteam-egress-lock.sh — restrict LXC 158 (ARTEX) outbound to the authorised
# pentest scope ONLY. This is the HARD scope boundary: ARTEX's in-app scope
# checks are advisory, so nothing but this stops an out-of-scope request from
# leaving the box. ARTEX runs entirely in containers, so DOCKER-USER is the
# operative chain; OUTPUT is kept for defence-in-depth (the host must not reach
# the internet either).
#
# ALLOWED outbound (everything else REJECTed, fast-fail):
#   - the 5 colo targets (the engagement scope)
#   - 192.168.6.153  the router = the qwen3.8-redteam model (ARTEX needs it)
#   - 192.168.6.155  searxng + docs MCP (local research; drop this line to
#                    fully isolate from the cluster — ARTEX may not need it)
#   - 192.168.6.1:53 DNS
#   - loopback + established/related
#
# REJECTed (deliberately): the broad internet AND the rest of the cluster
# (151/153-except-router/154/156 and the Proxmox host .175) — so this offensive
# box cannot scan your own production LXCs, and a compromised target cannot
# pivot inward. Idempotent; safe to re-run. Runs after docker.
set -euo pipefail

IPT=iptables            # iptables-nft on Debian bookworm
EXT_IF=eth0
DNS=192.168.6.1
ROUTER=192.168.6.153
MCP=192.168.6.155
TARGETS=(38.107.232.37 38.107.232.39 38.107.232.51 38.107.232.103 38.107.232.210)

# ---- OUTPUT chain (traffic originating on the LXC host itself) ----
$IPT -F OUTPUT
$IPT -P OUTPUT ACCEPT
$IPT -A OUTPUT -o lo -j ACCEPT
$IPT -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
$IPT -A OUTPUT -o "$EXT_IF" -p udp --dport 53 -d "$DNS" -j ACCEPT
$IPT -A OUTPUT -o "$EXT_IF" -p tcp --dport 53 -d "$DNS" -j ACCEPT
$IPT -A OUTPUT -o "$EXT_IF" -d "$ROUTER" -j ACCEPT
$IPT -A OUTPUT -o "$EXT_IF" -d "$MCP" -j ACCEPT
for t in "${TARGETS[@]}"; do
  $IPT -A OUTPUT -o "$EXT_IF" -d "$t" -j ACCEPT
done
$IPT -A OUTPUT -o "$EXT_IF" -p tcp -j REJECT --reject-with tcp-reset
$IPT -A OUTPUT -o "$EXT_IF" -j REJECT --reject-with icmp-port-unreachable

# ---- DOCKER-USER chain (ARTEX + tool containers, seen in FORWARD) ----
$IPT -F DOCKER-USER
$IPT -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
$IPT -A DOCKER-USER -o "$EXT_IF" -p udp --dport 53 -d "$DNS" -j RETURN
$IPT -A DOCKER-USER -o "$EXT_IF" -p tcp --dport 53 -d "$DNS" -j RETURN
$IPT -A DOCKER-USER -o "$EXT_IF" -d "$ROUTER" -j RETURN
$IPT -A DOCKER-USER -o "$EXT_IF" -d "$MCP" -j RETURN
for t in "${TARGETS[@]}"; do
  $IPT -A DOCKER-USER -o "$EXT_IF" -d "$t" -j RETURN
done
$IPT -A DOCKER-USER -o "$EXT_IF" -p tcp -j REJECT --reject-with tcp-reset
$IPT -A DOCKER-USER -o "$EXT_IF" -j REJECT --reject-with icmp-port-unreachable
$IPT -A DOCKER-USER -j RETURN

# ---- IPv6: closed but fast-fail ----
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -P OUTPUT ACCEPT 2>/dev/null || true
  ip6tables -P FORWARD ACCEPT 2>/dev/null || true
  ip6tables -F OUTPUT 2>/dev/null || true
  ip6tables -F FORWARD 2>/dev/null || true
  ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
  ip6tables -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset 2>/dev/null || true
  ip6tables -A OUTPUT -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null || true
  ip6tables -A FORWARD -p tcp -j REJECT --reject-with tcp-reset 2>/dev/null || true
  ip6tables -A FORWARD -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null || true
fi

echo "egress lock applied: targets=${TARGETS[*]} + router($ROUTER) + mcp($MCP) + dns($DNS); all else REJECT"
