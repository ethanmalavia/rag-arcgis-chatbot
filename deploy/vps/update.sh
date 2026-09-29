#!/bin/sh
# Pull main, rebuild, and restart the VPS deployment.
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo update-engage-estero"
  exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "Missing $SCRIPT_DIR/.env. Run install.sh first."
  exit 1
fi

git -C "$REPO_ROOT" pull --ff-only origin main
cd "$SCRIPT_DIR"
docker compose up -d --build

echo "Deployment updated."
echo "Status: docker compose ps"
echo "Logs:   cd $SCRIPT_DIR && docker compose logs -f"
