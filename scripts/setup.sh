#!/usr/bin/env bash
# DuckPond — new server setup
# Tested on Ubuntu 22.04 / 24.04. Run as root.
set -euo pipefail

REPO="https://github.com/jimmymoonstation/DuckPond.git"
INSTALL_DIR="/opt/job-hunt-partner"
SERVICE_USER="claudebot"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}▶ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠ $*${NC}"; }
success() { echo -e "${GREEN}✓ $*${NC}"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo su or sudo bash setup.sh)"; exit 1; }

# ── 1. System packages ────────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx git curl wget unzip

# ── 2. Node.js (for Claude Code CLI) ─────────────────────────────────────────
if ! command -v node &>/dev/null; then
    info "Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y -qq nodejs
fi

# ── 3. claudebot user ─────────────────────────────────────────────────────────
info "Creating $SERVICE_USER user..."
id "$SERVICE_USER" &>/dev/null || useradd -m -s /bin/bash "$SERVICE_USER"

# ── 4. Claude Code CLI for claudebot ─────────────────────────────────────────
info "Installing Claude Code CLI for $SERVICE_USER..."
runuser -u "$SERVICE_USER" -- bash -c "npm install -g @anthropic-ai/claude-code 2>/dev/null || npm install -g @anthropic-ai/claude-code --prefix /home/$SERVICE_USER/.local" || true

# ── 5. Clone repo ─────────────────────────────────────────────────────────────
info "Cloning repo to $INSTALL_DIR..."
if [ -d "$INSTALL_DIR/.git" ]; then
    warn "$INSTALL_DIR already exists — pulling latest instead"
    git -C "$INSTALL_DIR" pull
else
    git clone "$REPO" "$INSTALL_DIR"
fi

# ── 6. Python dependencies ────────────────────────────────────────────────────
info "Installing Python dependencies..."
pip3 install -r "$INSTALL_DIR/requirements.txt" -q

# ── 7. Environment file ───────────────────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn ".env created from .env.example — fill in your keys before starting the service"
else
    warn ".env already exists — skipping"
fi

# ── 8. Database ───────────────────────────────────────────────────────────────
info "Initializing database..."
cd "$INSTALL_DIR"
python3 scripts/init_db.py
python3 seed_companies.py

# ── 9. Nginx ──────────────────────────────────────────────────────────────────
info "Configuring nginx..."
cat > /etc/nginx/sites-available/job-hunt <<'NGINX'
server {
    listen 80;
    server_name _;

    location /jobs-dashboard {
        alias /opt/job-hunt-partner/src/dashboard;
        try_files $uri $uri/ /jobs-dashboard/index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:5057/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 20M;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/job-hunt /etc/nginx/sites-enabled/job-hunt
nginx -t && systemctl reload nginx

# ── 10. Systemd service ───────────────────────────────────────────────────────
info "Installing systemd service..."
cp "$INSTALL_DIR/systemd/job-hunter.service" /etc/systemd/system/job-hunter.service
systemctl daemon-reload
systemctl enable job-hunter

# ── Done ──────────────────────────────────────────────────────────────────────
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
success "Setup complete. Two manual steps before starting:"
echo ""
echo "  1. Fill in your API keys:"
echo "     nano $INSTALL_DIR/.env"
echo ""
echo "     Required: BRAVE_API_KEY, DISCORD_BOT_TOKEN, JOB_HUNT_CHANNEL_ID,"
echo "               EMAIL_ADDRESS, EMAIL_APP_PASSWORD"
echo ""
echo "  2. Authenticate Claude Code as $SERVICE_USER:"
echo "     runuser -u $SERVICE_USER -- claude auth login"
echo "     (follow the browser link it prints)"
echo ""
echo "  Then start everything:"
echo "     systemctl start job-hunter"
echo "     systemctl status job-hunter"
echo ""
echo "  Dashboard: http://$SERVER_IP/jobs-dashboard"
echo ""
echo "  Browser extension: load extension/ as an unpacked Chrome extension"
echo "  and set the server URL to http://$SERVER_IP"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
