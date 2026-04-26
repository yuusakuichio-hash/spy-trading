#!/usr/bin/env bash
# Stop hook: 現セッションの末尾 N 件 exchange を data/session_end_log/<date>.md に append
# 目的: 次セッションの session_bridge.sh が参照できる形でセッション末尾を保存
# CURRENT_STATE.md への自動書き込みは行わない（純度保持のため）

set -euo pipefail

PROJECT_JSONL_DIR="/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading"
SESSION_END_LOG_DIR="/Users/yuusakuichio/trading/data/session_end_log"
MAX_EXCHANGES=20
CONTENT_MAX_LEN=300
OUTPUT_MAX_LEN=250
MIN_USER_LEN=3

mkdir -p "$SESSION_END_LOG_DIR"
TODAY=$(date "+%Y-%m-%d")
OUT_FILE="$SESSION_END_LOG_DIR/${TODAY}.md"

# 最新更新された jsonl を現セッションとみなす（SC2012 回避: find + stat mtime で最大値を取る）
LATEST_JSONL=""
if [ -d "$PROJECT_JSONL_DIR" ]; then
  LATEST_JSONL=$(find "$PROJECT_JSONL_DIR" -maxdepth 1 -name "*.jsonl" -type f -exec stat -f "%m %N" {} \; 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2- || true)
fi

if [ -z "$LATEST_JSONL" ] || [ ! -f "$LATEST_JSONL" ]; then
  exit 0
fi

SESSION_ID=$(basename "$LATEST_JSONL" .jsonl)
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S JST")

# 冪等化: 同一 SESSION_ID を同一ファイルに重複追記しないため既存セクションを除去
# macOS awk は multibyte (UTF-8 日本語) 処理で towc エラーを起こすため Python で実装
if [ -f "$OUT_FILE" ] && grep -q "## Session $SESSION_ID (closed" "$OUT_FILE" 2>/dev/null; then
  python3 - "$SESSION_ID" "$OUT_FILE" <<'PY'
import sys, re, io

sid, path = sys.argv[1], sys.argv[2]

with io.open(path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# 除去対象: 「(任意の前置 --- 行) + ## Session <sid> (closed ...) + 以降、次の Session header もしくは EOF まで」
# 先頭 `---\n` はペアで除去、後続の `---\n## Session <他>` は保持
pattern = re.compile(
    r'(?:^|\n)(?:---\n)?## Session ' + re.escape(sid) + r' \(closed[^\n]*\n(?:(?!^## Session ).*\n?)*',
    re.MULTILINE
)
new_content = pattern.sub('\n', content)

# 末尾の空行を整理（単一改行で終わる）
new_content = new_content.rstrip() + '\n' if new_content.strip() else ''

# 連続する改行を 2 つ以内に（可読性）
new_content = re.sub(r'\n{3,}', '\n\n', new_content)

with io.open(path, 'w', encoding='utf-8') as f:
    f.write(new_content)
PY
fi

{
  echo ""
  echo "---"
  echo "## Session $SESSION_ID (closed $TIMESTAMP)"
  echo ""

  # user と assistant (text) を時系列で抽出 → 末尾 N 件
  jq -r --argjson min_len "$MIN_USER_LEN" --argjson max_len "$CONTENT_MAX_LEN" '
    if (.type == "user") and (.message.content | type == "string") then
      .message.content as $c
      | select($c | test("<system-reminder>|Stop hook feedback|PreToolUse|tool_result|<command-name>|<local-command|<task-notification>|MEMORY_RELOAD"; "i") | not)
      | select($c | length > $min_len)
      | [.timestamp, "USER", ($c[0:$max_len])]
      | @tsv
    elif (.type == "assistant") and (.message.content | type == "array") then
      (.message.content | map(select(.type == "text")) | .[0].text // "") as $t
      | select($t != "")
      | [.timestamp, "ASST", ($t[0:$max_len])]
      | @tsv
    else empty
    end
  ' "$LATEST_JSONL" 2>/dev/null \
    | tail -${MAX_EXCHANGES} \
    | while IFS=$'\t' read -r ts role content; do
        epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "${ts%.*}" "+%s" 2>/dev/null || echo "")
        if [ -n "$epoch" ]; then
          jst=$(date -r "$epoch" "+%m/%d %H:%M JST")
        else
          jst="${ts}"
        fi
        content_clean=$(printf "%s" "$content" | tr '\n' ' ' | cut -c1-${OUTPUT_MAX_LEN})
        echo "- **[${role}] ${jst}**: ${content_clean}"
      done
} >> "$OUT_FILE"

exit 0
