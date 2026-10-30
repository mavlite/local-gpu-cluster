#!/usr/bin/env bash
# 80-agent-runner-dispatcher.sh -- install and operate the work-queue dispatcher on VM 178 (agent-runner).
# Agent runner queue spec 2026-10-30, section 6. Runs on the Proxmox host as root; reaches the VM only
# through the admin key (/root/.ssh/agent-runner-admin). Files cross as base64 on stdin (no scp, no
# nested quoting).
#
#   install            copy the dispatcher + units to the VM, daemon-reload (timer left as it was)
#   enable | disable   start/stop the 5-minute timer
#   run-once REPO#N    run one dispatch now, restricted to that issue (e.g. DraconisCluster/SDG-ARMA3#12)
#   selftest           run agent-runner-dispatch.service ITSELF (its exact properties) with DISPATCH_SELFTEST=1:
#                      a preflight -- a real no-op sandbox start -- per repo; claims nothing. Exit 0 = all ready.
#   status             timer state, last runs, recent event logs
#   uninstall          disable and remove the units and the script (event logs are kept)
#
# Does not touch the runner's router key, opencode profile, gh token or firewall (owned elsewhere).
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
VM="${AGENT_RUNNER_SSH:-runner@10.81.0.10}"
KEY="${AGENT_RUNNER_KEY:-/root/.ssh/agent-runner-admin}"
LIB=/usr/local/lib/agent-runner-dispatch
SRC_PY="$LGC_DIR/files/agent_runner_dispatch.py"
SRC_SVC="$LGC_DIR/files/agent-runner-dispatch.service"
SRC_TMR="$LGC_DIR/files/agent-runner-dispatch.timer"

die() { echo "ERROR: $*" >&2; exit 1; }
vm() { ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$VM" "$@"; }
push() {   # push LOCAL REMOTE MODE -- via a temp file, then sudo install
  local src="$1" dst="$2" mode="$3" tmp
  [[ -r "$src" ]] || die "missing $src"
  tmp="/tmp/.ard-$(basename "$dst").$$"
  base64 -w0 "$src" | vm "base64 -d > '$tmp' && sudo install -D -m '$mode' -o root -g root '$tmp' '$dst' && rm -f '$tmp'"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  install)
    vm true || die "cannot reach $VM with $KEY"
    vm "command -v agent-run >/dev/null && command -v gh >/dev/null && command -v python3 >/dev/null" \
      || die "agent-run, gh or python3 missing on the VM"
    push "$SRC_PY"  "$LIB/agent_runner_dispatch.py"                    0755
    push "$SRC_SVC" /etc/systemd/system/agent-runner-dispatch.service 0644
    push "$SRC_TMR" /etc/systemd/system/agent-runner-dispatch.timer   0644
    vm "sudo systemctl daemon-reload && mkdir -p ~/runner-logs && chmod 700 ~/runner-logs"
    vm "sudo systemd-analyze verify /etc/systemd/system/agent-runner-dispatch.service /etc/systemd/system/agent-runner-dispatch.timer"
    # Parse-only check: py_compile would try to write __pycache__ into the root-owned $LIB.
    vm "python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' $LIB/agent_runner_dispatch.py"
    echo "installed; timer is $(vm 'systemctl is-enabled agent-runner-dispatch.timer 2>/dev/null || true')"
    ;;
  enable)  vm "sudo systemctl enable --now agent-runner-dispatch.timer && systemctl list-timers agent-runner-dispatch.timer --no-pager" ;;
  disable) vm "sudo systemctl disable --now agent-runner-dispatch.timer; systemctl is-enabled agent-runner-dispatch.timer || true" ;;
  run-once)
    target="${1:-}"
    [[ "$target" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+$ ]] || die "run-once needs OWNER/REPO#N"
    # Same user, environment and binary as the timer, but restricted to one issue.
    vm "sudo systemd-run --uid=runner --gid=runner --wait --pipe --collect \
          --property=WorkingDirectory=/home/runner --setenv=HOME=/home/runner \
          /usr/bin/python3 $LIB/agent_runner_dispatch.py --only '$target'"
    ;;
  selftest)
    # A runtime drop-in (gone at reboot, removed right after) so the check runs with the unit's own properties,
    # not over ssh and not via systemd-run -- that is the path NoNewPrivileges broke (2026-10-30).
    vm 'set -u; d=/run/systemd/system/agent-runner-dispatch.service.d
        sudo install -d "$d" && printf "[Service]\nEnvironment=DISPATCH_SELFTEST=1\n" | sudo tee "$d/selftest.conf" >/dev/null
        sudo systemctl daemon-reload
        rc=0; sudo systemctl start agent-runner-dispatch.service || rc=$?
        inv=$(systemctl show -p InvocationID --value agent-runner-dispatch.service)
        sudo rm -f "$d/selftest.conf"; sudo systemctl daemon-reload
        journalctl _SYSTEMD_INVOCATION_ID="$inv" --no-pager -o cat | grep -E "selftest|preflight|not ready|sudo"
        echo "selftest via agent-runner-dispatch.service: $([ $rc -eq 0 ] && echo PASS || echo "FAIL (rc=$rc)")"
        exit $rc'
    ;;
  status)
    vm "systemctl list-timers agent-runner-dispatch.timer --no-pager; systemctl status agent-runner-dispatch.service --no-pager -n 0 2>/dev/null | head -5; \
        journalctl -u agent-runner-dispatch.service --no-pager -n 15 -o short-iso; ls -lt ~/runner-logs 2>/dev/null | head -6"
    ;;
  uninstall)
    vm "sudo systemctl disable --now agent-runner-dispatch.timer 2>/dev/null || true; \
        sudo rm -f /etc/systemd/system/agent-runner-dispatch.service /etc/systemd/system/agent-runner-dispatch.timer; \
        sudo rm -rf $LIB; sudo systemctl daemon-reload"
    echo "removed (event logs in ~/runner-logs kept)"
    ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
