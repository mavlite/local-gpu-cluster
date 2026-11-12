#!/usr/bin/env bash
# redteam-egress-lock.sh — restrict LXC 158 (ARTEX) outbound to the authorised
# pentest scope ONLY. This is the HARD scope boundary: ARTEX's in-app scope
# checks are advisory (its task_scope even auto-adds out-of-scope IPs that a
# target's DNS points at), so nothing but this stops an out-of-scope request
# from leaving the box. ARTEX runs entirely in containers, so DOCKER-USER is the
# operative chain; OUTPUT is kept for defence-in-depth (the host must not reach
# the internet either).
#
# ALLOWED outbound (everything else REJECTed, fast-fail):
#   - the 5 colo targets (the engagement scope)
#   - 192.168.6.153  the router = the qwen3.8-redteam model (ARTEX needs it)
#   - 192.168.6.1:53 DNS
#   - loopback + established/related
#
# NOT allowed (deliberately): 192.168.6.155. It hosts a tinyproxy (:3128) that
# accepts all of 192.168.6.0/24 and forwards to github.com/githubusercontent.com/
# crt.sh/nvd/circl — allowlisting .155 was an internet path around this lock.
# ARTEX never used .155 (0 packets on its rule before removal, 2026-11-10).
#
# REJECTed: the broad internet AND the rest of the cluster (151/154/155/156 and
# the Proxmox host .175) — so this offensive box cannot scan production LXCs,
# and a compromised target cannot pivot inward.
#
# REJECT uses icmp-admin-prohibited (not tcp-reset): a reset is indistinguishable
# from a genuinely closed port, so the agent recorded firewall-blocked hosts as
# "1000 closed ports" and escalated its scans. admin-prohibited shows up as
# "filtered (admin-prohibited)" in nmap / "No route to host" in curl — still
# fast-fail, but unmistakably the firewall.
#
# AUDIT: every new outbound connection that the lock will reject is recorded,
# per destination IP + protocol + port with a packet counter, in the nftables
# table `inet redteam_egress_audit` (LOG does not work in an LXC netns while the
# host has nf_log_all_netns=0). The audit sets survive re-runs of this script;
# read them with redteam-egress-report.sh.
#
# Applied ATOMICALLY (one iptables-restore transaction + one nft transaction):
# the old flush-then-append sequence left DOCKER-USER empty — i.e. open — for a
# moment on every re-run and every docker restart. Idempotent; safe to re-run.
set -euo pipefail

EXT_IF=eth0
DNS=192.168.6.1
ROUTER=192.168.6.153
TARGETS=(38.107.232.37 38.107.232.39 38.107.232.51 38.107.232.103 38.107.232.210)
AUDIT_TABLE=redteam_egress_audit
AUDIT_TIMEOUT=30d          # how long a denied destination stays in the audit set
REJECT_ICMP=icmp-admin-prohibited

# ---- iptables payload (OUTPUT + DOCKER-USER), one atomic commit ----
filter_rules() {
  local chain=$1 ok=$2   # ok = ACCEPT (OUTPUT) or RETURN (DOCKER-USER)
  [[ $chain == OUTPUT ]] && echo "-A OUTPUT -o lo -j ACCEPT"
  echo "-A $chain -m conntrack --ctstate ESTABLISHED,RELATED -j $ok"
  echo "-A $chain -o $EXT_IF -p udp --dport 53 -d $DNS -j $ok"
  echo "-A $chain -o $EXT_IF -p tcp --dport 53 -d $DNS -j $ok"
  echo "-A $chain -o $EXT_IF -d $ROUTER -j $ok"
  local t
  for t in "${TARGETS[@]}"; do
    echo "-A $chain -o $EXT_IF -d $t -j $ok"
  done
  echo "-A $chain -o $EXT_IF -j REJECT --reject-with $REJECT_ICMP"
  [[ $chain == DOCKER-USER ]] && echo "-A DOCKER-USER -j RETURN"
  return 0
}

iptables_payload() {
  echo "*filter"
  echo ":OUTPUT ACCEPT [0:0]"
  echo ":DOCKER-USER - [0:0]"
  echo "-F OUTPUT"
  echo "-F DOCKER-USER"
  filter_rules OUTPUT ACCEPT
  filter_rules DOCKER-USER RETURN
  echo "COMMIT"
}

# ---- nft audit table: count (never decide) what the lock will reject ----
# Base chains run just before the filter-priority chains that hold OUTPUT and
# DOCKER-USER; they only record, so the verdict still comes from iptables.
# `add` is a no-op on existing objects, so the denied* sets (the audit trail)
# keep their contents across re-runs; only the allowlist + rules are rebuilt.
nft_payload() {
  local allowed
  allowed=$(IFS=,; echo "${ROUTER},${TARGETS[*]}")
  cat <<EOF
add table inet $AUDIT_TABLE
add set inet $AUDIT_TABLE allowed { type ipv4_addr; }
add set inet $AUDIT_TABLE denied { type ipv4_addr . inet_proto . inet_service; flags dynamic,timeout; timeout $AUDIT_TIMEOUT; size 65536; }
add set inet $AUDIT_TABLE denied_other { type ipv4_addr . inet_proto; flags dynamic,timeout; timeout $AUDIT_TIMEOUT; size 4096; }
add chain inet $AUDIT_TABLE audit
add chain inet $AUDIT_TABLE audit_out { type filter hook output priority filter - 5; policy accept; }
add chain inet $AUDIT_TABLE audit_fwd { type filter hook forward priority filter - 5; policy accept; }
flush set inet $AUDIT_TABLE allowed
add element inet $AUDIT_TABLE allowed { $allowed }
flush chain inet $AUDIT_TABLE audit
flush chain inet $AUDIT_TABLE audit_out
flush chain inet $AUDIT_TABLE audit_fwd
add rule inet $AUDIT_TABLE audit_out oifname "$EXT_IF" ct state new jump audit
add rule inet $AUDIT_TABLE audit_fwd oifname "$EXT_IF" ct state new jump audit
add rule inet $AUDIT_TABLE audit meta nfproto != ipv4 return
add rule inet $AUDIT_TABLE audit ip daddr @allowed return
add rule inet $AUDIT_TABLE audit ip daddr $DNS meta l4proto { tcp, udp } th dport 53 return
add rule inet $AUDIT_TABLE audit meta l4proto { tcp, udp } update @denied { ip daddr . meta l4proto . th dport counter } counter
add rule inet $AUDIT_TABLE audit meta l4proto != { tcp, udp } update @denied_other { ip daddr . meta l4proto counter } counter
EOF
}

# Enforcement first (IPv4 then IPv6); the audit table is applied last so a
# failure there can never leave the lock half-applied.
iptables_payload | iptables-restore --noflush

# ---- IPv6: closed but fast-fail ----
if command -v ip6tables-restore >/dev/null 2>&1; then
  ip6tables-restore --noflush <<'EOF'
*filter
:OUTPUT ACCEPT [0:0]
:FORWARD ACCEPT [0:0]
-F OUTPUT
-F FORWARD
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -j REJECT --reject-with icmp6-adm-prohibited
-A FORWARD -j REJECT --reject-with icmp6-adm-prohibited
COMMIT
EOF
fi

nft_payload | nft -f -

echo "egress lock applied: targets=${TARGETS[*]} + router($ROUTER) + dns($DNS); all else REJECT ($REJECT_ICMP); audit=inet $AUDIT_TABLE"
