#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/..";[[ -f .env ]]||{ echo ".env is missing." >&2;exit 1; };set -a;source .env;set +a
COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml);HTTPS="https:""//";TG="${HTTPS}api.telegram.org"
echo "== Static release checks ==";bash scripts/release-check.sh
echo "== Containers ==";docker compose "${COMPOSE[@]}" ps
echo "== PostgreSQL ==";docker compose "${COMPOSE[@]}" exec -T postgres pg_isready -U control -d control
echo "== Redis ==";docker compose "${COMPOSE[@]}" exec -T redis redis-cli ping
echo "== Internal API ==";CID="$(docker compose "${COMPOSE[@]}" ps -q api|head -n1)";docker exec "$CID" python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=3).read().decode())'
echo "== Telegram gateway ==";CID="$(docker compose "${COMPOSE[@]}" ps -q telegram-gateway|head -n1)";docker exec "$CID" python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8010/health",timeout=3).read().decode())'
echo "== Public API ==";curl -fsS --max-time 10 "${HTTPS}${DOMAIN}/health"|jq .
echo "== WebApp MIME ==";curl -fsSI "${HTTPS}${DOMAIN}/app/styles.css"|grep -i '^content-type:';curl -fsSI "${HTTPS}${DOMAIN}/app/app.js"|grep -i '^content-type:'
echo "== Telegram webhook ==";curl -fsS "${TG}/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"|jq '.result|{url,pending_update_count,last_error_message}'
echo "All diagnostics passed."
