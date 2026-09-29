#!/bin/sh
# One-time Ubuntu/Debian VPS installer for Ask Engage Estero.
set -eu

REPO_URL="https://github.com/krocks9903/rag-arcgis-chatbot.git"
INSTALL_DIR="/opt/engage-estero"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root (for example: sudo bash install.sh)."
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently supports Ubuntu/Debian (apt-get) servers."
  exit 1
fi

echo "Installing system prerequisites..."
apt-get update
apt-get install -y ca-certificates curl git openssl

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "Updating existing checkout..."
  git -C "$INSTALL_DIR" pull --ff-only origin main
else
  echo "Cloning Ask Engage Estero..."
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

DEPLOY_DIR="$INSTALL_DIR/deploy/vps"
ENV_FILE="$DEPLOY_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  ANTHROPIC_KEY=""
  if [ -r /dev/tty ]; then
    printf "Anthropic API key (press Enter to install without chat): " >/dev/tty
    stty -echo </dev/tty 2>/dev/null || true
    IFS= read -r ANTHROPIC_KEY </dev/tty || true
    stty echo </dev/tty 2>/dev/null || true
    printf "\n" >/dev/tty
  fi

  ADMIN_KEY="$(openssl rand -hex 32)"
  umask 077
  cat >"$ENV_FILE" <<EOF
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
ADMIN_API_KEY=$ADMIN_KEY
MCP_API_KEY=$ADMIN_KEY
ENABLE_MCP_HTTP=true
EOF
  chmod 600 "$ENV_FILE"

  echo
  echo "Save this generated admin/MCP key in a password manager:"
  echo "$ADMIN_KEY"
  if [ -z "$ANTHROPIC_KEY" ]; then
    echo
    echo "No Anthropic key was set. The site will work, but chatbot answers will not."
    echo "Add the key later to $ENV_FILE, then run: update-engage-estero"
  fi
else
  echo "Keeping existing $ENV_FILE"
fi

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "^Status: active"; then
  ufw allow 80/tcp
fi

chmod +x "$DEPLOY_DIR/up.sh" "$DEPLOY_DIR/update.sh"
ln -sf "$DEPLOY_DIR/update.sh" /usr/local/bin/update-engage-estero

echo "Building and starting the application (first build can take 15-25 minutes)..."
cd "$DEPLOY_DIR"
docker compose up -d --build

echo "Waiting for the application..."
READY=false
for _ in $(seq 1 36); do
  if curl -fsS http://127.0.0.1/ready >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 10
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
if [ "$READY" = "true" ]; then
  echo "Ask Engage Estero is ready: http://${IP:-<server-ip>}/"
else
  echo "The container is still warming. Check: cd $DEPLOY_DIR && docker compose logs -f"
fi
echo "Future updates: sudo update-engage-estero"
