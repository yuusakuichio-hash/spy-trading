#!/bin/bash
# Mac sleep 防止 daemon（T-1 Redteam r8 warning 対応）
# 2026-04-24 策定
#
# Sprint 2 Day 1 以降、MonitorDaemon を常駐させるには Mac が sleep しないことが必要。
# caffeinate -i で macOS を idle sleep させない。
#
# Usage:
#   scripts/mac_caffeinate_daemon.sh start        # caffeinate 起動
#   scripts/mac_caffeinate_daemon.sh stop         # 停止
#   scripts/mac_caffeinate_daemon.sh status       # 状態確認
#
# デーモン化（launchd）したい場合:
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.soralab.caffeinate.plist

PID_FILE="/tmp/soralab_caffeinate.pid"

case "${1:-status}" in
    start)
        if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
            echo "✓ caffeinate already running (PID $(cat $PID_FILE))"
            exit 0
        fi
        nohup caffeinate -i -s -w $$ > /dev/null 2>&1 &
        CAFF_PID=$!
        echo $CAFF_PID > "$PID_FILE"
        echo "✓ caffeinate started (PID $CAFF_PID). Mac will not sleep."
        ;;
    stop)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if kill -0 "$PID" 2>/dev/null; then
                kill "$PID" && echo "✓ caffeinate stopped (was PID $PID)"
                rm -f "$PID_FILE"
            else
                echo "✗ caffeinate not running"
                rm -f "$PID_FILE"
            fi
        else
            echo "✗ no pid file"
        fi
        ;;
    status)
        if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
            echo "✓ RUNNING (PID $(cat $PID_FILE))"
            exit 0
        else
            echo "✗ NOT RUNNING"
            exit 1
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
