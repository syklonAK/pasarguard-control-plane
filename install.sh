#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Installation stopped on line $LINENO." >&2' ERR
REPO="syklonAK/pasarguard-control-plane"
INSTALL_DIR="${INSTALL_DIR:-/opt/pasarguard-control-plane}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml)
HTTPS="https:""//";GITHUB="${HTTPS}github.com";TELEGRAM_API="${HTTPS}api.telegram.org"
info(){ printf '\033[0;32m➜\033[0m %s\n' "$*"; };warn(){ printf '\033[0;33m!\033[0m %s\n' "$*" >&2; };die(){ printf '\033[0;31m✖\033[0m %s\n' "$*" >&2;exit 1; }
ask(){ local value;read -r -p "$1: " value </dev/tty;printf '%s' "$value"; };ask_secret(){ local value;read -r -s -p "$1: " value </dev/tty;echo >&2;printf '%s' "$value"; };random_hex(){ openssl rand -hex "$1"; }
[[ "$(uname -s)" == Linux ]] || die "This installer supports Linux only."
[[ ${EUID} -eq 0 ]] || die "Run this command with sudo."
command -v apt-get >/dev/null || die "Automatic installation supports Ubuntu and Debian."
info "Installing system prerequisites";apt-get update -y;DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg git jq openssl
if ! command -v docker >/dev/null;then
 info "Installing Docker";install -m 0755 -d /etc/apt/keyrings;. /etc/os-release
 curl -fsSL "${HTTPS}download.docker.com/linux/${ID}/gpg"|gpg --dearmor -o /etc/apt/keyrings/docker.gpg;chmod a+r /etc/apt/keyrings/docker.gpg
 echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${HTTPS}download.docker.com/linux/${ID} ${VERSION_CODENAME} stable">/etc/apt/sources.list.d/docker.list
 apt-get update -y;DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker;docker compose version >/dev/null || die "Docker Compose is unavailable."
if [[ ! -f "$INSTALL_DIR/pyproject.toml" ]];then
 info "Installing project files in $INSTALL_DIR";rm -rf "$INSTALL_DIR";mkdir -p "$INSTALL_DIR"
 if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" ]];then cp -a "$SCRIPT_DIR/." "$INSTALL_DIR/";else git clone "${GITHUB}/${REPO}.git" "$INSTALL_DIR";fi
fi
cd "$INSTALL_DIR";git config --global --add safe.directory "$INSTALL_DIR" >/dev/null 2>&1 || true
if [[ -f .env ]];then info "Existing configuration found; preserving all secrets";set -a;source .env;set +a
else
 DOMAIN="${DOMAIN:-$(ask 'Public domain (example: panel.example.com)')}";[[ "$DOMAIN" == *.* ]] || die "Enter a valid domain."
 TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(ask_secret 'Telegram bot token')}";[[ "$TELEGRAM_BOT_TOKEN" == *:* ]] || die "Invalid Telegram bot token."
 ROOT_TELEGRAM_ID="${ROOT_TELEGRAM_ID:-$(ask 'Telegram administrator numeric ID')}";[[ "$ROOT_TELEGRAM_ID" =~ ^[0-9]{5,20}$ ]] || die "Invalid Telegram administrator numeric ID."
 POSTGRES_PASSWORD="$(random_hex 32)";CONTROL_API_KEY="$(random_hex 32)";TELEGRAM_WEBHOOK_SECRET="$(random_hex 32)";APP_ENCRYPTION_KEY="$(random_hex 32)";INITIAL_SETUP_TOKEN="$(random_hex 32)"
 cat>.env <<EOF
DOMAIN=$DOMAIN
WEBAPP_URL=${HTTPS}${DOMAIN}/app/
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+psycopg://control:$POSTGRES_PASSWORD@postgres:5432/control
CONTROL_API_KEY=$CONTROL_API_KEY
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
ROOT_TELEGRAM_ID=$ROOT_TELEGRAM_ID
TELEGRAM_WEBHOOK_SECRET=$TELEGRAM_WEBHOOK_SECRET
APP_ENCRYPTION_KEY=$APP_ENCRYPTION_KEY
INITIAL_SETUP_TOKEN=$INITIAL_SETUP_TOKEN
REDIS_URL=redis://redis:6379/0
USAGE_POLL_SECONDS=30
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_TIMEOUT=10
POSTGRES_CPU_LIMIT=1.0
POSTGRES_MEMORY_LIMIT=2G
API_CPU_LIMIT=0.75
API_MEMORY_LIMIT=1G
API_REPLICAS=1
API_WORKERS=1
TELEGRAM_CONSUMER_REPLICAS=1
EOF
 chmod 600 .env
fi
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
docker compose "${COMPOSE[@]}" config >/dev/null
info "Starting database services";docker compose "${COMPOSE[@]}" up -d postgres redis
info "Synchronizing PostgreSQL credentials";bash scripts/sync-db-password.sh
info "Building and starting application services";docker compose "${COMPOSE[@]}" up -d --build --remove-orphans
info "Waiting for the internal API"
for _ in $(seq 1 120);do API_CID="$(docker compose "${COMPOSE[@]}" ps -q api|head -n1)";if [[ -n "$API_CID" ]]&&docker exec "$API_CID" python -c 'import urllib.request;urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=2)' >/dev/null 2>&1;then INTERNAL_READY=1;break;fi;sleep 2;done
if [[ "${INTERNAL_READY:-0}" != 1 ]];then warn "API health check failed.";docker compose "${COMPOSE[@]}" ps;docker compose "${COMPOSE[@]}" logs --tail=80 api >&2 || true;exit 1;fi
if curl -fsS --max-time 10 "${HTTPS}${DOMAIN}/health" >/dev/null 2>&1;then info "Configuring Telegram webhook and menu";configure_telegram;else warn "The internal API is healthy, but public HTTPS is unavailable. Check DNS and ports 80/443, then run update.sh.";fi
cat >/root/pasarguard-control-plane-credentials.txt <<EOF
Install directory: $INSTALL_DIR
WebApp: $WEBAPP_URL
API docs: ${HTTPS}$DOMAIN/docs
Telegram administrator ID: $ROOT_TELEGRAM_ID
CONTROL_API_KEY: $CONTROL_API_KEY
INITIAL_SETUP_TOKEN: $INITIAL_SETUP_TOKEN
EOF
chmod 600 /root/pasarguard-control-plane-credentials.txt
info "Installation completed";echo "Open the Telegram bot and send /start.";echo "Register PasarGuard servers from WebApp > Servers.";echo "Future updates: sudo bash $INSTALL_DIR/update.sh"
