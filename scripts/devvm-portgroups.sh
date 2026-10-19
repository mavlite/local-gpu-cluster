#!/bin/bash
# Recreate the three developer-VM portgroups on the VCF-built VDS.
#
# Run this AFTER bring-up reaches a terminal state, not during it: VCF owns the
# VDS while it is still configuring, and it also reconciles the switch against
# its own inventory. Adding portgroups mid-deployment invites a conflict with
# the thing that is still writing to it.
#
# These are plain VLAN-backed distributed portgroups, not NSX segments. The
# isolation lives on the CRS309 (deny-all-private plus the one narrow accept),
# which survived the rebuild untouched -- so a correct VLAN tag here is the only
# thing standing between the dev VMs and working egress.
#
# Credentials on stdin, never argv:
#     printf '%s\n' "$SSO_PASSWORD" | ./devvm-portgroups.sh
set -u

read -r GOVC_PW
export GOVC_URL="${GOVC_URL:-https://172.16.10.129/sdk}"
export GOVC_USERNAME="${GOVC_USERNAME:-administrator@vsphere.local}"
export GOVC_PASSWORD="$GOVC_PW"
export GOVC_INSECURE=1
export GOVC_DATACENTER="${GOVC_DATACENTER:-lab01-datacenter}"
export GOVC_PERSIST_SESSION=false

G="${GOVC:-/usr/local/bin/govc}"

# name : vlan   -- must match docs/devvm-restore-record.md and the CRS309 VLANs
SPEC="dev-vm1-vlan70 70
dev-vm2-vlan71 71
dev-vm3-vlan72 72"

die() { echo "  FATAL: $*" >&2; exit 1; }

$G about >/dev/null 2>&1 || die "cannot authenticate to $GOVC_URL"

# Find the VDS rather than hardcoding its name: VCF generates it, and the name
# carries a cluster-specific suffix that changes between deployments.
DVS=$($G find / -type DistributedVirtualSwitch 2>/dev/null | head -1)
[ -n "$DVS" ] || die "no distributed switch found -- has bring-up created it yet?"
echo "  switch: $DVS"

# One port per VM plus headroom. Deliberately small: these are single-NIC VMs,
# and an oversized portgroup is just wasted state.
PORTS="${PORTS:-8}"

while read -r NAME VLAN; do
  [ -z "$NAME" ] && continue
  if $G find / -type n -name "$NAME" 2>/dev/null | grep -q .; then
    echo "  $NAME: already exists -- leaving alone"
    continue
  fi
  echo "  $NAME: creating (VLAN $VLAN, $PORTS ports)"
  $G dvs.portgroup.add -dvs "$DVS" -type earlyBinding -nports "$PORTS" \
     -vlan "$VLAN" "$NAME" 2>&1 | sed 's/^/      /' \
    || die "could not create $NAME"
done <<< "$SPEC"

echo "### verify ###"
fail=0
while read -r NAME VLAN; do
  [ -z "$NAME" ] && continue
  # `object.collect -s <path> config.defaultPortConfig.vlan.vlanId` returns
  # EMPTY for every portgroup, including VCF's own -- the vlan property is a
  # polymorphic VlanSpec and the scalar form cannot reach into it. Reading the
  # whole config as JSON and finding vlanId works. The first version of this
  # check reported MISMATCH on three correctly-tagged portgroups.
  got=$($G object.collect -json "/$GOVC_DATACENTER/network/$NAME" config 2>/dev/null         | python3 -c "
import json,sys
def f(n):
    if isinstance(n,dict):
        if 'vlanId' in n: return n['vlanId']
        for v in n.values():
            r=f(v)
            if r is not None: return r
    elif isinstance(n,list):
        for v in n:
            r=f(v)
            if r is not None: return r
    return None
try: print(f(json.load(sys.stdin)))
except Exception: pass
" 2>/dev/null)
  if [ "$got" = "$VLAN" ]; then
    printf "  %-16s VLAN %s  OK\n" "$NAME" "$got"
  else
    printf "  %-16s VLAN '%s' but expected %s  MISMATCH\n" "$NAME" "${got:-unreadable}" "$VLAN"
    fail=1
  fi
done <<< "$SPEC"

[ "$fail" = 0 ] || die "at least one portgroup has the wrong VLAN -- fix before importing"
echo "  all three portgroups present with the expected VLAN tags"
echo "  next: printf '%s\\n' \"\$SSO_PASSWORD\" | ./devvm-restore.sh"
