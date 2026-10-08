#!/usr/bin/env bash
# 56-fan-control.sh — Phase 5.13.3-5.13.4 of setup-runbook.md.
#
# Installs the HOST-side fan-control bridge (scripts/files/v620-fan-bridge.sh) that reads the
# hotter V620's JUNCTION temperature published by LXC 151 (v620-temp-publish.sh, via the
# bind-mounted /var/lib/v620-temps) and writes a PWM duty cycle to one or more motherboard fan
# headers. The duty curve lives in the bridge file, next to the sweep data it came from.
#
# Two supported topologies:
#   (a) ONE shared PWM that drives everything (Lancool 217 hub model — all V620
#       shroud fans + case fans on the same curve). Set FAN_PWM_PATH to a single path.
#   (b) MULTIPLE V620 shroud fans on independent motherboard headers (e.g., CHA_FAN4
#       and CHA_FAN5 each carrying one shroud fan). Set FAN_PWM_PATH to a
#       space-separated OR comma-separated list. Case fans then live on a separate
#       header with its own BIOS curve, decoupled from V620 temps.
#
# Discover with `sensors-detect --auto`, `sensors`, and a manual probe
# (echo 64 > .../pwmN; listen; echo 255 > .../pwmN).

set -Eeuo pipefail
LGC_DIR="${LGC_DIR:-$(cd "$(dirname "$0")" && pwd)}"
# shellcheck source=lib/common.sh
source "$LGC_DIR/lib/common.sh"

require_root
require_pve_host
load_config

FAN_PWM_PATH="${FAN_PWM_PATH:-}"

# Fan tunables (override in config.env). Tuned 2026-06-06 to kill the V620
# thermal-alarm chirp at query start: the router stamps "chat active" in its
# /healthz the instant a request ARRIVES (before any GPU work), and the bridge
# polls that to PRE-RAMP the fans ahead of the prefill heat. The temp curve is
# the steady-state floor; ramp UP is instant, ramp DOWN is capped at
# FAN_DECAY_STEP per poll so the fan eases off slowly instead of oscillating.
FAN_POLL_SECS="${FAN_POLL_SECS:-1}"     # bridge poll interval, seconds (1s for fast feed-forward)
FAN_DECAY_STEP="${FAN_DECAY_STEP:-4}"   # max PWM (0-255) drop per poll on ramp-down (~48s 100%->25% glide)
FAN_MIN_PWM="${FAN_MIN_PWM:-64}"        # idle floor (64 = 25%)
# Feed-forward boost: when the router reports a chat request within the last
# FAN_BOOST_WINDOW seconds, drive the fans to FAN_BOOST_PWM regardless of temp.
FAN_BOOST_URL="${FAN_BOOST_URL:-http://192.168.6.153:8000/healthz}"
FAN_BOOST_WINDOW="${FAN_BOOST_WINDOW:-20}"   # seconds since last chat to keep boosting
# 204 (80%) since the 180 W cap (66-v620-powercap.sh) cut the prefill spike: the boost
# only needs a head start; the temperature curve above still takes the fans to 100%.
FAN_BOOST_PWM="${FAN_BOOST_PWM:-204}"        # PWM while boosting (255 = 100%)
# A missing, stale (> FAN_TEMP_MAX_AGE s) or unparsable junction reading drives FAN_FAILSAFE_PWM.
# 255: the 2026-10-08 sweep showed anything below 90% overheats GPU0 under sustained load.
FAN_TEMP_MAX_AGE="${FAN_TEMP_MAX_AGE:-10}"
FAN_FAILSAFE_PWM="${FAN_FAILSAFE_PWM:-255}"
for _v in FAN_POLL_SECS FAN_DECAY_STEP FAN_MIN_PWM FAN_BOOST_WINDOW FAN_BOOST_PWM FAN_TEMP_MAX_AGE FAN_FAILSAFE_PWM; do
  [[ "${!_v}" =~ ^[0-9]+$ ]] || die "$_v must be a non-negative integer (got '${!_v}')"
done
for _v in FAN_MIN_PWM FAN_BOOST_PWM FAN_FAILSAFE_PWM; do
  (( 10#${!_v} <= 255 )) || die "$_v must be a PWM duty 0-255 (got '${!_v}')"
done
(( 10#$FAN_FAILSAFE_PWM >= 230 )) || die "FAN_FAILSAFE_PWM must be >= 230 (90%): below it GPU0 overheats under load (got $FAN_FAILSAFE_PWM)"
(( 10#$FAN_POLL_SECS >= 1 ))      || die "FAN_POLL_SECS must be >= 1 (0 is a busy loop)"
(( 10#$FAN_DECAY_STEP >= 1 ))     || die "FAN_DECAY_STEP must be >= 1 (0 never ramps down)"
(( 10#$FAN_TEMP_MAX_AGE >= 4 ))   || die "FAN_TEMP_MAX_AGE must be >= 4 s (the publisher writes every 2 s)"

if [[ -z "$FAN_PWM_PATH" ]]; then
  step "Fan PWM discovery"
  warn "FAN_PWM_PATH is empty in config.env — cannot install fan bridge."
  echo
  echo "Run these to identify the right PWM file(s):"
  echo "  apt install -y lm-sensors"
  echo "  sensors-detect --auto       # answer yes to all defaults"
  echo "  sensors                     # see what was detected"
  echo "  ls /sys/class/hwmon/"
  echo "  # On ASUS X870E you'll typically see nct6798-isa-0290."
  echo "  # Test each pwmN: switch to manual mode and probe duty:"
  echo "    echo 1   > /sys/class/hwmon/hwmonX/pwmN_enable"
  echo "    echo 64  > /sys/class/hwmon/hwmonX/pwmN     # ~25% — listen"
  echo "    echo 255 > /sys/class/hwmon/hwmonX/pwmN     # 100% — listen"
  echo "  # Set FAN_PWM_PATH in config.env to ONE path (shared-hub topology) or to"
  echo "  # MULTIPLE paths space- or comma-separated (per-header topology):"
  echo "  #   FAN_PWM_PATH=\"/sys/class/hwmon/hwmon3/pwm5 /sys/class/hwmon/hwmon3/pwm6\""
  exit 1
fi

# Normalize to a space-separated list (accept comma-separated for convenience)
FAN_PWMS="${FAN_PWM_PATH//,/ }"

# Validate every path. hwmonN numbering drifts across boots (config said hwmon12, the chip was
# hwmon14 on 2026-10-08), and the bridge resolves chips by name at runtime anyway — so a path
# whose hwmonN moved falls back to the same pwmN on whichever nct67xx chip has it.
resolve_live_pwm() {
  local want="$1" pwm h
  [[ -w "$want" ]] && { echo "$want"; return 0; }
  pwm="$(basename "$want")"
  for h in /sys/class/hwmon/hwmon*; do
    [[ "$(cat "$h/name" 2>/dev/null)" == nct67* && -w "$h/$pwm" ]] && { echo "$h/$pwm"; return 0; }
  done
  return 1
}
_live_pwms=""
for p in $FAN_PWMS; do
  q="$(resolve_live_pwm "$p")" || die "PWM not writable: $p (and no nct67xx chip has $(basename "$p"))"
  [[ "$q" == "$p" ]] || warn "$p is now $q (hwmon numbering drifted) — using $q; consider updating config.env"
  [[ -w "${q}_enable" ]] || die "${q}_enable missing — wrong PWM file?"
  _live_pwms+="$q "
done
FAN_PWMS="${_live_pwms% }"

apt_install_if_missing lm-sensors

# Resolve each configured PWM path to a (chip_family_glob, pwm_suffix) pair so
# the bridge can find the right hwmon entry at startup even when the kernel
# assigns a different hwmonN number after reboot AND when the nct6775 driver
# reports a slightly different chip name across boots (e.g., nct6798 vs nct6799
# for the same physical Nuvoton silicon on ASUS X870E — observed in the wild).
#
# We detect the live chip name once at install time to validate the path is
# real, but emit a family glob (nct67??) into the generated bridge script so
# the resolver tolerates kernel-driver name drift. The bridge's resolve_pair
# uses bash case-glob matching for this.
#
# pwm_name is validated against strict character classes before being
# interpolated into the generated bash script. Hwmon chip names from the
# kernel are always sane (nct6799, k10temp, amdgpu, etc.) but a corrupted
# /sys/class/hwmon/*/name file or an unexpected hardware driver could produce
# something with whitespace or shell metacharacters; the validation makes
# sure we're actually pointed at a Nuvoton sensor before generating PAIRS.
PAIRS=""
CHIP_FAMILY_GLOB="nct67??"
for p in $FAN_PWMS; do
  hwmon_dir="$(dirname "$p")"
  pwm_name="$(basename "$p")"
  chip_name="$(cat "$hwmon_dir/name" 2>/dev/null || true)"
  [[ -n "$chip_name" ]] || die "Cannot read chip name from $hwmon_dir/name. Verify $p exists."
  [[ "$chip_name" =~ ^[A-Za-z0-9_-]+$ ]] \
    || die "Unexpected chip name '$chip_name' from $hwmon_dir/name (must match [A-Za-z0-9_-]+)."
  [[ "$chip_name" == nct67* ]] \
    || die "Chip at $p is '$chip_name', not a Nuvoton NCT67xx. Bridge currently only supports NCT67xx family."
  [[ "$pwm_name" =~ ^pwm[0-9]+$ ]] \
    || die "Unexpected pwm filename '$pwm_name' from $p (must match pwm[0-9]+)."
  PAIRS+="${CHIP_FAMILY_GLOB}:$pwm_name "
done
PAIRS="${PAIRS% }"

step "Install host fan-bridge script + service ($(echo "$FAN_PWMS" | wc -w) PWM target(s)) [resolver pairs: $PAIRS]"

[[ -r "$LGC_DIR/files/v620-fan-bridge.sh" ]] || die "missing $LGC_DIR/files/v620-fan-bridge.sh"
[[ "$FAN_BOOST_URL" =~ ^https?://[A-Za-z0-9.:/_-]+$ ]] || die "FAN_BOOST_URL looks malformed: '$FAN_BOOST_URL'"
write_file_if_changed /usr/local/bin/v620-fan-bridge.sh 0755 < "$LGC_DIR/files/v620-fan-bridge.sh"

# Tunables reach the bridge through the unit's EnvironmentFile (every value validated above).
write_file_if_changed /etc/default/v620-fan-bridge 0644 <<EOF
# Written by 56-fan-control.sh — edit config.env and re-run it instead.
FAN_PAIRS="$PAIRS"
FAN_TEMP_FILE=/var/lib/v620-temps/current-junction
FAN_TEMP_MAX_AGE=$FAN_TEMP_MAX_AGE
FAN_FAILSAFE_PWM=$FAN_FAILSAFE_PWM
FAN_POLL_SECS=$FAN_POLL_SECS
FAN_DECAY_STEP=$FAN_DECAY_STEP
FAN_MIN_PWM=$FAN_MIN_PWM
FAN_BOOST_URL=$FAN_BOOST_URL
FAN_BOOST_WINDOW=$FAN_BOOST_WINDOW
FAN_BOOST_PWM=$FAN_BOOST_PWM
EOF

write_file_if_changed /etc/systemd/system/v620-fan-bridge.service 0644 <<'EOF'
[Unit]
Description=V620 GPU temperature -> motherboard PWM bridge
After=multi-user.target

[Service]
Type=simple
EnvironmentFile=/etc/default/v620-fan-bridge
ExecStart=/usr/local/bin/v620-fan-bridge.sh
# The chip holds its last duty forever once the bridge is gone; this also runs after SIGKILL.
ExecStopPost=/usr/local/bin/v620-fan-bridge.sh --failsafe
SuccessExitStatus=143   # the TERM trap exits 143 after writing fail-safe; a stop is not a failure
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable v620-fan-bridge.service
systemctl restart v620-fan-bridge.service   # a running bridge keeps the old curve until restarted
systemctl status v620-fan-bridge.service --no-pager || true
[[ -s /var/lib/v620-temps/current-junction ]]   || warn "/var/lib/v620-temps/current-junction is missing — the blowers run at fail-safe ${FAN_FAILSAFE_PWM}/255 until the LXC ${AMD_VMID:-151} publisher (51-lxc-amd.sh, 5.13) is updated."

ok "Fan bridge active. PWM target(s): $FAN_PWMS"
echo "  Stress test to confirm:"
echo "    journalctl -u v620-fan-bridge -f &"
echo "    pct exec ${AMD_VMID:-151} -- bash -c 'cd /opt/llama.cpp && ./build/bin/llama-bench -m /opt/models/*.gguf -ngl all'"
