#!/bin/bash
# Re-import the three developer VMs after the VCF rebuild, onto host-local
# storage, with their original MAC addresses.
#
# Counterpart to the export taken before the rebuild. See
# docs/devvm-restore-record.md for why each value is what it is.
#
# The MAC step is the point of this script. `govc import.ovf` assigns a FRESH
# MAC, and an OVF descriptor does not carry the original. Restoring the recorded
# MAC is done here explicitly, because "the VM came up" and "the VM came up with
# the right identity" look identical until something downstream disagrees.
#
# Credentials: the vCenter password is read from stdin, never argv, so it does
# not appear in any process list. Run as:
#     printf '%s\n' "$PASSWORD" | ./devvm-restore.sh
set -u

read -r GOVC_PW
export GOVC_URL="${GOVC_URL:-https://172.16.10.129/sdk}"
export GOVC_USERNAME="${GOVC_USERNAME:-administrator@vsphere.local}"
export GOVC_PASSWORD="$GOVC_PW"
export GOVC_INSECURE=1
export GOVC_DATACENTER="${GOVC_DATACENTER:-lab01-datacenter}"
# A cached session makes a wrong password look correct. Always authenticate.
export GOVC_PERSIST_SESSION=false

G="${GOVC:-/usr/local/bin/govc}"
SRC="${SRC:-/tank/backups/devvm-export}"
POOL="${POOL:-/lab01-datacenter/host/lab01-cluster-001/Resources}"
FOLDER="${FOLDER:-/lab01-datacenter/vm}"

# vm : host-local datastore : portgroup : recorded MAC
# MACs captured 2026-09-26 from the running VMs, before the rebuild.
SPEC="devvm01 hyp01-local dev-vm1-vlan70 00:50:56:bd:89:9d
devvm02 hyp02-local dev-vm2-vlan71 00:50:56:bd:fb:b1
devvm03 hyp03-local dev-vm3-vlan72 00:50:56:bd:58:53"

die() { echo "  FATAL: $*" >&2; exit 1; }

echo "### preflight ###"
$G about >/dev/null 2>&1 || die "cannot authenticate to $GOVC_URL"
echo "  vCenter reachable"
while read -r VM DS PG MAC; do
  [ -z "$VM" ] && continue
  [ -f "$SRC/$VM/$VM.ovf" ] || die "missing export descriptor $SRC/$VM/$VM.ovf"
  $G datastore.info "$DS" >/dev/null 2>&1 || die "datastore $DS not found"
  # The portgroup must exist FIRST. Importing onto a missing network silently
  # lands the NIC somewhere else, and the tunnel then cannot reach Cloudflare.
  $G find / -type n -name "$PG" 2>/dev/null | grep -q . || die "portgroup $PG not found -- recreate it before restoring"
  echo "  $VM: export, datastore $DS and portgroup $PG all present"
done <<< "$SPEC"

echo "### import ###"
while read -r VM DS PG MAC; do
  [ -z "$VM" ] && continue
  if $G vm.info "$VM" 2>/dev/null | grep -q "Name:"; then
    echo "  $VM already exists in inventory -- skipping"
    continue
  fi
  echo "  $VM: importing to $DS"
  $G import.ovf -ds "$DS" -pool "$POOL" -folder "$FOLDER" -name "$VM" \
      -options <(printf '{"NetworkMapping":[{"Name":"","Network":"%s"}]}' "$PG") \
      "$SRC/$VM/$VM.ovf" 2>&1 | sed 's/^/      /' \
    || die "import of $VM failed"

  echo "  $VM: pinning MAC to $MAC"
  # addressType=Manual, or vSphere regenerates it on the next power cycle.
  $G vm.network.change -vm "$VM" -net "$PG" -net.address "$MAC" \
      -net.addressType manual ethernet-0 2>&1 | sed 's/^/      /' \
    || die "could not set MAC on $VM"

  got=$($G device.info -vm "$VM" ethernet-0 2>/dev/null | awk '/MAC Address:/{print $3}')
  [ "$got" = "$MAC" ] || die "$VM MAC is '$got', expected '$MAC'"
  echo "  $VM: MAC verified $got"
done <<< "$SPEC"

echo "### power on ###"
# Deliberately no CD-ROM is attached. Re-attaching a seed ISO with a new
# instance-id makes cloud-init treat the VM as new and rewrite guest state.
while read -r VM DS PG MAC; do
  [ -z "$VM" ] && continue
  $G vm.power -on "$VM" 2>&1 | sed 's/^/      /'
done <<< "$SPEC"

echo "### verify ###"
while read -r VM DS PG MAC; do
  [ -z "$VM" ] && continue
  printf "  %-9s power=%s mac=%s\n" "$VM" \
    "$($G vm.info "$VM" 2>/dev/null | awk '/Power state/{print $3}')" \
    "$($G device.info -vm "$VM" ethernet-0 2>/dev/null | awk '/MAC Address:/{print $3}')"
done <<< "$SPEC"

cat <<'NOTE'

  Still to confirm, and NOT provable from here:
    - all three tunnels healthy in the Cloudflare dashboard
    - ssh through the Access ProxyCommand reaches sshd
    - factory has no sudo; viktor's sudo needs no password
  Ping is not a test: these VMs are isolated by design and nothing may reach
  them on the management network.
NOTE
