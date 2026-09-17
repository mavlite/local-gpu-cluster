#!/bin/bash
# v620-powercap-apply.sh — lower the V620 power limit below the VBIOS 250 W floor.
#
# Installed by 66-v620-powercap.sh as /usr/local/sbin/v620-powercap-apply.sh and
# run once per boot by v620-powercap.service (before pve-guests starts LXC 151).
# Safe to re-run: a card that already carries the soft table only gets its cap set.
#
# Why: the V620 VBIOS PowerPlay table ships the OverDrive POWER_LIMIT cap bit
# as 0, so amdgpu pins power1_cap_min == max == 250 W. This script writes a
# "soft" PowerPlay table (kernel RAM only — nothing is flashed, a reboot
# restores stock) that differs from stock in exactly two driver-side bytes:
#   0x0c5  overdrive_table.cap[3]  (POWER_LIMIT)      0 -> 1
#   0x202  overdrive_table.min[8]  (POWERPERCENTAGE)  7 -> V620_PPT_FLOOR_PCT
# Both sit before smc_pptable (0x322), so the SMU firmware receives a
# byte-identical table; only the kernel's allowed range changes. OverDrive stays
# off in ppfeaturemask, so the maximum cannot rise above 250 W.
#
# The write re-initialises the SMU. Guards:
#   - exact PCI ids, VBIOS and stock-table md5 (the byte offsets are only known
#     for this table)
#   - GFXOFF disabled via amdgpu.ppfeaturemask (drm/amd#2060: pp_table writes
#     racing GFXOFF hard-lock Navi 21; Secure Boot lockdown blocks the runtime
#     debugfs toggle)
#   - card idle (no VRAM in use, no GPU users in LXC 151)
# Any failed guard aborts and leaves the card on its current table.
set -euo pipefail

CONF=/etc/default/v620-powercap
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"
CAP_W="${V620_POWER_CAP_W:-}"
FLOOR_PCT="${V620_PPT_FLOOR_PCT:-40}"

STOCK_MD5=caa6facc23bbe335abe26f8cd587f2b2
STOCK_VBIOS=113-D6030500-100
STOCK_W=250
OFF_CAP_POWER_LIMIT=197     # 0x0c5, u8
OFF_MIN_POWERPCT=514        # 0x202, u32 LE
SMC_PPTABLE_OFFSET=802      # 0x322
PP_GFXOFF_MASK=0x8000
IDLE_VRAM_MIB=512
GPU_LXC=151
STATE=/var/lib/v620-powercap   # stock.bin: verified copy of the VBIOS table

log() { echo "v620-powercap: $*"; }
die() { echo "v620-powercap: ABORT: $*" >&2; exit 1; }
md5() { md5sum | cut -d' ' -f1; }

[[ "$CAP_W" =~ ^[0-9]+$ ]] || die "V620_POWER_CAP_W must be set to an integer in $CONF (got '$CAP_W')"
[[ "$FLOOR_PCT" =~ ^[0-9]+$ ]] || die "V620_PPT_FLOOR_PCT must be an integer (got '$FLOOR_PCT')"
(( FLOOR_PCT >= 7 && FLOOR_PCT <= 40 )) || die "V620_PPT_FLOOR_PCT must be 7-40"
floor_w=$(( STOCK_W * (100 - FLOOR_PCT) / 100 ))
(( CAP_W >= floor_w && CAP_W <= STOCK_W )) || die "cap ${CAP_W} W outside ${floor_w}-${STOCK_W} W"

gpu_users_in_lxc() {
    pct status "$GPU_LXC" 2>/dev/null | grep -q running || return 1
    [ -n "$(pct exec "$GPU_LXC" -- sh -c 'fuser /dev/kfd /dev/dri/* 2>/dev/null' < /dev/null | tr -d ' ' || true)" ]
}

build_table() {  # $1 = stock table, $2 = output
    cp "$1" "$2"
    [ "$(od -An -tu1 -j $OFF_CAP_POWER_LIMIT -N1 "$2" | tr -d ' ')" = 0 ] || die "unexpected cap[3] byte in stock table"
    [ "$(od -An -tu4 -j $OFF_MIN_POWERPCT -N4 "$2" | tr -d ' ')" = 7 ] || die "unexpected min[8] value in stock table"
    printf '\001' | dd of="$2" bs=1 seek=$OFF_CAP_POWER_LIMIT conv=notrunc status=none
    # shellcheck disable=SC2059  # the format string is the escaped byte sequence
    printf "$(printf '\\%03o\\000\\000\\000' "$FLOOR_PCT")" \
        | dd of="$2" bs=1 seek=$OFF_MIN_POWERPCT conv=notrunc status=none
    cmp -s <(tail -c +$((SMC_PPTABLE_OFFSET + 1)) "$1") <(tail -c +$((SMC_PPTABLE_OFFSET + 1)) "$2") \
        || die "patched table differs inside smc_pptable"
    [ "$(cmp -l "$1" "$2" | wc -l)" -le 2 ] || die "patched table differs from stock in more than two bytes"
}

write_table() {  # $1 = device dir, $2 = card name, $3 = table
    local dev="$1" name="$2" table="$3" mask since
    mask=$(cat /sys/module/amdgpu/parameters/ppfeaturemask)
    (( (mask & PP_GFXOFF_MASK) == 0 )) \
        || die "GFXOFF enabled (ppfeaturemask=$(printf 0x%x "$mask")); boot with amdgpu.ppfeaturemask=0xfff73fff"
    gpu_users_in_lxc && die "$name: GPU in use in LXC $GPU_LXC; stop the llama services first"
    (( $(cat "$dev/mem_info_vram_used") < IDLE_VRAM_MIB * 1048576 )) || die "$name: VRAM in use, GPU not idle"
    [ "$(cat "$dev/gpu_busy_percent")" -le 5 ] || die "$name: GPU busy"

    # The kernel rejects a size mismatch, but a truncated write must never reach it.
    [ "$(stat -c %s "$table")" = "$(wc -c < "$dev/pp_table")" ] || die "$name: table size differs from the live table"
    [ "$(stat -c %s "$table")" -le 4096 ] || die "$name: table larger than the single-write block"

    since=$(date '+%Y-%m-%d %H:%M:%S')
    log "$name: writing soft PowerPlay table (floor ${FLOOR_PCT}%)"
    dd if="$table" of="$dev/pp_table" bs=4096 count=1 status=none
    sleep 3
    if journalctl -k --since "$since" --no-pager | grep -qiE 'smu reset failed|device lost|failed to setup'; then
        die "$name: kernel reported an SMU error after the write — reboot to restore stock"
    fi
    [ "$(md5 < "$dev/pp_table")" = "$(md5 < "$table")" ] || die "$name: live table does not match what was written"
}

apply_card() {  # $1 = /sys/class/drm/cardN/device
    local dev="$1" name hw live_md5 want
    name=$(basename "$(readlink -f "$dev")")
    hw=""
    for _ in $(seq 1 15); do   # hwmon can register after pp_table on a slow boot
        hw=$(find "$dev/hwmon" -mindepth 1 -maxdepth 1 -name 'hwmon*' 2>/dev/null | head -1)
        [ -n "$hw" ] && break
        sleep 2
    done
    [ -n "$hw" ] || die "$name: no hwmon directory"
    [ "$(cat "$dev/vbios_version")" = "$STOCK_VBIOS" ] || die "$name: unexpected VBIOS"

    live_md5=$(md5 < "$dev/pp_table")
    if [ ! -f "$STATE/stock.bin" ]; then
        [ "$live_md5" = "$STOCK_MD5" ] || die "$name: no stock backup yet and live table is not stock ($live_md5)"
        cat "$dev/pp_table" > "$STATE/stock.bin"
    fi
    [ "$(md5 < "$STATE/stock.bin")" = "$STOCK_MD5" ] || die "$STATE/stock.bin is corrupt"

    want="$STATE/soft-floor${FLOOR_PCT}.bin"
    build_table "$STATE/stock.bin" "$want"
    if [ "$live_md5" = "$(md5 < "$want")" ]; then
        log "$name: soft table already applied"
    elif [ "$live_md5" = "$STOCK_MD5" ]; then
        write_table "$dev" "$name" "$want"
    else
        die "$name: live table is neither stock nor floor ${FLOOR_PCT}% ($live_md5); reboot to restore stock first"
    fi

    (( $(cat "$hw/power1_cap_min") <= CAP_W * 1000000 )) || die "$name: power1_cap_min still above ${CAP_W} W"
    echo $((CAP_W * 1000000)) > "$hw/power1_cap"
    log "$name: power1_cap=$(( $(cat "$hw/power1_cap") / 1000000 )) W (range $(( $(cat "$hw/power1_cap_min") / 1000000 ))-$(( $(cat "$hw/power1_cap_max") / 1000000 )) W)"
}

find_cards() {
    local dev
    for dev in /sys/class/drm/card*/device; do
        [ "$(cat "$dev/vendor" 2>/dev/null)" = 0x1002 ] || continue
        [ "$(cat "$dev/device")" = 0x73a1 ] || continue
        [ "$(cat "$dev/subsystem_device")" = 0x0e34 ] || continue
        if [ -w "$dev/pp_table" ]; then readlink -f "$dev"; fi
    done | sort -u
}

mkdir -p "$STATE"
cards=()
for _ in $(seq 1 30); do   # the amdgpu pp_table nodes can lag early boot
    mapfile -t cards < <(find_cards)
    [ ${#cards[@]} -gt 0 ] && break
    sleep 2
done
[ ${#cards[@]} -gt 0 ] || die "no V620 with a writable pp_table found"

for dev in "${cards[@]}"; do
    apply_card "$dev"
done
