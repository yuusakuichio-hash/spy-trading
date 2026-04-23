#!/usr/bin/env bash
# sycophancy_detector.sh
# UserPromptSubmit hook: 同調語検出 -> 次回応答に強制注入
# Stop hook: 根拠なし同調を HARD BLOCK
#
# 設計:
#   - 「その通りです」「おっしゃる通り」「確かに」等の同調語を検知
#   - 同調直後に具体的 evidence or 反例がなければ BLOCK (exit 2)
#   - SYCOPHANCY_BYPASS=1 で緊急回避
#
# 出典:
#   - Turpin et al. (2023) "Large Language Models Are Not Robust Multiple Choice Selectors"
#   - Sharma et al. (2023) "Towards Understanding Sycophancy in LLMs" (Anthropic)
#   -航空 CRM: "Challenge and Response" - 上位者の言葉でも根拠なき同調は事故源

set -euo pipefail

[[ "${SYCOPHANCY_BYPASS:-0}" == "1" ]] && exit 0

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('transcript_path',''))
" 2>/dev/null || echo "")

[[ -z "$TRANSCRIPT" ]] && exit 0
[[ ! -f "$TRANSCRIPT" ]] && exit 0

export _SYC_TRANSCRIPT="$TRANSCRIPT"
LAST_TEXT=$(python3 << 'PYEOF'
import json, sys, os
transcript = os.environ.get("_SYC_TRANSCRIPT","")
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
        print("\n".join(parts)[:8000])
        break
PYEOF
)

[[ -z "$LAST_TEXT" ]] && exit 0

# 同調パターン（文頭・文中）
SYCO_PATTERNS=(
    'その通りです'
    'おっしゃる通り'
    'ご指摘の通り'
    'なるほど、確かに'
    '^確かに'
    'おっしゃる通り'
    'ご明察'
    'まさにその通り'
    'that.s (correct|right|true)'
    'you.re (absolutely|completely) right'
    'great (point|observation|catch)'
    'excellent (point|question)'
    'i agree completely'
)

MATCHED=""
for pat in "${SYCO_PATTERNS[@]}"; do
    if echo "$LAST_TEXT" | grep -iEq "$pat"; then
        MATCHED="$MATCHED [$pat]"
    fi
done

[[ -z "$MATCHED" ]] && exit 0

# 同調後に evidence/反例/数値/コード引用があれば許可
# 根拠の存在チェック: grep 結果 / python 出力 / 数値 / "ただし" 等の反論語
if echo "$LAST_TEXT" | grep -Eq '(ただし|一方|ただ|しかし|but|however|although|except|実測|grep|pytest|実際には|実データ|[0-9]+\.[0-9]+%|lines:[[:space:]]*[0-9])'; then
    exit 0
fi

TS=$(python3 -c "from datetime import datetime,timezone,timedelta; print(datetime.now(timezone(timedelta(hours=9))).isoformat(timespec='seconds'))")
LOG_DIR="/Users/yuusakuichio/trading/data/logs"
mkdir -p "$LOG_DIR"
echo "[$TS] SYCOPHANCY_BLOCK | matched:${MATCHED}" >> "$LOG_DIR/sycophancy_violations.log"

PENDING="$LOG_DIR/pending_proposal_violations.md"
{
echo ""
echo "## [$TS] sycophancy_detector HARD BLOCK"
echo "同調語検知: ${MATCHED}"
echo "→ 同調前に反論/確認/evidence を提示してから再送"
echo "  緊急回避: SYCOPHANCY_BYPASS=1"
} >> "$PENDING"

echo "[SYCOPHANCY_DETECTOR] BLOCKED: agreement without evidence/counter-argument." >&2
echo "  Matched: ${MATCHED}" >&2
echo "  Provide evidence, counter-point, or data BEFORE agreeing." >&2
echo "  Override: SYCOPHANCY_BYPASS=1" >&2
exit 2
