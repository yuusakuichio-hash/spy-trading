#!/bin/bash
# VPS recovery script — ensures correct service startup order.
# Runs on boot via vps_recovery.service (Type=oneshot).

LOG=/root/logs/recovery.log
mkdir -p /root/logs
exec >> "$LOG" 2>&1

echo "=== VPS復旧開始 $(date) ==="

# ── 1. Wait for OpenD (up to 120s) ────────────────────────────────────────────
echo "[1/5] OpenD起動中..."
systemctl start opend 2>/dev/null || true

OPEND_OK=false
for i in $(seq 1 24); do
    sleep 5
    if systemctl is-active --quiet opend; then
        echo "  OpenD起動確認 (${i}回目, $((i*5))s)"
        OPEND_OK=true
        break
    fi
    # Also check if port 11111 is listening (OpenD may not have a systemd unit)
    if ss -tnl 2>/dev/null | grep -q ':11111'; then
        echo "  OpenD port 11111 ready (${i}回目, $((i*5))s)"
        OPEND_OK=true
        break
    fi
done

if [ "$OPEND_OK" = false ]; then
    echo "  OpenD未起動 (120s経過) — spxbotはOpenD待ちで起動"
fi

# ── 2. Start spxbot ────────────────────────────────────────────────────────────
echo "[2/5] spxbot起動..."
systemctl start spxbot || true
sleep 5

# ── 3. Start health server ─────────────────────────────────────────────────────
echo "[3/5] health起動..."
systemctl start health || true
sleep 2

# ── 4. Start hub_agent ────────────────────────────────────────────────────────
echo "[4/5] hub_agent起動..."
systemctl start hub_agent || true

# ── 5. Report status ──────────────────────────────────────────────────────────
sleep 3
SPXBOT=$(systemctl is-active spxbot 2>/dev/null || echo unknown)
OPEND_S=$(systemctl is-active opend   2>/dev/null || echo unknown)
HEALTH=$(systemctl is-active health   2>/dev/null || echo unknown)
HUBAGT=$(systemctl is-active hub_agent 2>/dev/null || echo unknown)
STATUS="spxbot:$SPXBOT opend:$OPEND_S health:$HEALTH hub:$HUBAGT"

echo "[5/5] サービス状態: $STATUS"

PUSHOVER_TOKEN="${PUSHOVER_TOKEN:-a5rb9ipb3yrdanv3vk4n8x28qt7io9}"
PUSHOVER_USER="${PUSHOVER_USER:-u2cevk8nktib3sr148rw2hs78ecvux}"

python3 -c "
import requests, os
requests.post('https://api.pushover.net/1/messages.json', data={
    'token': '${PUSHOVER_TOKEN}',
    'user':  '${PUSHOVER_USER}',
    'title': 'VPS復旧完了',
    'message': '${STATUS}',
    'priority': 0,
}, timeout=10)
" 2>/dev/null || echo "Pushover送信失敗（ネットワーク未準備の可能性）"

echo "=== VPS復旧完了 $(date) ==="
