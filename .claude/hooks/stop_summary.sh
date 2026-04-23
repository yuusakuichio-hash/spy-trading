#!/usr/bin/env bash
# stop_summary.sh
# Stop hook: セッション終了時に違反サマリをログ出力

set -euo pipefail

LOG_FILE="/Users/yuusakuichio/trading/data/logs/discipline_violations.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S JST")

# 今日の違反カウント
TODAY=$(date "+%Y-%m-%d")
TODAY_VIOLATIONS=0
if [[ -f "$LOG_FILE" ]]; then
  TODAY_VIOLATIONS=$(grep -c "$TODAY" "$LOG_FILE" 2>/dev/null || echo "0")
fi

if (( TODAY_VIOLATIONS > 0 )); then
  printf "\n[DISCIPLINE GUARD / SESSION END] %s に %d 件の違反が記録されました。\n" "$TODAY" "$TODAY_VIOLATIONS" >&2
  printf "[DISCIPLINE GUARD] 詳細: %s\n" "$LOG_FILE" >&2

  mkdir -p "$(dirname "$LOG_FILE")"
  {
    echo "=== SESSION END SUMMARY ==="
    echo "Timestamp: ${TIMESTAMP}"
    echo "Today violations: ${TODAY_VIOLATIONS}"
    echo "==="
  } >> "$LOG_FILE"
fi

exit 0
