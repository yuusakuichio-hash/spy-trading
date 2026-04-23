#!/bin/bash
# legacy_write_block.sh — 既存コードへの書込みを物理 block
#
# 2026-04-22 全コード書き直し方針確定。既存レガシーコード（spy_bot.py 等）を
# 参照のみで保護する。Claude Code PreToolUse hook として Write/Edit をブロック。
#
# 許可パス:
#   atlas_v3/ chronos_v3/ common_v3/ tests/ scripts/ data/ .claude/ memory/
#
# ブロックパス:
#   spy_bot.py chronos_bot.py atlas_agent.py chronos_agent.py
#   common/ 配下（common_v3/ は許可）
#   root 直下の戦術系 .py ファイル
#
# Bypass: LEGACY_WRITE_BYPASS=1

set -u

# PreToolUse hook は stdin で JSON を受け取る
INPUT=$(cat)

# bypass
if [ "${LEGACY_WRITE_BYPASS:-}" = "1" ]; then
    exit 0
fi

# tool_name と file_path を抽出
TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null)

# Write / Edit / NotebookEdit だけチェック
case "$TOOL_NAME" in
    Write|Edit|NotebookEdit) ;;
    *) exit 0 ;;
esac

FILE_PATH=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    tool_input = d.get('tool_input', {})
    path = tool_input.get('file_path') or tool_input.get('path') or ''
    print(path)
except Exception:
    print('')
" 2>/dev/null)

# 空ならスキップ（別 hook に任せる）
if [ -z "$FILE_PATH" ]; then
    exit 0
fi

# trading ディレクトリ外は無関係
case "$FILE_PATH" in
    /Users/yuusakuichio/trading/*) ;;
    *) exit 0 ;;
esac

# 相対パス化
REL_PATH="${FILE_PATH#/Users/yuusakuichio/trading/}"

# 明示許可パス（優先判定）
case "$REL_PATH" in
    atlas_v3/*|chronos_v3/*|common_v3/*) exit 0 ;;
    tests/*|scripts/*|data/*|docs/*|strategies/*) exit 0 ;;
    .claude/*|.github/*) exit 0 ;;
    chronos_rules_plugin/*) exit 0 ;;
    automation/*) exit 0 ;;
    *.md|*.yaml|*.yml|*.json|*.txt|*.conf|*.lock|*.toml|*.ini|*.cfg) exit 0 ;;
    *.plist|Makefile|README|LICENSE*|.gitignore|.env*) exit 0 ;;
esac

# ブロック対象判定
BLOCK=0
REASON=""

case "$REL_PATH" in
    # 2026-04-22 例外: 今日新規作成した llm_budget.py は common_v3/ 移行までの暫定許可
    # （Redteam S3 対応・bypass 常用化を回避）
    common/llm_budget.py) ;;
    common/*)
        BLOCK=1
        REASON="common/ 配下は legacy 保護対象。新規実装は common_v3/ に作成"
        ;;
    spy_bot.py|chronos_bot.py|atlas_agent.py|chronos_agent.py|atlas_watchdog.py|chronos_watchdog.py)
        BLOCK=1
        REASON="$REL_PATH は legacy bot 本体。書換禁止・参照のみ"
        ;;
    strategy_selector.py|symbol_selector.py|chronos_strategy_selector.py|chronos_pre_trade_check.py|chronos_rule_simulator.py|chronos_accounts.yaml|atlas_rules.yaml|tradovate_client.py|gmail_monitor.py|sora_heartbeat_monitor.py)
        BLOCK=1
        REASON="$REL_PATH は legacy コア設定/コード。書換禁止"
        ;;
esac

if [ "$BLOCK" = "1" ]; then
    cat >&2 <<EOF
[LEGACY_WRITE_BLOCK] 書込みを物理 block:
  path: $FILE_PATH
  reason: $REASON

2026-04-22 全コード書き直し方針（feedback_bug_zero_absolute_20260422.md）により、
legacy コードは参照のみで保護されています。新規実装は以下に作成してください:
  - Atlas 側: atlas_v3/
  - Chronos 側: chronos_v3/
  - 共通コア: common_v3/

緊急解除が必要な場合: LEGACY_WRITE_BYPASS=1 でバイパス（ただし書込み理由を audit log に残すこと）
EOF
    exit 2
fi

exit 0
