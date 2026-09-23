#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."; set -a; source .env; set +a
FILES=(-f docker-compose.enterprise.yml -f docker-compose.production.yml); HTTPS="https:""//"; TG="${HTTPS}api.telegram.org"
echo "== containers =="; docker compose "${FILES[@]}" ps
echo "== api =="; curl -fsS "${HTTPS}${DOMAIN}/health" | jq .
echo "== telegram =="; curl -fsS "${TG}/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | jq '.result|{url,pending_update_count,last_error_message}'
echo "✅ بررسی اولیه تمام شد."
