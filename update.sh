#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Update stopped on line $LINENO." >&2' ERR
INSTALL_DIR="${INSTALL_DIR:-/opt/pasarguard-control-plane}";COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml);HTTPS="https:""//";TELEGRAM_API="${HTTPS}api.telegram.org"
info(){ printf '\033[0;32m➜\033[0m %s\n' "$*"; };warn(){ printf '\033[0;33m!\033[0m %s\n' "$*" >&2; };die(){ printf '\033[0;31m✖\033[0m %s\n' "$*" >&2;exit 1; };random_hex(){ openssl rand -hex "$1"; };ask(){ local value;read -r -p "$1: " value </dev/tty;printf '%s' "$value"; }
[[ ${EUID} -eq 0 ]] || die "Run this updater with sudo.";cd "$INSTALL_DIR";[[ -f .env ]] || die ".env is missing. Run install.sh first.";[[ -d .git ]] || die "$INSTALL_DIR is not a Git checkout."
git config --global --add safe.directory "$INSTALL_DIR" >/dev/null 2>&1 || true;[[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "Tracked files contain local changes. Commit or discard them before updating."
OLD_COMMIT="$(git rev-parse HEAD)";info "Fetching the latest release";git fetch --prune origin main;git merge --ff-only origin/main
set -a;source .env;set +a
ensure_env(){ grep -q "^$1=" .env || printf '%s=%s\n' "$1" "$2" >>.env; }
if [[ ! "${ROOT_TELEGRAM_ID:-}" =~ ^[0-9]{5,20}$ ]];then ROOT_TELEGRAM_ID="$(ask 'Telegram administrator numeric ID')";[[ "$ROOT_TELEGRAM_ID" =~ ^[0-9]{5,20}$ ]] || die "Invalid Telegram administrator numeric ID.";fi
ensure_env ROOT_TELEGRAM_ID "$ROOT_TELEGRAM_ID";ensure_env WEBAPP_URL "${HTTPS}${DOMAIN}/app/";ensure_env APP_ENCRYPTION_KEY "$(random_hex 32)";ensure_env INITIAL_SETUP_TOKEN "$(random_hex 32)";ensure_env POSTGRES_CPU_LIMIT 1.0;ensure_env POSTGRES_MEMORY_LIMIT 2G;ensure_env API_CPU_LIMIT 0.75;ensure_env API_MEMORY_LIMIT 1G;ensure_env API_REPLICAS 1;ensure_env API_WORKERS 1;ensure_env TELEGRAM_CONSUMER_REPLICAS 1
set -a;source .env;set +a
configure_telegram(){
 curl -fsS "${TELEGRAM_API}/bot${TELEGRAM_BOT_TOKEN}/setWebhook" --data-urlencode "url=${HTTPS}${DOMAIN}/telegram/webhook" --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" >/dev/null
 local commands command_payload menu_payload;commands='[{"command":"start","description":"باز کردن منوی اختصاصی"},{"command":"id","description":"نمایش شناسه عددی تلگرام"},{"command":"dashboard","description":"نمایش داشبورد"},{"command":"servers","description":"مدیریت سرورها و نودها"},{"command":"support","description":"راهنما و پشتیبانی"}]'
 command_payload="$(jq -nc --argjson commands "$commands" '{commands:$commands}')";curl -fsS "${TELEGRAM_API}/bot${TELEGRAM_BOT_TOKEN}/setMyCommands" -H 'Content-Type: application/json' -d "$command_payload" >/dev/null
 menu_payload="$(jq -nc --arg url "$WEBAPP_URL" '{menu_button:{type:"web_app",text:"پنل مدیریت",web_app:{url:$url}}}')";curl -fsS "${TELEGRAM_API}/bot${TELEGRAM_BOT_TOKEN}/setChatMenuButton" -H 'Content-Type: application/json' -d "$menu_payload" >/dev/null
}
docker compose "${COMPOSE[@]}" config >/dev/null;info "Starting database services";docker compose "${COMPOSE[@]}" up -d postgres redis;info "Synchronizing PostgreSQL credentials";bash scripts/sync-db-password.sh;info "Rebuilding changed services";docker compose "${COMPOSE[@]}" up -d --build --remove-orphans
info "Waiting for the internal API";for _ in $(seq 1 120);do API_CID="$(docker compose "${COMPOSE[@]}" ps -q api|head -n1)";if [[ -n "$API_CID" ]]&&docker exec "$API_CID" python -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=2)' >/dev/null 2>&1;then INTERNAL_READY=1;break;fi;sleep 2;done
if [[ "${INTERNAL_READY:-0}" != 1 ]];then warn "Health check failed. Previous commit: $OLD_COMMIT";docker compose "${COMPOSE[@]}" ps;docker compose "${COMPOSE[@]}" logs --tail=80 api >&2 || true;exit 1;fi
if curl -fsS --max-time 10 "${HTTPS}${DOMAIN}/health" >/dev/null 2>&1;then configure_telegram;else warn "Internal services are healthy, but public HTTPS is unavailable.";fi
cat >/root/pasarguard-control-plane-credentials.txt <<EOF
Install directory: $INSTALL_DIR
WebApp: $WEBAPP_URL
API docs: ${HTTPS}$DOMAIN/docs
Telegram administrator ID: $ROOT_TELEGRAM_ID
CONTROL_API_KEY: $CONTROL_API_KEY
INITIAL_SETUP_TOKEN: $INITIAL_SETUP_TOKEN
EOF
chmod 600 /root/pasarguard-control-plane-credentials.txt;info "Update completed: $(git rev-parse --short HEAD)"
