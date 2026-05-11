#!/usr/bin/env bash
# deferral_language_guard.sh — 先延ばし語検出(ドメイン非依存版)
#
# Stop hook: 応答内の「明日」「後日」「後回し」等を検出して violation 記録
# Pushover 設定済みなら通知(未設定なら通知 skip・検知ロジックは機能継続)
#
# Bypass: DEFERRAL_GUARD_BYPASS=1

set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"
TS=$(date "+%Y-%m-%d %H:%M:%S")

if [ "${DEFERRAL_GUARD_BYPASS:-0}" = "1" ]; then
  exit 0
fi

# .env から Pushover 認証情報 load(任意)
if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
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

export _DLG_TRANSCRIPT="$TRANSCRIPT_PATH"
LAST_RESPONSE=$(python3 <<'PYEOF' 2>/dev/null || echo ""
import json, pathlib, os
p = pathlib.Path(os.environ.get("_DLG_TRANSCRIPT", ""))
if not p.exists():
    raise SystemExit(0)
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

DEFERRAL_PATTERN='(明日以降|明日の|明日中|明朝以降|明朝中|明朝の|後日|後で|後回し|次回|来週|週末に|本日は見送り|継続確認|後ほど|一旦保留)'

EXCLUDE_PATTERN='(禁句|禁止|「明日|"明日|`明日|ルール|規律|違反|feedback_)'

MATCHES=$(echo "$LAST_RESPONSE" | grep -oE "$DEFERRAL_PATTERN" | head -5 | tr '\n' ',')

if [ -z "$MATCHES" ]; then
  exit 0
fi

EXCLUDE_COUNT=$(echo "$LAST_RESPONSE" | grep -cE "$EXCLUDE_PATTERN" || echo 0)
DEFER_COUNT=$(echo "$MATCHES" | tr ',' '\n' | grep -c . || echo 0)

if [ "$EXCLUDE_COUNT" -gt "$DEFER_COUNT" ]; then
  echo "[$TS] deferral_language with rule context, skip | matches=$MATCHES | exclude_count=$EXCLUDE_COUNT" >> "$LOG_DIR/deferral_guard_passed.log"
  exit 0
fi

echo "[$TS] VIOLATION deferral_language | matches=$MATCHES | first200=$(echo "$LAST_RESPONSE" | head -c 200 | tr '\n' ' ')" >> "$LOG_DIR/deferral_violations.log"

cat >> "$LOG_DIR/pending_proposal_violations.md" <<EOF

## [$TS] 先延ばし語違反
検出語: $MATCHES
抜粋: $(echo "$LAST_RESPONSE" | head -c 200 | tr '\n' ' ')
→ 次回応答で「明日」「明朝」「後日」系を出した瞬間に即自覚すること。

EOF

# Pushover 通知(認証情報設定済みかつ静寂時間外のみ)
HOUR=$(date +%H)
if [ -n "${PUSHOVER_TOKEN:-}" ] && [ -n "${PUSHOVER_USER:-}" ] && [ "$HOUR" -ge 5 ] && [ "$HOUR" -lt 22 ]; then
  curl -s -X POST https://api.pushover.net/1/messages.json \
    -F "token=$PUSHOVER_TOKEN" \
    -F "user=$PUSHOVER_USER" \
    -F "title=[ALERT] Claude used deferral language" \
    -F "message=Matches: $MATCHES" \
    > /dev/null 2>&1 || true
fi

exit 0
