#!/bin/bash
# discipline_guard.sh — 規律違反リアルタイム検出(ドメイン非依存版)
#
# 先延ばし語・確認癖・桁違い見積・虚偽完了・メモリ完了扱いを検出
# 同カテゴリ 3 回目で hard block (exit 2)
#
# Bypass: DISCIPLINE_GUARD_BYPASS=1

set -u

if [ "${DISCIPLINE_GUARD_BYPASS:-0}" = "1" ]; then
  exit 0
fi

if [ "${DISCIPLINE_GUARD_TEST:-0}" = "1" ]; then
  exit 0
fi

INPUT_JSON=$(cat)
PAYLOAD=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    parts = []
    for k in ['prompt','content','new_string','description','command','old_string','file_path']:
        v = ti.get(k, '') if isinstance(ti, dict) else ''
        if isinstance(v, str):
            parts.append(v)
    up = d.get('user_prompt', '')
    if isinstance(up, str): parts.append(up)
    msg = d.get('message', {})
    if isinstance(msg, dict):
        c = msg.get('content', '')
        if isinstance(c, str): parts.append(c)
    print(' '.join(parts))
except Exception:
    pass
" 2>/dev/null)

[ -z "$PAYLOAD" ] && exit 0

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
LOG="$PROJECT_DIR/data/logs/discipline_violations.log"
RELOAD_LOG="$PROJECT_DIR/data/logs/discipline_memory_reloads.log"
mkdir -p "$(dirname "$LOG")"

if echo "$PAYLOAD" | grep -qE 'MEMORY_RELOAD_TRIGGERED'; then
  ts=$(date '+%Y-%m-%d %H:%M:%S')
  {
    echo "[$ts] MEMORY_RELOAD_TRIGGERED (discipline_guard skip)"
    echo "  payload_head: $(echo "$PAYLOAD" | head -c 200 | tr '\n' ' ')"
    echo "---"
  } >> "$RELOAD_LOG"
  exit 0
fi

violations=()

if echo "$PAYLOAD" | grep -qE "(禁句|deferral_language|先延ばし規律|feedback_no_schedule_delay|feedback_cycle_complete_today|discipline_guard)"; then
  exit 0
fi

check() {
  local pattern="$1"
  local category="$2"
  local memory="$3"
  if echo "$PAYLOAD" | grep -qE "$pattern"; then
    violations+=("[$category] matched '$pattern' (see memory/$memory)")
  fi
}

check '明日' '先延ばし' 'feedback_no_schedule_delay.md'
check '朝にする|朝に対応|朝チェック' '先延ばし' 'feedback_no_schedule_delay.md'
check '後日|週末に|別タスクで|来週|後ほど' '先延ばし' 'feedback_no_schedule_delay.md'
check '2-3日|2〜3日|3-7日|数日後|明日以降' '先延ばし' 'feedback_no_schedule_delay.md'

check '進めていい？|GOなら|どれからやる？|大丈夫？進める|全部Yes？' '確認癖' 'feedback_no_confirmation_execute_now.md'

check '[234]-?[356]h|[234]〜[356]時間|4-6時間|半日' '桁違い見積' 'feedback_implementation_process.md'

if echo "$PAYLOAD" | grep -qE '全件?合格|全テストpass|全PASS|完了宣言' && ! echo "$PAYLOAD" | grep -qE 'grep.*result|AST.*verify|実grep|mutation.*score|pytest.*output'; then
  violations+=("[虚偽完了] 証跡4点セットなし完了宣言 (see memory/feedback_false_completion_governance.md)")
fi

if echo "$PAYLOAD" | grep -qE 'メモリに保存.*対策|メモリ化.*解決|メモ.*刻印.*完了'; then
  violations+=("[認知限界放置] メモリだけで完了扱い禁止・hook/物理実装まで (see memory/feedback_no_general_claims.md)")
fi

[ ${#violations[@]} -eq 0 ] && exit 0

ts=$(date '+%Y-%m-%d %H:%M:%S')
{
  echo "[$ts] discipline violations:"
  for v in "${violations[@]}"; do echo "  - $v"; done
  echo "  payload_head: $(echo "$PAYLOAD" | head -c 200 | tr '\n' ' ')"
  echo "---"
} >> "$LOG"

registry="$PROJECT_DIR/data/logs/violation_registry.jsonl"
mkdir -p "$(dirname "$registry")"
cat_key=$(printf '%s\n' "${violations[@]}" | head -1 | grep -oE '\[[^]]+\]' | head -1)
count=$(grep -c "$cat_key" "$registry" 2>/dev/null || echo 0)
echo "{\"ts\":\"$ts\",\"category\":\"$cat_key\",\"count\":$((count+1))}" >> "$registry"

cat >&2 <<EOF
[DISCIPLINE GUARD] 規律違反検知 (カテゴリ再発回数: $((count+1))):
$(printf '  %s\n' "${violations[@]}")
[DISCIPLINE GUARD] 過去違反累計 $count 件同カテゴリ。
[DISCIPLINE GUARD] ログ: $LOG
EOF

if [ "$count" -ge 2 ]; then
  echo "[DISCIPLINE GUARD] HARD BLOCK: 同カテゴリ3回目・物理ブロック発動" >&2
  exit 2
fi

exit 0
