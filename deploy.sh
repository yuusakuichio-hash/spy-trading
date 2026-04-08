#!/bin/bash
# deploy.sh — Full VPS deployment for SPX Bot
# Usage: bash deploy.sh [SSH_KEY_PATH]
# Example: bash deploy.sh ~/.ssh/vultr_spxbot_key
# Run from local machine with repo cloned.

set -euo pipefail

VPS_IP="198.13.37.17"
VPS_USER="root"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="/root/spxbot"
SSH_KEY="${1:-/tmp/vultr_spxbot_key}"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $VPS_USER@$VPS_IP"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=no"

echo "=== SPX Bot Deployment Script ==="
echo "VPS: $VPS_IP  |  Remote: $REMOTE_DIR"
echo "SSH key: $SSH_KEY"
echo ""

# ── 1. Test connection ──────────────────────────────────────────────────────
echo "[1/8] Testing SSH connection..."
$SSH "echo '  ✅ SSH OK'" || { echo "❌ SSH failed. Check key at: $SSH_KEY"; exit 1; }

# ── 2. Ensure remote dir and venv ──────────────────────────────────────────
echo "[2/8] Preparing remote directory..."
$SSH "mkdir -p $REMOTE_DIR /var/log/spx_bot /root/logs"

# ── 3. Copy files ──────────────────────────────────────────────────────────
echo "[3/8] Uploading files..."
FILES=(
    spx_bot.py
    health_server.py
    health.service
    spxbot.service
    test_spx_bot.py
    spx_bot_verify.py
    logrotate.conf
)
for f in "${FILES[@]}"; do
    echo "  → $f"
    $SCP "$REPO_DIR/$f" "$VPS_USER@$VPS_IP:$REMOTE_DIR/$f"
done

# ── 4. Set timezone ────────────────────────────────────────────────────────
echo "[4/8] Setting timezone to America/New_York..."
$SSH "timedatectl set-timezone America/New_York && timedatectl | head -3"

# ── 5. Install systemd services ────────────────────────────────────────────
echo "[5/8] Installing systemd services..."
$SSH "
    cp $REMOTE_DIR/spxbot.service /etc/systemd/system/spxbot.service
    cp $REMOTE_DIR/health.service /etc/systemd/system/health.service
    systemctl daemon-reload
    systemctl enable spxbot health
    echo '  ✅ Services registered'
"

# ── 6. Install logrotate ───────────────────────────────────────────────────
echo "[6/8] Installing logrotate config..."
$SSH "
    cp $REMOTE_DIR/logrotate.conf /etc/logrotate.d/spxbot
    logrotate -d /etc/logrotate.d/spxbot 2>&1 | head -5
    echo '  ✅ logrotate installed'
"

# ── 7. Restart services ────────────────────────────────────────────────────
echo "[7/8] Restarting services..."
$SSH "
    systemctl restart spxbot || true
    systemctl restart health || true
    sleep 3
    echo '--- spxbot status ---'
    systemctl is-active spxbot
    echo '--- health status ---'
    systemctl is-active health
"

# ── 8. Run verify ──────────────────────────────────────────────────────────
echo "[8/8] Running spx_bot_verify.py on VPS..."
$SSH "SPX_LOG_DIR=/var/log/spx_bot python3 $REMOTE_DIR/spx_bot_verify.py"

echo ""
echo "=== Deployment Complete ==="
echo "Health endpoint: http://$VPS_IP:8080/health"
echo ""
