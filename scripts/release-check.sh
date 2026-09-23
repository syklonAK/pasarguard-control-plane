#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
echo "== Shell syntax"
find . -maxdepth 2 -type f -name '*.sh' -print0|xargs -0 -r -n1 bash -n
echo "== Python syntax"
python -m compileall -q src tests
echo "== Python unused names"
# Only a lint pass on the checkout; the runtime image never imports the linter.
if python -c 'import pyflakes' 2>/dev/null;then python -m pyflakes src tests;else echo "pyflakes is not installed; skipped.";fi
echo "== JavaScript syntax"
find webapp -type f -name '*.js' -print0|xargs -0 -r -n1 node --check
echo "== Compose files parse"
python - <<'PY'
import yaml
for path in ('docker-compose.yml','docker-compose.enterprise.yml','docker-compose.production.yml'):
 with open(path,encoding='utf-8') as fh:yaml.safe_load(fh)
print('compose files are valid')
PY
echo "== Secrets stay out of the tree"
if [[ -d .git ]];then
 tracked="$(git ls-files|grep -vE '\.(png|jpg|jpeg|gif|ico|woff2?|ttf|pdf)$')"
 # A committed .env or key material is unrecoverable by deletion: history keeps it forever.
 secrets_tracked="$(printf '%s\n' "$tracked"|grep -E '(^|/)(\.env|id_rsa|\.npmrc|\.netrc)'|grep -vE '\.env\.example$'||true)"
 if [[ -n "$secrets_tracked" ]];then
  echo "A configuration or credential file is tracked by git:";printf '%s\n' "$secrets_tracked";exit 1
 fi
 while read -r file;do
  [[ -n "$file"&&-f "$file" ]]||continue
  if grep -qE -- '-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[0-9A-Za-z]{10,}' "$file";then
   echo "Private key material found in $file.";exit 1
  fi
 done <<<"$tracked"
fi
if ! grep -qx '\.env' .gitignore 2>/dev/null;then echo ".env is not ignored by .gitignore.";exit 1;fi
echo "static checks passed"
if command -v docker >/dev/null&&docker compose version >/dev/null 2>&1;then test -f .env||cp .env.example .env;docker compose -f docker-compose.enterprise.yml -f docker-compose.production.yml config >/dev/null;fi
