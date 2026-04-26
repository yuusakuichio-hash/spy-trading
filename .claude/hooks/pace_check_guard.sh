#!/bin/bash
# ゆうさくさんの前倒し最大化方針との矛盾を検知するhook
# PreToolUse時に呼ばれ、Agent投入プロンプトや成果物テキスト内の保守ペース語彙を検出

INPUT_JSON=$(cat)

# memory/規律/成果物ファイル編集は例外（違反例を記述する必要があるため）
SKIP=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    fp = str(ti.get('file_path', ''))
    skip_patterns = ['memory/', 'MEMORY.md', 'feedback_', '/hooks/', 'premortem_reports/', 'discipline_violations', 'pace_violations', 'project_session_', 'project_300m_', 'project_retirement_', 'project_chronos_monthly_', 'project_atlas_', 'project_mffu_', 'research_', 'roadmap_', 'data/eval/', 'retirement_timing_', 'violation_', 'pending_', '/scripts/', 'tests/test_violation']
    if any(s in fp for s in skip_patterns):
        print('SKIP')
except:
    pass
" 2>/dev/null)

if [ "$SKIP" = "SKIP" ]; then
    exit 0
fi

# tool_input から prompt / content / new_string / old_string を抽出
CONTENT=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    parts = []
    for k in ['prompt', 'content', 'new_string', 'description']:
        v = ti.get(k, '')
        if isinstance(v, str):
            parts.append(v)
    print(' '.join(parts))
except:
    pass
" 2>/dev/null)

if [ -z "$CONTENT" ]; then
    exit 0
fi

# 保守ペース違反パターン
PATTERNS=(
    "段階的に"
    "徐々に"
    "一週間に1"
    "一ヶ月に1"
    "試運転期間"
    "慎重に進める"
    "ゆっくり"
    "様子を見て"
    "まずは1プラン"
    "まず1firm"
    "順次追加"
    "初月は.*単独"
    "来月から本格"
    "翌月以降に"
)

VIOLATIONS=""
for p in "${PATTERNS[@]}"; do
    if echo "$CONTENT" | grep -qE "$p"; then
        VIOLATIONS="$VIOLATIONS\n  [保守ペース] \"$p\""
    fi
done

if [ -n "$VIOLATIONS" ]; then
    LOG_FILE="/Users/yuusakuichio/trading/data/logs/pace_violations.log"
    mkdir -p "$(dirname "$LOG_FILE")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S JST')] PACE CHECK VIOLATION" >> "$LOG_FILE"
    echo -e "  Content excerpt: ${CONTENT:0:300}" >> "$LOG_FILE"
    echo -e "  Violations:$VIOLATIONS" >> "$LOG_FILE"

    echo "[PACE CHECK GUARD] ゆうさくさん前倒し最大化方針と矛盾する語彙を検知:" >&2
    echo -e "$VIOLATIONS" >&2
    echo "[PACE CHECK GUARD] 最速ケースを想定。段階展開は明示指示 or 物理的制約ある時のみ。" >&2
    echo "[PACE CHECK GUARD] 参照: feedback_goal_acceleration_first.md / feedback_full_speed_default.md / feedback_no_schedule_delay.md" >&2
    echo "[PACE CHECK GUARD] ログ: $LOG_FILE" >&2
    exit 2
fi
exit 0
