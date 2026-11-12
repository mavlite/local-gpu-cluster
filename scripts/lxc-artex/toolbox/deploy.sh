#!/usr/bin/env bash
# deploy.sh — load the toolbox image into LXC 158 and switch ARTEX onto it.
# Run ON THE PROXMOX HOST (it uses pct):
#
#   deploy.sh <image.tar.gz built in the build CT> [tag]
#
# Refuses to run while a task is active (a swap restarts the container and requeues
# running intents). Tags the image ARTEX is currently on as artex:pre-toolbox-<date>
# so a rollback is one retag away, then verifies the egress lock still holds afterwards.
set -euo pipefail

IMG=${1:?usage: deploy.sh <image.tar.gz> [tag]}
TAG=${2:-artex:toolbox-20261111}
CTID=158
STAMP=$(date +%Y%m%d)

pin() { pct exec "$CTID" -- bash -c "$1"; }

echo "[1/6] refuse if a task is running"
active=$(pin "docker exec artex-global-postgres-1 psql -U artex -d artex -At -c \
  \"select count(*) from tasks where status not in ('done','failed','timeout','created')\"")
if [ "$active" != "0" ]; then
  echo "ABORT: $active task(s) still active — pause them in the UI first." >&2
  exit 1
fi
echo "      no active tasks"

echo "[2/6] copy image into CT $CTID"
pin "mkdir -p /root/artex-images"
pct push "$CTID" "$IMG" "/root/artex-images/$(basename "$IMG")"

echo "[3/6] load"
pin "gunzip -c /root/artex-images/$(basename "$IMG") | docker load"

echo "[4/6] tag the current image for rollback, then switch artex:local"
# Never reuse an existing rollback tag. A second deploy on the same day would otherwise
# repoint artex:pre-toolbox-<date> at the image being replaced, destroying the only named
# pointer back to the image before that — the rollback would land on the wrong one.
ROLLBACK="artex:pre-toolbox-$STAMP"
n=2
while pin "docker image inspect $ROLLBACK >/dev/null 2>&1"; do
  ROLLBACK="artex:pre-toolbox-$STAMP-$n"
  n=$((n + 1))
done
pin "docker tag artex:local $ROLLBACK"
echo "      rollback tag: $ROLLBACK"
pin "docker tag $TAG artex:local"
pin "docker images artex --format '{{.Tag}}\t{{.Size}}'"

echo "[5/6] restart ARTEX"
pin "cd /root/artex-global && docker compose up -d artex"
sleep 10
pin "docker logs --since 2m artex-global-artex-1 2>&1 | grep -E 'listening|LLM configured' | head -3"
pin "curl -s -o /dev/null -w 'ui %{http_code}\n' http://127.0.0.1:8787/"

echo "[6/6] verify the egress lock still holds and the toolbox is present"
pin "docker exec artex-global-artex-1 sh -c '
  wget -q -T5 -O- https://pypi.org >/dev/null 2>&1 && echo \"INTERNET OPEN - STOP\" || echo \"internet blocked (good)\"
  curl -s -o /dev/null -w \"router %{http_code}\n\" http://192.168.6.153:8000/healthz
  head -8 /opt/TOOLBOX.txt'"

cat <<EOF

Done. Rollback if needed:
  pct exec 158 -- bash -c 'docker tag $ROLLBACK artex:local \\
    && cd /root/artex-global && docker compose up -d artex'
EOF
