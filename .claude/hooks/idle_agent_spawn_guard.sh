#!/bin/bash
# idle_agent_spawn_guard.sh — Stop hook
#
# 2026-04-25 ゆうさくさん指摘「誰も動いてない」の再発防止 (閾値厳格化 v2):
# stop_require_next_action.sh は tool_use 数 = 0 でのみ発動するため
# tool_use あり (TaskUpdate/Bash) + agent spawn ゼロ + pending task > 0 の
# 組合せを検出できなかった (「停止顕在化 33 分 gap」事象)。
#
# 本 hook: 直近 assistant turn で Agent 投入ゼロ + 以下いずれか TRUE で warn 注入:
#   [v1] ledger pending > 5 件
#   [v1] git 未 commit > 10 件
#
# v2 追加 (2026-04-25):
#   - pending > 3 件 (5 → 3 に厳格化)
#   - uncommitted > 5 件 (10 → 5 に厳格化)
#   - 10 分継続停滞で Pushover P0 送信
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
AGENT_SPAWN_COUNT=$(python3 << INNERPY
import json
from pathlib import Path

p = Path("$TRANSCRIPT")
if not p.exists():
    print(0)
    exit()

lines = p.read_text(encoding='utf-8').splitlines()
count = 0
in_assistant = False
for line in reversed(lines):
    try:
        rec = json.loads(line)
    except Exception:
        continue
    typ = rec.get('type', '')
    if typ == 'user':
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
INNERPY
)

if [ "$AGENT_SPAWN_COUNT" -gt 0 ]; then
    exit 0
fi

# pending 状況
PENDING_INSTR=$(grep -c '"status": "pending"' /Users/yuusakuichio/trading/data/user_instruction_ledger.jsonl 2>/dev/null || echo 0)
UNCOMMITTED=$(git status --short 2>/dev/null | wc -l | tr -d ' ')

SHOULD_WARN=0
SHOULD_P0=0
REASONS=""

# v2: 閾値厳格化 pending>3 / uncommitted>5
if [ "$PENDING_INSTR" -gt 3 ]; then
    SHOULD_WARN=1
    REASONS="instruction ledger pending=$PENDING_INSTR (threshold=3)"
fi
if [ "$UNCOMMITTED" -gt 5 ]; then
    SHOULD_WARN=1
    if [ -n "$REASONS" ]; then
        REASONS="$REASONS | uncommitted files=$UNCOMMITTED (threshold=5)"
    else
        REASONS="uncommitted files=$UNCOMMITTED (threshold=5)"
    fi
fi

# 10 min 停滞 Pushover P0
GUARD_STATE="/Users/yuusakuichio/trading/data/logs/idle_agent_guard_state.json"
NOW_EPOCH=$(date +%s)

if [ "$SHOULD_WARN" -eq 1 ]; then
    if [ -f "$GUARD_STATE" ]; then
        LAST_WARN=$(python3 -c "
import json
try:
    d = json.load(open('$GUARD_STATE'))
    print(d.get('last_warn_epoch', 0))
except:
    print(0)
" 2>/dev/null || echo 0)
        ELAPSED=$(( NOW_EPOCH - LAST_WARN ))
        if [ "$ELAPSED" -ge 600 ]; then
            SHOULD_P0=1
        fi
    fi

    mkdir -p "$(dirname "$GUARD_STATE")"
    python3 -c "
import json, time
state = {}
try:
    state = json.load(open('$GUARD_STATE'))
except:
    pass
if 'last_warn_epoch' not in state:
    state['last_warn_epoch'] = int(time.time())
state['last_check_epoch'] = int(time.time())
state['pending_count'] = $PENDING_INSTR
state['uncommitted_count'] = $UNCOMMITTED
json.dump(state, open('$GUARD_STATE', 'w'))
" 2>/dev/null
fi

if [ "$SHOULD_WARN" -eq 0 ]; then
    if [ -f "$GUARD_STATE" ]; then
        python3 -c "
import json
try:
    state = json.load(open('$GUARD_STATE'))
    state.pop('last_warn_epoch', None)
    json.dump(state, open('$GUARD_STATE', 'w'))
except:
    pass
" 2>/dev/null
    fi
    exit 0
fi

if [ "$SHOULD_P0" -eq 1 ]; then
    python3 -c "
import sys
sys.path.insert(0, '/Users/yuusakuichio/trading')
try:
    from common.pushover_client import send_pushover
    send_pushover(
        title='[P0] 停滞 10min 超: agent spawn ゼロ',
        message='idle_agent_spawn_guard: pending=$PENDING_INSTR uncommitted=$UNCOMMITTED. 即対処必要。',
        priority=1,
    )
except Exception as e:
    sys.stderr.write(f'pushover send failed: {e}\n')
" 2>/dev/null || true
fi

echo "[IDLE_AGENT_SPAWN_GUARD v2] 今 turn で Agent 投入ゼロ・停滞顕在化の可能性あり。"
echo "  reasons: $REASONS"
echo "  次 turn では以下いずれか必須:"
echo "    (A) Agent 並列投入 (pending task 消化)"
echo "    (B) 直接実装 (Write/Edit で pending 解消)"
echo "    (C) 明示的な一時停止宣言 (ゆうさく承認付きで IDLE_AGENT_SPAWN_BYPASS=1 export)"
echo "  参考: memory/feedback_declaration_execution_unified_20260424.md"
echo "  [Pushover P0 送信: $SHOULD_P0]"

exit 0
