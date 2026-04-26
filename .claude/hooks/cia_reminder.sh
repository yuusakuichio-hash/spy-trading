#!/bin/bash
# CIA (Change Impact Analysis) 芋づる式チェック強制 hook
# PreToolUse 発火 / Edit or Write 時に対象ファイルの影響箇所を自動注入
# 2026-04-24 ゆうさくさん指示「芋づる式チェックを物理強制」

# 抑制条件:
#   - 対象が Python file でない → スキップ
#   - 直近 120 秒以内に同一ファイルへの CIA を出した → スキップ（過剰通知防止）
#   - CIA_REMINDER_BYPASS=1 → スキップ

set -u

if [ "${CIA_REMINDER_BYPASS:-0}" = "1" ]; then
    exit 0
fi

INPUT_JSON=$(cat)

# Edit or Write 以外はスキップ
TOOL_NAME=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null)

case "$TOOL_NAME" in
    Edit|Write|NotebookEdit)
        ;;
    *)
        exit 0
        ;;
esac

# file_path 取得
FILE_PATH=$(echo "$INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', {})
    if isinstance(ti, dict):
        print(ti.get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null)

# Python ファイル限定
case "$FILE_PATH" in
    *.py)
        ;;
    *)
        exit 0
        ;;
esac

# プロジェクト外スキップ
case "$FILE_PATH" in
    /Users/yuusakuichio/trading/*)
        ;;
    *)
        exit 0
        ;;
esac

PROJ="/Users/yuusakuichio/trading"
CACHE_DIR="/tmp/cia_reminder_cache"
mkdir -p "$CACHE_DIR"

# ファイル名を cache key にして 120 秒以内の重複を抑制
CACHE_KEY=$(echo "$FILE_PATH" | shasum | awk '{print $1}')
CACHE_FILE="$CACHE_DIR/$CACHE_KEY"

if [ -f "$CACHE_FILE" ]; then
    last=$(stat -f %m "$CACHE_FILE" 2>/dev/null)
    now=$(date +%s)
    diff=$((now - last))
    if [ "$diff" -lt 120 ]; then
        exit 0
    fi
fi
touch "$CACHE_FILE"

# CIA 実行（stdout に結果を流す・Claude context に注入される）
CIA_SCRIPT="$PROJ/scripts/impact_analysis.py"
if [ ! -x "$CIA_SCRIPT" ] && [ ! -f "$CIA_SCRIPT" ]; then
    exit 0
fi

REL_PATH="${FILE_PATH#$PROJ/}"

echo ""
echo "========== CIA 芋づる式チェック（自動） =========="
echo "対象: $REL_PATH を修正します"
echo ""
python3 "$CIA_SCRIPT" "$REL_PATH" 2>&1 | head -40
echo ""
echo "【規律】バグ修正の場合、上記影響箇所に *類似パターン* が無いか必ず確認すること。"
echo "  同じ書き方のバグが別の関数・別のファイルに居る可能性が高い（Defect Clustering）。"
echo "  単発修正で済ませず、芋づる式で全件潰す。"
echo "====================================================="
echo ""

exit 0
