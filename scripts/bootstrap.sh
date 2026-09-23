#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."; [[ -f .env ]] || { echo "ابتدا install.sh را اجرا کنید."; exit 1; }
set -a; source .env; set +a; HTTPS="https:""//"; BASE="${HTTPS}${DOMAIN}"
read -r -p "نام کسب‌وکار اصلی: " NAME
read -r -p "شناسه انگلیسی (مثلاً main-business): " SLUG
read -r -p "Telegram numeric ID مدیر: " TG_ID
read -r -p "نام نمایشی مدیر: " DISPLAY
ORG="$(curl -fsS -X POST "$BASE/v1/organizations" -H "X-Control-Key: $CONTROL_API_KEY" -H 'Content-Type: application/json' -d "$(jq -nc --arg n "$NAME" --arg s "$SLUG" '{name:$n,slug:$s,credit_limit_irr:0}')")"
ORG_ID="$(jq -r .id <<<"$ORG")"
curl -fsS -X POST "$BASE/v1/admin/actor-bindings" -H "X-Control-Key: $CONTROL_API_KEY" -H 'Content-Type: application/json' -d "$(jq -nc --argjson t "$TG_ID" --arg d "$DISPLAY" --arg o "$ORG_ID" '{telegram_id:$t,display_name:$d,organization_id:$o,role:"reseller_admin"}')" | jq .
PANEL="$(curl -fsS -X POST "$BASE/v1/panels" -H "X-Control-Key: $CONTROL_API_KEY" -H 'Content-Type: application/json' -d "$(jq -nc --arg u "$PASARGUARD_URL" '{name:"PasarGuard Main",base_url:$u,api_key_ref:"env://PG_API_KEY",owner_user_ref:"env://PG_OWNER_USERNAME",owner_pass_ref:"env://PG_OWNER_PASSWORD"}')")"
echo "✅ سازمان: $ORG_ID"; echo "✅ پنل: $(jq -r .id <<<"$PANEL")"; echo "WebApp را در BotFather روی $BASE/app/ تنظیم کنید."
