#!/bin/bash
# pre_commit_inst_id_extractor.sh — commit msg から INST-<id> を抽出し
# user_instruction_ledger.jsonl の該当 entry を auto mark-done にする
#
# 運用: git commit msg に "INST-<12hex>" または "Related-INST: <id>" を含めれば
#       ledger の該当 pending を自動 done 化
#
# 例: git commit -m "fix calc_ivr typo (INST-f47456ea756a)"
set -euo pipefail

LEDGER="/Users/yuusakuichio/trading/data/user_instruction_ledger.jsonl"

if [ ! -f "$LEDGER" ]; then
    exit 0
fi

# 直近 commit の msg を取得
COMMIT_MSG=$(git log -1 --pretty=%B 2>/dev/null || echo "")
COMMIT_HASH=$(git log -1 --pretty=%H 2>/dev/null || echo "")

if [ -z "$COMMIT_MSG" ]; then
    exit 0
fi

# INST-<12hex> パターン抽出 (INST-xxxxxxxxxxxx or Related-INST: xxxxxxxxxxxx)
INST_IDS=$(echo "$COMMIT_MSG" | grep -oE 'INST-[a-f0-9]{12}' | sed 's/INST-//' | sort -u || true)
RELATED_IDS=$(echo "$COMMIT_MSG" | grep -oE 'Related-INST:?\s+[a-f0-9]{12}' | grep -oE '[a-f0-9]{12}$' | sort -u || true)

ALL_IDS="$INST_IDS $RELATED_IDS"
if [ -z "$(echo $ALL_IDS | tr -d ' ')" ]; then
    exit 0
fi

python3 <<PYEOF
import json
import datetime as _dt
from pathlib import Path

LEDGER = Path("$LEDGER")
COMMIT_HASH = "$COMMIT_HASH"
ids_raw = "$ALL_IDS".strip().split()

entries = [json.loads(l) for l in LEDGER.read_text(encoding='utf-8').splitlines() if l.strip()]
now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).isoformat(timespec="seconds")
marked = 0
for iid in ids_raw:
    for e in entries:
        if e.get("instruction_id") == iid and e.get("status") == "pending":
            e["status"] = "done"
            e["related_commit"] = COMMIT_HASH[:12]
            e["verified_by"] = "pre_commit_hook"
            e["verified_at"] = now
            marked += 1
            print(f"[INST-auto-mark] {iid} → done (commit={COMMIT_HASH[:8]})", file=__import__('sys').stderr)
            break

if marked > 0:
    with open(LEDGER, 'w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
PYEOF
exit 0
