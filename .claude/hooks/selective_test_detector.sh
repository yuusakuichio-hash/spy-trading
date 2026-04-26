#!/bin/bash
# Hook 1: selective_test_detector.sh
# UserPromptSubmit hook: 「全合格」宣言 + selective pytest実行 の組み合わせを HARD BLOCK
# exit 2 = HARD BLOCK

set -uo pipefail

INPUT_JSON=$(cat)

# transcript_path を stdin JSON から取得
TRANSCRIPT_PATH=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '')
except:
    print('')
" 2>/dev/null || echo "")

if [[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]]; then
    # Redteam r3 CRITICAL-1: scope 判定不能のため fail-open
    # (claim_ledger_guard / discipline_guard 等で別途検出される)
    LOG=/Users/yuusakuichio/trading/data/logs/selective_test_violations.log
    mkdir -p "$(dirname "$LOG")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] NO-TRANSCRIPT fail-open (scope undetermined)" >> "$LOG"
    exit 0
    # ============ dead code (reference only) ============
    CONTENT=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    parts = []
    for k in ['prompt', 'content', 'new_string']:
        v = ti.get(k, '') if isinstance(ti, dict) else ''
        if isinstance(v, str): parts.append(v)
    print(' '.join(parts))
except:
    pass
" 2>/dev/null || echo "")

    if [[ -z "$CONTENT" ]]; then
        exit 0
    fi

    HAS_CLAIM=$(echo "$CONTENT" | grep -cE '全合格|全.*PASS|all.*pass|All.*passed|全テスト合格|全件pass' || true)
    HAS_SELECTIVE=$(echo "$CONTENT" | grep -cE 'pytest tests/test_[a-zA-Z_]+\.py' || true)

    if [[ "$HAS_CLAIM" -gt 0 && "$HAS_SELECTIVE" -gt 0 ]]; then
        LOG=/Users/yuusakuichio/trading/data/logs/selective_test_violations.log
        mkdir -p "$(dirname "$LOG")"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] SELECTIVE TEST BLOCK (no-transcript mode)" >> "$LOG"
        echo "[SELECTIVE TEST GUARD] 選択的テスト実行で「全合格」宣言は虚偽完了パターン" >&2
        echo "→ 全体pytest実行を完了してから完了宣言してください" >&2
        echo "  正: pytest tests/ -v" >&2
        echo "  誤: pytest tests/test_specific.py -v → 「全合格」宣言" >&2
        echo "参照: feedback_false_completion_5th_governance.md / feedback_no_selective_testing.md" >&2
        exit 2
    fi
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

# Redteam r3 CRITICAL-2 対応: 境界探索は全範囲（最大 10000 行・保護上限）で実施。
# 旧: recent=lines[-200:] で切った後に境界を探す → 境界が 200 行より前だと退化。
# 新: 全範囲で境界探索 → 境界後の events のみを scope に。
_all_parsed = []
for _ln in lines[-10000:]:
    _ln = _ln.strip()
    if not _ln:
        continue
    try:
        _all_parsed.append(json.loads(_ln))
    except:
        continue

_last_u = -1
# 案 E' (Redteam r2): raw user event そのものを境界。user 発言の「内容」は判定しない。
# tool_result のみの user event は assistant turn の連続性保持のため境界にしない。
# Redteam r3 CRITICAL-3: toolUseResult は「キー存在」で判定（schema 変更耐性）。
for _i in range(len(_all_parsed) - 1, -1, -1):
    _o = _all_parsed[_i]
    if _o.get('type') != 'user':
        continue
    if 'toolUseResult' in _o:
        continue
    _m = _o.get('message') or {}
    _c = _m.get('content', '')
    if isinstance(_c, list):
        _is_only_tool_result = bool(_c) and all(
            isinstance(_x, dict) and _x.get('type') == 'tool_result'
            for _x in _c
        )
        if _is_only_tool_result:
            continue
    _last_u = _i
    break

if _last_u >= 0:
    recent = [json.dumps(_p) + '\n' for _p in _all_parsed[_last_u + 1:]]
else:
    recent = lines[-200:]

bash_commands = []
assistant_texts = []
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

selective_found = False
full_found = False

for cmd in bash_commands:
    for line_c in cmd.splitlines():
        line_c = line_c.strip()
        if not re.search(r'\bpytest\b', line_c):
            continue
        if re.search(r'pytest\s+tests/\s*($|-)|pytest\s*(-[a-zA-Z]|$)|pytest\s+\.\s*($|-)', line_c):
            full_found = True
        elif re.search(r'pytest\s+tests/test_\w+\.py', line_c):
            selective_found = True

all_pass_claim = False
patterns = [
    r'全件?合格', r'全.*テスト.*(?:pass|合格|PASS)', r'全.*PASS',
    r'all\s+(?:test[s]?\s+)?pass(?:ed)?',
    r'\d+\s+passed.*0\s+(?:failed|error)',
]
for text in assistant_texts + tool_outputs:
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            all_pass_claim = True
            break
    if all_pass_claim:
        break

if selective_found and not full_found and all_pass_claim:
    print('BLOCK')
else:
    print('PASS')
PYEOF2
)

if echo "$RESULT" | grep -q '^BLOCK$'; then
    LOG=/Users/yuusakuichio/trading/data/logs/selective_test_violations.log
    mkdir -p "$(dirname "$LOG")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SELECTIVE TEST BLOCK transcript=$TRANSCRIPT_PATH" >> "$LOG"
    echo "[SELECTIVE TEST GUARD] 選択的テスト実行で「全合格」宣言は虚偽完了パターン" >&2
    echo "→ 全体pytest実行を完了してから完了宣言してください" >&2
    echo "  正: pytest tests/ -v" >&2
    echo "  誤: pytest tests/test_specific.py -v → 「全合格」宣言" >&2
    echo "参照: feedback_false_completion_5th_governance.md / feedback_no_selective_testing.md" >&2
    exit 2
fi

exit 0
