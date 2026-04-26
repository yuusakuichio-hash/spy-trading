#!/usr/bin/env bash
# inject_recent_corrections.sh
# SessionStart hook: recent_corrections.md を読み込んでcontext injectionする
# Claude Code SessionStart hookから呼び出される
# stdin: {"session_id": "...", "cwd": "..."}
# stdout: 何も出力しない (context injectionはstderrで行う)

set -euo pipefail

CORRECTIONS_FILE="/Users/yuusakuichio/trading/data/recent_corrections.md"
GENERATOR="/Users/yuusakuichio/trading/.claude/hooks/generate_recent_corrections.py"

# recent_corrections.md が古い（24時間以上）か存在しない場合は再生成
if [ ! -f "$CORRECTIONS_FILE" ] || [ -n "$(find "$CORRECTIONS_FILE" -mtime +1 2>/dev/null)" ]; then
  python3 "$GENERATOR" 2>/dev/null || true
fi

# ファイルが存在すれば内容をstderrに注入
if [ -f "$CORRECTIONS_FILE" ]; then
  CONTENT=$(cat "$CORRECTIONS_FILE")
  printf "\n======================================================\n" >&2
  printf "[CONTEXT INJECTION] 直近の叱責・訂正履歴\n" >&2
  printf "======================================================\n" >&2
  printf "%s\n" "$CONTENT" >&2
  printf "======================================================\n" >&2
  printf "[重要] 上記と同じ違反パターンを繰り返すな。即自覚・即訂正。\n" >&2
  printf "======================================================\n\n" >&2
fi

exit 0
