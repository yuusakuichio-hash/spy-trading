#!/usr/bin/env bash
# prompt_reload_memory.sh
# UserPromptSubmit hook: ネガティブ感情語句検知時にMEMORY.md自動リロード指示を注入

set -euo pipefail

LOG_FILE="/Users/yuusakuichio/trading/data/logs/discipline_violations.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S JST")
MEMORY_PATH="/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory/MEMORY.md"

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('prompt',''))" 2>/dev/null || echo "")
PROMPT_LOWER=$(echo "$PROMPT" | tr '[:upper:]' '[:lower:]')

TRIGGER_FOUND=0
TRIGGER_DETAILS=""

# ネガティブフィードバック語句
declare -a NEG_PATTERNS=(
  "なぜ"
  "また"
  "毎回"
  "何度も"
  "繰り返す"
  "同じ違反"
  "前も言った"
  "言ったはず"
  "忘れた"
  "なんで"
  "どうして"
)

for pattern in "${NEG_PATTERNS[@]}"; do
  PATTERN_LOWER=$(echo "$pattern" | tr '[:upper:]' '[:lower:]')
  if echo "$PROMPT_LOWER" | grep -qF "$PATTERN_LOWER" 2>/dev/null; then
    TRIGGER_FOUND=1
    TRIGGER_DETAILS="${TRIGGER_DETAILS}[ネガ語句] \"${pattern}\"\n"
    break
  fi
done

if (( TRIGGER_FOUND == 1 )); then
  mkdir -p "$(dirname "$LOG_FILE")"
  {
    echo "=== MEMORY RELOAD TRIGGERED ==="
    echo "Timestamp: ${TIMESTAMP}"
    printf "Triggers:\n${TRIGGER_DETAILS}"
    echo "---"
  } >> "$LOG_FILE"

  # stdout に追加コンテキストを注入（UserPromptSubmit はstdoutにJSON出力で注入可能）
  MEMORY_CONTENT=""
  if [[ -f "$MEMORY_PATH" ]]; then
    MEMORY_CONTENT=$(cat "$MEMORY_PATH")
  fi

  python3 - << PYEOF
import json
memory_content = """${MEMORY_CONTENT}"""
injection = """
[SYSTEM: MEMORY RELOAD] ネガティブフィードバック検知。以下のMEMORY.mdを再確認してから応答すること。
MEMORY.md内容:
""" + memory_content
print(json.dumps({"additionalSystemPrompt": injection}))
PYEOF
fi

exit 0
