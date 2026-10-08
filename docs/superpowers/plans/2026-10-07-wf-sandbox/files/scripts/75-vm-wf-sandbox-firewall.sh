#!/usr/bin/env bash
# 75-vm-wf-sandbox-firewall.sh build|locked — install the workforce sandbox's PVE firewall policy.
#
#   build   provisioning: internet only, no private space (files/wf-sandbox-policy.sh)
#   locked  measured runs: router chat port + the three CPU workers, nothing else
#
# Same guards as 73-vm-tester-firewall.sh: the NIC must be filtered and on the sandbox bridge, IPv6
# forwarding must be off (the policy is IPv4-only), no unexpected guest may become filtered, and the
# firewall must be PROVEN enabled/running with this guest's fwbr filter bridge present when it runs.
# Policy changes apply to a running VM's existing conntrack entries only for new connections:
# run `locked` before handing the VM to the harness, never mid-run.
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"
require_root
require_pve_host
load_config

MODE="${1:-}"
[[ "$MODE" == "build" || "$MODE" == "locked" ]] || die "usage: 75-vm-wf-sandbox-firewall.sh build|locked"
WF_VMID="${WF_VMID:-176}"
WF_BRIDGE="${WF_BRIDGE:-vmbrwf}"
# Guests whose NICs are filtered by design (70: SE QA, 72: tester, this sandbox).
WF_FILTERED_OK="${WF_FILTERED_OK:-170 172 $WF_VMID}"
require_cmd pve-firewall qm ip

step "1 — preflight"
conf="/etc/pve/qemu-server/${WF_VMID}.conf"
[[ -f "$conf" ]] || die "VM $WF_VMID does not exist — run 74-vm-wf-sandbox.sh first"
nic="$(grep -E '^net[0-9]+:.*firewall=1' "$conf" | head -1)"
[[ -n "$nic" ]] || die "VM $WF_VMID has no NIC with firewall=1"
[[ "$nic" == *"bridge=${WF_BRIDGE}"* ]] || die "VM $WF_VMID's filtered NIC is not on ${WF_BRIDGE}: $nic"
[[ "$(grep -cE '^net[0-9]+:' "$conf")" == "1" ]] || die "VM $WF_VMID has more than one NIC"
for ifc in all vmbr0 "$WF_BRIDGE"; do
  p="/proc/sys/net/ipv6/conf/${ifc}/forwarding"
  [[ ! -r "$p" || "$(cat "$p")" == "0" ]] || die "net.ipv6.conf.${ifc}.forwarding is not 0. Stop."
done
ok "one filtered NIC on ${WF_BRIDGE}; IPv6 forwarding off"

step "2 — blast-radius guard"
others=""
for c in /etc/pve/lxc/*.conf /etc/pve/qemu-server/*.conf; do
  [[ -e "$c" ]] || continue
  id="$(basename "$c" .conf)"
  [[ " $WF_FILTERED_OK " == *" $id "* ]] && continue
  grep -qE '^net[0-9]+:.*firewall=1' "$c" && others+=" $id"
done
[[ -z "$others" ]] || die "these guests also have firewall=1 and would start being filtered:$others"
ok "only expected guests are filtered ($WF_FILTERED_OK)"

step "3 — write the $MODE policy"
policy="$(bash "$LGC_DIR/files/wf-sandbox-policy.sh" "$MODE")" || die "policy render failed"
write_file_if_changed "/etc/pve/firewall/${WF_VMID}.fw" 0640 <<<"$policy"

step "4 — reload and prove enforcement"
pve-firewall compile >/dev/null || die "pve-firewall failed to compile"
pve-firewall restart
sleep 2
pve-firewall status 2>&1 | grep -q "enabled/running" \
  || die "pve-firewall is not enabled/running — VM $WF_VMID would be unconfined"
if [[ "$(qm status "$WF_VMID" | awk '{print $2}')" == "running" ]]; then
  ip -br link show type bridge | grep -q "^fwbr${WF_VMID}i" \
    || die "VM $WF_VMID is running but has no fwbr${WF_VMID}i* filter bridge — it is NOT filtered"
  ok "fwbr${WF_VMID}i present: VM $WF_VMID filtered"
else
  skip "VM $WF_VMID not running; its filter bridge appears at start"
fi
diff <(printf '%s\n' "$policy") "/etc/pve/firewall/${WF_VMID}.fw" >/dev/null \
  || die "/etc/pve/firewall/${WF_VMID}.fw does not match the rendered $MODE policy"
ok "$MODE policy active for VM $WF_VMID"
