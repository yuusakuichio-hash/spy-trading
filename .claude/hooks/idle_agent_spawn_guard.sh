#!/bin/bash
# idle_agent_spawn_guard.sh — Stop hook
#
# 2026-04-25 ゆうさくさん指摘「誰も動いてない」の再発防止:
# stop_require_next_action.sh は tool_use 数 = 0 でのみ発動するため
# tool_use あり (TaskUpdate/Bash) + agent spawn ゼロ + pending task > 0 の
# 組合せを検出できなかった (「停止顕在化 33 分 gap」事象)。
#
# 本 hook: 直近 assistant turn で Agent 投入ゼロ + 以下いずれか TRUE で warn 注入:
#   - ledger pending > 5 件
#   - task list pending > 2 件
#   - git 未 commit > 10 件
#
# Bypass: IDLE_AGENT_SPAWN_BYPASS=1

set -u

if [ "${IDLE_AGENT_SPAWN_BYPASS:-0}" = "1" ]; then
    exit 0
fi

INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', ''))
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    exit 0
fi

# 直近 assistant turn で Agent 投入数をカウント
AGENT_SPAWN_COUNT=$(python3 << PYEOF
import json
from pathlib import Path

p = Path("$TRANSCRIPT")
if not p.exists():
    print(0)
    exit()

lines = p.read_text(encoding='utf-8').splitlines()
# 最新 assistant turn を逆スキャン
count = 0
in_assistant = False
for line in reversed(lines):
    try:
        rec = json.loads(line)
    except Exception:
        continue
    typ = rec.get('type', '')
    if typ == 'user':
        # 1 つ前の user turn で停止 (最新 assistant turn の range 確定)
        if in_assistant:
            break
        continue
    if typ != 'assistant':
        continue
    in_assistant = True
    msg = rec.get('message', {})
    content = msg.get('content', [])
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'tool_use':
                name = item.get('name', '')
                if name == 'Agent' or name.endswith('_agent'):
                    count += 1
print(count)
PYEOF
)

if [ "$AGENT_SPAWN_COUNT" -gt 0 ]; then
    # agent 投入してる → 停滞でない
    exit 0
fi

# pending 状況
PENDING_INSTR=$(grep -c '"status": "pending"' /Users/yuusakuichio/trading/data/user_instruction_ledger.jsonl 2>/dev/null || echo 0)
UNCOMMITTED=$(cd /Users/yuusakuichio/trading && git status --short 2>/dev/null | wc -l | tr -d ' ')

SHOULD_WARN=0
REASONS=()

if [ "$PENDING_INSTR" -gt 5 ]; then
    SHOULD_WARN=1
    REASONS+=("instruction ledger pending=$PENDING_INSTR")
fi
if [ "$UNCOMMITTED" -gt 10 ]; then
    SHOULD_WARN=1
    REASONS+=("uncommitted files=$UNCOMMITTED")
fi

if [ "$SHOULD_WARN" -eq 1 ]; then
    cat <<EOF
[IDLE_AGENT_SPAWN_GUARD] 今 turn で Agent 投入ゼロ・停滞顕在化の可能性あり。
  reasons: ${REASONS[@]}
  次 turn では以下いずれか必須:
    (A) Agent 並列投入 (pending task 消化)
    (B) 直接実装 (Write/Edit で pending 解消)
    (C) 明示的な一時停止宣言 (ゆうさく承認付きで IDLE_AGENT_SPAWN_BYPASS=1 export)
  参考: memory/feedback_declaration_execution_unified_20260424.md / feedback_execute_immediately.md
EOF
fi

exit 0
