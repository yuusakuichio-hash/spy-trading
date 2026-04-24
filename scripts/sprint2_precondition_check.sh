#!/bin/bash
# T-1 fix (Redteam r8): ADR-014 3 判断の前提崩壊検知
# 2026-04-24 策定
#
# ADR-014 は以下 3 判断を Secretary 自動採用:
#   Decision 1: OpenD 常駐 = Mac mini
#   Decision 2: セッション期限 = 手動再ログイン + Pushover 通知
#   Decision 3: Sprint 2 スコープ = read-only metrics のみ
#
# これらの前提が崩壊したら ADR 無効化して再判定が必要。
# 本 script は前提崩壊を自動検知して warning 出力。
#
# Usage: scripts/sprint2_precondition_check.sh

set -u

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

WARN_COUNT=0
CRIT_COUNT=0

warn() {
    echo -e "${YELLOW}⚠ WARN: $1${NC}"
    WARN_COUNT=$((WARN_COUNT+1))
}
crit() {
    echo -e "${RED}✗ CRIT: $1${NC}"
    CRIT_COUNT=$((CRIT_COUNT+1))
}
ok() {
    echo -e "${GREEN}✓ OK: $1${NC}"
}

echo "=== ADR-014 前提崩壊検知（Redteam r8 T-1 fix）==="

# Decision 1 check: OpenD が VPS で動いていないか
if pgrep -f "openD|OpenD" > /dev/null 2>&1; then
    LOCAL_OPENDD=$(pgrep -f "openD|OpenD" | head -1)
    ok "OpenD process running locally (PID: $LOCAL_OPENDD)"
fi
if [ -f ~/.ssh/config ] && grep -q "Host vps" ~/.ssh/config 2>/dev/null; then
    if ssh -o ConnectTimeout=3 -o BatchMode=yes vps "pgrep -f openD" 2>/dev/null; then
        crit "Decision 1 violation: OpenD running on VPS detected. ADR-014 assumed Mac mini."
    fi
fi

# Decision 2 check: 認証失敗が連続で発生していないか
AUTH_LOG=/Users/yuusakuichio/trading/data/logs/auth_failures.log
if [ -f "$AUTH_LOG" ]; then
    RECENT_FAILS=$(tail -100 "$AUTH_LOG" 2>/dev/null | grep -c "AuthenticationError" || echo 0)
    if [ "$RECENT_FAILS" -ge 5 ]; then
        warn "Decision 2 疲弊兆候: $RECENT_FAILS 回の AuthenticationError 直近 100 件中。手動再ログイン運用疲弊の可能性"
    else
        ok "Authentication failures in normal range ($RECENT_FAILS)"
    fi
else
    ok "Authentication log not yet created (new install)"
fi

# Decision 3 check: スコープ逸脱検知
SPRINT2_SCOPE_MARKER="Sprint 2 read-only metrics only"
if grep -rl "place_order\|cancel_order\|modify_order" atlas_v3/ops/moomoo_provider.py 2>/dev/null | grep -q .; then
    crit "Decision 3 violation: atlas_v3/ops/moomoo_provider.py に発注系 API (place_order 等) が混入"
else
    ok "Decision 3 compliant: read-only scope maintained in moomoo_provider.py"
fi

# Mac mini 電源状態（sleep 設定）
if pmset -g 2>/dev/null | grep -q "sleep\s*[1-9]"; then
    warn "Mac sleep 有効: long-running daemon 稼働中に sleep で落ちる可能性。caffeinate 推奨"
else
    ok "Mac sleep disabled or sleep=0"
fi

# LaunchAgent 稼働確認
for agent in com.soralab.status-server com.soralab.atlas-paper; do
    if launchctl list | grep -q "$agent"; then
        ok "LaunchAgent $agent loaded"
    else
        warn "LaunchAgent $agent not loaded（Sprint 2 Day 1 以降で必要）"
    fi
done

# cloudflared tunnel 稼働
if pgrep -f "cloudflared tunnel" > /dev/null; then
    URL=$(grep -oE "https://[a-z-]+\.trycloudflare\.com" /Users/yuusakuichio/trading/data/logs/cloudflared_tunnel.log 2>/dev/null | tail -1)
    ok "cloudflared tunnel running: ${URL:-unknown}"
else
    warn "cloudflared tunnel not running（外出先ダッシュボード無効）"
fi

echo ""
echo "=== Summary: $CRIT_COUNT CRITICAL / $WARN_COUNT WARN ==="
if [ "$CRIT_COUNT" -gt 0 ]; then
    echo -e "${RED}ADR-014 前提崩壊検知。再判定必要。${NC}"
    exit 2
fi
if [ "$WARN_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}警告あり・運用状況を確認。${NC}"
    exit 1
fi
echo -e "${GREEN}All preconditions healthy.${NC}"
exit 0
