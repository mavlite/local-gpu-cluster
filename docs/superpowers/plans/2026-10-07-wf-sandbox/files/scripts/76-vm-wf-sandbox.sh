#!/usr/bin/env bash
# 76-vm-wf-sandbox.sh provision|proof|validate|status — operate the workforce sandbox VM through its guest
# agent (no SSH exists). Files cross as base64 on stdin (`qm guest exec --pass-stdin`), chunked
# under its 1 MiB limit and size-checked in the guest.
#
#   provision  needs the `build` policy: pushes the harness (scripts/tools/workforce), the grader
#              Dockerfile, the repo's test requirements and files/wf-sandbox-provision.sh, then runs it
#   proof      needs the `locked` policy: pushes files/wf_boundary_proof.py and runs it; exit 0 only
#              if every check of spec §5.2 and Plan C Review Focus 1-2 passed
#   validate B R  push bundle dir B (to /srv/wf/bundles, 0700) and reference-patch dir R, then run
#              `wf-run bundle-validate --grader docker:wf-grader:1` in the VM: Docker grading must
#              reproduce local grading (Plan C Review Focus 3)
#   status     /etc/wf-sandbox.json and the active policy mode
# proof honours WF_PROOF_WORKERS=skip (pre-window: worker checks reported as skipped).
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"
require_root
require_pve_host
load_config

CMD="${1:-}"
WF_VMID="${WF_VMID:-176}"
PUSH=/root/wf-push
require_cmd qm python3 base64 tar

# Run argv in the guest; print its stdout; return its exit code.
vm_run() {
  local timeout="$1"; shift
  local out
  out="$(qm guest exec "$WF_VMID" --timeout "$timeout" -- "$@")" || die "qm guest exec failed: $*"
  python3 -c 'import json,sys; d=json.load(sys.stdin); sys.stdout.write(d.get("out-data","")); sys.stderr.write(d.get("err-data","")); sys.exit(d.get("exitcode", 1))' <<<"$out"
}

# Copy a local file into the guest, byte-exact: base64 on stdin in chunks under the 1 MiB
# --pass-stdin limit, appended in the guest, decoded once, then the size is checked.
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

policy_mode() {
  local fw="/etc/pve/firewall/${WF_VMID}.fw"
  for m in build locked; do
    diff <(bash "$LGC_DIR/files/wf-sandbox-policy.sh" "$m") "$fw" >/dev/null 2>&1 && { echo "$m"; return; }
  done
  echo "unknown"
}

[[ "$(qm status "$WF_VMID" 2>/dev/null | awk '{print $2}')" == "running" ]] || die "VM $WF_VMID is not running"
qm guest cmd "$WF_VMID" ping >/dev/null 2>&1 || die "VM $WF_VMID's guest agent does not answer"

case "$CMD" in
  provision)
    [[ "$(policy_mode)" == "build" ]] || die "provisioning needs the build policy: run 75-vm-wf-sandbox-firewall.sh build"
    step "push"
    vm_run 30 install -d -m 0700 "$PUSH" >/dev/null
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tar -C "$LGC_DIR/tools" --exclude=__pycache__ -cf "$tmp/workforce.tar" workforce
    vm_push "$tmp/workforce.tar" "$PUSH/workforce.tar"
    vm_push "$LGC_DIR/files/wf-grader.Dockerfile" "$PUSH/wf-grader.Dockerfile"
    vm_push "$LGC_DIR/rag/requirements.txt" "$PUSH/requirements.txt"
    vm_push "$LGC_DIR/files/wf-sandbox-provision.sh" "$PUSH/provision.sh"
    ok "pushed harness, grader Dockerfile, requirements, provision script"
    step "provision (apt, venv, opencode, users, grader image) — several minutes"
    vm_run 0 bash "$PUSH/provision.sh"
    ok "provisioned"
    ;;
  proof)
    [[ "$(policy_mode)" == "locked" ]] || die "the boundary proof needs the locked policy: run 75-vm-wf-sandbox-firewall.sh locked"
    vm_push "$LGC_DIR/files/wf_boundary_proof.py" "/root/wf_boundary_proof.py"
    vm_run 600 env "WF_PROOF_WORKERS=${WF_PROOF_WORKERS:-}" /opt/wfpy/bin/python3 /root/wf_boundary_proof.py
    ;;
  validate)
    bundles="${2:-}"; refs="${3:-}"
    [[ -d "$bundles" && -d "$refs" ]] || die "usage: 76-vm-wf-sandbox.sh validate <bundles-dir> <refs-dir>"
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tar -C "$bundles" -cf "$tmp/bundles.tar" .
    tar -C "$refs" -cf "$tmp/refs.tar" .
    vm_run 30 install -d -m 0700 "$PUSH" >/dev/null
    vm_push "$tmp/bundles.tar" "$PUSH/bundles.tar"
    vm_push "$tmp/refs.tar" "$PUSH/refs.tar"
    vm_run 120 sh -c "rm -rf /srv/wf/bundles/* /root/wf-refs && install -d -m 0700 /root/wf-refs && tar -x -C /srv/wf/bundles --no-same-owner -f $PUSH/bundles.tar && tar -x -C /root/wf-refs --no-same-owner -f $PUSH/refs.tar && chmod 0700 /srv/wf/bundles" >/dev/null
    vm_run 1800 /usr/local/sbin/wf-run bundle-validate --bundles /srv/wf/bundles --refs /root/wf-refs \
      --work /root/wf-validate --grader docker:wf-grader:1
    ;;
  status)
    echo "policy: $(policy_mode)"
    vm_run 30 cat /etc/wf-sandbox.json || true
    ;;
  *) die "usage: 76-vm-wf-sandbox.sh provision|proof|validate|status" ;;
esac
