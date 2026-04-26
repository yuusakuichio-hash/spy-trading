#!/bin/bash
# sprint_close_gate.sh
# 3 者統合推奨（2026-04-23）: Sprint 閉鎖の物理強制 gate
# carryover 残件 > 0 or Exit Criteria 未達で次 Sprint 着手 block
#
# 入力: tool_input.command で次 Sprint 着手コマンド検出
# 閉鎖条件（Phase 0-A 決定）:
#   - CRITICAL 実害高 == 0
#   - HIGH <= 5
#   - Navigator PASS（未 block）
#   - carryover 未解消件数 == 0（or 許容理由あり）

set -u

META_PATH="/Users/yuusakuichio/trading/.claude/hooks/sprint_meta.json"
CARRYOVER_PATH="/Users/yuusakuichio/trading/data/sprint1_carryovers.md"
LOG="/Users/yuusakuichio/trading/data/logs/sprint_close_gate.log"

if [ "${SPRINT_CLOSE_GATE_BYPASS:-0}" = "1" ]; then
    exit 0
fi

INPUT_JSON=$(cat)

# 対象: Sprint 着手コマンドパターン（新 Sprint の Builder dispatch や plan 起票）
TRIGGER=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    text = ''
    for k in ['command', 'prompt', 'description', 'content', 'new_string']:
        v = ti.get(k, '') if isinstance(ti, dict) else ''
        if isinstance(v, str):
            text += ' ' + v
    print(text)
except Exception:
    pass
" 2>/dev/null)

# Sprint 着手パターン検出
if ! echo "$TRIGGER" | grep -qiE 'Sprint\s*(2|3|[4-9])\s*(着手|開始|start|kickoff|dispatch)'; then
    exit 0
fi

mkdir -p "$(dirname "$LOG")"
ts=$(date '+%Y-%m-%d %H:%M:%S')

# carryover 未解消件数検査（Sprint 2 向け = C-001〜C-015）
if [ ! -f "$CARRYOVER_PATH" ]; then
    echo "[SPRINT_CLOSE_GATE] WARN: carryover file not found, skipping ($ts)" | tee -a "$LOG" >&2
    exit 0
fi

# 未解消項目カウント（## C-NNN: で始まる項目数）
CARRYOVER_COUNT=$(grep -cE '^## C-[0-9]+:' "$CARRYOVER_PATH" 2>/dev/null || echo 0)

# meta.json から Sprint 閉鎖条件読み込み（未作成なら警告のみ）
if [ -f "$META_PATH" ]; then
    EXIT_OK=$(python3 -c "
import json, sys
try:
    with open('$META_PATH') as f:
        m = json.load(f)
    # current_sprint_exit_criteria_met: bool
    print('1' if m.get('current_sprint_exit_criteria_met', False) else '0')
except Exception:
    print('0')
" 2>/dev/null)
else
    EXIT_OK='0'
    echo "[SPRINT_CLOSE_GATE] INFO: $META_PATH not found, treating exit criteria as not met ($ts)" | tee -a "$LOG" >&2
fi

if [ "$CARRYOVER_COUNT" -gt 0 ] || [ "$EXIT_OK" != '1' ]; then
    cat >&2 <<EOF
[SPRINT_CLOSE_GATE] BLOCK: 次 Sprint 着手 block
  - carryover 未解消件数: $CARRYOVER_COUNT (> 0 なら block)
  - 現 Sprint Exit Criteria 充足: $EXIT_OK (1 なら充足)
  参照: $CARRYOVER_PATH
  閉鎖条件:
    1) CRITICAL 実害高 == 0
    2) HIGH <= 5
    3) Navigator PASS
    4) carryover 残件 == 0 (許容例外は $META_PATH に記録)
  一時 bypass: export SPRINT_CLOSE_GATE_BYPASS=1
EOF
    echo "[$ts] BLOCK carryover=$CARRYOVER_COUNT exit_ok=$EXIT_OK" >> "$LOG"
    exit 2
fi

echo "[$ts] PASS carryover=$CARRYOVER_COUNT exit_ok=$EXIT_OK" >> "$LOG"
exit 0
