#!/bin/bash
# spec_premortem_required.sh — 仕様 premortem 強制 hook (P0-3)
#
# flow_audit C-03: 「仕様そのものの正しさを誰も検証していない」を物理化。
# 仕様書（data/specs/v3/*.md）への Write/Edit が premortem を経由しているか確認。
#
# 動作:
#   - data/specs/v3/*.md への Write/Edit を検出
#   - 直近 30 分以内に対応する premortem report があるか確認
#   - なければ exit 2 で block
#
# Bypass: SPEC_PREMORTEM_BYPASS=1

set -u

INPUT=$(cat)

if [ "${SPEC_PREMORTEM_BYPASS:-}" = "1" ]; then
    exit 0
fi

TOOL_NAME=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null)
case "$TOOL_NAME" in
    Write|Edit) ;;
    *) exit 0 ;;
esac

FILE_PATH=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    p = d.get('tool_input', {}).get('file_path', '')
    print(p)
except Exception:
    print('')
" 2>/dev/null)

# 仕様書 v3 への書き込みのみ対象
case "$FILE_PATH" in
    */data/specs/v3/*.md|*/data/specs/v3/*.yaml|*/data/specs/v3/*.json) ;;
    *) exit 0 ;;
esac

# 直近 30 分以内の premortem report 存在確認
PREMORTEM_DIR="/Users/yuusakuichio/trading/data/premortem_reports"
RECENT=$(find "$PREMORTEM_DIR" -name "*.md" -mmin -30 2>/dev/null | head -1)

if [ -z "$RECENT" ]; then
    cat >&2 <<EOF
[SPEC_PREMORTEM_BLOCK] 仕様書書込みを物理 block:
  path: $FILE_PATH
  reason: 直近 30 分以内の premortem report が存在しない

仕様書（data/specs/v3/*.md）の Write/Edit には事前 premortem が必須です。
（flow_audit C-03 対応・「仕様そのものの正しさ」物理検証）

実行コマンド:
  python3 /Users/yuusakuichio/trading/scripts/premortem.py --task "<仕様変更内容>"

完了後、再度 Write/Edit してください。

緊急解除: SPEC_PREMORTEM_BYPASS=1（ただし audit log に理由必須）
EOF
    exit 2
fi

exit 0
