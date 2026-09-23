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
echo "== Metrics endpoint =="
if [[ -n "${METRICS_TOKEN:-}" ]];then
 metrics="$(curl -sS --max-time 10 -f -H "X-Metrics-Token: $METRICS_TOKEN" "${HTTPS}${DOMAIN}/metrics")"
 printf '%s\n' "$metrics"|grep -E '^(ledger_drift_accounts|http_requests_total|http_rate_limited_total|process_uptime_seconds)'|head -n 12||true
 # An unauthenticated probe must fail: /metrics carries traffic counts, and a reachable one is a leak.
 anonymous="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${HTTPS}${DOMAIN}/metrics")"
 [[ "$anonymous" == 401||"$anonymous" == 503 ]]||{ echo "Metrics is exposed without a token (HTTP $anonymous)." >&2;exit 1; }
else echo "! METRICS_TOKEN is not set; /metrics stays closed.";fi
echo "== Ledger drift =="
drift="$(docker compose "${COMPOSE[@]}" exec -T postgres psql -U control -d control -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM ledger_imbalance;")"
[[ "$drift" == 0 ]]||{ echo "$drift account(s) diverge from their entries; see the ledger_imbalance view." >&2;exit 1; }
echo "ledger is balanced"
echo "== Outbox =="
docker compose "${COMPOSE[@]}" exec -T postgres psql -U control -d control -v ON_ERROR_STOP=1 -c "SELECT status,count(*) FROM outbox GROUP BY status ORDER BY status;"
dead="$(docker compose "${COMPOSE[@]}" exec -T postgres psql -U control -d control -v ON_ERROR_STOP=1 -Atc "SELECT count(*) FROM outbox WHERE status='dead';")"
[[ "$dead" == 0 ]]||echo "! $dead dead-letter event(s) need attention."
echo "== Telegram stream =="
docker compose "${COMPOSE[@]}" exec -T redis redis-cli xinfo groups telegram_updates 2>/dev/null||echo "! consumer group is not ready yet."
echo "All diagnostics passed."
