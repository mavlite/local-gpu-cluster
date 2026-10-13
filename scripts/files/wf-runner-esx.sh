#!/usr/bin/env bash
# wf-runner-esx.sh -- the ESXi half of the runner rollout (coding engine phase 2,
# docs/superpowers/plans/2026-10-13-coding-engine-phase2-runners.md): register a runner VM on a Minisforum host
# and power it on safely. Host-direct govc only, so it works in Minimal mode with vCenter down. Called by
# scripts/79-vm-wf-runner-image.sh deploy|power-on; needs only govc, sha256sum and python3.
#
#   deploy HOST DS NAME IP VMDK  import VMDK into [DS] NAME/, register NAME powered off (pvscsi, vmxnet3 on the
#                                VM-Mgmt port group), static IP via cloud-init guestinfo, sched.mem.enableTiering=FALSE.
#                                Re-running on a registered, powered-off NAME re-applies the configuration only.
#                                The full memory reservation is attempted too, but a host that vCenter manages
#                                refuses resource settings host-direct while vCenter is up -- power-on sets it then.
#   power-on HOST NAME           set the reservation if it is missing, refuse unless it covers the VM's memory and
#                                enableTiering is FALSE, and refuse if starting the runner would take the host past
#                                80% of its DRAM (crash-hardening rule 6: never power on a large VM above ~80%).
# Credentials: GOVC_USERNAME / GOVC_PASSWORD from the environment only (the caller pipes them in; never argv).
set -Eeuo pipefail

RUNNER_MEM_MB="${RUNNER_MEM_MB:-16384}"
RUNNER_CORES="${RUNNER_CORES:-8}"
RUNNER_NET="${RUNNER_NET:-SDDC-DPortGroup-VM-Mgmt}"
RUNNER_GW=172.16.10.1
RUNNER_DNS="172.16.10.150, 1.1.1.1"
HOST_DRAM_MB="${RUNNER_HOST_DRAM_MB:-96256}"   # installed DRAM; with tiering on the host reports DRAM+tier as total
HOST_MAX_PCT="${RUNNER_HOST_MAX_PCT:-80}"
PYTHON="${PYTHON:-python3}"

die()  { echo "[err ] $*" >&2; exit 1; }
warn() { echo "[warn] $*" >&2; }
ok()   { echo "[ ok ] $*"; }
step() { echo "==> $*"; }

check_name() { [[ "$1" =~ ^wf-runner-0[1-9]$ ]] || die "runner names are wf-runner-0N: $1"; }

vlan10_ip() {
  [[ "$1" =~ ^172\.16\.10\.([1-9][0-9]{0,2})$ ]] && (( BASH_REMATCH[1] <= 254 ))
}

govc_env() {
  vlan10_ip "$1" || die "ESXi hosts are on VLAN 10 (172.16.10.0/24): $1"
  [[ -n "${GOVC_USERNAME:-}" && -n "${GOVC_PASSWORD:-}" ]] || die "GOVC_USERNAME/GOVC_PASSWORD must come from the environment"
  export GOVC_URL="https://$1" GOVC_INSECURE=1 GOVC_PERSIST_SESSION=false
}

vm_prop() { govc object.collect -s "vm/$1" "$2"; }

vm_exists() {
  local info
  info="$(govc vm.info "$1" 2>/dev/null || true)"
  [[ "$info" == *"Name:"* ]]
}

tiering_off() {
  local info
  info="$(govc vm.info -e "$1")" || return 1
  grep -Eq '^[[:space:]]*sched\.mem\.enableTiering:[[:space:]]+FALSE[[:space:]]*$' <<<"$info"
}

cloud_metadata() {   # NAME IP -> base64 cloud-init metadata with a static v2 network config
  printf 'instance-id: %s\nlocal-hostname: %s\nnetwork:\n  version: 2\n  ethernets:\n    nics:\n      match: {name: "en*"}\n      addresses: [%s/24]\n      routes: [{to: default, via: %s}]\n      nameservers: {addresses: [%s]}\n' \
    "$1" "$1" "$2" "$RUNNER_GW" "$RUNNER_DNS" | base64 -w0
}

cmd_deploy() {
  local host="$1" ds="$2" name="$3" ip="$4" vmdk="$5"
  check_name "$name"
  vlan10_ip "$ip" || die "runner IPs are on VLAN 10 (172.16.10.0/24): $ip"
  [[ "$ds" =~ ^[A-Za-z0-9._-]+$ ]] || die "bad datastore name: $ds"
  govc_env "$host"

  if vm_exists "$name"; then
    [[ "$(vm_prop "$name" runtime.powerState)" == poweredOff ]] || die "$name exists on $host and is not powered off"
    step "$name already registered on $host: re-applying its configuration"
  else
    [[ -f "$vmdk" && -f "$vmdk.sha256" ]] || die "missing $vmdk or $vmdk.sha256"
    [[ "$(sha256sum "$vmdk" | cut -d' ' -f1)" == "$(tr -d '[:space:]' < "$vmdk.sha256")" ]] || die "VMDK checksum mismatch"
    step "import $vmdk -> [$ds] $name/wf-runner.vmdk"
    govc import.vmdk -ds "$ds" "$vmdk" "$name/wf-runner.vmdk" >/dev/null || die "import failed"
    step "register $name (powered off)"
    govc vm.create -m "$RUNNER_MEM_MB" -c "$RUNNER_CORES" -g debian12_64Guest -ds "$ds" \
      -disk "$name/wf-runner.vmdk" -disk.controller pvscsi \
      -net "$RUNNER_NET" -net.adapter vmxnet3 -on=false "$name" || die "vm.create failed"
  fi

  govc vm.change -vm "$name" -e "guestinfo.metadata=$(cloud_metadata "$name" "$ip")" \
    -e "guestinfo.metadata.encoding=base64" -e "sched.mem.enableTiering=FALSE" \
    || die "vm.change (guestinfo, sched.mem.enableTiering) failed"
  tiering_off "$name" || die "sched.mem.enableTiering did not read back FALSE on $name"
  govc vm.change -vm "$name" -mem.reservation "$RUNNER_MEM_MB" 2>/dev/null \
    || warn "memory reservation refused host-direct (vCenter is managing $host); power-on sets it"

  local hw
  hw="$(vm_prop "$name" config.version)"
  [[ "${hw#vmx-}" =~ ^[0-9]+$ ]] && (( ${hw#vmx-} >= 22 )) || warn "$name hardware is $hw; vmx-22+ expected"
  ok "$name registered on $host ([$ds]), IP $ip, hardware $hw, reservation $(vm_prop "$name" config.memoryAllocation.reservation) MB, NOT powered on"
}

cmd_power_on() {
  local host="$1" name="$2"
  check_name "$name"
  govc_env "$host"
  vm_exists "$name" || die "$name is not registered on $host -- run deploy first"
  [[ "$(vm_prop "$name" runtime.powerState)" == poweredOff ]] || die "$name is not powered off"
  tiering_off "$name" || die "$name: sched.mem.enableTiering is not FALSE -- run deploy first"

  local mem res
  mem="$(vm_prop "$name" config.hardware.memoryMB)"
  res="$(vm_prop "$name" config.memoryAllocation.reservation)"
  [[ "$mem" =~ ^[1-9][0-9]*$ ]] || die "cannot read $name's memory size (got '$mem')"
  [[ "$res" =~ ^[0-9]+$ ]] || die "cannot read $name's memory reservation (got '$res')"
  if (( res < mem )); then
    govc vm.change -vm "$name" -mem.reservation "$mem" \
      || die "cannot set the ${mem} MB reservation host-direct while vCenter manages $host -- power on in Minimal mode"
    res="$(vm_prop "$name" config.memoryAllocation.reservation)"
    (( res >= mem )) || die "$name reservation reads back ${res} MB, not ${mem} MB"
  fi

  # "in use" is the larger of what the host has consumed and what its running VMs are sized for: a VM started
  # moments ago (another runner, a worker) has not touched its pages yet, so overallMemoryUsage alone lags
  local hjson vjson consumed committed used
  hjson="$(govc host.info -json)" || die "cannot read the memory in use on $host"
  vjson="$(govc vm.info -json '*')" || die "cannot read the running VMs' memory on $host"
  consumed="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["hostSystems"][0]["summary"]["quickStats"]["overallMemoryUsage"])' <<<"$hjson" 2>/dev/null)" || consumed=""
  committed="$("$PYTHON" -c 'import json,sys; print(sum(v["config"]["hardware"]["memoryMB"] for v in json.load(sys.stdin)["virtualMachines"] or [] if v["runtime"]["powerState"] == "poweredOn"))' <<<"$vjson" 2>/dev/null)" || committed=""
  [[ "$consumed" =~ ^[0-9]+$ && "$committed" =~ ^[0-9]+$ ]] || die "cannot read the memory in use on $host"
  used=$(( consumed > committed ? consumed : committed ))
  if (( (used + mem) * 100 > HOST_DRAM_MB * HOST_MAX_PCT )); then
    die "host $host: ${used} MB in use (consumed ${consumed}, running VMs ${committed}) + ${mem} MB would pass ${HOST_MAX_PCT}% of ${HOST_DRAM_MB} MB DRAM -- refusing (crash-hardening rule 6)"
  fi
  govc vm.power -on "$name" >/dev/null || die "power-on failed"
  ok "$name powered on (host at $(( (used + mem) * 100 / HOST_DRAM_MB ))% of DRAM with it)"
}

case "${1:-}" in
  deploy)   [[ $# -eq 6 ]] || die "usage: $0 deploy HOST DS NAME IP VMDK"; cmd_deploy "$2" "$3" "$4" "$5" "$6" ;;
  power-on) [[ $# -eq 3 ]] || die "usage: $0 power-on HOST NAME"; cmd_power_on "$2" "$3" ;;
  *) echo "usage: $0 deploy HOST DS NAME IP VMDK | power-on HOST NAME" >&2; exit 2 ;;
esac
