#!/bin/bash
# Blue Team自己採点バイアス検出hook
# builder Agentが「完了」と書いた瞬間にgrep/AST自動検証を要求する

set -u

# 環境変数からtool情報取得（Claude Code hook仕様）
TOOL_NAME="${CLAUDE_TOOL_NAME:-}"
TOOL_INPUT="${CLAUDE_TOOL_INPUT:-}"

# Agent tool以外はskip
[ "$TOOL_NAME" != "Agent" ] && exit 0

# builder以外はskip
if ! echo "$TOOL_INPUT" | grep -qE '"subagent_type"[^"]*"builder"'; then
  exit 0
fi

# promptに「完了」「complete」「全合格」「all pass」等の危険語句がある場合警告
if echo "$TOOL_INPUT" | grep -qE '(完了宣言|全合格|All.*pass|all.*pass|complete.*confirmed|全.*PASS)'; then
  cat >&2 <<'EOF'
[BLUE_TEAM_BIAS_DETECTOR] builder promptに完了宣言トリガ語句検出

ガバナンス規律 (.claude/agents/governance.md) により、builder自己採点は受け入れ不可。

以下の4点セット提出がない完了宣言は差し戻し対象：
  1. 実grep結果（変更がchronos_bot.py等に統合されているか）
  2. AST検証（参照変数・関数が実際に定義されているか）
  3. 実fixture整合（tests/redteam_fixtures/ との比較）
  4. mutation score（<50%は不合格）

完了判定は独立redteam採点後にsecretaryが行う（三権分立）。
builderに自己完了宣言を求めていないか、prompt再確認。

参照: feedback_false_completion_5th_governance.md
EOF
fi

# 5度目虚偽完了パターン過去メモリ提示
echo "[BLUE_TEAM_BIAS_DETECTOR] 過去5回の虚偽完了事案: F12/F13 / agent-watchdog / auto_resume Guard / Phase A-C前回 / Phase A-C/D今回" >&2

exit 0
