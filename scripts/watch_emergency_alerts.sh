#!/usr/bin/env bash
# watch_emergency_alerts.sh — emergency_alerts.log tail監視 + macOS通知
# P-3: Pushover 429 IP ban中のfallback通知経路
# 起動: LaunchAgent com.soralab.emergency_watcher.plist が常時起動する
# 方式: tail -F で新規行を検知 → osascript で macOS通知発動

ALERT_LOG="/Users/yuusakuichio/trading/data/logs/emergency_alerts.log"
MARKER_FILE="/tmp/emergency_watcher_last_pos"

# ログファイルが存在しない場合は作成して待機
mkdir -p "$(dirname "$ALERT_LOG")"
touch "$ALERT_LOG"

echo "[watch_emergency_alerts] started at $(date '+%Y-%m-%d %H:%M:%S JST')"
echo "[watch_emergency_alerts] watching: $ALERT_LOG"

# tail -F で追記を検知しながら macOS通知を発火
tail -F "$ALERT_LOG" | while IFS= read -r line; do
    # 空行スキップ
    [[ -z "$line" ]] && continue

    # EMERGENCY キーワード検知
    if echo "$line" | grep -q "EMERGENCY"; then
        # タイトルとメッセージを抽出 (フォーマット: timestamp | EMERGENCY | title | message)
        title=$(echo "$line" | awk -F' \\| ' '{print $3}')
        message=$(echo "$line" | awk -F' \\| ' '{print $4}')
        [[ -z "$title" ]]   && title="Chronos 緊急アラート"
        [[ -z "$message" ]] && message="$line"

        echo "[watch_emergency_alerts] ALERT DETECTED: $title — $message"

        # macOS通知発動 (Pushover代替)
        osascript -e "display notification \"$message\" with title \"[EMERGENCY] $title\" sound name \"Basso\""

        # 追加: ターミナルベル
        printf '\a'
    fi
done
