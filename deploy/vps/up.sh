#!/bin/sh
# Build and start Ask Engage Estero on this VPS (port 80).
set -eu
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Install Docker first: https://docs.docker.com/engine/install/"
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — set ANTHROPIC_API_KEY (and ADMIN_API_KEY), then run ./up.sh again."
  exit 1
fi

docker compose up -d --build
echo "Up. Open http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '<this-server>')/"
echo "First build can take 15–25 minutes (models + index)."
