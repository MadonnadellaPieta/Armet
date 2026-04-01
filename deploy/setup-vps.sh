#!/usr/bin/env bash
# VPS setup script — installs Armet on a fresh Ubuntu 22.04 / Debian 12 system.
# Run as root: bash deploy/setup-vps.sh
set -euo pipefail

APP_DIR=/opt/armet
APP_USER=armet

echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3.11-dev \
    build-essential git curl \
    nodejs npm

# Node 20 LTS via NodeSource
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

echo "==> Creating app user..."
id -u $APP_USER &>/dev/null || useradd -r -s /bin/false -d $APP_DIR $APP_USER

echo "==> Cloning / updating repo..."
if [ -d "$APP_DIR/.git" ]; then
    git -C $APP_DIR pull
else
    # Adjust URL to your repo
    git clone https://github.com/madonnadellapieta/armet.git $APP_DIR
fi

chown -R $APP_USER:$APP_USER $APP_DIR

echo "==> Creating Python venv and installing deps..."
sudo -u $APP_USER python3.11 -m venv $APP_DIR/venv
sudo -u $APP_USER $APP_DIR/venv/bin/pip install --no-cache-dir -r $APP_DIR/requirements.txt

echo "==> Building React frontend..."
cd $APP_DIR/frontend
sudo -u $APP_USER npm ci --legacy-peer-deps
sudo -u $APP_USER npm run build

echo "==> Setting up data directory..."
sudo -u $APP_USER mkdir -p $APP_DIR/data

echo "==> Installing systemd service..."
cp $APP_DIR/deploy/armet.service /etc/systemd/system/armet.service
systemctl daemon-reload
systemctl enable armet
systemctl restart armet

echo ""
echo "Done! Armet is running on port 8000."
echo "Check status: systemctl status armet"
echo "View logs:    journalctl -u armet -f"
