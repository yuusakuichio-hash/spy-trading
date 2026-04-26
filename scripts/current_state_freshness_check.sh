#!/usr/bin/env bash
# scripts/current_state_freshness_check.sh
# SessionStart hook: CURRENT_STATE.md の鮮度チェック
# - data/research/*.md, data/*.md, memory/project_*.md に新ファイルがあれば警告
# exit 0 (セッション開始はブロックしない)
#
# settings.local.json への登録手順:
#   "SessionStart": [...既存..., {"hooks": [{"type": "command", "command": "/Users/yuusakuichio/trading/scripts/current_state_freshness_check.sh"}]}]

set -euo pipefail

TRADING_DIR="/Users/yuusakuichio/trading"
MEMORY_DIR="/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading/memory"
CURRENT_STATE="${TRADING_DIR}/CURRENT_STATE.md"

# --- CURRENT_STATE.md の last_updated を取得 ---
get_current_state_date() {
    if [[ ! -f "$CURRENT_STATE" ]]; then
        echo ""
        return
    fi
    local lu
    lu=$(grep -m1 '^last_updated:' "$CURRENT_STATE" 2>/dev/null | sed 's/last_updated:[[:space:]]*//' | tr -d '"' | tr -d "'" | xargs) || true
    echo "$lu"
}

# YYYY-MM-DD → epoch 秒 (macOS date)
date_to_epoch() {
    local d="$1"
    if [[ -z "$d" ]]; then
        echo "0"
        return
    fi
    date -j -f "%Y-%m-%d" "$d" "+%s" 2>/dev/null || echo "0"
}

file_mtime_epoch() {
    stat -f "%m" "$1" 2>/dev/null || echo "0"
}

file_mtime_date() {
    stat -f "%Sm" -t "%Y-%m-%d" "$1" 2>/dev/null || echo "unknown"
}

# --- 基準日時取得 ---
CS_DATE=$(get_current_state_date)
CS_EPOCH=$(date_to_epoch "$CS_DATE")

USE_MTIME_FALLBACK=false
if [[ -z "$CS_DATE" || "$CS_EPOCH" == "0" ]]; then
    USE_MTIME_FALLBACK=true
    if [[ -f "$CURRENT_STATE" ]]; then
        CS_EPOCH=$(file_mtime_epoch "$CURRENT_STATE")
        CS_DATE=$(file_mtime_date "$CURRENT_STATE")
    else
        CS_EPOCH=0
        CS_DATE="(missing)"
    fi
fi

# --- 新しいファイルを検索 ---
NEW_FILES=()

# 1. data/research/*.md
if [[ -d "${TRADING_DIR}/data/research" ]]; then
    while IFS= read -r -d '' f; do
        fepoch=$(file_mtime_epoch "$f")
        if [[ "$fepoch" -gt "$CS_EPOCH" ]]; then
            fdate=$(file_mtime_date "$f")
            NEW_FILES+=("  - data/research/$(basename "$f") (${fdate})")
        fi
    done < <(find "${TRADING_DIR}/data/research" -maxdepth 1 -name "*.md" -print0 2>/dev/null)
fi

# 2. data/*.md (トップレベルのみ・サブディレクトリ除外)
while IFS= read -r -d '' f; do
    fepoch=$(file_mtime_epoch "$f")
    if [[ "$fepoch" -gt "$CS_EPOCH" ]]; then
        fdate=$(file_mtime_date "$f")
        NEW_FILES+=("  - data/$(basename "$f") (${fdate})")
    fi
done < <(find "${TRADING_DIR}/data" -maxdepth 1 -name "*.md" -print0 2>/dev/null)

# 3. memory/project_*.md
if [[ -d "$MEMORY_DIR" ]]; then
    while IFS= read -r -d '' f; do
        fepoch=$(file_mtime_epoch "$f")
        if [[ "$fepoch" -gt "$CS_EPOCH" ]]; then
            fdate=$(file_mtime_date "$f")
            NEW_FILES+=("  - memory/$(basename "$f") (${fdate})")
        fi
    done < <(find "$MEMORY_DIR" -maxdepth 1 -name "project_*.md" -print0 2>/dev/null)
fi

COUNT=${#NEW_FILES[@]}

# --- 出力 ---
if [[ "$COUNT" -eq 0 ]]; then
    NOW_EPOCH=$(date +%s)
    AGE_DAYS=$(( (NOW_EPOCH - CS_EPOCH) / 86400 ))
    if [[ "$AGE_DAYS" -ge 1 && "$CS_EPOCH" -gt 0 ]]; then
        echo "[CURRENT_STATE FRESHNESS] last_updated: ${CS_DATE} (${AGE_DAYS}日経過) — 新ファイル未検出"
    fi
    exit 0
fi

# 警告レベル判定
if [[ "$COUNT" -ge 10 ]]; then
    HEADER="############################################################"
    LEVEL="STRONG WARNING (${COUNT}件 >= 10)"
elif [[ "$COUNT" -ge 5 ]]; then
    HEADER="============================================================"
    LEVEL="STRONG WARNING (${COUNT}件 >= 5)"
else
    HEADER="------------------------------------------------------------"
    LEVEL="WARNING (${COUNT}件)"
fi

FALLBACK_NOTE=""
if [[ "$USE_MTIME_FALLBACK" == "true" ]]; then
    FALLBACK_NOTE=" (mtime fallback)"
fi

echo ""
echo "$HEADER"
echo "[CURRENT_STATE FRESHNESS ${LEVEL}]"
echo "last_updated: ${CS_DATE}${FALLBACK_NOTE}"
echo "新しいファイル検出:"
for f in "${NEW_FILES[@]}"; do
    echo "$f"
done
echo "-> CURRENT_STATE.md に反映されていない可能性があります"
echo "-> セッション中に鮮度を確認してください"
echo "$HEADER"
echo ""

exit 0
