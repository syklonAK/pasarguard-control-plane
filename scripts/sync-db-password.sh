#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml)

[[ -f .env ]] || { echo ".env is missing." >&2; exit 1; }
set -a; source .env; set +a

if [[ ! "${POSTGRES_PASSWORD:-}" =~ ^[A-Za-z0-9._~-]{16,256}$ ]]; then
  echo "POSTGRES_PASSWORD must be 16-256 URL-safe characters." >&2
  exit 1
fi

for _ in $(seq 1 60); do
  if docker compose "${COMPOSE[@]}" exec -T postgres \
    psql -U control -d postgres -Atqc 'SELECT 1' >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
[[ "${READY:-0}" == 1 ]] || { echo "PostgreSQL local administration is unavailable." >&2; exit 1; }

printf "ALTER ROLE control WITH LOGIN PASSWORD '%s';\n" "$POSTGRES_PASSWORD" |
  docker compose "${COMPOSE[@]}" exec -T postgres \
    psql -v ON_ERROR_STOP=1 -U control -d postgres >/dev/null

DATABASE_URL_VALUE="postgresql+psycopg://control:${POSTGRES_PASSWORD}@postgres:5432/control"
TMP_ENV="$(mktemp)"
awk -v value="$DATABASE_URL_VALUE" '
  BEGIN { updated=0 }
  /^DATABASE_URL=/ { print "DATABASE_URL=" value; updated=1; next }
  { print }
  END { if (!updated) print "DATABASE_URL=" value }
' .env > "$TMP_ENV"
cat "$TMP_ENV" > .env
rm -f "$TMP_ENV"
chmod 600 .env

echo "PostgreSQL credentials synchronized."
