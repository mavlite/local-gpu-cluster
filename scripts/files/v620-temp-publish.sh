#!/bin/bash
# v620-temp-publish.sh — runs in LXC 151 (where ROCm lives). Every 2 s it publishes into the
# bind-mounted /var/lib/v620-temps:
#   current-junction  hotter card's JUNCTION temp — the sensor that trips at 100 C; the host
#                     fan bridge (v620-fan-bridge.sh) drives the blowers from this
#   current-temp      hotter card's edge temp (legacy; day-2 checks still read it)
# A card that cannot be read counts as FAIL_HOT, so a broken reading means full airflow, never
# a guess. (The old publisher defaulted a failed read to 60 C, which kept the fans at idle.)

OUT_DIR="${V620_TEMPS_DIR:-/var/lib/v620-temps}"
CARDS="${V620_CARDS:-2}"
SMI_TIMEOUT="${V620_SMI_TIMEOUT:-5}"   # s; a hung rocm-smi must not freeze the publisher
FAIL_HOT=105

# max_temp <sensor> — reads `rocm-smi --showtemp` text on stdin, prints the hotter card's value.
# Takes every GPU index it finds (an extra device can shift the V620s' indices), and prints FAIL_HOT
# when fewer than CARDS cards gave a readable value. "[)]" not "\)": portable across gawk and mawk.
max_temp() {
    awk -v s="$1" -v n="$CARDS" -v hot="$FAIL_HOT" '
        tolower($0) ~ ("sensor " s "[)]") && match($0, /GPU\[[0-9]+\]/) {
            i = substr($0, RSTART + 4, RLENGTH - 5); v = $NF
            if (v ~ /^[0-9]+(\.[0-9]+)?$/) { if (!(i in t)) got++; t[i] = int(v) }
        }
        END {
            if (got < n) { print hot; exit }
            m = 0; for (i in t) if (t[i] > m) m = t[i]; print m
        }'
}

publish() {  # publish <name> <value> — atomic replace, so the bridge never reads a partial file
    echo "$2" > "$OUT_DIR/$1.tmp" && mv "$OUT_DIR/$1.tmp" "$OUT_DIR/$1"
}

publish_once() {
    local text
    text="$(timeout "$SMI_TIMEOUT" rocm-smi --showtemp 2>/dev/null)"
    publish current-junction "$(max_temp junction <<< "$text")"
    publish current-temp "$(max_temp edge <<< "$text")"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    mkdir -p "$OUT_DIR"
    while true; do
        publish_once
        sleep 2
    done
fi
