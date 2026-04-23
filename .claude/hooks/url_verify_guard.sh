#!/usr/bin/env bash
# url_verify_guard.sh
# Stop hook: assistant応答中のURLを curl-check し、404/5xx なら violation 記録
# 2026-04-21 導入
# 背景: URL verify なしで user に提示して 404 を踏ませるパターン繰り返し防止

set -uo pipefail

LOG_DIR="/Users/yuusakuichio/trading/data/logs"
mkdir -p "$LOG_DIR"
TS=$(date "+%Y-%m-%d %H:%M:%S JST")

# Bypass
if [ "${URL_VERIFY_GUARD_BYPASS:-0}" = "1" ]; then
  exit 0
fi

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '')
except:
    print('')
" 2>/dev/null || echo "")

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# 最後のアシスタント応答 text
LAST_RESPONSE=$(python3 <<PYEOF 2>/dev/null || echo ""
import json, pathlib
p = pathlib.Path("$TRANSCRIPT_PATH")
for line in reversed(p.read_text(errors="replace").splitlines()):
    if not line.strip(): continue
    try: d = json.loads(line)
    except: continue
    if d.get("type") == "assistant":
        msg = d.get("message", {}); content = msg.get("content", [])
        if isinstance(content, list):
            texts = [b.get("text","") for b in content if isinstance(b,dict) and b.get("type")=="text"]
            print("\n".join(texts)[:8000]); break
PYEOF
)

[ -z "$LAST_RESPONSE" ] && exit 0

# URL 抽出（https://... で スペース・括弧・日本語まで）
URLS=$(echo "$LAST_RESPONSE" | grep -oE 'https://[A-Za-z0-9./?#_%=&:+-]+' | sort -u | head -5)
[ -z "$URLS" ] && exit 0

BROKEN=""
while IFS= read -r url; do
  [ -z "$url" ] && continue
  # 末尾の句読点除去
  url=$(echo "$url" | sed 's/[.,、。)]$//')
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -L --max-time 5 "$url" 2>/dev/null || echo "000")
  if echo "$CODE" | grep -qE '^(404|410|5..)$'; then
    BROKEN="$BROKEN $url($CODE)"
  fi
done <<< "$URLS"

if [ -n "$BROKEN" ]; then
  echo "[$TS] VIOLATION broken_url_provided |$BROKEN" >> "$LOG_DIR/url_verify_violations.log"
  # pending injection for next prompt
  cat >> "$LOG_DIR/pending_proposal_violations.md" <<EOF

## [$TS] URL 検証違反
Broken URL(s) given to user without verification:$BROKEN
→ 次回URL提示前に必ず curl -sI で動作確認すること

EOF
  # Pushover通知 (quiet時間以外)
  HOUR=$(date +%H)
  if [ "$HOUR" -ge 5 ] && [ "$HOUR" -lt 22 ]; then
    curl -s -X POST https://api.pushover.net/1/messages.json \
      -F "token=a5rb9ipb3yrdanv3vk4n8x28qt7io9" \
      -F "user=u2cevk8nktib3sr148rw2hs78ecvux" \
      -F "title=[ALERT] Broken URL sent to user" \
      -F "message=URL(s) returning error:$BROKEN" \
      > /dev/null 2>&1 || true
  fi
fi

exit 0
