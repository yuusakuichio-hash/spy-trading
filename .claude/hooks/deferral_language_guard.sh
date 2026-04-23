#!/usr/bin/env bash
# deferral_language_guard.sh
# Stop hook: Claude の出力に「明日」「明朝」「後日」「後回し」等の先延ばし語が含まれたら
# 即 violation 記録 + Pushover + 次 session 注入
#
# 2026-04-21 導入 - 既存 discipline_guard.sh は PreToolUse/UserPromptSubmit 限定で
# Claude の応答テキストを check してなかった
#
# ゆうさくさん「明日禁句・違反時物理ブロック」指示

set -uo pipefail

LOG_DIR="/Users/yuusakuichio/trading/data/logs"
mkdir -p "$LOG_DIR"
TS=$(date "+%Y-%m-%d %H:%M:%S JST")

if [ "${DEFERRAL_GUARD_BYPASS:-0}" = "1" ]; then
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

# 先延ばし語句パターン (assistant が自ら使った場合のみ)
DEFERRAL_PATTERN='(明日以降|明日の|明日中|明朝以降|明朝中|明朝の|後日|後で|後回し|次回|来週|週末に|本日は見送り|継続確認|後ほど|一旦保留)'

# 除外パターン: quote / user引用 / 禁句説明
EXCLUDE_PATTERN='(禁句|禁止|「明日|"明日|`明日|ルール|規律|違反|feedback_)'

MATCHES=$(echo "$LAST_RESPONSE" | grep -oE "$DEFERRAL_PATTERN" | head -5 | tr '\n' ',')

if [ -z "$MATCHES" ]; then
  exit 0
fi

# 除外語彙が応答内に多く含まれる = ルール説明文脈の可能性
EXCLUDE_COUNT=$(echo "$LAST_RESPONSE" | grep -cE "$EXCLUDE_PATTERN" || echo 0)
DEFER_COUNT=$(echo "$MATCHES" | tr ',' '\n' | grep -c . || echo 0)

if [ "$EXCLUDE_COUNT" -gt "$DEFER_COUNT" ]; then
  # ルール説明文脈 = OK
  echo "[$TS] deferral_language with rule context, skip | matches=$MATCHES | exclude_count=$EXCLUDE_COUNT" >> "$LOG_DIR/deferral_guard_passed.log"
  exit 0
fi

# 違反確定
echo "[$TS] VIOLATION deferral_language | matches=$MATCHES | first200=$(echo "$LAST_RESPONSE" | head -c 200 | tr '\n' ' ')" >> "$LOG_DIR/deferral_violations.log"

# pending_proposal_violations.md へ注入
cat >> "$LOG_DIR/pending_proposal_violations.md" <<EOF

## [$TS] 明日禁句違反
検出語: $MATCHES
抜粋: $(echo "$LAST_RESPONSE" | head -c 200 | tr '\n' ' ')
→ 次回応答で「明日」「明朝」「後日」系を出した瞬間に即自覚すること。

EOF

# Pushover 通知 (quiet 時間外)
HOUR=$(date +%H)
if [ "$HOUR" -ge 5 ] && [ "$HOUR" -lt 22 ]; then
  curl -s -X POST https://api.pushover.net/1/messages.json \
    -F "token=a5rb9ipb3yrdanv3vk4n8x28qt7io9" \
    -F "user=u2cevk8nktib3sr148rw2hs78ecvux" \
    -F "title=[ALERT] Claude used deferral language" \
    -F "message=Matches: $MATCHES" \
    > /dev/null 2>&1 || true
fi

exit 0
