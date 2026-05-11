#!/usr/bin/env bash
# confidence_assertion_guard.sh — 根拠なし断定検出 hook(ドメイン非依存版)
#
# Stop hook: 「X%確実」「全合格」「完全稼働」等の断定 + evidence path 不在で exit 2 BLOCK
#
# Bypass: CONFIDENCE_GUARD_BYPASS=1

set -euo pipefail

[[ "${CONFIDENCE_GUARD_BYPASS:-0}" == "1" ]] && exit 0

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    print(d.get('transcript_path',''))
except Exception:
    print('')
" 2>/dev/null || echo "")

[[ -z "$TRANSCRIPT" ]] && exit 0
[[ ! -f "$TRANSCRIPT" ]] && exit 0

export _CAG_TRANSCRIPT="$TRANSCRIPT"
LAST_TEXT=$(python3 << 'PYEOF'
import json, sys, os
transcript = os.environ.get("_CAG_TRANSCRIPT","")
if not transcript or not os.path.exists(transcript):
    sys.exit(0)
events = []
for line in open(transcript, errors="replace"):
    line = line.strip()
    if not line:
        continue
    try:
        events.append(json.loads(line))
    except:
        continue
for ev in reversed(events):
    if ev.get("type") != "assistant":
        continue
    content = ev.get("message",{}).get("content",[])
    if not isinstance(content, list):
        continue
    parts = [b.get("text","") for b in content if isinstance(b,dict) and b.get("type")=="text"]
    if parts:
        text = "\n".join(parts)[:8000]
        print(text)
        break
PYEOF
)

[[ -z "$LAST_TEXT" ]] && exit 0

PATTERNS=(
    '[0-9][0-9]?%[[:space:]]*(確実|保証|保障|guaranteed|certain)'
    '全[件合格テスト]*(合格|PASS|pass)'
    '完全稼働'
    '完全に動作'
    '100%'
    'all.*pass'
    'fully.*operational'
    '動作確認済み$'
    '全テスト.*合格'
    '0.*failed'
    'zero.*failure'
)

MATCHED=""
for pat in "${PATTERNS[@]}"; do
    if echo "$LAST_TEXT" | grep -iEq "$pat"; then
        MATCHED="$MATCHED|$pat"
    fi
done

[[ -z "$MATCHED" ]] && exit 0

# evidence file path が含まれているか確認
if echo "$LAST_TEXT" | grep -Eq 'data/(ops|eval|logs|backtest)|tests/|\.md|grep -|pytest|mutmut|coverage'; then
    exit 0
fi

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
TS=$(python3 -c "from datetime import datetime,timezone,timedelta; print(datetime.now(timezone(timedelta(hours=9))).isoformat(timespec='seconds'))")
LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"
echo "[$TS] CONFIDENCE_ASSERTION_BLOCK | matched: ${MATCHED}" >> "$LOG_DIR/confidence_assertion_violations.log"

PENDING="$LOG_DIR/pending_proposal_violations.md"
{
echo ""
echo "## [$TS] confidence_assertion_guard HARD BLOCK"
echo "断定パターン検知: ${MATCHED}"
echo "→ evidence file path (data/ops/*.md, pytest 出力, grep 結果 等) を応答に含めてから再送"
echo "  緊急回避: CONFIDENCE_GUARD_BYPASS=1"
} >> "$PENDING"

echo "[CONFIDENCE_ASSERTION_GUARD] BLOCKED: assertion without evidence." >&2
echo "  Matched pattern(s): ${MATCHED}" >&2
echo "  Add evidence file path (data/ops/*.md, pytest output, etc.) to your response." >&2
echo "  Override: CONFIDENCE_GUARD_BYPASS=1" >&2
exit 2
