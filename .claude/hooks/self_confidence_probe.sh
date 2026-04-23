#!/usr/bin/env bash
# self_confidence_probe.sh
# Stop hook: P(True)/P(IK) Self-Ask pattern (Kadavath Anthropic 2022)
# 応答内の断定 に self-declared confidence がないと violation 記録
# 2026-04-21 導入・feedback_no_schedule_delay 遵守

set -uo pipefail

if [ "${SELF_CONFIDENCE_PROBE_BYPASS:-0}" = "1" ]; then
  exit 0
fi

LOG="/Users/yuusakuichio/trading/data/logs/self_confidence_probe.log"
mkdir -p "$(dirname "$LOG")"
TS=$(date "+%Y-%m-%d %H:%M:%S JST")

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '')
except:
    print('')
" 2>/dev/null)

[ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ] && exit 0

LAST_RESPONSE=$(python3 <<PYEOF 2>/dev/null || echo ""
import json, pathlib
p = pathlib.Path("$TRANSCRIPT_PATH")
for line in reversed(p.read_text(errors="replace").splitlines()):
    if not line.strip(): continue
    try: d = json.loads(line)
    except: continue
    if d.get("type") == "assistant":
        m = d.get("message", {}); c = m.get("content", [])
        if isinstance(c, list):
            ts = [b.get("text","") for b in c if isinstance(b,dict) and b.get("type")=="text"]
            print("\n".join(ts)[:6000]); break
PYEOF
)

[ -z "$LAST_RESPONSE" ] && exit 0

# 断定 pattern: 「である」「確定」「保証」「完璧」「必ず」
ASSERTION=$(echo "$LAST_RESPONSE" | grep -cE "(である|確定|保証|完璧|必ず|絶対)" || echo 0)

# self-declared confidence: 「確信度 X%」「X%確実」「信頼度 X」
CONFIDENCE=$(echo "$LAST_RESPONSE" | grep -cE "(確信度[ ]*[0-9]+|[0-9]+%[ ]*(確実|確信|信頼)|信頼度[ ]*[0-9]+|P\(true\)|P\(IK\))" || echo 0)

# 断定多数 かつ confidence 宣言なし → violation
if [ "$ASSERTION" -ge 2 ] && [ "$CONFIDENCE" -eq 0 ]; then
  echo "[$TS] VIOLATION self_confidence_missing | assertion=$ASSERTION confidence=0" >> "$LOG"
fi

exit 0
