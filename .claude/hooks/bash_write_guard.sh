#!/bin/bash
# bash_write_guard.sh - CRIT-R6-1: Bash route legacy file write protection
# Protects spy_bot.py/chronos_bot.py/atlas_agent.py/common/ etc from Bash-routed writes
# (sed -i, perl -i, python open write, echo redirect, cat redirect, rsync, cp, mv, tee)
# Bypass: LEGACY_WRITE_BYPASS=1

set -u

INPUT=$(cat)

if [ "${LEGACY_WRITE_BYPASS:-}" = "1" ]; then
    exit 0
fi

TOOL_NAME=$(echo "$INPUT" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null)

if [ "$TOOL_NAME" != "Bash" ]; then
    exit 0
fi

COMMAND=$(echo "$INPUT" | python3 -c \
    "import json,sys; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null)

if [ -z "$COMMAND" ]; then
    exit 0
fi

RESULT=$(echo "$COMMAND" | python3 /Users/yuusakuichio/trading/.claude/hooks/_bash_write_guard_logic.py 2>/dev/null)

if [ "${RESULT:-OK}" = "BLOCK" ]; then
    echo "[BASH_WRITE_GUARD] CRIT-R6-1: Bash route write to protected file detected — BLOCKED" >&2
    echo "  command (first 300 chars): $(echo "$COMMAND" | head -c 300)" >&2
    echo "  New code must go in atlas_v3/ / chronos_v3/ / common_v3/" >&2
    echo "  Emergency bypass: LEGACY_WRITE_BYPASS=1" >&2
    exit 2
fi

exit 0
