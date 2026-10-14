#!/usr/bin/env bash
# wf-runner-fleet.sh -- operate the three Minisforum test-runner VMs (wf-runner-01..03, coding engine phase 2) as one
# fleet from the Proxmox host: deploy, power, run a command, and grading parity. Wraps scripts/79-vm-wf-runner-image.sh
# (resolved relative to this script: scripts/files/ -> scripts/) for deploy / power-on.
#
#   wf-runner-fleet.sh deploy             register every runner powered off (79 deploy); hashes the guest password
#   wf-runner-fleet.sh power-on           start every runner (79 power-on: reservation, tiering and 80% DRAM checks)
#   wf-runner-fleet.sh shutdown           guest shutdown of every runner, then poll powerState for up to 5 minutes
#   wf-runner-fleet.sh guest CMD          run CMD (one shell string) inside every runner, last 25 lines of output
#   wf-runner-fleet.sh parity RUN_DIR BUNDLES_DIR
#                                         grade a harvested run's patches with the Docker grader on every runner
#                                         (wf-runner-grade.py, next to this script) and compare pass/fail per task
#                                         with the run's own records (graded on VM 176); exit 1 on any difference
#
# stdin, one value per line, in order (never argv, never printed, never written to disk):
#   1. a throwaway line   (PowerShell 5.1 pipes put a BOM on line 1)
#   2. ESXi user          -> GOVC_USERNAME
#   3. ESXi password      -> GOVC_PASSWORD
#   4. runner guest password -- required for deploy / guest / parity only. deploy hashes it with
#      `openssl passwd -6 -stdin` into WF_RUNNER_GUEST_HASH; guest / parity put it in GOVC_GUEST_LOGIN="wfctl:<pass>"
#      (environment only). The plain value is unset as soon as it has been used.
# Control path: VMware Tools guest operations as the runner account wfctl (runners have no ssh), host-direct govc to
# each ESXi host, so it works in Minimal mode with vCenter down. GOVC_INSECURE=1 and GOVC_PERSIST_SESSION=false always
# (govc's session cache lies after a host is rebuilt).
set -uo pipefail

# The fleet, defined once: "ESXi host IP, local datastore, VM name, runner IP".
FLEET=(
  "172.16.10.145 hyp01-local wf-runner-01 172.16.10.208"
  "172.16.10.143 hyp02-local wf-runner-02 172.16.10.209"
  "172.16.10.141 hyp03-local wf-runner-03 172.16.10.210"
)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_SCRIPT="$HERE/../79-vm-wf-runner-image.sh"
GRADE_PY="$HERE/wf-runner-grade.py"

usage() {
  cat >&2 <<'EOF'
usage: wf-runner-fleet.sh deploy|power-on|shutdown
       wf-runner-fleet.sh guest CMD
       wf-runner-fleet.sh parity RUN_DIR BUNDLES_DIR
stdin: throwaway line, ESXi user, ESXi password, runner guest password (deploy / guest / parity)
EOF
  exit 2
}

strip_ansi() { sed 's/\x1b\[[0-9;]*m//g' | grep -v '^$'; }

action="${1:-}"
case "$action" in
  deploy | power-on | shutdown) (( $# == 1 )) || usage ;;
  guest) (( $# == 2 )) || usage ;;
  parity) (( $# == 3 )) || usage ;;
  *) usage ;;
esac

read -r _ || true
read -r U || true
read -r P || true
G=""
read -r G || true
U="${U%$'\r'}"; P="${P%$'\r'}"; G="${G%$'\r'}"   # tolerate CRLF from Windows pipes
export GOVC_USERNAME="$U" GOVC_PASSWORD="$P" GOVC_INSECURE=1 GOVC_PERSIST_SESSION=false
unset U P

case "$action" in
  deploy | guest | parity)
    if [[ -z "$G" ]]; then
      echo "$action needs the runner guest password as the 4th stdin line" >&2
      exit 2
    fi ;;
esac

if [[ "$action" == deploy ]]; then
  WF_RUNNER_GUEST_HASH="$(printf '%s' "$G" | openssl passwd -6 -stdin)" || { echo "openssl passwd failed" >&2; exit 1; }
  export WF_RUNNER_GUEST_HASH
elif [[ "$action" == guest || "$action" == parity ]]; then
  export GOVC_GUEST_LOGIN="wfctl:$G"
fi
unset G

shutdown_runner() {
  local name="$1" i state=""
  govc vm.power -s "$name" >/dev/null 2>&1 || true
  for i in $(seq 1 60); do
    state="$(govc object.collect -s "vm/$name" runtime.powerState 2>/dev/null)"
    [[ "$state" == poweredOff ]] && break
    sleep 5
  done
  echo "$name: $state"
  [[ "$state" == poweredOff ]]
}

wait_for_tools() {
  local name="$1" i
  for i in $(seq 1 "${WF_FLEET_TOOLS_POLLS:-36}"); do
    [[ "$(govc object.collect -s "vm/$name" guest.toolsRunningStatus 2>/dev/null)" == guestToolsRunning ]] && return 0
    sleep "${WF_FLEET_POLL_SLEEP:-5}"
  done
  echo "$name: VMware Tools not running after waiting" >&2
  return 1
}

guest_cmd() {
  local name="$1" cmd="$2"
  wait_for_tools "$name" || return 1
  govc guest.run -vm "$name" /bin/bash -c "$cmd" 2>&1 | tail -n 25
  (( PIPESTATUS[0] == 0 ))
}

# --- parity -----------------------------------------------------------------------------------------------------
parity_prepare() {
  local run_dir="$1" bundles="$2" tmp="$3"
  tar -C "$bundles" -cf "$tmp/bundles.tar" . || return 1
  tar -C "$run_dir" -cf "$tmp/patches.tar" patches || return 1
  python3 - "$run_dir" > "$tmp/expected.json" <<'PY' || return 1
import json, os, sys
run = sys.argv[1]
out = {}
for tid in sorted(os.listdir(os.path.join(run, "tasks"))):
    p = os.path.join(run, "tasks", tid, "record.json")
    if os.path.exists(p):
        out[tid] = bool((json.load(open(p)).get("grade") or {}).get("pass"))
print(json.dumps(out))
PY
  cat > "$tmp/job.sh" <<'JOB'
#!/bin/bash
# runs inside the runner as wfctl: unpack, grade as root (grading dirs are root-only), result in /tmp/parity.json
set -uo pipefail
cd /tmp/parity-in || exit 1
sudo -n rm -rf /srv/wf/grading/b /srv/wf/grading/p /srv/wf/grading/work
sudo -n mkdir -p /srv/wf/grading/b /srv/wf/grading/p
sudo -n tar -xf bundles.tar -C /srv/wf/grading/b && sudo -n tar -xf patches.tar -C /srv/wf/grading/p
sudo -n /opt/wfpy/bin/python3 grade.py /srv/wf/grading/b /srv/wf/grading/p/patches > /tmp/parity.json 2> /tmp/parity.err
echo "rc=$?" > /tmp/parity.done
JOB
}

parity_compare() {
  python3 - "$1" "$2" <<'PY'
import json, sys
exp = json.load(open(sys.argv[1])); got = json.load(open(sys.argv[2]))
diff = {t: (exp.get(t), got.get(t)) for t in sorted(set(exp) | set(got)) if exp.get(t) != got.get(t)}
print("  " + json.dumps({"tasks": len(exp), "passed_on_176": sum(exp.values()),
                         "passed_on_runner": sum(1 for v in got.values() if v), "differences": diff}))
sys.exit(1 if diff else 0)
PY
}

# parity_runner NAME TMP -- returns 1 on any failure or difference
parity_runner() {
  local name="$1" tmp="$2" f t0 done_line=""
  govc guest.run -vm "$name" /bin/bash -c 'rm -rf /tmp/parity-in /tmp/parity.json /tmp/parity.err /tmp/parity.done; mkdir -p /tmp/parity-in' >/dev/null
  for f in bundles.tar patches.tar job.sh; do
    govc guest.upload -vm "$name" -f "$tmp/$f" "/tmp/parity-in/$f" || { echo "upload $f failed"; return 1; }
  done
  govc guest.upload -vm "$name" -f "$GRADE_PY" /tmp/parity-in/grade.py || { echo "upload grade.py failed"; return 1; }
  t0=$SECONDS
  govc guest.start -vm "$name" /bin/bash /tmp/parity-in/job.sh >/dev/null
  while (( SECONDS - t0 < 3600 )); do
    sleep 20
    done_line="$(govc guest.run -vm "$name" /bin/cat /tmp/parity.done 2>/dev/null || true)"
    [[ "$done_line" == rc=* ]] && break
  done
  echo "  graded in $(( SECONDS - t0 )) s ($done_line)"
  govc guest.download -vm "$name" /tmp/parity.json "$tmp/$name.json" 2>/dev/null \
    || { echo "  no result"; govc guest.run -vm "$name" /usr/bin/tail -n 5 /tmp/parity.err; return 1; }
  parity_compare "$tmp/expected.json" "$tmp/$name.json"
}

# --- main -------------------------------------------------------------------------------------------------------
ptmp=""
if [[ "$action" == parity ]]; then
  run_dir="$2"; bundles="$3"
  if [[ ! -d "$run_dir/patches" || ! -d "$bundles" || ! -f "$GRADE_PY" ]]; then
    echo "need $run_dir/patches, $bundles, $GRADE_PY" >&2
    exit 2
  fi
  ptmp="$(mktemp -d)" || { echo "could not create a temp directory" >&2; exit 1; }
  [[ -n "$ptmp" && -d "$ptmp" ]] || { echo "could not create a temp directory" >&2; exit 1; }
  trap 'rm -rf "$ptmp"' EXIT
  parity_prepare "$run_dir" "$bundles" "$ptmp" || { echo "could not prepare the parity inputs" >&2; exit 1; }
fi

rc=0
for spec in "${FLEET[@]}"; do
  read -r host ds name ip <<<"$spec"
  echo "#### $name on $host"
  [[ "$action" == deploy || "$action" == power-on ]] || export GOVC_URL="https://$host"
  case "$action" in
    deploy)
      bash "$IMAGE_SCRIPT" deploy "$host" "$ds" "$name" "$ip" 2>&1 | strip_ansi
      (( PIPESTATUS[0] == 0 )) || rc=1 ;;
    power-on)
      bash "$IMAGE_SCRIPT" power-on "$host" "$name" 2>&1 | strip_ansi
      (( PIPESTATUS[0] == 0 )) || rc=1 ;;
    shutdown) shutdown_runner "$name" || rc=1 ;;
    guest) guest_cmd "$name" "$2" || rc=1 ;;
    parity) parity_runner "$name" "$ptmp" || rc=1 ;;
  esac
done
exit $rc
