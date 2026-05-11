#!/bin/bash
# pronoun_guard.sh — 一人称規律の物理ガード
#
# 「僕」「俺」検出で exit 2 block(引用内・コードブロック内は許可)
# ソラ(女性設定)として「私」で統一
#
# Bypass: PRONOUN_GUARD_BYPASS=1

set -u

if [ "${PRONOUN_GUARD_BYPASS:-}" = "1" ]; then
    exit 0
fi

INPUT=$(cat 2>/dev/null || true)

if [ -z "$INPUT" ]; then
    exit 0
fi

TEXT=""
if echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(type(d).__name__)" 2>/dev/null | grep -q "dict"; then
    TEXT=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    text = d.get('response', '') or d.get('text', '') or d.get('content', '') or d.get('message', '')
    if not text:
        text = json.dumps(d)
    print(text)
except Exception:
    print('')
" 2>/dev/null)
else
    TEXT="$INPUT"
fi

if [ -z "$TEXT" ]; then
    exit 0
fi

RESULT=$(PRONOUN_INPUT="$TEXT" python3 - <<'PYEOF'
import os, re
text = os.environ.get("PRONOUN_INPUT", "")

text_no_quote = re.sub(r'「[^」]*」', '', text)
text_no_quote = re.sub(r'```[\s\S]*?```', '', text_no_quote)
text_no_quote = re.sub(r'`[^`]*`', '', text_no_quote)
text_no_quote = re.sub(r'^>.*$', '', text_no_quote, flags=re.MULTILINE)
text_no_quote = re.sub(r'"[^"]*"', '', text_no_quote)

patterns = [
    (r'(?<![一-龥A-Za-z])僕', '僕'),
    (r'(?<![一-龥A-Za-z])俺', '俺'),
]
hits = []
for pattern, label in patterns:
    matches = list(re.finditer(pattern, text_no_quote))
    if matches:
        first = matches[0]
        start = max(0, first.start() - 30)
        end = min(len(text_no_quote), first.end() + 30)
        context = text_no_quote[start:end]
        hits.append(f'{label} ({len(matches)}回・例: "...{context}...")')

if hits:
    print('VIOLATION', '; '.join(hits))
else:
    print('OK')
PYEOF
)

if echo "$RESULT" | grep -q "^VIOLATION"; then
    cat >&2 <<EOF
[PRONOUN_GUARD] 一人称規律違反検出:
  $RESULT

ソラ(女性設定)として一人称は「私」で統一してください。
引用内(「」内・コードブロック内)は許可されています。

緊急 bypass: PRONOUN_GUARD_BYPASS=1
EOF
    exit 2
fi

exit 0
