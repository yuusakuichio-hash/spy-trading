#!/bin/bash
# Hook 3: time_estimate_sanity.sh
# UserPromptSubmit hook: 「2時間以上」見積もり検知 → stdout に警告追加（BLOCK しない）
# hook 仕様: stdout に text を出力すると user prompt に追記される

set -uo pipefail

INPUT_JSON=$(cat)

# tool_input から prompt/content を抽出
CONTENT=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    parts = []
    for k in ['prompt', 'content', 'description', 'new_string']:
        v = ti.get(k, '') if isinstance(ti, dict) else ''
        if isinstance(v, str): parts.append(v)
    # also check direct message
    msg = d.get('message', '')
    if isinstance(msg, str): parts.append(msg)
    print(' '.join(parts)[:3000])
except:
    pass
" 2>/dev/null || echo "")

if [[ -z "$CONTENT" ]]; then
    exit 0
fi

# 数値 >= 2 の「時間」「hours」「h」見積もりパターン抽出
ESTIMATE_MATCH=$(echo "$CONTENT" | python3 -c "
import sys, re
text = sys.stdin.read()
# patterns: '2-3時間' '4時間' '2h' '3hours' '半日' etc
patterns = [
    r'(\d+)\s*[-~〜]\s*(\d+)\s*(時間|hours?|h(?=\s|$|\b))',
    r'(\d+)\s*(時間|hours?)',
    r'半日',
]
matches = []
for pat in patterns:
    for m in re.finditer(pat, text, re.IGNORECASE):
        g = m.groups()
        nums = [int(x) for x in g if x and x.isdigit()]
        if any(n >= 2 for n in nums) or m.group(0) == '半日':
            matches.append(m.group(0))
if matches:
    print(' / '.join(set(matches)))
" 2>/dev/null || echo "")

if [[ -n "$ESTIMATE_MATCH" ]]; then
    LOG=/Users/yuusakuichio/trading/data/logs/time_estimate_sanity.log
    mkdir -p "$(dirname "$LOG")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] TIME ESTIMATE WARN: $ESTIMATE_MATCH" >> "$LOG"

    # stdout出力: UserPromptSubmit hook では stdout がプロンプトに追記される
    echo ""
    echo "[TIME ESTIMATE SANITY] \"${ESTIMATE_MATCH}\"の見積もりが出現"
    echo "→ builder前提なら数分〜数十分のはず。見直してください。"
    echo "  参照: feedback_builder_time_estimate_minutes.md"
fi

exit 0
