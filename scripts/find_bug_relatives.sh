#!/bin/bash
# find_bug_relatives.sh — bug_id で関連コード + 類似箇所 + 関連 bug を即抽出
#
# 使い方:
#   scripts/find_bug_relatives.sh BUG-20260425-003
#
# 出力:
#   - affected_files (FIXED 済箇所)
#   - similar_risk_locations (類似 pattern で未検証の箇所)
#   - related_bugs (連鎖)
#   - tests_added (test ファイル)
#   - related_commits (履歴)
#   - wrapper_available (代替 wrapper 実装の有無)

set -euo pipefail

LEDGER="/Users/yuusakuichio/trading/data/bug_ledger.jsonl"

if [ $# -lt 1 ]; then
    echo "Usage: $0 <BUG-YYYYMMDD-NNN>" >&2
    echo "" >&2
    echo "Available bugs:" >&2
    if [ -f "$LEDGER" ]; then
        python3 -c "
import json, sys
for line in open('$LEDGER'):
    try:
        e = json.loads(line)
        print(f\"  {e['bug_id']}: {e['title'][:70]}\", file=sys.stderr)
    except: pass
"
    fi
    exit 1
fi

BUG_ID="$1"

if [ ! -f "$LEDGER" ]; then
    echo "ERROR: ledger not found: $LEDGER" >&2
    exit 2
fi

python3 <<PYEOF
import json, sys

LEDGER = "$LEDGER"
BUG_ID = "$BUG_ID"

found = None
for line in open(LEDGER):
    try:
        entry = json.loads(line)
        if entry.get("bug_id") == BUG_ID:
            found = entry
            break
    except json.JSONDecodeError:
        continue

if not found:
    print(f"ERROR: bug_id not found: {BUG_ID}", file=sys.stderr)
    sys.exit(3)

print(f"=== {found['bug_id']} ===")
print(f"title: {found['title']}")
print(f"discovered: {found['discovered_at']}")
print(f"root_cause: {found['root_cause']}")
print()
print("=== affected_files (FIXED or in-progress) ===")
for af in found.get("affected_files", []):
    print(f"  - {af.get('file', '?')}:{af.get('line', af.get('symbol', '?'))} [{af.get('status', '?')}]")
print()
print("=== similar_risk_locations (未検証・類似 pattern) ===")
for sr in found.get("similar_risk_locations", []):
    print(f"  - {sr.get('file', '?')}:{sr.get('line', sr.get('symbol', '?'))} [{sr.get('status', '?')}]")
if not found.get("similar_risk_locations"):
    print("  (なし)")
print()
print("=== related_bugs (連鎖) ===")
for rb in found.get("related_bugs", []):
    print(f"  - {rb}")
if not found.get("related_bugs"):
    print("  (なし)")
print()
print("=== tests_added ===")
for t in found.get("tests_added", []):
    print(f"  - {t}")
print()
print("=== related_commits ===")
for c in found.get("related_commits", []):
    print(f"  - {c}")
print()
print(f"=== wrapper_available ===")
print(f"  {found.get('wrapper_available', '(なし)')}")
print()
print(f"=== detection_method ===")
print(f"  {found.get('detection_method', '?')}")
print()
print(f"=== root_cause_pattern ===")
print(f"  {found.get('root_cause_pattern', '?')}")
PYEOF
