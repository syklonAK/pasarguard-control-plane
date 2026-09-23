#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose -f docker-compose.enterprise.yml -f docker-compose.production.yml up -d --build --remove-orphans
echo "✅ بروزرسانی انجام شد."
