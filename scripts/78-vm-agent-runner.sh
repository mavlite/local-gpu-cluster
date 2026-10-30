#!/usr/bin/env bash
# 78-vm-agent-runner.sh -- the agent runner (VM 178) host + VM files, as code. Runs on the Proxmox host as root.
# Captured byte-for-byte from the live machines on 2026-10-30 (built by the SDG-IAC session); docs/agent-runner.md.
#
#   manifest                   list every managed file: repo source -> where it is installed (owner, mode)
#   diff                       READ-ONLY: compare repo vs live (content + owner/mode), plus the vmbr0/vmbrar stanzas
#                              and the VM's key qm settings. Exit 0 = no drift, 1 = drift.
#   install-host [--dry-run]   install the host files (SNAT script/unit, key renewal script/units, 178.fw), daemon-reload
#   install-vm   [--dry-run]   install the VM files (agent-run wrapper, locked opencode profile) over the admin key
#   setup-sandbox [--dry-run]  create the unprivileged `agent` user and the sandbox dirs on the VM (idempotent),
#                              remove the old /etc/agent-runner/opencode.json (the profile moved out of that dir)
#   probe [REPO]               run scripts/files/agent-runner-probes.sh on the VM: the sandbox's walls, shell + agent
#
# Not managed here (not files, or not this repo's): VM creation itself (see docs/agent-runner.md for the qm settings),
# /etc/network/interfaces (checked by `diff`, edited by hand with ifreload -a), the router key and gh token values,
# the OpenVPN client config and everything on OPNsense / the panel (SDG-IAC). The dispatcher is 80-*.sh.
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
F="$LGC_DIR/files"
VM="${AGENT_RUNNER_SSH:-runner@10.81.0.10}"
KEY="${AGENT_RUNNER_KEY:-/root/.ssh/agent-runner-admin}"
VMID="${AGENT_RUNNER_VMID:-178}"

# where|repo file|installed path|owner:group|mode
MANIFEST=(
  "host|agent-runner-net.sh|/usr/local/sbin/agent-runner-net.sh|root:root|755"
  "host|agent-runner-net.service|/etc/systemd/system/agent-runner-net.service|root:root|644"
  "host|agent-runner-key-renew.sh|/usr/local/sbin/agent-runner-key-renew|root:root|750"
  "host|agent-runner-key-renew.service|/etc/systemd/system/agent-runner-key-renew.service|root:root|644"
  "host|agent-runner-key-renew.timer|/etc/systemd/system/agent-runner-key-renew.timer|root:root|644"
  # 178.fw: its [IPSET github] block (last section) is filled by the ipset refresher; diff ignores that block.
  "host|agent-runner-178.fw|/etc/pve/firewall/${VMID}.fw|root:www-data|640"
  "host|agent-runner-github-ipset.sh|/usr/local/sbin/agent-runner-github-ipset|root:root|750"
  "host|agent-runner-github-ipset.service|/etc/systemd/system/agent-runner-github-ipset.service|root:root|644"
  "host|agent-runner-github-ipset.timer|/etc/systemd/system/agent-runner-github-ipset.timer|root:root|644"
  "host|agent-runner-chrony.conf|/etc/chrony/conf.d/agent-runner.conf|root:root|644"
  "vm|agent-run.sh|/usr/local/bin/agent-run|root:root|755"
  "vm|agent-runner-timesyncd.conf|/etc/systemd/timesyncd.conf.d/agent-runner.conf|root:root|644"
  # The profile lives OUTSIDE /etc/agent-runner: the sandbox makes that dir inaccessible to the agent.
  "vm|agent-runner-opencode.json|/etc/agent-runner-agent/opencode.json|root:root|644"
)
# Sandbox prerequisites on the VM (setup-sandbox creates them; diff checks them).
SANDBOX_CHECKS=(
  "agent user exists, no sudo, not in group runner|id agent >/dev/null && ! id -Gn agent | grep -qwE 'runner|sudo'"
  "/etc/agent-runner is root:runner 750|[ \"\$(stat -c '%U:%G %a' /etc/agent-runner)\" = 'root:runner 750' ]"
  "/home/runner is 700|[ \"\$(stat -c '%a' /home/runner)\" = 700 ]"
  "/var/lib/agent-results is root:runner 750|[ \"\$(stat -c '%U:%G %a' /var/lib/agent-results)\" = 'root:runner 750' ]"
  "old profile /etc/agent-runner/opencode.json removed|[ ! -e /etc/agent-runner/opencode.json ]"
)
# Lines that must be present in /etc/network/interfaces (checked, never written).
IFACE_LINES=(
  $'\taddress 192.168.6.78/24'
  'iface vmbrar inet static'
  $'\taddress 10.81.0.254/24'
)
# qm settings the runner depends on (checked, never written). onboot 1: owner's decision 2026-10-30.
QM_EXPECT=("net0:bridge=vmbrar,firewall=1" "ipconfig0:ip=10.81.0.10/24,gw=10.81.0.254" "cpuunits:50" "memory:8192"
           "onboot:1")

die() { echo "ERROR: $*" >&2; exit 1; }
vm() { ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 "$VM" "$@"; }
field() { cut -d'|' -f"$2" <<<"$1"; }

# Content hash; for a .fw file only up to and including the '[IPSET github]' header (the refresher owns the rest).
HASH_CMD="sha256sum"
FW_HASH_CMD="sed '/^\\[IPSET github\\]\$/q' | sha256sum"
repo_sha() { if [[ "$1" == *.fw ]]; then sed '/^\[IPSET github\]$/q' "$1" | sha256sum | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }

live_meta() {   # where path -> "sha256 owner:group mode" or "missing"
  local where="$1" path="$2" cmd h="$HASH_CMD"
  [[ "$path" == *.fw ]] && h="$FW_HASH_CMD"
  cmd="if [ -e '$path' ]; then echo \"\$(cat '$path' | $h | cut -d' ' -f1) \$(stat -c '%U:%G %a' '$path')\"; else echo missing; fi"
  if [[ "$where" == host ]]; then bash -c "$cmd"; else vm "$cmd"; fi
}

do_diff() {
  local drift=0 e where src dst own mode want got
  for e in "${MANIFEST[@]}"; do
    where=$(field "$e" 1); src=$(field "$e" 2); dst=$(field "$e" 3); own=$(field "$e" 4); mode=$(field "$e" 5)
    [[ -r "$F/$src" ]] || die "missing repo file $F/$src"
    want="$(repo_sha "$F/$src") $own $mode"
    got=$(live_meta "$where" "$dst")
    if [[ "$got" == "$want" ]]; then printf 'ok     %-4s %s\n' "$where" "$dst"
    else printf 'DRIFT  %-4s %s\n       repo: %s\n       live: %s\n' "$where" "$dst" "$want" "$got"; drift=1; fi
  done
  local l
  for l in "${IFACE_LINES[@]}"; do
    if grep -qxF -- "$l" /etc/network/interfaces; then printf 'ok     host interfaces: %s\n' "${l//$'\t'/}"
    else printf 'DRIFT  host interfaces: missing line %q\n' "$l"; drift=1; fi
  done
  local cfg q k v
  cfg=$(qm config "$VMID")
  for q in "${QM_EXPECT[@]}"; do
    k=${q%%:*}; v=${q#*:}
    if grep -E "^${k}: " <<<"$cfg" | grep -qF -- "$v"; then printf 'ok     qm %s: has %s\n' "$k" "$v"
    else printf 'DRIFT  qm %s: expected to contain %s, got: %s\n' "$k" "$v" "$(grep -E "^${k}: " <<<"$cfg" || echo none)"; drift=1; fi
  done
  local s
  for s in "${SANDBOX_CHECKS[@]}"; do
    if vm "${s#*|}" 2>/dev/null; then printf 'ok     vm   %s\n' "${s%%|*}"
    else printf 'DRIFT  vm   %s\n' "${s%%|*}"; drift=1; fi
  done
  for u in agent-runner-net.service agent-runner-key-renew.timer; do
    printf 'info   host %s: %s\n' "$u" "$(systemctl is-enabled "$u" 2>&1 || true)"
  done
  (( drift == 0 )) && echo "no drift" || echo "DRIFT found"
  return "$drift"
}

do_install() {   # $1 = host|vm, $2 = --dry-run or empty
  local target="$1" dry="${2:-}" e where src dst own mode
  for e in "${MANIFEST[@]}"; do
    where=$(field "$e" 1); [[ "$where" == "$target" ]] || continue
    src=$(field "$e" 2); dst=$(field "$e" 3); own=$(field "$e" 4); mode=$(field "$e" 5)
    [[ -r "$F/$src" ]] || die "missing repo file $F/$src"
    if [[ -n "$dry" ]]; then printf 'would install %s -> %s:%s (%s %s)\n' "$src" "$where" "$dst" "$own" "$mode"; continue; fi
    if [[ "$where" == host ]]; then
      if [[ "$dst" == *.fw ]]; then
        # Repo rules + the LIVE [IPSET github] entries, so installing never empties GitHub egress. pmxcfs fixes
        # owner/mode itself; write via a temp copy.
        local t; t=$(mktemp)
        { cat "$F/$src"; [[ -e "$dst" ]] && sed '1,/^\[IPSET github\]$/d' "$dst" | grep -v '^#'; } > "$t"
        cp "$t" "$dst"; rm -f "$t"
      elif [[ "$dst" == /etc/pve/* ]]; then cp "$F/$src" "$dst"
      else install -D -m "$mode" -o "${own%%:*}" -g "${own##*:}" "$F/$src" "$dst"; fi
    else
      base64 -w0 "$F/$src" | vm "t=\$(mktemp) && base64 -d > \"\$t\" && sudo install -D -m '$mode' -o '${own%%:*}' -g '${own##*:}' \"\$t\" '$dst'; rc=\$?; rm -f \"\$t\"; exit \$rc"
    fi
    echo "installed $dst"
  done
  [[ -n "$dry" ]] && return 0
  if [[ "$target" == host ]]; then
    systemctl daemon-reload
    /usr/local/sbin/agent-runner-github-ipset            # fill/refresh the GitHub set now, not at the next timer tick
    pve-firewall compile >/dev/null && echo "pve-firewall compiles"
    systemctl enable --now agent-runner-github-ipset.timer >/dev/null && echo "github ipset timer enabled"
    systemctl restart chrony && echo "chrony restarted (serving 10.81.0.0/24 on 10.81.0.254)"
  else
    vm "OPENCODE_CONFIG=/etc/agent-runner-agent/opencode.json opencode debug config >/dev/null && echo 'opencode profile validates'"
    vm "sudo systemctl restart systemd-timesyncd && echo 'timesyncd restarted (NTP=10.81.0.254)'"
  fi
}

setup_sandbox() {   # $1 = --dry-run or empty. Idempotent.
  local steps=(
    "id agent >/dev/null 2>&1 || sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin agent"
    "sudo install -d -o root -g runner -m 0750 /var/lib/agent-results"
    "sudo install -d -o root -g root -m 0755 /etc/agent-runner-agent"
    "sudo chown root:runner /etc/agent-runner && sudo chmod 0750 /etc/agent-runner"
    "sudo chmod 0700 /home/runner"
    "sudo rm -f /etc/agent-runner/opencode.json"
  )
  local s
  for s in "${steps[@]}"; do
    if [[ -n "${1:-}" ]]; then echo "would run on VM: $s"; else vm "$s" && echo "ok: $s"; fi
  done
}

cmd="${1:-}"; shift || true
case "$cmd" in
  manifest) for e in "${MANIFEST[@]}"; do printf '%-4s %-34s -> %-52s %s %s\n' "$(field "$e" 1)" "$(field "$e" 2)" "$(field "$e" 3)" "$(field "$e" 4)" "$(field "$e" 5)"; done ;;
  diff) do_diff ;;
  install-host) do_install host "${1:-}" ;;
  install-vm) vm true || die "cannot reach $VM with $KEY"; do_install vm "${1:-}" ;;
  setup-sandbox) vm true || die "cannot reach $VM with $KEY"; setup_sandbox "${1:-}" ;;
  probe) vm "bash -s -- '${1:-SDG-ARMA3}'" < "$F/agent-runner-probes.sh" ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
