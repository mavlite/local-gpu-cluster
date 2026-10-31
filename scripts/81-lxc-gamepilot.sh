#!/usr/bin/env bash
# 81-lxc-gamepilot.sh -- the GamePilot conductor (CT 160) and its egress, as code. Runs on the Proxmox host as root.
# Request: mavlite/local-gpu-cluster#66. Owner decision 2026-10-31: an unprivileged LXC (house pattern, not a VM),
# on its own bridge vmbrgp 10.82.0.0/24, and ALL egress through an allowlisting proxy instance on LXC 155:3129.
# docs/gamepilot-conductor.md.
#
#   manifest                    list every managed file: repo source -> where it is installed
#   bridge                      add the vmbrgp stanza to /etc/network/interfaces if missing, then ifreload -a
#                               (backs the file up; refuses if the NAT table changes in any other way)
#   host                        SNAT unit + CT firewall (160.fw) on the host
#   proxy                       the gamepilot tinyproxy instance on LXC 155 (its own port, allowlist, pid, log)
#   create                      pct create CT 160 if absent, start it
#   provision                   proxy settings, packages, the pinned claude CLI (host-downloaded, sha256-checked)
#   clone BUNDLE SHA            /opt/gamepilot from a git bundle (the repo is private; the CT has no GitHub
#                               credentials until the owner adds one), checked at SHA, then deploy/install.sh
#   verify                      allowed hosts answer through the proxy, everything else is refused
#   diff                        READ-ONLY drift check: managed files + bridge + CT settings. Exit 1 on drift.
#
# Never does the owner steps: claude login as gamepilot, /etc/gamepilot/gh-token, game clones, config, the timer.
set -Eeuo pipefail

LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
F="$LGC_DIR/files"
CT=160; HOSTNAME_CT=gamepilot; PROXY_CT=155
BR=vmbrgp; BR_ADDR=10.82.0.254/24; CT_IP=10.82.0.10/24; CT_GW=10.82.0.254
TEMPLATE="local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
DEBS=(python3 python3-yaml git gh curl ca-certificates sudo)   # sudo: the GamePilot runbook uses it
LOCK="$F/gamepilot-tools.lock.json"

# where|repo file|installed path|owner:group|mode   (where: host, proxy = CT 155, ct = CT 160)
MANIFEST=(
  "host|gamepilot-net.sh|/usr/local/sbin/gamepilot-net.sh|root:root|755"
  "host|gamepilot-net.service|/etc/systemd/system/gamepilot-net.service|root:root|644"
  "host|gamepilot-160.fw|/etc/pve/firewall/${CT}.fw|root:www-data|640"
  "proxy|gamepilot-proxy.conf|/etc/tinyproxy/gamepilot.conf|root:root|644"
  "proxy|gamepilot-proxy-allowlist|/etc/tinyproxy/gamepilot-allowlist|root:root|644"
  "proxy|gamepilot-proxy.service|/etc/systemd/system/gamepilot-proxy.service|root:root|644"
  "ct|gamepilot-ct-environment|/etc/environment|root:root|644"
  "ct|gamepilot-ct-systemd-proxy.conf|/etc/systemd/system.conf.d/gamepilot-proxy.conf|root:root|644"
  "ct|gamepilot-ct-apt-proxy.conf|/etc/apt/apt.conf.d/90gamepilot-proxy|root:root|644"
  "ct|gamepilot-ct-profile.sh|/etc/profile.d/gamepilot-proxy.sh|root:root|644"
  "ct|gamepilot-ct-sudoers|/etc/sudoers.d/gamepilot-proxy|root:root|440"
)
BRIDGE_STANZA=$'auto vmbrgp\niface vmbrgp inet static\n\taddress 10.82.0.254/24\n\tbridge-ports none\n\tbridge-stp off\n\tbridge-fd 0\n\tpost-up sysctl -qw net.ipv6.conf.vmbrgp.disable_ipv6=1\n\tpost-up sysctl -qw net.ipv6.conf.vmbrgp.forwarding=0'
# qm/pct settings the conductor depends on (checked by diff, set by create)
CT_EXPECT=("hostname:gamepilot" "unprivileged:1" "cores:2" "memory:4096" "onboot:1"
           "net0:bridge=vmbrgp" "net0:firewall=1" "net0:ip=10.82.0.10/24" "rootfs:size=20G")

die() { echo "ERROR: $*" >&2; exit 1; }
# A command in CT 160 with exactly the environment a systemd unit there gets (the proxy via DefaultEnvironment).
# pct exec alone reads neither /etc/environment nor profile.d, so it is not what the conductor sees.
in_unit() { pct exec "$CT" -- systemd-run --wait --pipe -q "$@"; }
field() { cut -d'|' -f"$2" <<<"$1"; }
in_ct() { local c="$1"; shift; pct exec "$c" -- "$@"; }

push_file() {   # where src dst own mode
  local where="$1" src="$2" dst="$3" own="$4" mode="$5"
  [[ -r "$F/$src" ]] || die "missing repo file $F/$src"
  case "$where" in
    host)
      if [[ "$dst" == /etc/pve/* ]]; then cp "$F/$src" "$dst"
      else install -D -m "$mode" -o "${own%%:*}" -g "${own##*:}" "$F/$src" "$dst"; fi ;;
    proxy|ct)
      local c=$CT; [[ "$where" == proxy ]] && c=$PROXY_CT
      in_ct "$c" install -d "$(dirname "$dst")"
      pct push "$c" "$F/$src" "$dst" --user "${own%%:*}" --group "${own##*:}" --perms "$mode" ;;
  esac
  echo "installed $where:$dst"
}
push_where() { local e; for e in "${MANIFEST[@]}"; do [[ "$(field "$e" 1)" == "$1" ]] || continue
  push_file "$1" "$(field "$e" 2)" "$(field "$e" 3)" "$(field "$e" 4)" "$(field "$e" 5)"; done; }

live_sha() {   # where path
  local where="$1" path="$2"
  case "$where" in
    host) sha256sum "$path" 2>/dev/null | cut -d' ' -f1 ;;
    proxy) in_ct "$PROXY_CT" sha256sum "$path" 2>/dev/null | cut -d' ' -f1 ;;
    ct) in_ct "$CT" sha256sum "$path" 2>/dev/null | cut -d' ' -f1 ;;
  esac
}

do_bridge() {
  if grep -qx 'auto vmbrgp' /etc/network/interfaces; then echo "ok: vmbrgp already in /etc/network/interfaces"
  else
    local bak; bak=/etc/network/interfaces.bak-gamepilot-$(date -u +%Y%m%d-%H%M%S)
    cp -p /etc/network/interfaces "$bak"
    local nat_before; nat_before=$(iptables -t nat -S POSTROUTING | sort)
    printf '\n%s\n' "$BRIDGE_STANZA" >> /etc/network/interfaces
    ifreload -a
    local nat_after; nat_after=$(iptables -t nat -S POSTROUTING | sort)
    [[ "$nat_before" == "$nat_after" ]] || { echo "NAT table changed by ifreload (de-duplicate by hand):" >&2
      diff <(echo "$nat_before") <(echo "$nat_after") >&2 || true; }
    echo "added vmbrgp (backup $bak)"
  fi
  ip -4 addr show dev "$BR" | grep -q " ${BR_ADDR}" || die "$BR does not have $BR_ADDR"
  ip -4 addr show dev vmbr0 | grep -q " 192.168.6.78/" || die "192.168.6.78 is missing from vmbr0 after ifreload"
  echo "ok: $BR up with $BR_ADDR; vmbr0 still carries 192.168.6.78"
}

do_host() {
  push_where host
  systemctl daemon-reload
  systemctl enable --now gamepilot-net.service >/dev/null
  /usr/local/sbin/gamepilot-net.sh check && echo "ok: SNAT 10.82.0.0/24 -> 192.168.6.175"
  pve-firewall compile >/dev/null && echo "ok: pve-firewall compiles"
}

do_proxy() {
  in_ct "$PROXY_CT" sh -c 'command -v tinyproxy >/dev/null' || die "tinyproxy is not installed on CT $PROXY_CT"
  push_where proxy
  in_ct "$PROXY_CT" systemctl daemon-reload
  in_ct "$PROXY_CT" systemctl enable gamepilot-proxy.service >/dev/null
  in_ct "$PROXY_CT" systemctl restart gamepilot-proxy.service
  sleep 1
  in_ct "$PROXY_CT" systemctl is-active gamepilot-proxy.service
  in_ct "$PROXY_CT" sh -c 'ss -ltn | grep -q "192.168.6.155:3129"' && echo "ok: gamepilot proxy listening on 192.168.6.155:3129"
  in_ct "$PROXY_CT" systemctl is-active tinyproxy.service >/dev/null && echo "ok: redteam proxy (3128) untouched and active"
}

do_create() {
  if pct status "$CT" >/dev/null 2>&1; then echo "ok: CT $CT exists"
  else
    pct create "$CT" "$TEMPLATE" --hostname "$HOSTNAME_CT" --unprivileged 1 --features nesting=1 \
      --cores 2 --memory 4096 --swap 512 --rootfs local-lvm:20 --onboot 1 --ostype debian \
      --net0 "name=eth0,bridge=$BR,firewall=1,ip=$CT_IP,gw=$CT_GW" --nameserver 127.0.0.1   # no DNS: proxy only
    echo "created CT $CT"
  fi
  pct status "$CT" | grep -q running || pct start "$CT"
  echo "ok: CT $CT running"
}

do_provision() {
  push_where ct
  in_ct "$CT" systemctl daemon-reexec          # pick up DefaultEnvironment (the proxy) for every unit
  in_ct "$CT" sh -c '! command -v visudo >/dev/null || visudo -c -q'   # a bad sudoers drop-in would lock sudo
  in_ct "$CT" env DEBIAN_FRONTEND=noninteractive apt-get -q update
  in_ct "$CT" env DEBIAN_FRONTEND=noninteractive apt-get -q -y install "${DEBS[@]}"
  local ver url sha dst t
  read -r ver url sha dst < <(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1]))["claude"]
print(d["version"], d["url"], d["sha256"], d["install_path"])' "$LOCK")
  if [[ "$(in_ct "$CT" sh -c "sha256sum $dst 2>/dev/null | cut -d' ' -f1")" == "$sha" ]]; then echo "ok: claude $ver already at $dst"
  else
    t=$(mktemp -d)
    curl -fsSL --retry 2 -o "$t/claude" "$url"
    echo "$sha  $t/claude" | sha256sum -c --quiet || { rm -rf "$t"; die "claude binary sha256 mismatch"; }
    pct push "$CT" "$t/claude" "$dst" --user root --group root --perms 0755
    rm -rf "$t"; echo "installed claude $ver -> $dst (sha256 verified)"
  fi
  # pct exec has a short PATH without /usr/local/bin; check with the PATH a systemd unit gets.
  in_ct "$CT" env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin sh -c 'for b in python3 git gh claude; do printf "%s: " $b; command -v $b; done; python3 -c "import yaml; print(\"python3-yaml\", yaml.__version__)"; gh --version | head -1; claude --version'
}

do_clone() {
  local bundle="${1:?clone needs the git bundle path on the host}" want="${2:?clone needs the expected sha}"
  [[ -r "$bundle" ]] || die "no bundle at $bundle"
  if in_ct "$CT" test -d /opt/gamepilot/.git; then echo "ok: /opt/gamepilot already cloned"
  else
    pct push "$CT" "$bundle" /root/gamepilot.bundle
    in_ct "$CT" git clone -q --branch main /root/gamepilot.bundle /opt/gamepilot
    in_ct "$CT" git -C /opt/gamepilot remote set-url origin https://github.com/DraconisCluster/SDG-GAMEPILOT.git
    in_ct "$CT" rm -f /root/gamepilot.bundle
  fi
  local head; head=$(in_ct "$CT" git -C /opt/gamepilot rev-parse HEAD)
  [[ "$head" == "$want"* ]] || die "/opt/gamepilot is at $head, not $want"
  echo "ok: /opt/gamepilot at $head (origin = github, fetches need the owner's token)"
  in_ct "$CT" bash /opt/gamepilot/deploy/install.sh
  in_ct "$CT" systemctl is-enabled gamepilot-conductor.timer 2>/dev/null | sed 's/^/timer: /' || true
}

do_verify() {
  local u r fail=0
  # %{http_connect} is the proxy's answer to CONNECT: 200 = tunnel opened (allowlisted), 403 = refused by the filter.
  # The site's own status after that (claude.ai may well say 403 to a bare HEAD) is reported, not judged.
  for u in https://api.github.com https://github.com https://api.anthropic.com https://claude.ai https://console.anthropic.com; do
    r=$(in_unit /usr/bin/curl -sI -o /dev/null -w '%{http_connect} %{http_code}' -m 20 "$u" || true)
    if [[ "$r" == "200 "* && "${r#* }" != 000 ]]; then echo "PASS  allowed  $u -> tunnel 200, site HTTP ${r#* }"
    else echo "FAIL  allowed  $u -> ${r:-no answer}"; fail=1; fi
  done
  for u in https://example.com https://registry.npmjs.org https://objects.githubusercontent.com; do
    r=$(in_unit /usr/bin/curl -sI -o /dev/null -w '%{http_connect} %{http_code}' -m 20 "$u" || true)
    if [[ "$r" == "403 "* ]]; then echo "PASS  refused  $u (proxy CONNECT 403)"
    else echo "FAIL  refused  $u -> ${r:-no answer}"; fail=1; fi
  done
  r=$(in_unit /usr/bin/curl -s -o /dev/null -w '%{http_code}' -m 20 http://deb.debian.org/debian/dists/bookworm/Release || true)
  [[ "$r" == 200 ]] && echo "PASS  allowed  http://deb.debian.org (apt over plain HTTP)" \
    || { echo "FAIL  allowed  http://deb.debian.org -> $r"; fail=1; }
  if in_ct "$CT" sudo -u gamepilot -H sh -c 'test "$https_proxy" = http://192.168.6.155:3129'; then
    echo "PASS  sudo -u gamepilot gets the proxy (sudoers env_file)"
  else echo "FAIL  sudo -u gamepilot has no proxy"; fail=1; fi
  # claude.ai connectors (the owner's Gmail/Drive via mcp-proxy.anthropic.com) are off for units and sudo sessions.
  if in_unit /bin/sh -c 'test "$ENABLE_CLAUDEAI_MCP_SERVERS" = false'      && in_ct "$CT" sudo -u gamepilot -H sh -c 'test "$ENABLE_CLAUDEAI_MCP_SERVERS" = false'; then
    echo "PASS  claude.ai connectors off (units + sudo)"
  else echo "FAIL  ENABLE_CLAUDEAI_MCP_SERVERS is not false everywhere"; fail=1; fi
  # Without the proxy nothing leaves at all: not the internet, not the LAN, not the router, not the runner.
  for u in https://1.1.1.1 http://192.168.6.153:8000/healthz http://10.81.0.10:22 http://192.168.6.1; do
    if in_ct "$CT" env -u https_proxy -u HTTPS_PROXY -u http_proxy -u HTTP_PROXY curl -s -o /dev/null -m 8 "$u"
    then echo "FAIL  direct   $u reachable"; fail=1; else echo "PASS  direct   $u blocked"; fi
  done
  (( fail == 0 )) && echo "verify: all passed" || { echo "verify: FAILURES"; return 1; }
}

do_diff() {
  local drift=0 e where src dst want got
  for e in "${MANIFEST[@]}"; do
    where=$(field "$e" 1); src=$(field "$e" 2); dst=$(field "$e" 3)
    want=$(sha256sum "$F/$src" | cut -d' ' -f1); got=$(live_sha "$where" "$dst")
    if [[ "$want" == "$got" ]]; then printf 'ok     %-5s %s\n' "$where" "$dst"
    else printf 'DRIFT  %-5s %s\n' "$where" "$dst"; drift=1; fi
  done
  grep -qx 'auto vmbrgp' /etc/network/interfaces && echo "ok     host  vmbrgp in /etc/network/interfaces" \
    || { echo "DRIFT  host  vmbrgp missing from /etc/network/interfaces"; drift=1; }
  /usr/local/sbin/gamepilot-net.sh check 2>/dev/null && echo "ok     host  SNAT 10.82.0.0/24" \
    || { echo "DRIFT  host  SNAT 10.82.0.0/24 missing"; drift=1; }
  local cfg q k v; cfg=$(pct config "$CT" 2>/dev/null || true)
  for q in "${CT_EXPECT[@]}"; do
    k=${q%%:*}; v=${q#*:}
    if grep -E "^${k}: " <<<"$cfg" | grep -qF -- "$v"; then echo "ok     ct    $k has $v"
    else echo "DRIFT  ct    $k: expected $v"; drift=1; fi
  done
  (( drift == 0 )) && echo "no drift" || echo "DRIFT found"
  return "$drift"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  manifest) for e in "${MANIFEST[@]}"; do printf '%-5s %-34s -> %s (%s %s)\n' "$(field "$e" 1)" "$(field "$e" 2)" "$(field "$e" 3)" "$(field "$e" 4)" "$(field "$e" 5)"; done ;;
  bridge) do_bridge ;;
  host) do_host ;;
  proxy) do_proxy ;;
  create) do_create ;;
  provision) do_provision ;;
  clone) do_clone "$@" ;;
  verify) do_verify ;;
  diff) do_diff ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
