#!/usr/bin/env bash
# SessionStart hook: 直近 N h の jsonl から user 発言と assistant 結論を抽出して注入
# 目的: 新セッションで前セッション文脈を自動引き継ぎ（ゆうさくさんが「覚えてる？」を言わなくて済む状態）

set -euo pipefail

PROJECT_JSONL_DIR="/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading"
SESSION_END_LOG_DIR="/Users/yuusakuichio/trading/data/session_end_log"
LOOKBACK_HOURS=24
MAX_USER_UTTERANCES=20
MAX_ASSISTANT_CLOSE=5
MAX_LOG_FILES=3
CONTENT_MAX_LEN=240
OUTPUT_MAX_LEN=200
MIN_USER_LEN=3

# macOS/Linux 両対応で「N時間前」の cutoff を作る
if date -v-1d >/dev/null 2>&1; then
  CUTOFF=$(date -v-${LOOKBACK_HOURS}H "+%Y-%m-%d %H:%M:%S")
else
  CUTOFF=$(date -d "${LOOKBACK_HOURS} hours ago" "+%Y-%m-%d %H:%M:%S")
fi

# 直近 N h 以内に更新された jsonl を配列で安全に取得（ファイル名にスペース含んでも壊れない）
JSONL_FILES=()
if [ -d "$PROJECT_JSONL_DIR" ]; then
  while IFS= read -r line; do
    [ -n "$line" ] && JSONL_FILES+=("$line")
  done < <(find "$PROJECT_JSONL_DIR" -maxdepth 1 -name "*.jsonl" -type f -newermt "$CUTOFF" 2>/dev/null | sort)
fi

HAS_LOGS=""
if [ -d "$SESSION_END_LOG_DIR" ] && [ -n "$(ls -A "$SESSION_END_LOG_DIR" 2>/dev/null)" ]; then
  HAS_LOGS=1
fi

# jsonl も session_end_log も無ければ何も出さない（新規環境配慮）
if [ ${#JSONL_FILES[@]} -eq 0 ] && [ -z "$HAS_LOGS" ]; then
  exit 0
fi

echo ""
echo "=== session_bridge: 直近 ${LOOKBACK_HOURS}h 引き継ぎ（自動注入） ==="
echo ""

# UTC→JST 変換 ヘルパ（stdin: ISO timestamp / stdout: MM/DD HH:MM）
iso_to_jst() {
  local ts="$1"
  local epoch
  epoch=$(TZ=UTC date -j -f "%Y-%m-%dT%H:%M:%S" "${ts%.*}" "+%s" 2>/dev/null || echo "")
  if [ -n "$epoch" ]; then
    date -r "$epoch" "+%m/%d %H:%M"
  else
    echo "$ts"
  fi
}

# Part 1: user 発言
if [ ${#JSONL_FILES[@]} -gt 0 ]; then
  echo "## ゆうさくさん 直近発言（時系列・最大 ${MAX_USER_UTTERANCES} 件）"
  echo ""

  TMP_FILE=$(mktemp)
  CLOSE_TMP=$(mktemp)
  trap 'rm -f "$TMP_FILE" "$CLOSE_TMP"' EXIT

  for f in "${JSONL_FILES[@]}"; do
    jq -r --argjson min_len "$MIN_USER_LEN" --argjson max_len "$CONTENT_MAX_LEN" '
      select(.type=="user")
      | select(.message.content | type == "string")
      | select(.message.content | test("<system-reminder>|Stop hook feedback|PreToolUse|tool_result|<command-name>|<local-command|<task-notification>|MEMORY_RELOAD"; "i") | not)
      | select(.message.content | length > $min_len)
      | [.timestamp, (.message.content[0:$max_len])]
      | @tsv
    ' "$f" 2>/dev/null >> "$TMP_FILE" || true
  done

  sort "$TMP_FILE" | tail -${MAX_USER_UTTERANCES} | while IFS=$'\t' read -r ts content; do
    jst=$(iso_to_jst "$ts")
    content_clean=$(printf "%s" "$content" | tr '\n' ' ' | cut -c1-${OUTPUT_MAX_LEN})
    echo "- [${jst}] ${content_clean}"
  done
  echo ""

  # Part 2: 各 jsonl の最後の assistant text（結論的発言）— jq 1 pass に簡素化
  echo "## 直近セッションの末尾 assistant 発言（最大 ${MAX_ASSISTANT_CLOSE} 件）"
  echo ""

  for f in "${JSONL_FILES[@]}"; do
    # timestamp と text を TSV で同時に取得（1 pass）
    LAST_LINE=$(jq -r '
      select(.type == "assistant")
      | select(.message.content | type == "array")
      | (.message.content | map(select(.type == "text")) | .[0].text // "") as $t
      | select($t != "")
      | [.timestamp, $t] | @tsv
    ' "$f" 2>/dev/null | tail -1 || true)

    if [ -n "$LAST_LINE" ]; then
      ts_last=$(printf "%s" "$LAST_LINE" | cut -f1)
      text_last=$(printf "%s" "$LAST_LINE" | cut -f2-)
      sid_short=$(basename "$f" .jsonl | cut -c1-8)
      text_clean=$(printf "%s" "$text_last" | tr '\n' ' ' | cut -c1-${CONTENT_MAX_LEN})
      printf "%s\t%s\t%s\n" "$ts_last" "$sid_short" "$text_clean" >> "$CLOSE_TMP"
    fi
  done

  sort "$CLOSE_TMP" | tail -${MAX_ASSISTANT_CLOSE} | while IFS=$'\t' read -r ts sid text; do
    jst=$(iso_to_jst "$ts")
    echo "- [${jst} / ${sid}] ${text}"
  done
  echo ""
fi

# Part 3: session_end_log 直近 N ファイル（配列で安全取得）
if [ -n "$HAS_LOGS" ]; then
  RECENT_LOGS=()
  while IFS= read -r line; do
    [ -n "$line" ] && RECENT_LOGS+=("$line")
  done < <(find "$SESSION_END_LOG_DIR" -maxdepth 1 -name "*.md" -type f -exec stat -f "%m %N" {} \; 2>/dev/null | sort -rn | head -${MAX_LOG_FILES} | cut -d' ' -f2-)

  if [ ${#RECENT_LOGS[@]} -gt 0 ]; then
    echo "## 直近 session_end_log 抜粋"
    echo ""
    for log in "${RECENT_LOGS[@]}"; do
      echo "### $(basename "$log")"
      head -25 "$log" 2>/dev/null || true
      echo ""
    done
  fi
fi

echo "=== session_bridge: 注入 完了 ==="
exit 0
