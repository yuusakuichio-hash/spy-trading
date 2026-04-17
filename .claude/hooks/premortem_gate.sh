#!/usr/bin/env bash
# premortem_gate.sh - PreToolUse gate: require premortem for large Agent calls
# Sora Lab 2026-04-12
set -uo pipefail

REPORT_DIR="/Users/yuusakuichio/trading/data/premortem_reports"
LOG_FILE="/Users/yuusakuichio/trading/data/logs/premortem_gate.log"
PREMORTEM_PY="/Users/yuusakuichio/trading/scripts/premortem.py"
WINDOW_MIN="${PREMORTEM_WINDOW_MIN:-30}"
THRESHOLD="${PREMORTEM_PROMPT_THRESHOLD:-500}"
TS="$(date '+%Y-%m-%d %H:%M:%S JST')"

mkdir -p "$(dirname "$LOG_FILE")" "$REPORT_DIR"

INPUT="$(cat)"
if [[ -z "$INPUT" ]]; then
    exit 0
fi

PARSED="$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("_ 0"); sys.exit(0)
tn = d.get("tool_name", "") or "_"
ti = d.get("tool_input", {}) or {}
prompt = ""
if isinstance(ti, dict):
    prompt = ti.get("prompt") or ti.get("description") or ""
print(f"{tn} {len(prompt)}")
')"

TOOL_NAME="${PARSED%% *}"
PROMPT_LEN="${PARSED##* }"

case "$TOOL_NAME" in
    Task|Agent) ;;
    *) exit 0 ;;
esac

if ! [[ "$PROMPT_LEN" =~ ^[0-9]+$ ]]; then
    exit 0
fi
if (( PROMPT_LEN < THRESHOLD )); then
    exit 0
fi

if [[ "${PREMORTEM_BYPASS:-0}" == "1" ]]; then
    echo "[$TS] BYPASS tool=$TOOL_NAME prompt_len=$PROMPT_LEN" >>"$LOG_FILE"
    exit 0
fi

RECENT="$(find "$REPORT_DIR" -maxdepth 1 -type f -name '*.md' -mmin "-$WINDOW_MIN" 2>/dev/null | head -1)"

if [[ -n "$RECENT" ]]; then
    echo "[$TS] PASS tool=$TOOL_NAME prompt_len=$PROMPT_LEN recent=$RECENT" >>"$LOG_FILE"
    exit 0
fi

echo "[$TS] BLOCK tool=$TOOL_NAME prompt_len=$PROMPT_LEN no_recent_premortem" >>"$LOG_FILE"

{
    echo ""
    echo "[PREMORTEM GATE] Large task (${PROMPT_LEN} chars >= ${THRESHOLD}) detected,"
    echo "[PREMORTEM GATE] but no premortem report in ${REPORT_DIR} within last ${WINDOW_MIN} min."
    echo "[PREMORTEM GATE]"
    echo "[PREMORTEM GATE] Run Gary Klein premortem (HAZOP + ACH) before dispatching the Agent:"
    echo "[PREMORTEM GATE]   python3 ${PREMORTEM_PY} --task \"<task description>\" [--files a.py,b.py]"
    echo "[PREMORTEM GATE]"
    echo "[PREMORTEM GATE] To override temporarily: export PREMORTEM_BYPASS=1"
    echo "[PREMORTEM GATE] log: $LOG_FILE"
    echo ""
} >&2

exit 2
