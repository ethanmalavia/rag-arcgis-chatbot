#!/bin/sh
# Build and start Ask Engage Estero on this VPS (bound to 127.0.0.1:8080).
set -eu
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Install Docker first: https://docs.docker.com/engine/install/"
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — set ADMIN_API_KEY and optionally ANTHROPIC_API_KEY, then run ./up.sh again."
  exit 1
fi

docker compose up -d --build
echo "Up on http://127.0.0.1:8080/ (Plesk should proxy https://tracker.esterotoday.com/)."
echo "First build can take 15–25 minutes (models + index)."
