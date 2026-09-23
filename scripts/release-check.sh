#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
find . -maxdepth 2 -type f -name '*.sh' -print0|xargs -0 -r -n1 bash -n
python -m compileall -q src
node --check webapp/app.js
python - <<'PY'
import yaml
for path in ('docker-compose.enterprise.yml','docker-compose.production.yml'):
 with open(path,encoding='utf-8') as fh:yaml.safe_load(fh)
print('static checks passed')
PY
if command -v docker >/dev/null&&docker compose version >/dev/null 2>&1;then test -f .env||cp .env.example .env;docker compose -f docker-compose.enterprise.yml -f docker-compose.production.yml config >/dev/null;fi
