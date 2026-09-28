#!/usr/bin/env bash
# TrendHub veritabanının anlık yedeği (pg_dump custom format). Hiçbir şeyi değiştirmez.
#   ./deploy/backup.sh           -> backups/trendhub-<zaman>.dump
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
BACKUP_DIR="${TRENDHUB_BACKUP_DIR:-$PWD/backups}"
mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
envget() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2- ; }
OUT="$BACKUP_DIR/trendhub-$(date +%Y%m%d-%H%M%S).dump"
docker exec trendhub-db pg_dump -U "$(envget POSTGRES_USER)" -d "$(envget POSTGRES_DB)" -Fc > "$OUT"
chmod 600 "$OUT"
docker exec -i trendhub-db pg_restore -l < "$OUT" >/dev/null
echo "Yedek: $OUT ($(du -h "$OUT" | cut -f1))"
