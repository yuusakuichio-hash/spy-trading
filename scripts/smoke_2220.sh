#!/bin/bash
# 22:20 JST atlas_agent スモーク確認 + Pushover通知
LOGFILE="/Users/yuusakuichio/trading/data/logs/smoke_2220.log"
TS=$(date '+%Y-%m-%d %H:%M:%S')

# atlas_agent PID確認
ATLAS_PID=$(launchctl list | grep "com.atlas.agent" | grep -v "stop\|stop" | awk '{print $1}')
STATE=$(cat /Users/yuusakuichio/trading/data/atlas_state.json 2>/dev/null)
PDT=$(echo "$STATE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('pdt_constrained','NOT_FOUND'))" 2>/dev/null || echo "READ_ERR")

echo "[$TS] SMOKE 22:20: atlas_pid=$ATLAS_PID pdt_constrained=$PDT" >> "$LOGFILE"

if [ "$ATLAS_PID" = "-" ] || [ -z "$ATLAS_PID" ]; then
    # 停止中 → キックスタート
    launchctl kickstart -k "gui/$(id -u)/com.atlas.agent" >> "$LOGFILE" 2>&1
    MSG="[ALERT] atlas_agent DOWN → kickstart実施 (22:20JST)"
    PRIORITY=1
else
    MSG="[OK] atlas_agent PID=$ATLAS_PID 稼働確認 (22:20JST)\npdt_constrained=$PDT"
    PRIORITY=0
fi

echo "[$TS] $MSG" >> "$LOGFILE"

# Pushover送信
python3 -c "
import sys
sys.path.insert(0, '/Users/yuusakuichio/trading')
from common.pushover_client import send
send('[Atlas/SMOKE]', '''$MSG''', priority=$PRIORITY)
" >> "$LOGFILE" 2>&1
