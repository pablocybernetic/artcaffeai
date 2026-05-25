#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Deploy ArtCaffe Brand Pipeline to a Google Cloud VM
#
# Prerequisites (already done on VM):
#   - Python 3.11+ installed
#   - Git installed
#   - This repo cloned to /opt/artcaffe  (or adjust DEPLOY_DIR below)
#
# Run as root or a user with sudo access:
#   chmod +x scripts/deploy.sh
#   sudo bash scripts/deploy.sh
# =============================================================================

set -euo pipefail

DEPLOY_DIR="/opt/artcaffe"
APP_USER="artcaffe"
PYTHON="python3"

echo "==> [1/6] Creating system user '$APP_USER' (if not exists)..."
id -u "$APP_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> [2/6] Setting up deployment directory at $DEPLOY_DIR..."
mkdir -p "$DEPLOY_DIR"

# Copy project files
cp -r "$(dirname "$0")/.." "$DEPLOY_DIR"
chown -R "$APP_USER":"$APP_USER" "$DEPLOY_DIR"

echo "==> [3/6] Creating Python virtual environment..."
$PYTHON -m venv "$DEPLOY_DIR/venv"
"$DEPLOY_DIR/venv/bin/pip" install --upgrade pip
"$DEPLOY_DIR/venv/bin/pip" install -r "$DEPLOY_DIR/requirements.txt"

echo "==> [4/6] Setting up .env file..."
if [ ! -f "$DEPLOY_DIR/.env" ]; then
    cp "$DEPLOY_DIR/.env.example" "$DEPLOY_DIR/.env"
    echo ""
    echo "  *** ACTION REQUIRED ***"
    echo "  Edit $DEPLOY_DIR/.env and fill in your credentials:"
    echo "    - SUPABASE_URL"
    echo "    - SUPABASE_SERVICE_ROLE_KEY"
    echo "    - ANTHROPIC_API_KEY"
    echo "  Then re-run: sudo systemctl restart artcaffe-api"
    echo ""
else
    echo "  .env already exists, skipping."
fi
chmod 600 "$DEPLOY_DIR/.env"
chown "$APP_USER":"$APP_USER" "$DEPLOY_DIR/.env"

echo "==> [5/6] Installing systemd services..."
cp "$DEPLOY_DIR/systemd/artcaffe-api.service" /etc/systemd/system/
cp "$DEPLOY_DIR/systemd/artcaffe-worker.service" /etc/systemd/system/
systemctl daemon-reload

echo "==> [6/6] Enabling and starting artcaffe-api service..."
systemctl enable artcaffe-api
systemctl restart artcaffe-api
systemctl status artcaffe-api --no-pager

echo ""
echo "========================================"
echo " Deployment complete!"
echo "========================================"
echo " API running at: http://$(hostname -I | awk '{print $1}'):8000"
echo " Health check:   curl http://localhost:8000/health"
echo " View logs:      journalctl -u artcaffe-api -f"
echo ""
echo " NOTE: artcaffe-worker is NOT started by default."
echo "       Enable it only if you want a decoupled poller:"
echo "   sudo systemctl enable --now artcaffe-worker"
echo "========================================"
