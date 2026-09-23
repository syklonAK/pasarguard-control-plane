#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."; mkdir -p backups; umask 077
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"; OUT="backups/control-$STAMP.sql.gz"
docker compose -f docker-compose.enterprise.yml exec -T postgres pg_dump -U control -d control | gzip -9 > "$OUT"
find backups -type f -name 'control-*.sql.gz' -mtime +14 -delete
echo "✅ $OUT"
