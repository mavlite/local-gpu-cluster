#!/usr/bin/env bash
# RETIRED 2026-10-27: the VM this script builds/operates was destroyed (see docs/wf-sandbox-runbook.md). Do not run.
# 79-vm-wf-runner-image.sh -- build the runner image for the Minisforum nodes (coding engine phase 2,
# docs/superpowers/plans/2026-10-13-coding-engine-phase2-runners.md). Runs on the Proxmox host as root.
#
#   fetch-constraints   start sandbox VM 176 if needed (its policy stays as it is), copy /opt/wfpy/constraints.txt
#                       to $RUNNER_DIR/constraints.txt, and stop 176 again if this command started it
#   create              VM 177 from the Debian 13 *generic* cloud image (verified against Debian's SHA512SUMS),
#                       on vmbr0 with DHCP (internet for the build), guest agent via a cloud-init vendor snippet.
#                       Not started.
#   provision           start 177, wait for the guest agent, push the inputs, run wf-runner-provision.sh
#                       (WF_RUNNER_TEMPLATE=0: the parity test still runs on 177), print /etc/wf-runner.json
#   parity RUN BUNDLES  grade a harvested run's patches on 177 with the Docker grader and compare pass/fail per
#                       task with the run's own records (graded on VM 176); exit 1 on any difference
#   export              clear 177's instance identity (cloud-init state, machine-id), shut it down, and convert its
#                       disk to a streamOptimized VMDK + sha256 in $RUNNER_DIR (govc import.vmdk on each host later)
#   deploy HOST DS NAME IP   files/wf-runner-esx.sh on ESXi host HOST (host-direct, works with vCenter down): import
#                       the VMDK into datastore DS, register NAME POWERED OFF (8 vCPU, 16 GB, pvscsi, vmxnet3 on
#                       SDDC-DPortGroup-VM-Mgmt), static IP via cloud-init guestinfo, sched.mem.enableTiering=FALSE;
#                       the full memory reservation only if the host accepts it (vCenter-managed hosts refuse it
#                       host-direct while vCenter is up)
#   power-on HOST NAME  set the reservation if missing; refuse without it, without enableTiering=FALSE, or if the
#                       runner would take the host past 80% of DRAM (crash-hardening rule 6)
# ESXi credentials: GOVC_USERNAME / GOVC_PASSWORD in the environment only (the caller pipes them in; never argv).
#
# Why generic, not genericcloud like VM 176: the image is later imported onto ESXi, and genericcloud's cloud kernel
# drops most hardware drivers (pvscsi, vmxnet3). Why Proxmox at all: VLAN 10 has no internet.
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"
require_root
require_pve_host

CMD="${1:-}"
RUNNER_VMID="${RUNNER_VMID:-177}"
RUNNER_NAME="${RUNNER_NAME:-wf-runner-build}"
RUNNER_STORAGE="${RUNNER_STORAGE:-tank-lxc}"
RUNNER_SNIPPET_STORAGE="${RUNNER_SNIPPET_STORAGE:-local}"
RUNNER_DIR="${RUNNER_DIR:-/root/wf/runner-image}"
RUNNER_MEM_MB="${RUNNER_MEM_MB:-16384}"
RUNNER_CORES="${RUNNER_CORES:-8}"
RUNNER_DISK_GB="${RUNNER_DISK_GB:-40}"
DEBIAN_BUILD="${RUNNER_DEBIAN_BUILD:-20260518-2482}"
IMAGE_NAME="debian-13-generic-amd64-${DEBIAN_BUILD}.qcow2"
IMAGE_BASE="https://cloud.debian.org/images/cloud/trixie/${DEBIAN_BUILD}"
SANDBOX_VMID="${WF_VMID:-176}"
PUSH=/root/wf-push
require_cmd qm pvesm pvesh wget sha512sum python3 base64 split stat tar

WF_VMID="$RUNNER_VMID"   # the helpers below act on this VM

# Run argv in the guest; print its stdout; return its exit code. (Same helpers as 76-vm-wf-sandbox.sh.)
vm_run() {
  local timeout="$1"; shift
  local out
  out="$(qm guest exec "$WF_VMID" --timeout "$timeout" -- "$@")" || die "qm guest exec failed: $*"
  python3 -c 'import json,sys; d=json.load(sys.stdin); sys.stdout.write(d.get("out-data","")); sys.stderr.write(d.get("err-data","")); sys.exit(d.get("exitcode", 1))' <<<"$out"
}

# Copy a local file into the guest, byte-exact: base64 chunks under the 1 MiB --pass-stdin limit.
vm_push() {
  local src="$1" dest="$2" chunks part
  chunks="$(mktemp -d)"
  base64 -w0 "$src" | split -b 600000 - "$chunks/c."
  vm_run 30 sh -c ": > '$dest.b64'" >/dev/null
  for part in "$chunks"/c.*; do
    qm guest exec "$WF_VMID" --pass-stdin 1 --timeout 60 -- sh -c "cat >> '$dest.b64'" < "$part" >/dev/null \
      || { rm -rf "$chunks"; die "push of $src failed"; }
  done
  rm -rf "$chunks"
  vm_run 120 sh -c "base64 -d '$dest.b64' > '$dest' && rm -f '$dest.b64'" >/dev/null || die "decode of $dest failed"
  [[ "$(vm_run 30 stat -c %s "$dest")" == "$(stat -c %s "$src")" ]] || die "$dest size differs after push"
}

wait_agent() {
  local i
  for i in $(seq 1 90); do qm guest cmd "$WF_VMID" ping >/dev/null 2>&1 && return 0; sleep 5; done
  die "guest agent of VM $WF_VMID did not answer in 450 s"
}

case "$CMD" in
  fetch-constraints)
    install -d -m 0700 "$RUNNER_DIR"
    started=0
    if [[ "$(qm status "$SANDBOX_VMID" | awk '{print $2}')" != running ]]; then
      qm start "$SANDBOX_VMID" >/dev/null; started=1
    fi
    WF_VMID="$SANDBOX_VMID"; wait_agent
    vm_run 30 cat /opt/wfpy/constraints.txt > "$RUNNER_DIR/constraints.txt.new" || die "cannot read constraints from VM $SANDBOX_VMID"
    [[ -s "$RUNNER_DIR/constraints.txt.new" ]] || die "empty constraints from VM $SANDBOX_VMID"
    mv "$RUNNER_DIR/constraints.txt.new" "$RUNNER_DIR/constraints.txt"
    if [[ "$started" == 1 ]]; then qm shutdown "$SANDBOX_VMID" --timeout 120 >/dev/null || qm stop "$SANDBOX_VMID"; fi
    ok "constraints: $(wc -l < "$RUNNER_DIR/constraints.txt") pins, sha256 $(sha256sum "$RUNNER_DIR/constraints.txt" | cut -c1-16)"
    ;;

  create)
    step "image $IMAGE_NAME"
    install -d -m 0700 "$RUNNER_DIR"
    img="/var/lib/vz/template/$IMAGE_NAME"
    wget -q -O "$RUNNER_DIR/SHA512SUMS" "$IMAGE_BASE/SHA512SUMS" || die "cannot fetch SHA512SUMS"
    want="$(awk -v n="$IMAGE_NAME" '$2 == n {print $1}' "$RUNNER_DIR/SHA512SUMS")"
    [[ -n "$want" ]] || die "$IMAGE_NAME not listed in SHA512SUMS"
    if ! { [[ -f "$img" ]] && echo "$want  $img" | sha512sum -c - >/dev/null 2>&1; }; then
      wget -q -O "$img" "$IMAGE_BASE/$IMAGE_NAME" || die "download failed"
      echo "$want  $img" | sha512sum -c - >/dev/null 2>&1 || die "SHA512 MISMATCH on $img"
    fi
    ok "image verified"

    step "VM $RUNNER_VMID ($RUNNER_NAME)"
    qm status "$RUNNER_VMID" >/dev/null 2>&1 && die "VM $RUNNER_VMID already exists (destroy it first to rebuild)"
    qm create "$RUNNER_VMID" --name "$RUNNER_NAME" --ostype l26 --machine q35 --memory "$RUNNER_MEM_MB" --balloon 0 \
      --cores "$RUNNER_CORES" --sockets 1 --cpu host --scsihw virtio-scsi-single \
      --net0 "virtio,bridge=vmbr0" --agent enabled=1 --serial0 socket --vga serial0 --onboot 0 \
      || die "qm create failed"
    qm disk import "$RUNNER_VMID" "$img" "$RUNNER_STORAGE" >/dev/null || die "qm disk import failed"
    volid="$(qm config "$RUNNER_VMID" | awk -F': ' '/^unused[0-9]+:/{print $2; exit}')"
    [[ -n "$volid" ]] || die "imported disk did not appear"
    qm set "$RUNNER_VMID" --scsi0 "${volid},discard=on,iothread=1,ssd=1" --boot order=scsi0 >/dev/null || die "attach failed"
    qm disk resize "$RUNNER_VMID" scsi0 "${RUNNER_DISK_GB}G" >/dev/null || die "resize failed"

    step "cloud-init (guest agent via vendor snippet, DHCP, no login user)"
    snip_path="$(pvesh get "/storage/${RUNNER_SNIPPET_STORAGE}" --output-format json \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("path",""))')"
    [[ -n "$snip_path" ]] || die "storage '$RUNNER_SNIPPET_STORAGE' has no path"
    mkdir -p "${snip_path}/snippets"
    cat > "${snip_path}/snippets/wf-runner-vendor.yaml" <<'EOF'
#cloud-config
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
EOF
    qm set "$RUNNER_VMID" --ide2 "${RUNNER_STORAGE}:cloudinit" --citype nocloud --ciuser wfadmin \
      --ipconfig0 "ip=dhcp" --cicustom "vendor=${RUNNER_SNIPPET_STORAGE}:snippets/wf-runner-vendor.yaml" >/dev/null \
      || die "cloud-init configuration failed"
    ok "VM $RUNNER_VMID created, not started. Next: $0 provision"
    ;;

  provision)
    for f in "$RUNNER_DIR/constraints.txt" "$LGC_DIR/files/wf-runner-provision.sh" "$LGC_DIR/files/wf-grader.Dockerfile" \
             "$LGC_DIR/rag/requirements.txt" "$LGC_DIR/files/wf-sandbox-requirements.txt"; do
      [[ -s "$f" ]] || die "missing $f (run fetch-constraints first?)"
    done
    [[ "$(qm status "$RUNNER_VMID" | awk '{print $2}')" == running ]] || qm start "$RUNNER_VMID" >/dev/null
    wait_agent
    tmp="$(mktemp -d)"
    tar -C "$LGC_DIR/tools" --exclude=__pycache__ -cf "$tmp/workforce.tar" workforce
    vm_run 30 install -d -m 0700 "$PUSH" >/dev/null
    vm_push "$RUNNER_DIR/constraints.txt" "$PUSH/constraints.txt"
    vm_push "$LGC_DIR/rag/requirements.txt" "$PUSH/requirements.txt"
    vm_push "$LGC_DIR/files/wf-sandbox-requirements.txt" "$PUSH/extra-requirements.txt"
    vm_push "$LGC_DIR/files/wf-grader.Dockerfile" "$PUSH/wf-grader.Dockerfile"
    vm_push "$tmp/workforce.tar" "$PUSH/workforce.tar"
    vm_push "$LGC_DIR/files/wf-runner-provision.sh" "$PUSH/wf-runner-provision.sh"
    rm -rf "$tmp"
    vm_run 3600 env WF_RUNNER_TEMPLATE=0 bash "$PUSH/wf-runner-provision.sh" || die "provisioning failed"
    ok "VM $RUNNER_VMID provisioned"
    ;;

  parity)
    run_dir="${2:?usage: parity RUN_DIR BUNDLES_DIR}"; bundles="${3:?usage: parity RUN_DIR BUNDLES_DIR}"
    [[ -d "$run_dir/patches" && -d "$bundles" ]] || die "need $run_dir/patches and $bundles"
    [[ "$(qm status "$RUNNER_VMID" | awk '{print $2}')" == running ]] || qm start "$RUNNER_VMID" >/dev/null
    wait_agent
    tmp="$(mktemp -d)"
    tar -C "$bundles" -cf "$tmp/bundles.tar" .
    tar -C "$run_dir" -cf "$tmp/patches.tar" patches
    python3 - "$run_dir" > "$tmp/expected.json" <<'PY'
import json, os, sys
run = sys.argv[1]
out = {}
for tid in sorted(os.listdir(os.path.join(run, "tasks"))):
    p = os.path.join(run, "tasks", tid, "record.json")
    if os.path.exists(p):
        g = json.load(open(p)).get("grade") or {}
        out[tid] = bool(g.get("pass"))
print(json.dumps(out))
PY
    vm_push "$tmp/bundles.tar" /srv/wf/grading/bundles.tar
    vm_push "$tmp/patches.tar" /srv/wf/grading/patches.tar
    vm_push "$LGC_DIR/files/wf-runner-grade.py" /srv/wf/grading/grade.py
    vm_run 60 sh -c "cd /srv/wf/grading && rm -rf b p && mkdir b p && tar -xf bundles.tar -C b && tar -xf patches.tar -C p" >/dev/null
    got="$(vm_run 3600 /opt/wfpy/bin/python3 /srv/wf/grading/grade.py /srv/wf/grading/b /srv/wf/grading/p/patches)" \
      || die "grading on the runner failed"
    python3 - "$tmp/expected.json" "$got" <<'PY'
import json, sys
exp = json.load(open(sys.argv[1])); got = json.loads(sys.argv[2])
diff = {t: (exp.get(t), got.get(t)) for t in sorted(set(exp) | set(got)) if exp.get(t) != got.get(t)}
print(json.dumps({"tasks": len(exp), "passed_on_176": sum(exp.values()), "passed_on_runner": sum(1 for v in got.values() if v),
                  "differences": diff}))
sys.exit(1 if diff else 0)
PY
    rc=$?
    rm -rf "$tmp"
    exit $rc
    ;;

  export)
    require_cmd qemu-img sha256sum
    [[ "$(qm status "$RUNNER_VMID" | awk '{print $2}')" == running ]] || qm start "$RUNNER_VMID" >/dev/null
    wait_agent
    vm_run 120 sh -c "rm -rf /srv/wf/grading/* && cloud-init clean --logs && truncate -s 0 /etc/machine-id && rm -f /var/lib/dbus/machine-id && sync" >/dev/null \
      || die "template cleanup failed"
    qm shutdown "$RUNNER_VMID" --timeout 180 >/dev/null || die "VM $RUNNER_VMID did not shut down"
    scsi0="$(qm config "$RUNNER_VMID" | awk -F': ' '/^scsi0:/{print $2; exit}' | cut -d, -f1)"
    src="$(pvesm path "$scsi0")"
    out="$RUNNER_DIR/wf-runner.vmdk"
    rm -f "$out" "$out.sha256"
    qemu-img convert -p -f raw -O vmdk -o subformat=streamOptimized,adapter_type=lsilogic "$src" "$out" >/dev/null \
      || die "qemu-img convert failed"
    sha256sum "$out" | cut -d' ' -f1 > "$out.sha256"
    ok "exported $out ($(du -h "$out" | cut -f1), sha256 $(cut -c1-16 "$out.sha256"))"
    ;;

  deploy)
    [[ $# -eq 5 ]] || die "usage: $0 deploy HOST DS NAME IP"
    require_cmd govc
    RUNNER_MEM_MB="$RUNNER_MEM_MB" RUNNER_CORES="$RUNNER_CORES" \
      bash "$LGC_DIR/files/wf-runner-esx.sh" deploy "$2" "$3" "$4" "$5" "$RUNNER_DIR/wf-runner.vmdk"
    ;;

  power-on)
    [[ $# -eq 3 ]] || die "usage: $0 power-on HOST NAME"
    require_cmd govc
    bash "$LGC_DIR/files/wf-runner-esx.sh" power-on "$2" "$3"
    ;;

  *)
    echo "usage: $0 fetch-constraints | create | provision | parity RUN_DIR BUNDLES_DIR | export | deploy HOST DS NAME IP | power-on HOST NAME" >&2; exit 2 ;;
esac
