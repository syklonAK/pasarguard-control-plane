#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]]||{ echo ".env is missing." >&2;exit 1; }
umask 077
SKIP_ROTATE=0
for argument in "$@";do case "$argument" in
 --skip-rotate) SKIP_ROTATE=1;;
 -h|--help) echo "Usage: bash scripts/backup.sh [--skip-rotate]";exit 0;;
 *) echo "Unknown option: $argument" >&2;exit 1;;
esac;done
mkdir -p backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="backups/control-$STAMP.sql.gz"
echo "== Dumping the control database"
# The pipe hides pg_dump's exit status, so the failure has to be caught from the pipeline itself.
if ! docker compose -f docker-compose.enterprise.yml -f docker-compose.production.yml exec -T postgres \
     pg_dump -U control -d control | gzip -9 > "$OUT";then rm -f "$OUT";echo "pg_dump failed; no backup written." >&2;exit 1;fi
echo "== Verifying the archive"
[[ -s "$OUT" ]]||{ rm -f "$OUT";echo "The backup is empty." >&2;exit 1; }
gzip -t "$OUT" 2>/dev/null||{ rm -f "$OUT";echo "The backup is corrupted." >&2;exit 1; }
if ! gunzip -c "$OUT"|grep -q 'CREATE SCHEMA public\|PostgreSQL database dump';then rm -f "$OUT";echo "The backup does not contain a PostgreSQL dump." >&2;exit 1;fi
if [[ $SKIP_ROTATE -eq 0 ]];then
 KEEP="${BACKUP_RETENTION:-30}"
 [[ "$KEEP" =~ ^[0-9]+$ ]]&&[[ "$KEEP" -gt 0 ]]||KEEP=30
 # Rotation counts files, so a manual --skip-rotate snapshot can never delete a scheduled one.
 mapfile -t existing < <(find backups -type f -name 'control-*.sql.gz'|sort)
 if [[ ${#existing[@]} -gt $KEEP ]];then
  for stale in "${existing[@]:0:${#existing[@]}-KEEP}";do rm -f "$stale"&&echo "Rotated out: $stale";done
 fi
fi
echo "Backup created: $OUT ($(du -h "$OUT"|cut -f1))"
