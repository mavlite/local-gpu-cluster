#!/usr/bin/env bash
# build.sh — build the ARTEX toolbox image on a machine WITH internet and Docker
# (a temporary build CT; LXC 158 itself stays egress-locked).
#
#   build.sh <base-image.tar.gz> <output-image.tar.gz> [tag]
#
# <base-image.tar.gz> is `docker save artex:local | gzip` from LXC 158 (the running,
# locally patched image). Writes lock/inventory files next to this script:
#   requirements.lock  pip freeze of the result (copy over requirements.txt to pin)
#   apt.lock           packages added or changed versus the base image
#   seclists.sha256    checksums of the baked wordlists
#   TOOLBOX.txt        the inventory also baked in at /opt/TOOLBOX.txt
set -euo pipefail

IN=${1:?usage: build.sh <base-image.tar.gz> <output-image.tar.gz> [tag]}
OUT=${2:?usage: build.sh <base-image.tar.gz> <output-image.tar.gz> [tag]}
TAG=${3:-artex:toolbox}
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=artex:local

echo "[1/4] load base image"
gunzip -c "$IN" | docker load
docker image inspect "$BASE" >/dev/null

echo "[2/4] build $TAG FROM $BASE"
docker build --pull=false --build-arg BASE="$BASE" -t "$TAG" "$HERE"

echo "[3/4] lock + inventory files"
pkgs() { docker run --rm --entrypoint dpkg-query "$1" -W -f='${Package}=${Version}\n' | sort; }
comm -13 <(pkgs "$BASE") <(pkgs "$TAG") > "$HERE/apt.lock"
docker run --rm --entrypoint pip "$TAG" freeze > "$HERE/requirements.lock"
docker run --rm --entrypoint cat "$TAG" /usr/share/seclists/SHA256SUMS > "$HERE/seclists.sha256"
docker run --rm --entrypoint cat "$TAG" /opt/TOOLBOX.txt > "$HERE/TOOLBOX.txt"
wc -l "$HERE/apt.lock" "$HERE/requirements.lock" "$HERE/seclists.sha256"

echo "[4/4] save $TAG -> $OUT"
docker save "$TAG" | gzip -1 > "$OUT"
ls -la "$OUT"
sha256sum "$OUT"
