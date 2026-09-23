#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "❌ نصب در خط $LINENO متوقف شد." >&2' ERR
REPO="syklonAK/pasarguard-control-plane"
INSTALL_DIR="${INSTALL_DIR:-/opt/pasarguard-control-plane}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml)
HTTPS="https:""//"; GITHUB="${HTTPS}github.com"; TELEGRAM_API="${HTTPS}api.telegram.org"
red='\033[0;31m'; green='\033[0;32m'; reset='\033[0m'
info(){ echo -e "${green}➜${reset} $*"; }
die(){ echo -e "${red}✖${reset} $*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || die "این نصب‌کننده فقط Linux را پشتیبانی می‌کند."
[[ ${EUID} -eq 0 ]] || die "دستور را با sudo اجرا کنید."
command -v apt-get >/dev/null || die "نسخه خودکار فعلاً Ubuntu/Debian را پشتیبانی می‌کند."
REAL_USER="${SUDO_USER:-root}"
info "نصب پیش‌نیازها"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg git jq openssl
if ! command -v docker >/dev/null; then
  info "نصب Docker از مخزن رسمی"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "${HTTPS}download.docker.com/linux/$(. /etc/os-release; echo "$ID")/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${HTTPS}download.docker.com/linux/$ID $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
docker compose version >/dev/null || die "Docker Compose نصب نشد."
if [[ ! -f "$INSTALL_DIR/pyproject.toml" ]]; then
  info "انتقال پروژه به $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"; mkdir -p "$INSTALL_DIR"
  if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    cp -a "$SCRIPT_DIR/." "$INSTALL_DIR/"
  elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
    git clone "${HTTPS}x-access-token:${GITHUB_TOKEN}@github.com/${REPO}.git" "$INSTALL_DIR"
  elif command -v gh >/dev/null && sudo -u "$REAL_USER" gh auth status >/dev/null 2>&1; then
    chown "$REAL_USER":"$REAL_USER" "$INSTALL_DIR"
    sudo -u "$REAL_USER" gh repo clone "$REPO" "$INSTALL_DIR"
  else
    git clone "${GITHUB}/${REPO}.git" "$INSTALL_DIR" || die "دریافت پروژه از GitHub ناموفق بود."
  fi
fi
cd "$INSTALL_DIR"
ask(){ local prompt="$1" default="${2:-}" value; read -r -p "$prompt${default:+ [$default]}: " value </dev/tty; echo "${value:-$default}"; }
ask_secret(){ local prompt="$1" value; read -r -s -p "$prompt: " value </dev/tty; echo >&2; echo "$value"; }
DOMAIN="${DOMAIN:-$(ask 'دامنه متصل به سرور (مثلاً panel.example.com)')}"; [[ "$DOMAIN" == *.* ]] || die "دامنه معتبر نیست."
BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(ask_secret 'توکن ربات تلگرام')}"; [[ "$BOT_TOKEN" == *:* ]] || die "فرمت توکن ربات معتبر نیست."
PG_URL="${PASARGUARD_URL:-$(ask 'آدرس HTTPS پاسارگارد')}"; [[ "$PG_URL" == https://* ]] || die "آدرس پاسارگارد باید HTTPS باشد."
PG_KEY="${PG_API_KEY:-$(ask_secret 'API Key پاسارگارد')}"; [[ -n "$PG_KEY" ]] || die "API Key خالی است."
PG_OWNER_USER="${PG_OWNER_USERNAME:-$(ask 'نام کاربری Owner پاسارگارد')}"
PG_OWNER_PASS="${PG_OWNER_PASSWORD:-$(ask_secret 'رمز Owner پاسارگارد')}"
CONTROL_API_KEY="$(openssl rand -hex 32)"; WEBHOOK_SECRET="$(openssl rand -hex 32)"; POSTGRES_PASSWORD="$(openssl rand -hex 32)"
cat > .env <<EOF
DOMAIN=$DOMAIN
DATABASE_URL=postgresql+psycopg://control:$POSTGRES_PASSWORD@postgres:5432/control
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
CONTROL_API_KEY=$CONTROL_API_KEY
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET=$WEBHOOK_SECRET
PG_API_KEY=$PG_KEY
PG_OWNER_USERNAME=$PG_OWNER_USER
PG_OWNER_PASSWORD=$PG_OWNER_PASS
PASARGUARD_URL=$PG_URL
REDIS_URL=redis://redis:6379/0
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40
DB_POOL_TIMEOUT=10
USAGE_POLL_SECONDS=30
POSTGRES_CPU_LIMIT=1.5
API_CPU_LIMIT=0.75
EOF
chmod 600 .env
export POSTGRES_PASSWORD
docker compose "${COMPOSE[@]}" config >/dev/null
info "ساخت و اجرای سرویس‌ها"
docker compose "${COMPOSE[@]}" up -d --build --remove-orphans
info "انتظار برای HTTPS و API"
for _ in $(seq 1 120); do curl -fsS "${HTTPS}${DOMAIN}/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "${HTTPS}${DOMAIN}/health" >/dev/null || die "API آماده نشد؛ لاگ‌ها را بررسی کنید."
curl -fsS "${TELEGRAM_API}/bot${BOT_TOKEN}/setWebhook" --data-urlencode "url=${HTTPS}${DOMAIN}/telegram/webhook" --data-urlencode "secret_token=${WEBHOOK_SECRET}" >/dev/null
cat > /root/pasarguard-control-plane-credentials.txt <<EOF
Install directory: $INSTALL_DIR
Dashboard: ${HTTPS}$DOMAIN/app/
API docs: ${HTTPS}$DOMAIN/docs
CONTROL_API_KEY: $CONTROL_API_KEY
EOF
chmod 600 /root/pasarguard-control-plane-credentials.txt
info "نصب کامل شد"
echo "وب‌اپ: ${HTTPS}$DOMAIN/app/"
echo "Swagger: ${HTTPS}$DOMAIN/docs"
echo "مرحله بعد: sudo bash $INSTALL_DIR/scripts/bootstrap.sh"
