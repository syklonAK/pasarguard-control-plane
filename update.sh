#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Update failed on line $LINENO" >&2' ERR
INSTALL_DIR="${INSTALL_DIR:-/opt/pasarguard-control-plane}"
[[ ${EUID} -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
cd "$INSTALL_DIR"
[[ -f .env ]] || { echo ".env not found; run install.sh first." >&2; exit 1; }
[[ -d .git ]] || { echo "Git checkout not found in $INSTALL_DIR." >&2; exit 1; }
COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml)
OLD_COMMIT="$(git rev-parse HEAD)"
echo "Updating from $OLD_COMMIT..."
git fetch --prune origin main
git merge --ff-only origin/main
set -a; source .env; set +a
export POSTGRES_CPU_LIMIT="${POSTGRES_CPU_LIMIT:-1.5}"
export API_CPU_LIMIT="${API_CPU_LIMIT:-0.75}"
docker compose "${COMPOSE[@]}" config >/dev/null
docker compose "${COMPOSE[@]}" up -d --build --remove-orphans
HTTPS="https:""//"
for _ in $(seq 1 90); do
  if curl -fsS "${HTTPS}${DOMAIN}/health" >/dev/null 2>&1; then
    echo "Update completed: $(git rev-parse --short HEAD)"
    exit 0
  fi
  sleep 2
done
echo "Health check failed. Check: docker compose ${COMPOSE[*]} logs --tail=200" >&2
exit 1
