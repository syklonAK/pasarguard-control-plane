#!/usr/bin/env bash
set -Eeuo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/pasarguard-control-plane}"
COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml)
HTTPS="https:""//";TG_API="${HTTPS}api.telegram.org"
info(){ printf '\033[0;32m➜\033[0m %s\n' "$*"; };die(){ printf '\033[0;31m✖\033[0m %s\n' "$*" >&2;exit 1; }
[[ ${EUID} -eq 0 ]] || die "Run with sudo.";cd "$INSTALL_DIR";[[ -f .env ]] || die ".env is missing."
set -a;source .env;set +a
[[ -n "${DOMAIN:-}" ]] || die "DOMAIN is missing.";[[ "${TELEGRAM_BOT_TOKEN:-}" == *:* ]] || die "TELEGRAM_BOT_TOKEN is invalid.";[[ -n "${TELEGRAM_WEBHOOK_SECRET:-}" ]] || die "TELEGRAM_WEBHOOK_SECRET is missing."
info "Reloading the reverse-proxy route"
docker compose "${COMPOSE[@]}" up -d --force-recreate gateway
docker compose "${COMPOSE[@]}" restart caddy
info "Waiting for Telegram gateway health"
for _ in $(seq 1 60);do CID="$(docker compose "${COMPOSE[@]}" ps -q telegram-gateway|head -n1)";if [[ -n "$CID" ]]&&docker exec "$CID" python -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8010/health",timeout=2)' >/dev/null 2>&1;then INTERNAL_READY=1;break;fi;sleep 2;done
if [[ "${INTERNAL_READY:-0}" != 1 ]];then docker compose "${COMPOSE[@]}" logs --tail=100 telegram-gateway >&2 || true;die "Telegram gateway internal health check failed.";fi
info "Waiting for the public webhook route"
for _ in $(seq 1 60);do STATUS="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${HTTPS}${DOMAIN}/telegram/webhook" || true)";if [[ "$STATUS" == "405" ]];then PUBLIC_READY=1;break;fi;sleep 2;done
if [[ "${PUBLIC_READY:-0}" != 1 ]];then docker compose "${COMPOSE[@]}" logs --tail=100 caddy gateway telegram-gateway >&2 || true;die "Public webhook route is unavailable (last HTTP status: ${STATUS:-none}).";fi
info "Registering Telegram webhook"
SET_RESULT="$(curl -fsS "${TG_API}/bot${TELEGRAM_BOT_TOKEN}/setWebhook" --data-urlencode "url=${HTTPS}${DOMAIN}/telegram/webhook" --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" --data-urlencode 'allowed_updates=["message","callback_query"]' --data-urlencode "max_connections=40")"
printf '%s' "$SET_RESULT"|jq -e '.ok == true' >/dev/null || die "Telegram rejected the webhook configuration."
INFO_RESULT="$(curl -fsS "${TG_API}/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo")"
printf '%s' "$INFO_RESULT"|jq '{ok,url:.result.url,pending:.result.pending_update_count,last_error:.result.last_error_message}'
REGISTERED_URL="$(printf '%s' "$INFO_RESULT"|jq -r '.result.url // ""')";[[ "$REGISTERED_URL" == "${HTTPS}${DOMAIN}/telegram/webhook" ]] || die "Webhook URL verification failed."
info "Telegram webhook is reachable and configured"
