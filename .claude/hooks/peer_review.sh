#!/usr/bin/env bash
# peer_review.sh - Stop hook: Haiku peer review of assistant responses
# TACL 2024 degeneration-of-thought countermeasure
# Inputs via stdin: {"hook_event_name":"Stop","session_id":"...","transcript_path":"..."}

set -uo pipefail

REVIEW_LOG="/Users/yuusakuichio/trading/data/logs/peer_review.log"
STREAK_FILE="/Users/yuusakuichio/trading/data/logs/peer_review_streak.txt"
PUSHOVER_TOKEN="a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER="u2cevk8nktib3sr148rw2hs78ecvux"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S JST")
SESSION_DATE=$(date "+%Y-%m-%d")

mkdir -p "$(dirname "$REVIEW_LOG")"

INPUT=$(cat)
TRANSCRIPT_PATH=""
SESSION_ID=""

if [[ -n "$INPUT" ]]; then
    TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '')
except:
    print('')
" 2>/dev/null || echo "")
    SESSION_ID=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('session_id', '') or '')
except:
    print('')
" 2>/dev/null || echo "")
fi

if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
    exit 0
fi

export TRANSCRIPT_PATH
LAST_ASSISTANT_TEXT=$(python3 -c "
import json, os
tp = os.environ.get('TRANSCRIPT_PATH', '')
last_text = ''
try:
    with open(tp, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get('type') == 'assistant':
                    blocks = e.get('message', {}).get('content', [])
                    texts = [b['text'] for b in blocks if isinstance(b, dict) and b.get('type') == 'text']
                    if texts:
                        last_text = '\n'.join(texts)
            except:
                continue
except:
    pass
if len(last_text) > 4000:
    last_text = last_text[:4000] + '...[truncated]'
print(last_text)
" 2>/dev/null || echo "")

TEXT_LEN=${#LAST_ASSISTANT_TEXT}
if [[ $TEXT_LEN -lt 50 ]]; then
    exit 0
fi

# Write review prompt to temp file via python (avoids shell string escaping issues)
PROMPT_FILE=$(mktemp /tmp/pr_XXXXXX.txt)
export LAST_ASSISTANT_TEXT PROMPT_FILE
python3 -c "
import os, json
text = os.environ.get('LAST_ASSISTANT_TEXT', '')
fname = os.environ.get('PROMPT_FILE', '/tmp/pr_fallback.txt')
checks = [
    '1. Procrastination: agent delays action when it can act now',
    '2. Unnecessary delegation: agent asks human to resolve something it should handle autonomously',
    '3. Symbol hardcoding: code handles only SPY with no generalization',
    '4. Memory-as-completion: memory save reported as task done, actual implementation absent',
    '5. Pushover omission: important task completed but Pushover notification not mentioned'
]
prompt = 'You are Sora Lab discipline checker. Analyze assistant response for rule violations.\n\nRules:\n' + chr(10).join(checks)
prompt += '\n\nAssistant response:\n' + text
prompt += '\n\nOutput JSON only:\n{\"violations\": [\"desc\"], \"clean\": true, \"severity\": \"none\"}'
with open(fname, 'w') as f:
    f.write(prompt)
"

REVIEW_RESULT=""
REVIEW_EXIT=0
REVIEW_RESULT=$(claude -p \
    --model claude-haiku-4-5 \
    --dangerously-skip-permissions \
    --no-session-persistence \
    "$(cat "$PROMPT_FILE")" \
    2>/dev/null
) || REVIEW_EXIT=$?

rm -f "$PROMPT_FILE"

if [[ $REVIEW_EXIT -ne 0 || -z "$REVIEW_RESULT" ]]; then
    exit 0
fi

REVIEW_JSON=$(echo "$REVIEW_RESULT" | python3 -c "
import sys, re, json
text = sys.stdin.read()
for m in re.findall(r'\{[^{}]*\}', text, re.DOTALL):
    try:
        d = json.loads(m)
        if 'violations' in d and 'clean' in d:
            print(json.dumps(d))
            break
    except:
        continue
" 2>/dev/null || echo "")

if [[ -z "$REVIEW_JSON" ]]; then
    exit 0
fi

IS_CLEAN=$(echo "$REVIEW_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('clean', True)).lower())" 2>/dev/null || echo "true")
VIOLATIONS_JSON=$(echo "$REVIEW_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('violations', [])))" 2>/dev/null || echo "[]")
SEVERITY=$(echo "$REVIEW_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('severity', 'none'))" 2>/dev/null || echo "none")
CLEAN_BOOL=$([[ "$IS_CLEAN" == "true" ]] && echo "true" || echo "false")

python3 -c "
import json, sys
clean_val = sys.argv[6].lower() == 'true'
e = {
    'timestamp': sys.argv[1],
    'date': sys.argv[2],
    'session_id': sys.argv[3],
    'clean': clean_val,
    'severity': sys.argv[4],
    'violations': json.loads(sys.argv[5]),
    'text_length': int(sys.argv[7])
}
print(json.dumps(e, ensure_ascii=False))
" "$TIMESTAMP" "$SESSION_DATE" "$SESSION_ID" "$SEVERITY" "$VIOLATIONS_JSON" "$IS_CLEAN" "$TEXT_LEN" >> "$REVIEW_LOG" 2>/dev/null || true

if [[ "$IS_CLEAN" == "true" ]]; then
    echo "0" > "$STREAK_FILE"
    exit 0
fi

printf "\n[PEER REVIEW / Haiku] violation detected severity=%s\n" "$SEVERITY" >&2
echo "$VIOLATIONS_JSON" | python3 -c "
import sys, json
for v in json.load(sys.stdin):
    print('  -', v)
" >&2 2>/dev/null || true

CURRENT_STREAK=1
if [[ -f "$STREAK_FILE" ]]; then
    RAW=$(cat "$STREAK_FILE" 2>/dev/null || echo "0")
    CURRENT_STREAK=$(( RAW + 1 )) 2>/dev/null || CURRENT_STREAK=1
fi
echo "$CURRENT_STREAK" > "$STREAK_FILE"
printf "[PEER REVIEW] consecutive violations: %d\n" "$CURRENT_STREAK" >&2

if [[ $CURRENT_STREAK -ge 3 ]]; then
    VSUMMARY=$(echo "$VIOLATIONS_JSON" | python3 -c "
import sys, json
items = json.load(sys.stdin)
print(' / '.join(items[:3]))
" 2>/dev/null || echo "see log")

    curl -s \
        --form-string "token=${PUSHOVER_TOKEN}" \
        --form-string "user=${PUSHOVER_USER}" \
        --form-string "title=[SYS/ALERT] 規律違反アラート" \
        --form-string "message=[SYS/ALERT] Sora Lab 連続${CURRENT_STREAK}回違反: ${VSUMMARY}" \
        --form-string "priority=1" \
        --form-string "retry=60" \
        --form-string "expire=3600" \
        "https://api.pushover.net/1/messages.json" \
        > /dev/null 2>&1 || true

    printf "[PEER REVIEW] Pushover priority=1 sent streak=%d\n" "$CURRENT_STREAK" >&2
fi

exit 0
