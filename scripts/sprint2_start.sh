#!/bin/bash
# Sprint 2 Day 1/2 着手用ワンコマンド（ゆうさくさん戻り後用）
# 2026-04-24 策定
#
# Usage:
#   bash scripts/sprint2_start.sh        # 対話モード・各ステップ確認
#   bash scripts/sprint2_start.sh --yes  # 非対話・全自動（危険）
#
# 実行ステップ:
#   1. futu-api pip install
#   2. allowlist hook 本番 lock (chflags schg)
#   3. moomoo smoke_test
#   4. cloudflared quick tunnel 起動 + URL 取得
#   5. Sprint 2 Day 2 moomoo 実接続可否判定

set -e
cd /Users/yuusakuichio/trading

AUTO_YES=0
DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --yes) AUTO_YES=1 ;;
        --dry-run) DRY_RUN=1; AUTO_YES=1; echo "★ DRY RUN mode - actions will be simulated" ;;
    esac
done

confirm() {
    if [ "$AUTO_YES" -eq 1 ]; then return 0; fi
    read -p "$1 [y/N]: " ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

echo "╔═══════════════════════════════════════════╗"
echo "║  Sprint 2 Day 1/2 着手ワンコマンド        ║"
echo "╚═══════════════════════════════════════════╝"

# Step 1: futu-api install
echo ""
echo "─── Step 1: futu-api (moomoo SDK) インストール ───"
if python3 -c "import futu" 2>/dev/null; then
    echo "  ✓ futu-api already installed"
else
    if confirm "pip install futu-api を実行しますか？"; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "  [dry-run] pip3 install futu-api"
        else
            pip3 install futu-api
        fi
        echo "  ✓ installed"
    else
        echo "  ✗ skipped（Day 2 moomoo 実接続不可）"
    fi
fi

# Step 2: allowlist lock
echo ""
echo "─── Step 2: 既存コード allowlist 本番 lock ───"
LOCK_STATUS=$(bash scripts/lock_legacy_files.sh status 2>&1 | grep "Summary" | tail -1)
echo "  現状: $LOCK_STATUS"
if echo "$LOCK_STATUS" | grep -q "locked=0"; then
    if confirm "lock を実行しますか？ (chflags schg で immutable 化)"; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "  [dry-run] bash scripts/lock_legacy_files.sh lock"
        else
            bash scripts/lock_legacy_files.sh lock
        fi
    else
        echo "  ✗ skipped"
    fi
else
    echo "  ✓ already locked"
fi

# Step 3: moomoo smoke_test
echo ""
echo "─── Step 3: moomoo OpenD 接続テスト (smoke_test) ───"
echo "  前提: moomoo OpenD アプリ起動 + Paper login 済"
if confirm "smoke_test を実行しますか？"; then
    python3 -c "
from atlas_v3.ops.moomoo_provider import MoomooMetricProvider, AuthenticationError, MoomooProviderNotImplementedError
import os
password = os.environ.get('MOOMOO_TRADE_PASSWORD')
try:
    provider = MoomooMetricProvider(trade_password=password)
    provider.smoke_test()
    print('  ✓ smoke_test PASSED')
except AuthenticationError as e:
    print(f'  ✗ AUTH ERROR (re-login required): {e}')
    exit(2)
except MoomooProviderNotImplementedError as e:
    print(f'  ✗ futu-api missing: {e}')
    exit(3)
except Exception as e:
    print(f'  ✗ OpenD not running? {e}')
    exit(4)
" || echo "  ⚠ 上記エラーに応じて対処（OpenD 起動 / Paper login / MOOMOO_TRADE_PASSWORD 設定）"
else
    echo "  ✗ skipped"
fi

# Step 4: cloudflared tunnel 起動
echo ""
echo "─── Step 4: cloudflared quick tunnel（外出先ダッシュボード）───"
if pgrep -f "cloudflared tunnel" > /dev/null; then
    URL=$(grep -oE "https://[a-z-]+\.trycloudflare\.com" data/logs/cloudflared_tunnel.log 2>/dev/null | tail -1)
    echo "  ✓ already running: $URL"
else
    if confirm "cloudflared 起動しますか？"; then
        nohup /opt/homebrew/bin/cloudflared tunnel --url http://localhost:8765 \
            > data/logs/cloudflared_tunnel.log 2>&1 &
        disown
        sleep 8
        URL=$(grep -oE "https://[a-z-]+\.trycloudflare\.com" data/logs/cloudflared_tunnel.log | tail -1)
        echo "  ✓ started: $URL"
    else
        echo "  ✗ skipped（Tailscale 設定後に本番化）"
    fi
fi

# Step 5: 状態総括
echo ""
echo "─── Step 5: Sprint 2 着手 Readiness 判定 ───"
READY=1
if ! python3 -c "import futu" 2>/dev/null; then echo "  ✗ futu-api not installed"; READY=0; fi
if bash scripts/lock_legacy_files.sh status 2>&1 | grep -q "locked=0"; then echo "  ⚠ allowlist not locked"; fi
echo ""
if [ "$READY" -eq 1 ]; then
    echo "  ✓ Sprint 2 Day 2 着手可"
else
    echo "  ⚠ 前提条件不足・再実行推奨"
fi
echo ""
echo "ダッシュボード: http://192.168.10.123:8765/  (LAN)"
[ -n "${URL:-}" ] && echo "              $URL  (外出先)"
