#!/bin/bash
# atlas_tonight_monitor.sh — 22:30 fire時・23:00 trade count・23:30 P&L確認
# [FIX 2026-04-21] data feed health check 追加
LOGFILE="/Users/yuusakuichio/trading/data/logs/atlas_tonight_monitor.log"
TS=$(date '+%Y-%m-%d %H:%M:%S JST')
CURRENT_HOUR=$(date '+%H')
CURRENT_MIN=$(date '+%M')

echo "[$TS] monitor fired hour=$CURRENT_HOUR min=$CURRENT_MIN" >> "$LOGFILE"

# spy_bot PID確認
SPY_PID=$(launchctl list | grep "com.spybot.paper" | awk '{print $1}')
ATLAS_PID=$(launchctl list | grep "com.atlas.agent" | grep -v "stop" | awk '{print $1}')

# 最新ログ行
LAST_STDOUT=$(tail -1 /Users/yuusakuichio/trading/data/logs/spybot_stdout.log 2>/dev/null)
LAST_ATLAS=$(tail -1 /Users/yuusakuichio/trading/data/logs/atlas_agent_stdout.log 2>/dev/null)

# ─── data feed health check ────────────────────────────────────────────────
# 1) ORB 1分足失敗が30秒以上連続していないか検知
ORB_FAIL_COUNT=$(grep "2026-$(date '+%m-%d')" /Users/yuusakuichio/trading/data/logs/spybot_stdout.log 2>/dev/null | \
    grep "\[ORB\] 1分足データ取得失敗" | wc -l | tr -d ' ')

# 2) center 価格アノマリー検知: center が ±50% 以上の乖離 (SPX ~5400 対して 709 など)
CENTER_ANOMALY=$(grep "2026-$(date '+%m-%d')" /Users/yuusakuichio/trading/data/logs/spybot_stdout.log 2>/dev/null | \
    grep "ChainGuard.*center=" | wc -l | tr -d ' ')

# 3) 場中の1分足失敗が10件超えたら自動再起動
ET_HOUR=$(TZ=America/New_York date '+%H')
ET_MIN=$(TZ=America/New_York date '+%M')
IS_MARKET_HOURS=0
if [ "$ET_HOUR" -ge 9 ] && [ "$ET_HOUR" -lt 16 ]; then
    IS_MARKET_HOURS=1
fi

RESTART_DONE="false"
if [ "$IS_MARKET_HOURS" -eq 1 ] && [ "$ORB_FAIL_COUNT" -gt 10 ]; then
    echo "[$TS] [ALERT] ORB 1分足失敗 $ORB_FAIL_COUNT 件 → spy_bot 自動再起動" >> "$LOGFILE"
    launchctl kickstart -k gui/$(id -u)/com.spybot.paper >> "$LOGFILE" 2>&1
    RESTART_DONE="true"
fi

# ─── SIMULATE注文件数カウント ──────────────────────────────────────────────
ORDER_COUNT=$(PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python /opt/homebrew/bin/python3 -c "
import os
os.environ['PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION'] = 'python'
from futu import *
ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host='127.0.0.1', port=11111, security_firm=SecurityFirm.FUTUJP)
ret, orders = ctx.order_list_query(trd_env=TrdEnv.SIMULATE)
if ret == 0:
    today = orders[orders['create_time'].str.contains('$(date +%Y-%m-%d)', na=False)] if not orders.empty else orders
    print(len(today))
else:
    print(-1)
ctx.close()
" 2>/dev/null || echo "N/A")

MSG="[Atlas/OPS] 夜間監視 ${CURRENT_HOUR}:${CURRENT_MIN} JST
spy_bot PID=$SPY_PID atlas PID=$ATLAS_PID
SIMULATE注文=$ORDER_COUNT件
ORB_FAIL=${ORB_FAIL_COUNT}件 CENTER_ANOMALY=${CENTER_ANOMALY}件 restart=$RESTART_DONE
最新: $(echo $LAST_STDOUT | tail -c 120)"

echo "[$TS] $MSG" >> "$LOGFILE"

# center anomaly / ORB失敗 > 10 件はpriority=1緊急通知
PRIORITY=0
if [ "$CENTER_ANOMALY" -gt 5 ] || ([ "$IS_MARKET_HOURS" -eq 1 ] && [ "$ORB_FAIL_COUNT" -gt 10 ]); then
    PRIORITY=1
fi

# Pushover送信
/opt/homebrew/bin/python3 -c "
import sys, os
sys.path.insert(0, '/Users/yuusakuichio/trading')
with open('/Users/yuusakuichio/trading/.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())
from common.pushover_client import send
send('[Atlas/OPS]', '''$MSG''', priority=$PRIORITY)
" >> "$LOGFILE" 2>&1

echo "[$TS] done" >> "$LOGFILE"
