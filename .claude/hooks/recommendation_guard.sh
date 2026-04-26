#!/bin/bash
# recommendation_guard.sh
# 結論/推奨語句を事前調査なしで出させない物理強制hook
# 2026-04-21 導入（ゆうさくさん指摘の繰り返しパターン対策）
#
# 検出対象: PreToolUse on Agent tool calls
# 検出語句: 推奨・最適・べき・不可・破綻・確定・一択等
# 除外: 疑問形・引用内・「不可能」単体
# 事前調査証拠: data/research/*.md 直近30分 / data/premortem_reports/ / prompt内ボトルネック等の語句

set -u

TOOL_NAME="${CLAUDE_TOOL_NAME:-}"
TOOL_INPUT="${CLAUDE_TOOL_INPUT:-}"

# Bypass
if [ "${RECOMMENDATION_GUARD_BYPASS:-0}" = "1" ]; then
  echo "[RECOMMENDATION_GUARD] BYPASS mode active" >&2
  exit 0
fi

# Agent tool以外はskip
[ "$TOOL_NAME" != "Agent" ] && exit 0

# 結論語句パターン（除外: 「不可能」「なるべき」等の他意味）
CONCLUSION_PATTERN='(第[0-9一二三]推奨|最適解|最優先|するべき|すべき|やるべき|破綻|一択|絶対に必要|決定しました|決まり|必ず必要|唯一の|絶対NG|絶対OK)'

# 引用内・疑問形を除いて検索
CLEANED=$(echo "$TOOL_INPUT" | sed 's/"[^"]*"//g' | grep -vE '(推奨|最適|べき|不可|破綻|確定|一択).*[？?]' || true)

HAS_CONCLUSION=0
if echo "$CLEANED" | grep -qE "$CONCLUSION_PATTERN"; then
  HAS_CONCLUSION=1
fi

if [ "$HAS_CONCLUSION" -eq 0 ]; then
  exit 0
fi

# 結論語句あり → 事前調査証拠チェック
HAS_EVIDENCE=0
EVIDENCE_TYPE=""

# data/research/ 直近30分
RESEARCH_COUNT=$(find /Users/yuusakuichio/trading/data/research -name "*.md" -mmin -30 2>/dev/null | wc -l | tr -d ' ')
if [ "$RESEARCH_COUNT" -gt 0 ]; then
  HAS_EVIDENCE=1
  EVIDENCE_TYPE="research_${RESEARCH_COUNT}"
fi

# data/premortem_reports/ 直近30分
PREMORTEM_COUNT=$(find /Users/yuusakuichio/trading/data/premortem_reports -name "*.md" -mmin -30 2>/dev/null | wc -l | tr -d ' ')
if [ "$PREMORTEM_COUNT" -gt 0 ]; then
  HAS_EVIDENCE=1
  EVIDENCE_TYPE="${EVIDENCE_TYPE}+premortem_${PREMORTEM_COUNT}"
fi

# プロンプト内のボトルネック/制約/リスク等キーワード
if echo "$TOOL_INPUT" | grep -qE "ボトルネック|制約|リスク|検証|確認済み|調査済|根拠|ソース|URL|引用"; then
  HAS_EVIDENCE=1
  EVIDENCE_TYPE="${EVIDENCE_TYPE}+keywords"
fi

# ログ
LOG_DIR="/Users/yuusakuichio/trading/data/logs"
mkdir -p "$LOG_DIR"

if [ "$HAS_EVIDENCE" -eq 0 ]; then
  cat >&2 <<'EOF'
[RECOMMENDATION_GUARD] HARD BLOCK: 結論/推奨語句検出・事前調査証拠なし

結論語句（推奨・最適・べき・破綻・一択 等）をAgent promptに含める時は、
以下のいずれかを満たす必要があります：

  1. data/research/*.md に直近30分以内の更新
  2. data/premortem_reports/*.md に直近30分以内のファイル
  3. prompt内に「ボトルネック」「制約」「リスク」「検証」
     「確認済み」「根拠」「ソース」「URL」のいずれかを含む

ゆうさくさんから度々指摘された「推奨を軽く出す」「ボトルネック洗い出し前に結論」
パターンの物理強制防止です。

緊急bypass: RECOMMENDATION_GUARD_BYPASS=1 (証跡残る)
参照: feedback_recommendation_physical_enforcement.md
EOF
  echo "$(date '+%Y-%m-%d %H:%M:%S') | BLOCKED | no_evidence | $(echo "$TOOL_INPUT" | head -c 300 | tr '\n' ' ')" >> "$LOG_DIR/recommendation_violations.log"
  exit 2
fi

# 通過ログ（メトリクス用）
echo "$(date '+%Y-%m-%d %H:%M:%S') | PASSED | ${EVIDENCE_TYPE}" >> "$LOG_DIR/recommendation_passed.log"
exit 0
