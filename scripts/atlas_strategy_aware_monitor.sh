#!/bin/bash
# atlas_strategy_aware_monitor.sh — 戦術固有 anomaly 検知 (5分毎 22:30-05:10 JST)
# 検知条件 7項目 → 違反時 Pushover P2 即送信
# 2026-04-21 新規作成

set -euo pipefail

TRADING_DIR="/Users/yuusakuichio/trading"
LOGFILE="$TRADING_DIR/data/logs/atlas_strategy_aware_monitor.log"
SPY_LOG="$TRADING_DIR/data/logs/spybot_stdout.log"
ATLAS_LOG="$TRADING_DIR/data/logs/atlas_agent_stdout.log"
TS=$(date '+%Y-%m-%d %H:%M:%S JST')

mkdir -p "$TRADING_DIR/data/logs"
# stdout/stderr を LOGFILE に向ける (Pushover Python の INFO ログ除く)
exec >> "$LOGFILE" 2>&1
TODAY=$(date '+%Y-%m-%d')
NOW_EPOCH=$(date '+%s')

echo "[$TS] === strategy_aware_monitor fired ==="

# ─── .env 読み込み ────────────────────────────────────────────────────────────
set +u
while IFS='=' read -r key val; do
    [[ "$key" =~ ^#.*$ ]] && continue
    [[ -z "$key" ]] && continue
    export "$key"="${val}" 2>/dev/null || true
done < "$TRADING_DIR/.env"
set -u

PUSHOVER_USER="${PUSHOVER_USER:-}"
PUSHOVER_ALERT_TOKEN="${PUSHOVER_ALERT_TOKEN:-}"

send_alert() {
    local title="$1"
    local msg="$2"
    echo "[$TS] [ALERT] $title: $msg"
    /opt/homebrew/bin/python3 - <<PYEOF 2>/dev/null || true
import os, sys, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, '$TRADING_DIR')
os.environ['PUSHOVER_USER'] = '$PUSHOVER_USER'
os.environ['PUSHOVER_ALERT_TOKEN'] = '$PUSHOVER_ALERT_TOKEN'
from common.pushover_client import send
send('[Atlas/ALERT]', """$title
$msg""", priority=2)
PYEOF
}

# ─── JST 現在時刻 ─────────────────────────────────────────────────────────────
JST_HOUR=$(date '+%H')
JST_MIN=$(date '+%M')
JST_HHMM=$((JST_HOUR * 60 + JST_MIN))

# 市場時間判定 (ET 9:30-16:00 = JST 22:30-05:00)
IN_MARKET_HOURS=0
# 22:30 = 1350, 05:00 = 300 (翌日) → 1350以上 or 300以下
if [ "$JST_HHMM" -ge 1350 ] || [ "$JST_HHMM" -le 300 ]; then
    IN_MARKET_HOURS=1
fi

echo "[$TS] JST=${JST_HOUR}:${JST_MIN} IN_MARKET=${IN_MARKET_HOURS}"

# ─── 条件1: 1分足失敗連発 (10分以内に10件以上) ────────────────────────────────
check_orb_fail() {
    local WINDOW_START
    WINDOW_START=$(date -v -10M '+%H:%M' 2>/dev/null || date -d '10 minutes ago' '+%H:%M' 2>/dev/null || echo "00:00")

    # 今日のログから [ORB] 1分足データ取得失敗 の件数を過去10分で絞り込む
    local FAIL_COUNT=0
    if [ -f "$SPY_LOG" ]; then
        FAIL_COUNT=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
            grep "\[ORB\] 1分足データ取得失敗" | \
            awk -v ws="$WINDOW_START" '{
                # HH:MM:SS 部分を抽出して比較
                match($0, /[0-9]{2}:[0-9]{2}:[0-9]{2}/)
                t = substr($0, RSTART, 5)
                if (t >= ws) count++
            } END{print count+0}' 2>/dev/null || echo "0")
    fi

    echo "[$TS] [CHECK1] ORB 1分足失敗(10分以内)=${FAIL_COUNT}"
    if [ "${FAIL_COUNT:-0}" -ge 10 ]; then
        send_alert "[Atlas] ORB 1分足失敗連発" "10分以内に${FAIL_COUNT}件の1分足データ取得失敗。feed断絶の可能性"
        echo "FAIL_ORB" >> /tmp/atlas_monitor_alerts_$$
    fi
}

# ─── 条件2: SPX center anomaly (500未満 or 20000超) ──────────────────────────
check_spx_center_anomaly() {
    if [ ! -f "$SPY_LOG" ]; then return; fi

    # macOS grep は -P 非対応 → -E + sed で center 値を抽出
    local ANOMALY_COUNT
    set +e
    ANOMALY_COUNT=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
        grep -E '\[ATMSubscribe\] SPX center=[0-9.]+' | \
        sed 's/.*\[ATMSubscribe\] SPX center=\([0-9.]*\).*/\1/' | \
        awk '{if ($1+0 < 500 || $1+0 > 20000) count++} END{print count+0}')
    set -e
    ANOMALY_COUNT=$(printf '%s' "${ANOMALY_COUNT:-0}" | tr -d ' \n')

    echo "[$TS] [CHECK2] SPX center anomaly=${ANOMALY_COUNT}"
    if [ "${ANOMALY_COUNT:-0}" -ge 1 ]; then
        local BAD_VAL
        BAD_VAL=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
            grep -E '\[ATMSubscribe\] SPX center=[0-9.]+' | \
            sed 's/.*\[ATMSubscribe\] SPX center=\([0-9.]*\).*/\1/' | \
            awk '{if ($1+0 < 500 || $1+0 > 20000) print $1}' | head -3 | tr '\n' ' ')
        send_alert "[Atlas] SPX center 異常値" "center=${BAD_VAL} (正常範囲:500-20000)。桁落ちまたは銘柄混入"
        echo "FAIL_CENTER" >> /tmp/atlas_monitor_alerts_$$
    fi
}

# ─── 条件3: Opening Range 未記録 (22:50 JST 以降チェック) ─────────────────────
check_opening_range() {
    # 22:50 = 1370 分
    if [ "$JST_HHMM" -lt 1370 ]; then
        echo "[$TS] [CHECK3] Opening Range check: 22:50前のためスキップ"
        return
    fi
    # 05:10 = 310 分を超えたら当日チェック不要(場クローズ後)
    if [ "$JST_HHMM" -gt 310 ] && [ "$JST_HHMM" -lt 1350 ]; then
        echo "[$TS] [CHECK3] Opening Range check: 市場外スキップ"
        return
    fi

    if [ ! -f "$SPY_LOG" ]; then
        send_alert "[Atlas] Opening Range 未記録" "spybot_stdout.log が存在しない"
        return
    fi

    local ORB_FOUND
    ORB_FOUND=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
        grep -c '\[ORB\] Opening Range: H=' || echo "0")
    ORB_FOUND=$(echo "$ORB_FOUND" | tr -d ' \n')

    echo "[$TS] [CHECK3] Opening Range logged=${ORB_FOUND}"
    if [ "${ORB_FOUND:-0}" -eq 0 ]; then
        send_alert "[Atlas] Opening Range 未記録" "22:50 JST 時点で [ORB] Opening Range: H=.*L= が本日ログに存在しない"
        echo "FAIL_ORB_RANGE" >> /tmp/atlas_monitor_alerts_$$
    fi
}

# ─── 条件4: premarket_check 未実施 ────────────────────────────────────────────
check_premarket() {
    if [ ! -f "$SPY_LOG" ]; then return; fi

    # CS_SELL: 23:35 JST (= 1415 分) 以降チェック
    if [ "$JST_HHMM" -ge 1415 ] || [ "$JST_HHMM" -le 300 ]; then
        local CS_FOUND
        CS_FOUND=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
            grep -c "\[CS_SELL\] premarket_check" 2>/dev/null || true)
        CS_FOUND=$(echo "${CS_FOUND:-0}" | tr -d ' \n')
        echo "[$TS] [CHECK4a] CS_SELL premarket_check=${CS_FOUND}"
        if [ "${CS_FOUND:-0}" -eq 0 ]; then
            send_alert "[Atlas] CS_SELL premarket_check 未実施" "23:35 JST 時点で [CS_SELL] premarket_check が本日ログにない"
            echo "FAIL_CS_PREMARKET" >> /tmp/atlas_monitor_alerts_$$
        fi
    fi

    # STRADDLE_BUY: 22:37 JST (= 1357 分) 以降チェック
    if [ "$JST_HHMM" -ge 1357 ] || [ "$JST_HHMM" -le 300 ]; then
        local ST_FOUND
        ST_FOUND=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
            grep -c "\[STRADDLE_BUY\] 9:35 premarket_check" 2>/dev/null || true)
        ST_FOUND=$(echo "${ST_FOUND:-0}" | tr -d ' \n')
        echo "[$TS] [CHECK4b] STRADDLE_BUY premarket_check=${ST_FOUND}"
        if [ "${ST_FOUND:-0}" -eq 0 ]; then
            send_alert "[Atlas] STRADDLE_BUY premarket_check 未実施" "22:37 JST 時点で [STRADDLE_BUY] 9:35 premarket_check が本日ログにない"
            echo "FAIL_STRADDLE_PREMARKET" >> /tmp/atlas_monitor_alerts_$$
        fi
    fi
}

# ─── 条件5: Bot 生死 (ログ更新が 120 秒以上途絶) ──────────────────────────────
check_bot_liveness() {
    if [ "$IN_MARKET_HOURS" -eq 0 ]; then
        echo "[$TS] [CHECK5] Bot liveness: 市場外スキップ"
        return
    fi

    for LOG_PATH in "$SPY_LOG" "$ATLAS_LOG"; do
        local LABEL
        LABEL=$(basename "$LOG_PATH" .log)
        if [ ! -f "$LOG_PATH" ]; then
            send_alert "[Atlas] $LABEL が存在しない" "ログファイルが見つからない: $LOG_PATH"
            echo "FAIL_LIVENESS_${LABEL}" >> /tmp/atlas_monitor_alerts_$$
            continue
        fi

        local MTIME
        # macOS stat
        MTIME=$(stat -f '%m' "$LOG_PATH" 2>/dev/null || stat -c '%Y' "$LOG_PATH" 2>/dev/null || echo "0")
        local AGE=$(( NOW_EPOCH - MTIME ))
        echo "[$TS] [CHECK5] $LABEL age=${AGE}s"
        if [ "$AGE" -ge 120 ]; then
            send_alert "[Atlas] $LABEL 更新途絶" "最終更新から${AGE}秒経過 (閾値120秒)。Bot停止の可能性"
            echo "FAIL_LIVENESS_${LABEL}" >> /tmp/atlas_monitor_alerts_$$
        fi
    done
}

# ─── 条件6: entry_check 到達ゼロ (03:30 JST=14:00ET 以降) ──────────────────
check_entry_attempt() {
    # 03:30 JST = 210 分
    if [ "$JST_HHMM" -lt 210 ] && [ "$JST_HHMM" -gt 50 ]; then
        echo "[$TS] [CHECK6] entry_attempt: 03:30前スキップ"
        return
    fi
    # 05:10 以降は場クローズ後でスキップ
    if [ "$JST_HHMM" -gt 310 ] && [ "$JST_HHMM" -lt 1350 ]; then
        echo "[$TS] [CHECK6] entry_attempt: 市場外スキップ"
        return
    fi
    if [ ! -f "$SPY_LOG" ]; then return; fi

    local ATTEMPT_COUNT
    ATTEMPT_COUNT=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
        grep -cE "entry_attempt|entry_fill|order_sent|SIMULATE.*placed" 2>/dev/null || true)
    ATTEMPT_COUNT=$(echo "${ATTEMPT_COUNT:-0}" | tr -d ' \n')

    # selector skip は除外(正常)
    local SKIP_COUNT
    SKIP_COUNT=$(grep "$TODAY" "$SPY_LOG" 2>/dev/null | \
        grep -cE "selector.*skip|strategy.*skip|no_trade|ノートレード" 2>/dev/null || true)
    SKIP_COUNT=$(echo "${SKIP_COUNT:-0}" | tr -d ' \n')

    echo "[$TS] [CHECK6] entry_attempt=${ATTEMPT_COUNT} skip=${SKIP_COUNT}"

    if [ "${ATTEMPT_COUNT:-0}" -eq 0 ] && [ "${SKIP_COUNT:-0}" -eq 0 ]; then
        send_alert "[Atlas] entry_check 到達ゼロ" "03:30 JST 時点でentry_attempt/fill/skip がゼロ。戦術パイプライン断絶の可能性"
        echo "FAIL_ENTRY" >> /tmp/atlas_monitor_alerts_$$
    fi
}

# ─── 条件7: Chronos 経路 (VPS SSH 経由で確認) ────────────────────────────────
check_chronos_vps() {
    local SSH_CMD="ssh -i $HOME/.ssh/deploy_key -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@198.13.37.17"

    # 7a: chronos_webhook.service active 確認
    local WEBHOOK_STATUS
    WEBHOOK_STATUS=$($SSH_CMD "systemctl is-active chronos_webhook.service 2>/dev/null" 2>/dev/null || echo "ssh_error")
    echo "[$TS] [CHECK7a] chronos_webhook.service=${WEBHOOK_STATUS}"
    if [ "$WEBHOOK_STATUS" != "active" ]; then
        send_alert "[Chronos] chronos_webhook.service 停止" "VPS上 chronos_webhook.service=$WEBHOOK_STATUS"
        echo "FAIL_CHRONOS_WEBHOOK" >> /tmp/atlas_monitor_alerts_$$
    fi

    # 7b: chronos_traderspost_forwarder.service active 確認
    local FORWARDER_STATUS
    FORWARDER_STATUS=$($SSH_CMD "systemctl is-active chronos_traderspost_forwarder.service 2>/dev/null" 2>/dev/null || echo "ssh_error")
    echo "[$TS] [CHECK7b] chronos_traderspost_forwarder.service=${FORWARDER_STATUS}"
    if [ "$FORWARDER_STATUS" != "active" ]; then
        send_alert "[Chronos] chronos_traderspost_forwarder.service 停止" "VPS上 chronos_traderspost_forwarder.service=$FORWARDER_STATUS"
        echo "FAIL_CHRONOS_FORWARDER" >> /tmp/atlas_monitor_alerts_$$
    fi

    # 7c: queue に signal 入ったのに 2 分以内に forwarder が処理していない
    # queue の最終書込み(stat) vs forwarder の最終 journal タイムスタンプ(epoch) を比較
    local QUEUE_MTIME FW_LAST_EPOCH
    QUEUE_MTIME=$($SSH_CMD "stat -c '%Y' /root/spxbot/data/chronos_webhook_queue.jsonl 2>/dev/null || echo 0" 2>/dev/null || echo "0")
    QUEUE_MTIME=$(echo "${QUEUE_MTIME:-0}" | tr -d ' \n')

    # journalctl の最終エントリを ISO8601 で取得してepoch変換
    FW_LAST_EPOCH=$($SSH_CMD "journalctl -u chronos_traderspost_forwarder.service --no-pager -n 1 --output=short-iso 2>/dev/null | awk '{print \$1}' | head -1 | xargs -I{} date -d '{}' '+%s' 2>/dev/null || echo 0" 2>/dev/null || echo "0")
    FW_LAST_EPOCH=$(echo "${FW_LAST_EPOCH:-0}" | tr -d ' \n')

    echo "[$TS] [CHECK7c] queue_mtime=${QUEUE_MTIME} fw_last_epoch=${FW_LAST_EPOCH}"

    local QUEUE_AGE=$(( NOW_EPOCH - ${QUEUE_MTIME:-0} ))
    local FW_LAG=$(( ${QUEUE_MTIME:-0} - ${FW_LAST_EPOCH:-0} ))

    # queueが最近(10分以内)に更新されていて、forwarder最終処理がqueue更新から2分以上前
    if [ "${QUEUE_AGE:-9999}" -le 600 ] && [ "${FW_LAG:-0}" -ge 120 ]; then
        send_alert "[Chronos] forwarder 処理遅延" "queue更新から${FW_LAG}秒後もforwarder未処理(queue_age=${QUEUE_AGE}s)"
        echo "FAIL_CHRONOS_FWDELAY" >> /tmp/atlas_monitor_alerts_$$
    fi
}

# ─── 全チェック実行 ────────────────────────────────────────────────────────────
touch /tmp/atlas_monitor_alerts_$$ 2>/dev/null || true

check_orb_fail
check_spx_center_anomaly
check_opening_range
check_premarket
check_bot_liveness
check_entry_attempt
check_chronos_vps

ALERT_COUNT=$(wc -l < /tmp/atlas_monitor_alerts_$$ 2>/dev/null || echo "0")
ALERT_COUNT=$(echo "$ALERT_COUNT" | tr -d ' ')
rm -f /tmp/atlas_monitor_alerts_$$

echo "[$TS] === done alerts=${ALERT_COUNT} ==="
