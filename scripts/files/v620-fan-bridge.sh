#!/bin/bash
# v620-fan-bridge.sh — host side of V620 fan control. Reads the hotter card's JUNCTION temperature
# that LXC 151 publishes (v620-temp-publish.sh, bind-mounted /var/lib/v620-temps) and drives every
# configured motherboard PWM header to the duty below. Installed by 56-fan-control.sh; tunables
# come from /etc/default/v620-fan-bridge via the unit's EnvironmentFile.
#
# Blowers: BFB1012SHA01 on one SATA-powered splitter, PWM from the board, NO tach — so control is
# open-loop and the curve is the only thing between load and the 100 C junction trip.
#
# The nct67xx chip has no watchdog: a header in manual mode holds its last duty forever. So every
# way out of this script — stop, TERM, a failed write — drives FAN_FAILSAFE_PWM first, and the unit
# runs `v620-fan-bridge.sh --failsafe` as ExecStopPost to cover SIGKILL and failed starts.

FAN_HWMON_ROOT="${FAN_HWMON_ROOT:-/sys/class/hwmon}"

FAN_PAIRS="${FAN_PAIRS:-nct67??:pwm5 nct67??:pwm6}"   # <chip-name-glob>:<pwmN> per header
FAN_TEMP_FILE="${FAN_TEMP_FILE:-/var/lib/v620-temps/current-junction}"
FAN_TEMP_MAX_AGE="${FAN_TEMP_MAX_AGE:-10}"   # s; the publisher writes every 2 s
FAN_FAILSAFE_PWM="${FAN_FAILSAFE_PWM:-255}"  # missing/stale/unparsable reading
FAN_POLL_SECS="${FAN_POLL_SECS:-1}"
FAN_DECAY_STEP="${FAN_DECAY_STEP:-4}"        # max PWM drop per poll (~48 s from 100 % to 25 %)
FAN_MIN_PWM="${FAN_MIN_PWM:-64}"
FAN_BOOST_URL="${FAN_BOOST_URL:-http://192.168.6.153:8000/healthz}"
FAN_BOOST_WINDOW="${FAN_BOOST_WINDOW:-20}"   # s since the router saw a chat request
FAN_BOOST_PWM="${FAN_BOOST_PWM:-204}"

# Curve: hotter card's junction -> duty. From the 2026-10-08 loaded sweep at the 180 W cap:
# 100 % settles GPU0 at 76-79 C, 90 % at 81-84 C, 85 % reaches 88 C in 3 min and is still rising.
# So sustained load settles in the 90 % band, and 100 % takes over if GPU0 creeps past 83 C.
target_pwm() {
    local t=$((10#$1))   # base 10: "077" must not read as octal
    if   (( t < 60 )); then echo 64    # 25 %  idle, inaudible
    elif (( t < 70 )); then echo 128   # 50 %
    elif (( t < 78 )); then echo 191   # 75 %
    elif (( t < 84 )); then echo 230   # 90 %  lowest duty that holds full load
    else                    echo 255   # 100 %
    fi
}

# read_temp <file> <max_age_s> — prints the reading, or nothing when it is missing, older than
# max_age_s, dated in the future (a clock step must not revive a dead publisher's last value), or
# not a plain decimal integer.
read_temp() {
    local file="$1" max_age="$2" mtime age t
    mtime="$(stat -c %Y "$file" 2>/dev/null)" || return 0
    age=$(( $(date +%s) - mtime ))
    (( age >= -2 && age <= max_age )) || return 0
    t="$(head -c 16 "$file" 2>/dev/null)"   # $( ) strips only the trailing newline
    [[ "$t" =~ ^(0|[1-9][0-9]*)$ ]] && echo "$t"
    return 0
}

# decide_target <temp or ""> <seconds_since_chat or ""> — curve or fail-safe, raised (never
# lowered) by the feed-forward boost while a chat request is recent.
decide_target() {
    local temp="$1" ssc="$2" target
    if [[ -z "$temp" ]]; then
        target="$FAN_FAILSAFE_PWM"
    else
        target="$(target_pwm "$temp")"
    fi
    if [[ -n "$ssc" ]] && awk -v s="$ssc" -v w="$FAN_BOOST_WINDOW" 'BEGIN{exit !(s < w)}'; then
        (( FAN_BOOST_PWM > target )) && target="$FAN_BOOST_PWM"
    fi
    echo "$target"
}

# shape <current> <target> — ramp up at once, down by at most FAN_DECAY_STEP, never below
# FAN_MIN_PWM.
shape() {
    local cur="$1" target="$2"
    if (( target >= cur )); then
        cur="$target"
    else
        cur=$(( cur - FAN_DECAY_STEP ))
        (( cur < target )) && cur="$target"
    fi
    (( cur < FAN_MIN_PWM )) && cur="$FAN_MIN_PWM"
    echo "$cur"
}

# resolve_pair <chip-glob:pwmN> — hwmonN numbering and the nct67xx chip name both drift across
# boots (nct6798 vs nct6799 for the same silicon), so match the chip name by glob.
resolve_pair() {
    local chip_pat="${1%%:*}" suffix="${1#*:}" h chip
    for h in "$FAN_HWMON_ROOT"/hwmon*; do
        chip="$(cat "$h/name" 2>/dev/null)" || continue
        # shellcheck disable=SC2254  # chip_pat is a deliberate glob
        case "$chip" in
            $chip_pat) [[ -w "$h/$suffix" ]] && { echo "$h/$suffix"; return 0; } ;;
        esac
    done
    return 1
}

# write_pwms <duty> <pwm path>... — manual mode, then the duty, on every header. Keeps going past a
# failure so the other headers still get the duty; returns non-zero if any write failed.
write_pwms() {
    local duty="$1" p rc=0
    shift
    for p in "$@"; do
        { echo 1 > "${p}_enable" && echo "$duty" > "$p"; } 2>/dev/null || rc=1
    done
    return "$rc"
}

PWMS=()
resolve_all() {
    local pairs pair p
    read -ra pairs <<< "$FAN_PAIRS"   # read -a: the chip globs must not expand against the cwd
    for pair in "${pairs[@]}"; do
        if p="$(resolve_pair "$pair")"; then
            PWMS+=("$p")
            echo "v620-fan-bridge: $pair -> $p" >&2
        else
            echo "v620-fan-bridge: WARNING could not resolve $pair — skipping" >&2
        fi
    done
    (( ${#PWMS[@]} > 0 )) || { echo "v620-fan-bridge: FATAL no PWMs resolved" >&2; return 1; }
}

main() {
    local cur target temp ssc
    resolve_all || exit 1
    trap 'exit 143' TERM INT
    trap 'write_pwms "$FAN_FAILSAFE_PWM" "${PWMS[@]}"' EXIT   # every exit leaves the blowers at fail-safe

    cur="$FAN_FAILSAFE_PWM"   # start loud; the curve settles it within a minute
    while true; do
        temp="$(read_temp "$FAN_TEMP_FILE" "$FAN_TEMP_MAX_AGE")"
        ssc="$(curl -s -m 1 "$FAN_BOOST_URL" 2>/dev/null                | grep -oE '"seconds_since_chat":[0-9.]+' | head -1 | cut -d: -f2)"
        target="$(decide_target "$temp" "$ssc")"
        cur="$(shape "$cur" "$target")"
        # A failed write means the hwmon paths moved (driver reload) or vanished: exit so systemd
        # restarts the bridge and re-resolves, instead of looking healthy while holding nothing.
        write_pwms "$cur" "${PWMS[@]}"             || { echo "v620-fan-bridge: FATAL PWM write failed — exiting to re-resolve" >&2; exit 1; }
        sleep "$FAN_POLL_SECS"
    done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    if [[ "${1:-}" == "--failsafe" ]]; then   # ExecStopPost: also runs after SIGKILL / failed start
        resolve_all && write_pwms "$FAN_FAILSAFE_PWM" "${PWMS[@]}"
        exit
    fi
    main
fi
