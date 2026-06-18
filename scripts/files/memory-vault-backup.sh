#!/usr/bin/env bash
# memory-vault-backup.sh — pg_dump the Memory Vault DB to /tank/backups.
# Installed in LXC 156 and run by memory-vault-backup.timer (daily).
set -Eeuo pipefail
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="/opt/memory-vault-data/backups"          # on the tank bind mount -> snapshotted
mkdir -p "$OUT"
cd /opt/memory-vault
docker compose exec -T db pg_dump -U memory_vault memory_vault \
  | gzip > "$OUT/memory_vault-${STAMP}.sql.gz"
# Retain 14 most recent dumps.
ls -1t "$OUT"/memory_vault-*.sql.gz | tail -n +15 | xargs -r rm -f
echo "backup written: $OUT/memory_vault-${STAMP}.sql.gz"
