#!/usr/bin/env bash
set -Eeuo pipefail
# Restore a backup produced by scripts/backup.sh.
#
# The default target is a scratch database, never the live one: overwriting production data
# has to be an explicit, two-step decision.
cd "$(dirname "$0")/..";[[ -f .env ]]||{ echo ".env is missing." >&2;exit 1; };set -a;source .env;set +a
COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml)
TARGET="control_restore";FORCED=0;FILE="";KEEP_PREVIOUS=0
usage(){ cat <<'EOF'
Usage: bash scripts/restore.sh <backup-file> [--into <database>] [--force] [--keep-previous]

  --into <database>   defaults to control_restore (a scratch database)
  --force             required together with --into control, which replaces the live database
  --keep-previous     do not drop the target database first (append into an existing schema)
EOF
}
while [[ $# -gt 0 ]];do case "$1" in
 --into) TARGET="${2:-}";shift 2;;
 --force) FORCED=1;shift;;
 --keep-previous) KEEP_PREVIOUS=1;shift;;
 -h|--help) usage;exit 0;;
 -*) echo "Unknown option: $1" >&2;usage;exit 1;;
 *) FILE="$1";shift;;
esac;done
[[ -n "$FILE" ]]||{ usage;exit 1; }
[[ -f "$FILE" ]]||{ echo "Backup file not found: $FILE" >&2;exit 1; }
[[ "$TARGET" =~ ^[a-z_][a-z0-9_]{0,62}$ ]]||{ echo "Invalid database name." >&2;exit 1; }
if [[ "$TARGET" == "control"&&$FORCED -ne 1 ]];then echo "Refusing to replace the live database without --force." >&2;exit 1;fi
echo "== Verifying the archive";gzip -t "$FILE" 2>/dev/null||{ echo "The archive is not a readable gzip file." >&2;exit 1; }
if [[ "$TARGET" == "control" ]];then
 # pipefail makes a failed backup abort here, before anything is replaced.
 echo "== Taking a safety backup of the live database first";SAFETY_LINE="$(bash scripts/backup.sh --skip-rotate|tail -n1)";echo "${SAFETY_LINE#Backup created: }"
 echo "== Stopping application services (database stays up)";docker compose "${COMPOSE[@]}" stop api billing-worker outbox-worker telegram-consumer telegram-gateway||true
fi
if [[ $KEEP_PREVIOUS -eq 0 ]];then
 echo "== Recreating target database: $TARGET"
 docker compose "${COMPOSE[@]}" exec -T postgres psql -U control -d postgres -v ON_ERROR_STOP=1 \
   -c "DROP DATABASE IF EXISTS $TARGET WITH (FORCE);" -c "CREATE DATABASE $TARGET OWNER control;"
else
 echo "== Reusing existing database: $TARGET"
fi
echo "== Restoring";gunzip -c "$FILE"|docker compose "${COMPOSE[@]}" exec -T postgres psql -U control -d "$TARGET" -v ON_ERROR_STOP=1 -q
echo "== Checking ledger integrity in $TARGET"
# Dollar quoting keeps the literals intact through the shell argument.
docker compose "${COMPOSE[@]}" exec -T postgres psql -U control -d "$TARGET" -v ON_ERROR_STOP=1 -Atc \
 "SELECT case when count(*)=0 then \$\$ledger is balanced\$\$ else format(\$\$LEDGER DRIFT: %s accounts\$\$, count(*)) end from ledger_imbalance;"||{ echo "Ledger integrity check failed in $TARGET." >&2;exit 1; }
if [[ "$TARGET" == "control" ]];then
 echo "== Restarting application services";docker compose "${COMPOSE[@]}" up -d api billing-worker outbox-worker telegram-consumer telegram-gateway
 for _ in $(seq 1 60);do CID="$(docker compose "${COMPOSE[@]}" ps -q api|head -n1)";if [[ -n "$CID" ]]&&docker exec "$CID" python -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=3)' >/dev/null 2>&1;then echo "API healthy.";break;fi;sleep 2;done
fi
echo "Restored $FILE into $TARGET."
