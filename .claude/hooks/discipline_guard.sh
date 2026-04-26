#!/bin/bash
# 規律違反リアルタイム検出hook
# 秘書の発話・tool_input内の禁止語句をPreToolUse/UserPromptSubmitで検出
# 参照: memory/feedback_*.md

set -u

# Fix 2: テスト環境フラグ — hook自身のテストや開発環境では記録せずexit 0
if [ "${DISCIPLINE_GUARD_TEST:-0}" = "1" ]; then
  exit 0
fi

# Fix (2026-04-21): stdin JSON から payload 抽出
# 旧: CLAUDE_TOOL_INPUT / CLAUDE_USER_PROMPT env var 参照 (→ Claude Code hook 仕様違反で dead hook)
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

LOG=/Users/yuusakuichio/trading/data/logs/discipline_violations.log
RELOAD_LOG=/Users/yuusakuichio/trading/data/logs/discipline_memory_reloads.log
mkdir -p "$(dirname "$LOG")"

# Fix 3: MEMORY_RELOAD_TRIGGERED は別ログへ分離（VIOLATION件数カウント精度向上）
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

# 規律説明文脈: rule context の payload は skip (hook自己参照回避)
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

# 先延ばし（feedback_no_schedule_delay.md / feedback_cycle_complete_today.md）
check '明日' '先延ばし' 'feedback_no_schedule_delay.md'
check '朝にする|朝に対応|朝チェック' '先延ばし' 'feedback_no_schedule_delay.md'
check '後日|週末に|クローズ後に|本番移行前に|別タスクで|来週|後ほど' '先延ばし' 'feedback_no_schedule_delay.md'
check '2-3日|2〜3日|3-7日|数日後|明日以降' '先延ばし' 'feedback_cycle_complete_today.md'

# 確認癖（feedback_no_confirmation_execute_now.md / feedback_recommend_means_execute.md）
check '進めていい？|GOなら|どれからやる？|大丈夫？進める|全部Yes？' '確認癖' 'feedback_no_confirmation_execute_now.md'

# 桁違い見積（feedback_builder_time_estimate_minutes.md）
check '[234]-?[356]h|[234]〜[356]時間|4-6時間|半日' '桁違い見積' 'feedback_builder_time_estimate_minutes.md'

# 虚偽完了（feedback_false_completion_5th_governance.md）
# "完了"単独ではなく証跡なし文脈でのみ警告（簡易判定）
if echo "$PAYLOAD" | grep -qE '全件?合格|全テストpass|全PASS|完了宣言' && ! echo "$PAYLOAD" | grep -qE 'grep.*result|AST.*verify|実grep|mutation.*score'; then
  violations+=("[虚偽完了] 証跡4点セットなし完了宣言 (see memory/feedback_false_completion_5th_governance.md)")
fi

# 保守設計違反（feedback_paper_aggressive_config.md）
if echo "$PAYLOAD" | grep -qE 'ペーパー.*保守|ペーパー.*PDT|paper.*conservative|ペーパー.*$25K'; then
  violations+=("[保守設計] ペーパー環境に保守/本番制約を適用 (see memory/feedback_paper_aggressive_config.md)")
fi

# 銘柄固定化（feedback_atlas_naming_discipline.md）
if echo "$PAYLOAD" | grep -qE 'SPY専用|SPY only|SPY限定'; then
  violations+=("[銘柄固定] Atlasはマルチ銘柄・SPY固定化禁止 (see memory/feedback_atlas_naming_discipline.md)")
fi

# 場中バグ早期Bot停止（feedback_market_hours_are_precious.md）
if echo "$PAYLOAD" | grep -qE 'Bot停止.*推奨|停止してから.*修正|クローズ.*待って'; then
  violations+=("[場中時間浪費] 場中修正サイクル回せ (see memory/feedback_market_hours_are_precious.md)")
fi

# メモリだけで完了扱い（feedback_cognitive_limit_design.md）
if echo "$PAYLOAD" | grep -qE 'メモリに保存.*対策|メモリ化.*解決|メモ.*刻印.*完了'; then
  violations+=("[認知限界放置] メモリだけで完了扱い禁止・hook/物理実装まで (see memory/feedback_cognitive_limit_design.md)")
fi

# 違反なしなら通過
[ ${#violations[@]} -eq 0 ] && exit 0

# 違反検知
ts=$(date '+%Y-%m-%d %H:%M:%S')
{
  echo "[$ts] discipline violations:"
  for v in "${violations[@]}"; do echo "  - $v"; done
  echo "  payload_head: $(echo "$PAYLOAD" | head -c 200 | tr '\n' ' ')"
  echo "---"
} >> "$LOG"

# stderr警告（2回目以降はHARD BLOCK）
registry=/Users/yuusakuichio/trading/data/logs/violation_registry.jsonl
mkdir -p "$(dirname "$registry")"
cat_key=$(printf '%s\n' "${violations[@]}" | head -1 | grep -oE '\[[^]]+\]' | head -1)
count=$(grep -c "$cat_key" "$registry" 2>/dev/null || echo 0)
echo "{\"ts\":\"$ts\",\"category\":\"$cat_key\",\"count\":$((count+1))}" >> "$registry"

cat >&2 <<EOF
[DISCIPLINE GUARD] 規律違反検知 (カテゴリ再発回数: $((count+1))):
$(printf '  %s\n' "${violations[@]}")
[DISCIPLINE GUARD] 過去違反累計 $count 件同カテゴリ。メモリは機能してない＝構造でブロック必要。
[DISCIPLINE GUARD] ログ: $LOG
EOF

# 3回目以降はexit 2でblock
if [ "$count" -ge 2 ]; then
  echo "[DISCIPLINE GUARD] HARD BLOCK: 同カテゴリ3回目・物理ブロック発動" >&2
  exit 2
fi

exit 0
