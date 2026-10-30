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
#   install-tools [--dry-run]  install the task tools on the VM (idempotent): the debs in VM_DEBS (apt-get download
#                              on the host, dpkg -i on the VM -- it has no apt mirror) and the pinned binaries in
#                              files/agent-runner-tools.lock.json (downloaded + sha256-checked on the host). No egress change.
#   seed-umod DIR [--dry-run]  copy SDG-RUST's plugin cache (<sha1>.cs, gitignored) into the VM clone; each file must
#                              hash to its name and be listed by a plugins.lock on origin
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
  # Per-repo sandbox egress: one table -> sandbox user + IPAddressAllow (agent-run) + nft rules (the unit).
  "vm|agent-runner-egress.conf|/etc/agent-runner-agent/egress.conf|root:root|644"
  "vm|agent_runner_egress.py|/usr/local/lib/agent-runner/agent_runner_egress.py|root:root|755"
  "vm|agent-runner-egress.service|/etc/systemd/system/agent-runner-egress.service|root:root|644"
  # SDG-ARMA3's `ssh panel`: an ssh-agent as agent-arma holding the panel key (use, never read) + the client config.
  # The panel's forced command (SDG-IAC) is what makes this safe; the key's public half + host key are copied from
  # runner's ~/.ssh at install (not kept in the repo), so a key rotation there cannot leave a stale copy here.
  "vm|agent-runner-panel-agent.service|/etc/systemd/system/agent-runner-panel-agent.service|root:root|644"
  "vm|agent-runner-panel-ssh.conf|/etc/ssh/ssh_config.d/agent-runner-panel.conf|root:root|644"
  # Task results (/var/lib/agent-results/<task>) are kept 14 days, then deleted by a daily timer.
  "vm|agent-runner-results-clean.service|/etc/systemd/system/agent-runner-results-clean.service|root:root|644"
  "vm|agent-runner-results-clean.timer|/etc/systemd/system/agent-runner-results-clean.timer|root:root|644"
)
# Sandbox prerequisites on the VM (setup-sandbox creates them; diff checks them).
SANDBOX_CHECKS=(
  "per-repo users agent-rust/agent-arma exist, no sudo, not in group runner|for u in agent-rust agent-arma; do id \$u >/dev/null && ! id -Gn \$u | grep -qwE 'runner|sudo' || exit 1; done"
  "old shared user 'agent' removed|! id agent >/dev/null 2>&1"
  "nft table inet agent_egress loaded|sudo /usr/sbin/nft list table inet agent_egress >/dev/null"
  "agent-runner-egress.service enabled|systemctl is-enabled agent-runner-egress.service >/dev/null"
  "/etc/agent-runner is root:runner 750|[ \"\$(stat -c '%U:%G %a' /etc/agent-runner)\" = 'root:runner 750' ]"
  "/home/runner is 700|[ \"\$(stat -c '%a' /home/runner)\" = 700 ]"
  "/var/lib/agent-results is root:runner 750|[ \"\$(stat -c '%U:%G %a' /var/lib/agent-results)\" = 'root:runner 750' ]"
  "old profile /etc/agent-runner/opencode.json removed|[ ! -e /etc/agent-runner/opencode.json ]"
  "results retention timer enabled (14 days)|systemctl is-enabled agent-runner-results-clean.timer >/dev/null"
  "SDG-RUST umod cache holds every plugin in origin/main's plugins.lock (seed-umod)|cd /home/runner/work/SDG-RUST && h=\$(git show origin/main:plugins.lock | grep -oE '[0-9a-f]{40}') && [ -n \"\$h\" ] && for x in \$h; do [ -s .cache/umod/\$x.cs ] || exit 1; done"
  "panel ssh-agent (agent-arma) up, holding exactly runner's panel key|[ \"\$(sudo env SSH_AUTH_SOCK=/run/agent-ssh-arma/agent.sock ssh-add -L | cut -d' ' -f1,2)\" = \"\$(cut -d' ' -f1,2 /home/runner/.ssh/id_ed25519_panel.pub)\" ]"
  "panel socket dir is agent-arma 700|[ \"\$(sudo stat -c '%U %a' /run/agent-ssh-arma)\" = 'agent-arma 700' ]"
  "/etc/agent-runner-agent panel.pub + known_hosts_panel = runner's copies|cmp -s /etc/agent-runner-agent/panel.pub /home/runner/.ssh/id_ed25519_panel.pub && cmp -s /etc/agent-runner-agent/known_hosts_panel /home/runner/.ssh/known_hosts_panel"
)
# Debian packages the tasks need on the VM (install-tools; host and VM are both trixie). python-is-python3:
# SDG-ARMA3#1 checks /usr/bin/python as a precondition. python3-paramiko (+ python3-nacl, its one dependency the VM
# lacked): SDG-RUST tools/lab/ptero.py, Wings SFTP. dpkg -i resolves nothing, so list every dependency the VM lacks.
VM_DEBS=(nftables python-is-python3 python3-nacl python3-paramiko)
TOOLS_LOCK="$F/agent-runner-tools.lock.json"
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

# Content hash; for a .fw file without the [IPSET github] entries, which the refresher owns. In this file only
# set entries start with a digit (rules start IN/OUT, options are key: value), so dropping ^[0-9] lines is exact.
HASH_CMD="sha256sum"
FW_HASH_CMD="grep -vE '^[0-9]' | sha256sum"
repo_sha() { if [[ "$1" == *.fw ]]; then grep -vE '^[0-9]' "$1" | sha256sum | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi; }

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
  local p
  for p in "${VM_DEBS[@]}"; do
    if vm "$(deb_installed_cmd "$p")"; then printf 'ok     vm   deb %s installed\n' "$p"
    else printf 'DRIFT  vm   deb %s not installed (install-tools)\n' "$p"; drift=1; fi
  done
  local ver url zsha member msha dst
  read -r ver url zsha member msha dst <<<"$(lock_sqfvm)"
  if [[ "$(vm "sha256sum '$dst' 2>/dev/null | cut -d' ' -f1; stat -c '%U:%G %a' '$dst' 2>/dev/null")" == "$msha"$'\n'"root:root 755" ]]
  then printf 'ok     vm   sqfvm %s at %s (sha256 + root:root 755)\n' "$ver" "$dst"
  else printf 'DRIFT  vm   sqfvm %s missing or not the pinned build at %s (install-tools)\n' "$ver" "$dst"; drift=1; fi
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
        local keep=""
        if [[ -e "$dst" ]] && grep -qx '\[IPSET github\]' "$dst"; then   # first install: no live set to keep
          keep=$(awk '/^\[IPSET github\]$/{f=1;next} /^\[/{f=0} f && /^[0-9]/' "$dst")
        fi
        # repo file, with the kept entries inserted after the set's comment line
        awk -v keep="$keep" '{print} /^\[IPSET github\]$/{h=1;next} h && /^#/ {if (keep != "") print keep; h=0}' \
          "$F/$src" > "$t"
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
    # (re)render and load the per-repo egress table; the users must exist (setup-sandbox) or rendering fails
    vm "sudo systemctl daemon-reload && sudo systemctl enable agent-runner-egress.service >/dev/null && sudo systemctl restart agent-runner-egress.service && sudo /usr/sbin/nft list table inet agent_egress | grep -c 'accept' | sed 's/^/agent_egress loaded, accept rules: /'"
    # the panel key's public half + the panel host key, from runner's own files (public data; never the private key)
    vm "sudo install -m 644 -o root -g root /home/runner/.ssh/id_ed25519_panel.pub /etc/agent-runner-agent/panel.pub && sudo install -m 644 -o root -g root /home/runner/.ssh/known_hosts_panel /etc/agent-runner-agent/known_hosts_panel"
    vm "sudo systemctl daemon-reload && sudo systemctl enable --now agent-runner-results-clean.timer >/dev/null && systemctl list-timers agent-runner-results-clean.timer --no-pager --no-legend | sed 's/^/results retention: /'"
    vm "sudo systemctl enable agent-runner-panel-agent.service >/dev/null && sudo systemctl restart agent-runner-panel-agent.service && sudo env SSH_AUTH_SOCK=/run/agent-ssh-arma/agent.sock ssh-add -l | sed 's/^/panel ssh-agent holds: /'"
  fi
}

setup_sandbox() {   # $1 = --dry-run or empty. Idempotent.
  # one unprivileged user per repo, straight from the egress table (so the table and the users cannot drift)
  local u users
  users=$(python3 "$F/agent_runner_egress.py" --conf "$F/agent-runner-egress.conf" users)
  local steps=()
  for u in $users; do
    steps+=("id $u >/dev/null 2>&1 || sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin $u")
  done
  steps+=(
    "! id agent >/dev/null 2>&1 || sudo userdel agent"      # the shared pre-2026-10-30 sandbox user
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

deb_installed_cmd() { echo "dpkg-query -W -f='\${Status}' '$1' 2>/dev/null | grep -qx 'install ok installed'"; }
lock_sqfvm() {   # -> "version url sha256 member member_sha256 install_path"
  python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1]))["sqfvm"]
print(*(d[k] for k in ("version","url","sha256","member","member_sha256","install_path")))' "$TOOLS_LOCK"
}

install_tools() {   # $1 = --dry-run or empty. Idempotent; downloads and verifies on the host, never on the VM.
  local dry="${1:-}" p need=() t
  for p in "${VM_DEBS[@]}"; do vm "$(deb_installed_cmd "$p")" || need+=("$p"); done
  if (( ${#need[@]} )); then
    if [[ -n "$dry" ]]; then echo "would install debs: ${need[*]}"
    else
      # apt-get download verifies against the host's signed index; the VM installs exactly those files
      t=$(mktemp -d); chmod 755 "$t"   # so apt's sandbox user (_apt) can write the download
      (cd "$t" && apt-get download -q "${need[@]}")
      tar -C "$t" -cf - . | vm 'set -e; d=$(mktemp -d); tar -C "$d" -xf -; sudo dpkg -i "$d"/*.deb >/dev/null; rm -rf "$d"'
      rm -rf "$t"; echo "installed debs: ${need[*]}"
    fi
  else echo "ok: debs ${VM_DEBS[*]} already installed"; fi

  local ver url zsha member msha dst
  read -r ver url zsha member msha dst <<<"$(lock_sqfvm)"
  if [[ "$(vm "sha256sum '$dst' 2>/dev/null | cut -d' ' -f1")" == "$msha" ]]; then echo "ok: sqfvm $ver already at $dst"
  elif [[ -n "$dry" ]]; then echo "would install sqfvm $ver -> $dst"
  else
    t=$(mktemp -d)
    curl -fsSL --retry 2 -o "$t/sqfvm.zip" "$url"
    echo "$zsha  $t/sqfvm.zip" | sha256sum -c --quiet || { rm -rf "$t"; die "sqfvm zip sha256 mismatch"; }
    # extract ONE named member, by name, to a fixed path (no path from the archive is used)
    python3 -I -c 'import sys,zipfile; open(sys.argv[3],"wb").write(zipfile.ZipFile(sys.argv[1]).read(sys.argv[2]))' \
      "$t/sqfvm.zip" "$member" "$t/sqfvm.bin"
    echo "$msha  $t/sqfvm.bin" | sha256sum -c --quiet || { rm -rf "$t"; die "sqfvm binary sha256 mismatch"; }
    base64 -w0 "$t/sqfvm.bin" | vm "t=\$(mktemp) && base64 -d > \"\$t\" && sudo install -D -m 755 -o root -g root \"\$t\" '$dst'; rc=\$?; rm -f \"\$t\"; exit \$rc"
    rm -rf "$t"; echo "installed sqfvm $ver -> $dst (zip + binary sha256 verified)"
  fi
  [[ -n "$dry" ]] && return 0
  vm "ldd '$dst' | grep 'not found' && exit 1; '$dst' --version 2>&1 | head -3" || die "sqfvm does not run on the VM"
}

seed_umod() {   # $1 = dir of <sha1>.cs files (copied from a workstation's raidable-bases-sdg/.cache/umod), $2 = --dry-run
  # SDG-RUST tools/build.py reads plugins from .cache/umod (gitignored) and the sandbox mounts the clone read-only,
  # so the cache is seeded here. Every file must hash to its own name and be named by a plugins.lock on origin.
  local src="${1:?seed-umod needs the directory holding the <sha1>.cs files}" dry="${2:-}" f h n=0 lock
  local clone=/home/runner/work/SDG-RUST
  lock=$(vm "cd $clone && for r in \$(git for-each-ref --format='%(refname)' refs/remotes/origin); do git show \$r:plugins.lock 2>/dev/null; done; true" | grep -oE '[0-9a-f]{40}' | sort -u)
  [[ -n "$lock" ]] || die "no plugins.lock hashes found in $clone"
  for f in "$src"/*.cs; do
    [[ -e "$f" ]] || die "no .cs files in $src"
    h=$(sha1sum "$f" | cut -d' ' -f1)
    [[ "$(basename "$f")" == "$h.cs" ]] || die "$f: its content hashes to $h, not to its name"
    grep -qx "$h" <<<"$lock" || die "$f: no plugins.lock on origin names it"
    n=$((n+1))
  done
  if [[ -n "$dry" ]]; then echo "would seed $n files into $clone/.cache/umod"; return 0; fi
  (cd "$src" && tar --owner=0 --group=0 --mode=644 -cf - ./*.cs) | vm "set -e; install -d -m 755 $clone/.cache $clone/.cache/umod; tar -C $clone/.cache/umod --no-same-owner -xf -; echo \"seeded \$(ls $clone/.cache/umod | wc -l) files in $clone/.cache/umod\""
}

cmd="${1:-}"; shift || true
case "$cmd" in
  manifest) for e in "${MANIFEST[@]}"; do printf '%-4s %-34s -> %-52s %s %s\n' "$(field "$e" 1)" "$(field "$e" 2)" "$(field "$e" 3)" "$(field "$e" 4)" "$(field "$e" 5)"; done ;;
  diff) do_diff ;;
  install-host) do_install host "${1:-}" ;;
  install-vm) vm true || die "cannot reach $VM with $KEY"; do_install vm "${1:-}" ;;
  setup-sandbox) vm true || die "cannot reach $VM with $KEY"; setup_sandbox "${1:-}" ;;
  install-tools) vm true || die "cannot reach $VM with $KEY"; install_tools "${1:-}" ;;
  seed-umod) vm true || die "cannot reach $VM with $KEY"; seed_umod "${1:-}" "${2:-}" ;;
  probe) vm "bash -s -- '${1:-SDG-ARMA3}'" < "$F/agent-runner-probes.sh" ;;
  *) sed -n '2,21p' "$0"; exit 2 ;;
esac
