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
#   push-control           install files/wf-run-control.sh as /usr/local/sbin/wf-run-control (Plan D)
#   push-harness           replace /opt/workforce/workforce with scripts/tools/workforce (no provision,
#              any policy): the same sorted sha256 manifest must match on both sides afterwards
#   push-bundles B         replace /srv/wf/bundles with bundle dir B (0700) -- the frozen task set
#   start-run ID T|G [--w1]  start harness run ID detached in the VM (needs `locked`). Keys come from
#              root-only files on the host ($WF_KEY_DIR/router.key, worker.key for arm T) and reach
#              the guest on stdin only -- never argv
#   run-status ID          the run's unit state, record presence and log tail
#   harvest ID DEST        pack the finished run in the VM, pull it out in chunks, verify its sha256,
#              unpack into DEST/ID
#   clear-keys             delete any leftover key file in the VM (window teardown)
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
require_cmd qm python3 base64 tar split stat conntrack pve-firewall

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

# Like vm_run, with stdin passed to the guest command (qm guest exec --pass-stdin; under 1 MiB).
vm_run_stdin() {
  local timeout="$1"; shift
  local out
  out="$(qm guest exec "$WF_VMID" --pass-stdin 1 --timeout "$timeout" -- "$@")" || die "qm guest exec failed: $*"
  python3 -c 'import json,sys; d=json.load(sys.stdin); sys.stdout.write(d.get("out-data","")); sys.stderr.write(d.get("err-data","")); sys.exit(d.get("exitcode", 1))' <<<"$out"
}

# Copy a guest file out, byte-exact: base64 slices of CHUNK bytes through guest exec's out-data.
vm_pull() {
  local src="$1" dest="$2" size="$3" chunk=524288 i n
  n=$(( (size + chunk - 1) / chunk ))
  : > "$dest"
  for (( i = 0; i < n; i++ )); do
    vm_run 120 sh -c "dd if='$src' bs=$chunk skip=$i count=1 status=none | base64 -w0" | base64 -d >> "$dest" \
      || die "pull of $src failed at chunk $i"
  done
  [[ "$(stat -c %s "$dest")" == "$size" ]] || die "$dest size differs after pull"
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
# The policy file alone proves nothing: the firewall must be running and this VM's NIC filtered.
pve-firewall status 2>&1 | grep -q "enabled/running" || die "pve-firewall is not enabled/running"
ip -br link show type bridge | grep -q "^fwbr${WF_VMID}i" || die "VM $WF_VMID's NIC is not filtered (no fwbr${WF_VMID}i*)"

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
    vm_push "$LGC_DIR/files/wf-sandbox-requirements.txt" "$PUSH/extra-requirements.txt"
    vm_push "$LGC_DIR/files/wf-sandbox-provision.sh" "$PUSH/provision.sh"
    vm_push "$LGC_DIR/files/wf-run-control.sh" "/usr/local/sbin/wf-run-control"
    vm_run 30 chmod 0700 /usr/local/sbin/wf-run-control >/dev/null
    ok "pushed harness, grader Dockerfile, requirements, provision script"
    step "provision (apt, venv, opencode, users, grader image) — several minutes"
    vm_run 0 bash "$PUSH/provision.sh"
    ok "provisioned"
    ;;
  proof)
    [[ "$(policy_mode)" == "locked" ]] || die "the boundary proof needs the locked policy: run 75-vm-wf-sandbox-firewall.sh locked"
    allowed="${WF_ROUTER:-192.168.6.153}:${WF_ROUTER_PORT:-8000}"
    for w in ${WF_WORKERS:-172.16.10.205 172.16.10.206 172.16.10.207}; do allowed+=" $w:${WF_WORKER_PORT:-8090}"; done
    # Host side, before the proof creates its own flows: nothing but allowed flows may be tracked.
    # shellcheck disable=SC2086
    conntrack -L -s "${WF_CIDR:-10.79.0.0/24}" 2>/dev/null | python3 "$LGC_DIR/files/wf-conntrack-check.py" $allowed \
      || die "the host tracks disallowed sandbox flows (see above)"
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
    vm_run 120 sh -c "rm -rf /srv/wf/bundles/* /root/wf-refs && install -d -m 0700 /root/wf-refs && tar -x -C /srv/wf/bundles --no-same-owner -f $PUSH/bundles.tar && tar -x -C /root/wf-refs --no-same-owner -f $PUSH/refs.tar && chmod 0700 /srv/wf/bundles && rm -f $PUSH/bundles.tar $PUSH/refs.tar" >/dev/null
    vm_run 1800 /usr/local/sbin/wf-run bundle-validate --bundles /srv/wf/bundles --refs /root/wf-refs \
      --work /root/wf-validate --grader docker:wf-grader:1
    ;;
  status)
    echo "policy: $(policy_mode)"
    vm_run 30 cat /etc/wf-sandbox.json || true
    ;;
  push-control)
    vm_push "$LGC_DIR/files/wf-run-control.sh" "/usr/local/sbin/wf-run-control"
    vm_run 30 chmod 0700 /usr/local/sbin/wf-run-control >/dev/null
    ok "wf-run-control installed"
    ;;
  push-harness)
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tar -C "$LGC_DIR/tools" --exclude=__pycache__ -cf "$tmp/workforce.tar" workforce
    vm_run 30 install -d -m 0700 "$PUSH" >/dev/null
    vm_push "$tmp/workforce.tar" "$PUSH/workforce.tar"
    vm_run 120 sh -c "rm -rf /opt/workforce/workforce && tar -x -C /opt/workforce --no-same-owner -f $PUSH/workforce.tar && chmod -R go-rwx /opt/workforce && rm -f $PUSH/workforce.tar" >/dev/null
    manifest="find workforce -type f -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1"
    local_sum="$(cd "$LGC_DIR/tools" && find workforce -type f -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
    guest_sum="$(vm_run 120 sh -c "cd /opt/workforce && $manifest")"
    [[ "$local_sum" == "$guest_sum" ]] || die "harness differs after push (local $local_sum, guest $guest_sum)"
    ok "harness pushed to /opt/workforce/workforce (manifest sha256 $local_sum)"
    ;;
  push-bundles)
    bundles="${2:-}"
    [[ -d "$bundles" ]] || die "usage: 76-vm-wf-sandbox.sh push-bundles <bundles-dir>"
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    tar -C "$bundles" -cf "$tmp/bundles.tar" .
    vm_run 30 install -d -m 0700 "$PUSH" >/dev/null
    vm_push "$tmp/bundles.tar" "$PUSH/bundles.tar"
    vm_run 120 sh -c "rm -rf /srv/wf/bundles/* && tar -x -C /srv/wf/bundles --no-same-owner -f $PUSH/bundles.tar && chmod 0700 /srv/wf/bundles && rm -f $PUSH/bundles.tar" >/dev/null
    ok "bundles pushed: $(vm_run 30 sh -c 'ls /srv/wf/bundles | wc -l') task(s)"
    ;;
  start-run)
    id="${2:-}"; arm="${3:-}"
    [[ -n "$id" && -n "$arm" ]] || die "usage: 76-vm-wf-sandbox.sh start-run <run-id> <T|G> [--w1]"
    [[ "$(policy_mode)" == "locked" ]] || die "measured runs need the locked policy: run 75-vm-wf-sandbox-firewall.sh locked"
    keys="${WF_KEY_DIR:-/root/wf/keys}"
    [[ -s "$keys/router.key" ]] || die "$keys/router.key missing (router-keys add --out ... ; pct pull)"
    [[ "$arm" == G || -s "$keys/worker.key" ]] || die "$keys/worker.key missing (arm T)"
    # The keys go to the guest on stdin, built by shell builtins: never on any command line.
    {
      printf 'WF_ROUTER_KEY=%s\n' "$(tr -d '[:space:]' < "$keys/router.key")"
      if [[ "$arm" == T ]]; then printf 'WF_WORKER_KEY=%s\n' "$(tr -d '[:space:]' < "$keys/worker.key")"; fi
    } | vm_run_stdin 60 /usr/local/sbin/wf-run-control start "$id" "$arm" "${@:4}"
    ;;
  run-status)
    vm_run 30 /usr/local/sbin/wf-run-control status "${2:?usage: run-status <run-id>}"
    ;;
  harvest)
    id="${2:-}"; dest="${3:-}"
    [[ -n "$id" && -n "$dest" ]] || die "usage: 76-vm-wf-sandbox.sh harvest <run-id> <dest-dir>"
    read -r src size sha < <(vm_run 600 /usr/local/sbin/wf-run-control pack "$id")
    [[ "$size" =~ ^[0-9]+$ && "$sha" =~ ^[0-9a-f]{64}$ ]] || die "pack of $id returned no size/sha256"
    mkdir -p "$dest"
    vm_pull "$src" "$dest/$id.tgz" "$size"
    [[ "$(sha256sum "$dest/$id.tgz" | cut -d' ' -f1)" == "$sha" ]] || die "$dest/$id.tgz sha256 differs from the guest's"
    tar -C "$dest" -xzf "$dest/$id.tgz"
    ok "harvested $id into $dest/$id ($size bytes, sha256 $sha)"
    ;;
  clear-keys)
    vm_run 30 /usr/local/sbin/wf-run-control clear-keys
    ;;
  *) die "usage: 76-vm-wf-sandbox.sh provision|proof|validate|status|push-control|push-harness|push-bundles|start-run|run-status|harvest|clear-keys" ;;
esac
