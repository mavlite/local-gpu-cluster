#!/usr/bin/env bash
# 56-fan-control.sh — Phase 5.13.3-5.13.4 of setup-runbook.md.
#
# Installs the HOST-side fan-control bridge that reads the V620 max edge
# temperature published by LXC 151 (via the bind-mounted /var/lib/v620-temps)
# and writes a PWM duty cycle to a motherboard fan header.
#
# Requires FAN_PWM_PATH in config.env (e.g. /sys/class/hwmon/hwmon3/pwm4).
# Discover the right path with `sensors-detect --auto`, `sensors`, and a
# manual probe (echo 64 > .../pwmN; listen; echo 255 > .../pwmN).

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
  echo "Run these to identify the right PWM file:"
  echo "  apt install -y lm-sensors"
  echo "  sensors-detect --auto       # answer yes to all defaults"
  echo "  sensors                     # see what was detected"
  echo "  ls /sys/class/hwmon/"
  echo "  # On ASUS X870E you'll typically see nct6798-isa-0290."
  echo "  # Test each pwmN: switch to manual mode and probe duty:"
  echo "    echo 1   > /sys/class/hwmon/hwmonX/pwmN_enable"
  echo "    echo 64  > /sys/class/hwmon/hwmonX/pwmN     # ~25% — listen"
  echo "    echo 255 > /sys/class/hwmon/hwmonX/pwmN     # 100% — listen"
  echo "  # When you find the one that drives the V620 shroud fans, set"
  echo "  # FAN_PWM_PATH=/sys/class/hwmon/hwmonX/pwmN in config.env"
  echo "  # and re-run this script."
  exit 1
fi

[[ -w "$FAN_PWM_PATH" ]] || die "FAN_PWM_PATH is not writable: $FAN_PWM_PATH"
[[ -w "${FAN_PWM_PATH}_enable" ]] || die "${FAN_PWM_PATH}_enable missing — wrong PWM file?"

apt_install_if_missing lm-sensors

step "Install host fan-bridge script + service"

write_file_if_changed /usr/local/bin/v620-fan-bridge.sh 0755 <<EOF
#!/bin/bash
# v620-fan-bridge.sh — reads /var/lib/v620-temps/current-temp (published by
# the LXC 151 publisher) and translates max edge temp into a PWM duty cycle.

TEMP_FILE="/var/lib/v620-temps/current-temp"
PWM="$FAN_PWM_PATH"
ENABLE="\${PWM}_enable"

# Manual mode
echo 1 > "\$ENABLE"

while true; do
    if [ -r "\$TEMP_FILE" ]; then
        TEMP=\$(cat "\$TEMP_FILE" 2>/dev/null)
        TEMP=\${TEMP:-65}

        if   [ "\$TEMP" -lt 50 ]; then PWM_VAL=64
        elif [ "\$TEMP" -lt 60 ]; then PWM_VAL=102
        elif [ "\$TEMP" -lt 70 ]; then PWM_VAL=153
        elif [ "\$TEMP" -lt 80 ]; then PWM_VAL=204
        else                          PWM_VAL=255
        fi

        echo "\$PWM_VAL" > "\$PWM"
    else
        # File missing -> safe fail-over to 75%
        echo 192 > "\$PWM"
    fi
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

ok "Fan bridge active. PWM target: $FAN_PWM_PATH"
echo "  Stress test to confirm:"
echo "    journalctl -u v620-fan-bridge -f &"
echo "    pct exec ${AMD_VMID:-151} -- bash -c 'cd /opt/llama.cpp && ./build/bin/llama-bench -m /opt/models/*.gguf -ngl all'"
