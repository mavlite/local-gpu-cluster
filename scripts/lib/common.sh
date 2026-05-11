# shellcheck shell=bash
# common.sh — shared helpers for local-gpu-cluster bootstrap scripts.
# Source this from each phase script; do not execute directly.

set -Eeuo pipefail

# ---------- logging ----------
__LGC_NC='\033[0m'; __LGC_RED='\033[0;31m'; __LGC_GRN='\033[0;32m'
__LGC_YLW='\033[0;33m'; __LGC_BLU='\033[0;34m'; __LGC_CYN='\033[0;36m'

log()  { printf "${__LGC_BLU}[%s]${__LGC_NC} %s\n" "$(date +%H:%M:%S)" "$*"; }
ok()   { printf "${__LGC_GRN}[ ok ]${__LGC_NC} %s\n" "$*"; }
warn() { printf "${__LGC_YLW}[warn]${__LGC_NC} %s\n" "$*" >&2; }
err()  { printf "${__LGC_RED}[err ]${__LGC_NC} %s\n" "$*" >&2; }
step() { printf "\n${__LGC_CYN}==> %s${__LGC_NC}\n" "$*"; }
skip() { printf "${__LGC_YLW}[skip]${__LGC_NC} %s\n" "$*"; }

die() { err "$*"; exit 1; }

trap '__lgc_on_err $LINENO $?' ERR
__lgc_on_err() {
  err "Failed at line $1 (exit $2). Re-run the script; phases are idempotent."
}

# ---------- environment checks ----------
require_root() {
  [[ $EUID -eq 0 ]] || die "Must run as root."
}

require_pve_host() {
  command -v pveversion >/dev/null 2>&1 || die "pveversion not found — run this on the Proxmox host."
  command -v pct        >/dev/null 2>&1 || die "pct not found — run this on the Proxmox host."
}

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "Missing required command: $c"
  done
}

load_config() {
  # Load config.env from the scripts/ directory (parent of phase script).
  local cfg="${LGC_CONFIG:-${LGC_DIR:-$(dirname "${BASH_SOURCE[1]}")/..}/config.env}"
  if [[ -r "$cfg" ]]; then
    # shellcheck disable=SC1090
    source "$cfg"
    ok "Loaded config: $cfg"
  else
    warn "No config.env found at $cfg — using defaults. Copy config.env.example to config.env."
  fi
}

# ---------- idempotency helpers ----------
pkg_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed"
}

apt_install_if_missing() {
  local missing=()
  for p in "$@"; do
    pkg_installed "$p" || missing+=("$p")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    log "Installing: ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt install -y "${missing[@]}"
  else
    skip "All requested packages already installed."
  fi
}

lxc_exists() {
  pct status "$1" >/dev/null 2>&1
}

lxc_running() {
  pct status "$1" 2>/dev/null | grep -q running
}

lxc_get_ip() {
  # Returns just the IPv4 address (no CIDR). Empty on failure.
  pct exec "$1" -- ip -4 -o addr show eth0 2>/dev/null \
    | awk '{print $4}' | cut -d/ -f1 | head -1
}

wait_for_lxc_network() {
  local id="$1" timeout="${2:-60}" ip=""
  log "Waiting up to ${timeout}s for LXC $id to get an IPv4 address..."
  for ((i=0; i<timeout; i++)); do
    ip=$(lxc_get_ip "$id" || true)
    if [[ -n "$ip" ]]; then
      ok "LXC $id is reachable at $ip"
      echo "$ip"
      return 0
    fi
    sleep 1
  done
  die "Timed out waiting for LXC $id network."
}

ensure_lxc_started() {
  local id="$1"
  if lxc_running "$id"; then
    skip "LXC $id already running."
  else
    log "Starting LXC $id"
    pct start "$id"
    sleep 5
  fi
}

ensure_lxc_stopped() {
  local id="$1"
  if lxc_running "$id"; then
    log "Stopping LXC $id"
    pct stop "$id"
    # Wait for full stop to avoid `pct set` races.
    for _ in {1..30}; do
      lxc_running "$id" || return 0
      sleep 1
    done
    die "Timed out waiting for LXC $id to stop."
  fi
}

# Set a single LXC config key (e.g. dev0, mp0) only if the desired value differs.
# Stops the LXC if a change is needed, then restarts only if it was originally running.
# Usage: pct_set_if_changed VMID key "value"
pct_set_if_changed() {
  local id="$1" key="$2" value="$3"
  local current
  current="$(pct config "$id" 2>/dev/null | awk -v k="^$key:" '$0 ~ k {sub(/^[^:]+: */,""); print; exit}')"
  if [[ "$current" == "$value" ]]; then
    skip "$key on LXC $id already set to '$value'."
    return 0
  fi
  local was_running=0
  lxc_running "$id" && was_running=1
  (( was_running )) && ensure_lxc_stopped "$id"
  pct set "$id" "--$key" "$value"
  (( was_running )) && pct start "$id" && sleep 5
  ok "Set $key=$value on LXC $id"
}

# Validate IPv4 dotted-quad. Returns 0 if valid, 1 otherwise.
is_ipv4() {
  local ip="$1"
  [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  local -a octs
  read -ra octs <<< "${ip//./ }"
  for o in "${octs[@]}"; do
    (( o >= 0 && o <= 255 )) || return 1
  done
  return 0
}

# Run a heredoc'd script inside an LXC. Captures stderr into the parent log.
in_lxc() {
  local id="$1"; shift
  pct exec "$id" -- bash -lc "$*"
}

# ---------- file helpers ----------
# Atomically write a file with mode; only updates if content changed.
write_file_if_changed() {
  local path="$1" mode="$2"
  local tmp; tmp="$(mktemp)"
  # shellcheck disable=SC2064  # expand $tmp now, not at trap time
  trap "rm -f '$tmp'" RETURN
  cat > "$tmp"
  if [[ -f "$path" ]] && cmp -s "$tmp" "$path"; then
    skip "$path unchanged."
    return 0
  fi
  install -m "$mode" "$tmp" "$path"
  ok "Wrote $path"
}

# ---------- LXC file push from host ----------
push_to_lxc() {
  local id="$1" src="$2" dst="$3" mode="${4:-0644}"
  pct push "$id" "$src" "$dst" --perms "$mode"
}

# ---------- version checks ----------
pve_major() {
  pveversion 2>/dev/null | awk -F'/' '{print $2}' | cut -d. -f1
}

kernel_series() {
  uname -r | awk -F. '{print $1"."$2}'
}

# ---------- export functions used by phase scripts ----------
export -f log ok warn err step skip die require_root require_pve_host require_cmd 2>/dev/null || true
