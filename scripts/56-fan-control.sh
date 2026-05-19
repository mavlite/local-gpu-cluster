#!/usr/bin/env bash
# 56-fan-control.sh — Phase 5.13.3-5.13.4 of setup-runbook.md.
#
# Installs the HOST-side fan-control bridge that reads the V620 max edge
# temperature published by LXC 151 (via the bind-mounted /var/lib/v620-temps)
# and writes a PWM duty cycle to one or more motherboard fan headers.
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

# Validate every path
for p in $FAN_PWMS; do
  [[ -w "$p" ]] || die "PWM not writable: $p"
  [[ -w "${p}_enable" ]] || die "${p}_enable missing — wrong PWM file?"
done

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

# Use printf so we can safely interpolate the host's PAIRS list into the script body
write_file_if_changed /usr/local/bin/v620-fan-bridge.sh 0755 <<EOF
#!/bin/bash
# v620-fan-bridge.sh — reads /var/lib/v620-temps/current-temp (published by
# the LXC 151 publisher) and translates max V620 edge temp into a PWM duty
# cycle applied to every PWM target resolved below.
#
# PAIR format is "<chip-name-glob>:<pwmN>", e.g. "nct67??:pwm5". The chip side
# is a bash case-glob so we tolerate kernel-driver name drift across boots
# (nct6798 vs nct6799 for the same NCT6798D silicon). hwmonN numbering is
# also not stable, so we walk /sys/class/hwmon/* and match by chip name.

TEMP_FILE="/var/lib/v620-temps/current-temp"
PAIRS=( $PAIRS )

resolve_pair() {
    local pair="\$1"
    local chip_pat="\${pair%%:*}"
    local suffix="\${pair#*:}"
    local chip
    for h in /sys/class/hwmon/hwmon*; do
        chip="\$(cat \$h/name 2>/dev/null)" || continue
        case "\$chip" in
            \$chip_pat)
                [ -w "\$h/\$suffix" ] && { echo "\$h/\$suffix"; return 0; }
                ;;
        esac
    done
    return 1
}

PWMS=()
for pair in "\${PAIRS[@]}"; do
    p="\$(resolve_pair "\$pair")"
    if [ -n "\$p" ] && [ -w "\$p" ]; then
        PWMS+=( "\$p" )
        echo "v620-fan-bridge: \$pair -> \$p" >&2
    else
        echo "v620-fan-bridge: WARNING could not resolve \$pair — skipping" >&2
    fi
done

if [ "\${#PWMS[@]}" -eq 0 ]; then
    echo "v620-fan-bridge: FATAL no PWMs resolved, exiting" >&2
    exit 1
fi

# Switch every PWM to manual mode
for p in "\${PWMS[@]}"; do
    echo 1 > "\${p}_enable" 2>/dev/null || true
done

while true; do
    if [ -r "\$TEMP_FILE" ]; then
        TEMP=\$(cat "\$TEMP_FILE" 2>/dev/null)
        TEMP=\${TEMP:-65}

        if   [ "\$TEMP" -lt 50 ]; then PWM_VAL=64    # 25%
        elif [ "\$TEMP" -lt 60 ]; then PWM_VAL=102   # 40%
        elif [ "\$TEMP" -lt 70 ]; then PWM_VAL=153   # 60%
        elif [ "\$TEMP" -lt 80 ]; then PWM_VAL=204   # 80%
        else                          PWM_VAL=255    # 100%
        fi
    else
        # Temp file missing (LXC down) — safe fail-over to 75%
        PWM_VAL=192
    fi

    for p in "\${PWMS[@]}"; do
        echo "\$PWM_VAL" > "\$p"
    done
    sleep 5
done
EOF

write_file_if_changed /etc/systemd/system/v620-fan-bridge.service 0644 <<'EOF'
[Unit]
Description=V620 GPU temperature -> motherboard PWM bridge
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/local/bin/v620-fan-bridge.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now v620-fan-bridge.service
systemctl status v620-fan-bridge.service --no-pager || true

ok "Fan bridge active. PWM target(s): $FAN_PWMS"
echo "  Stress test to confirm:"
echo "    journalctl -u v620-fan-bridge -f &"
echo "    pct exec ${AMD_VMID:-151} -- bash -c 'cd /opt/llama.cpp && ./build/bin/llama-bench -m /opt/models/*.gguf -ngl all'"
