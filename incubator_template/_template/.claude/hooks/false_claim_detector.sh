#!/bin/bash
# false_claim_detector.sh — 虚偽完了検知(ドメイン非依存版)
#
# Stop hook: 「完了」宣言時に pytest 実行証跡なければ警告ログ
# Pushover 設定済みなら通知(未設定なら通知 skip)
# block はしない(警告のみ・exit 0)

set -uo pipefail

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
LOG="$PROJECT_DIR/data/logs/false_claim_detected.log"
mkdir -p "$(dirname "$LOG")"

# .env から Pushover 認証情報 load(任意)
if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

INPUT_JSON=$(cat)

TRANSCRIPT_PATH=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '')
except:
    print('')
" 2>/dev/null || echo "")

if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
    exit 0
fi

RESULT=$(python3 - "$TRANSCRIPT_PATH" 2>/dev/null << 'PYEOF2'
import json, os, sys, re

transcript_path = sys.argv[1] if len(sys.argv) > 1 else ''
if not transcript_path or not os.path.exists(transcript_path):
    print('NO_TRANSCRIPT')
    sys.exit(0)

with open(transcript_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

recent = lines[-100:]
assistant_texts = []
bash_commands = []
tool_outputs = []

for line in recent:
    line = line.strip()
    if not line:
        continue
    try:
        d = json.loads(line)
    except:
        continue
    if d.get('type') == 'assistant':
        content = d.get('message', {}).get('content', [])
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'text':
                assistant_texts.append(block.get('text', ''))
            elif block.get('type') == 'tool_use' and block.get('name') == 'Bash':
                cmd = block.get('input', {}).get('command', '')
                if cmd:
                    bash_commands.append(cmd)
    if d.get('type') == 'user':
        tr = d.get('toolUseResult')
        if isinstance(tr, dict) and 'stdout' in tr:
            stdout = tr.get('stdout', '')
            if stdout:
                tool_outputs.append(stdout)

completion_claim = False
completion_patterns = [
    r'全合格', r'all\s+pass(?:ed)?', r'DONE', r'完遂', r'完了',
    r'全.*PASS', r'実装完了', r'修正完了', r'対応完了',
]
recent_assistant = [t for t in assistant_texts[-5:]]
for text in recent_assistant:
    for pat in completion_patterns:
        if re.search(pat, text, re.IGNORECASE):
            completion_claim = True
            break

if not completion_claim:
    print('NO_CLAIM')
    sys.exit(0)

pytest_found = False
for cmd in bash_commands:
    if re.search(r'\bpytest\b', cmd):
        pytest_found = True
        break
for out in tool_outputs:
    if re.search(r'\bpassed\b|\bfailed\b|\berror\b', out, re.IGNORECASE) and re.search(r'\d+\s+(?:passed|failed)', out):
        pytest_found = True
        break

if completion_claim and not pytest_found:
    print('WARN')
else:
    print('OK')
PYEOF2
)

if echo "$RESULT" | grep -q '^WARN$'; then
    TS=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$TS] FALSE CLAIM DETECTED: 完了宣言あり・pytest証跡なし" >> "$LOG"
    echo "  transcript: $TRANSCRIPT_PATH" >> "$LOG"

    echo "[FALSE CLAIM DETECTOR] 完了宣言を検知したがpytest実行証跡なし" >&2
    echo "→ 証拠なし完了宣言は虚偽完了パターン。全体pytest実行後に宣言してください。" >&2
    echo "  ログ: $LOG" >&2

    # Pushover 通知(認証情報設定済みのみ)
    if [ -n "${PUSHOVER_TOKEN:-}" ] && [ -n "${PUSHOVER_USER:-}" ]; then
        curl -s \
            --form-string "token=${PUSHOVER_TOKEN}" \
            --form-string "user=${PUSHOVER_USER}" \
            --form-string "title=[SYS/ALERT] 虚偽完了パターン検知" \
            --form-string "message=[FALSE CLAIM DETECTOR] 完了宣言あり・pytest証跡なし。全体テスト実行を確認してください。" \
            --form-string "priority=0" \
            "https://api.pushover.net/1/messages.json" \
            > /dev/null 2>&1 || true
    fi
fi

exit 0
