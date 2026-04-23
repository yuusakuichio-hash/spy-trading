#!/usr/bin/env bash
# stepwise_test_report.sh
# Stop hook: Process Reward (Lightman OpenAI 2023)
# "test passed" 検出時 step breakdown を要求
# 2026-04-21 導入

set -uo pipefail

if [ "${STEPWISE_TEST_BYPASS:-0}" = "1" ]; then
  exit 0
fi

LOG="/Users/yuusakuichio/trading/data/logs/stepwise_test_report.log"
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

# "N passed" pattern 検出
if echo "$LAST_RESPONSE" | grep -qE "([0-9]+)[ ]*(passed|PASS|合格)"; then
  # step breakdown: "ファイル名.*N/N" or "module: N/N" 等
  HAS_BREAKDOWN=0
  if echo "$LAST_RESPONSE" | grep -qE "([a-zA-Z_]+\.(py|sh):[0-9]+/[0-9]+|test_[a-zA-Z_]+[^0-9]*[0-9]+/[0-9]+|[0-9]+件.*PASS|個別.*件数)"; then
    HAS_BREAKDOWN=1
  fi

  if [ "$HAS_BREAKDOWN" -eq 0 ]; then
    echo "[$TS] WARN stepwise_breakdown_missing | summary_only_test_report" >> "$LOG"
  fi
fi

exit 0
