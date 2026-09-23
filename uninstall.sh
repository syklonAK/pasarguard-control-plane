#!/usr/bin/env bash
set -Eeuo pipefail
trap 'echo "Uninstall stopped on line $LINENO." >&2' ERR

INSTALL_DIR="${INSTALL_DIR:-/opt/pasarguard-control-plane}"
PURGE_DATA=0
KEEP_FILES=0
ASSUME_YES=0

usage(){
  cat <<'EOF'
PasarGuard Control Plane uninstaller

Usage:
  sudo bash uninstall.sh [options]

Options:
  --yes          Skip the main confirmation prompt
  --purge-data   Permanently delete PostgreSQL and Redis Docker volumes
  --keep-files   Keep the installation directory and .env file
  --help         Show this help message

By default, containers and application files are removed, but database volumes
are preserved so a future installation can reuse them.
EOF
}

info(){ printf '\033[0;32m➜\033[0m %s\n' "$*"; }
warn(){ printf '\033[0;33m!\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[0;31m✖\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --purge-data) PURGE_DATA=1 ;;
    --keep-files) KEEP_FILES=1 ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

[[ ${EUID} -eq 0 ]] || die "Run this uninstaller with sudo."
[[ -d "$INSTALL_DIR" ]] || die "Installation directory not found: $INSTALL_DIR"
cd "$INSTALL_DIR"

if [[ $ASSUME_YES -ne 1 ]]; then
  echo "This will stop and remove PasarGuard Control Plane containers."
  if [[ $KEEP_FILES -eq 1 ]]; then
    echo "Application files will be preserved."
  else
    echo "Application files in $INSTALL_DIR will be removed."
  fi
  if [[ $PURGE_DATA -eq 1 ]]; then
    warn "PostgreSQL and Redis volumes will be permanently deleted."
    read -r -p 'Type DELETE to confirm permanent data removal: ' answer </dev/tty
    [[ "$answer" == "DELETE" ]] || die "Data purge cancelled."
  else
    echo "Database volumes will be preserved."
  fi
  read -r -p 'Continue with uninstall? [y/N]: ' answer </dev/tty
  [[ "$answer" =~ ^[Yy]$ ]] || die "Uninstall cancelled."
fi

COMPOSE=(-f docker-compose.enterprise.yml -f docker-compose.production.yml)
if command -v systemctl >/dev/null; then
  info "Removing the nightly backup timer"
  systemctl disable --now pasarguard-backup.timer >/dev/null 2>&1 || true
  systemctl reset-failed pasarguard-backup.timer pasarguard-backup.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/pasarguard-backup.timer /etc/systemd/system/pasarguard-backup.service
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  info "Stopping services"
  if [[ -f docker-compose.enterprise.yml && -f docker-compose.production.yml ]]; then
    if [[ $PURGE_DATA -eq 1 ]]; then
      docker compose "${COMPOSE[@]}" down --remove-orphans --volumes
    else
      docker compose "${COMPOSE[@]}" down --remove-orphans
    fi
  else
    warn "Compose files are missing; skipping container shutdown."
  fi
else
  warn "Docker Compose is unavailable; skipping container shutdown."
fi

rm -f /root/pasarguard-control-plane-credentials.txt

if [[ $KEEP_FILES -eq 1 ]]; then
  info "Application files preserved in $INSTALL_DIR"
else
  info "Removing application files"
  cd /
  rm -rf --one-file-system "$INSTALL_DIR"
fi

if [[ $PURGE_DATA -eq 1 ]]; then
  info "Uninstall completed; application data was permanently deleted."
else
  info "Uninstall completed; Docker volumes were preserved."
  echo "A future installation in the same directory can reuse the preserved volumes."
fi

echo "Docker itself was not removed."
