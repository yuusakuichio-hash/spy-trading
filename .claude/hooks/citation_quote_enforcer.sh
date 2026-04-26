#!/usr/bin/env bash
# citation_quote_enforcer.sh
# Stop hook: Citation/Quote enforcement (Menick DeepMind GopherCite 2022)
# "X によると" "X と書いてある" 検出時 実引用 or file:line 必須
# 2026-04-21 導入

set -uo pipefail

if [ "${CITATION_QUOTE_BYPASS:-0}" = "1" ]; then
  exit 0
fi

LOG="/Users/yuusakuichio/trading/data/logs/citation_quote_enforcer.log"
mkdir -p "$(dirname "$LOG")"
TS=$(date "+%Y-%m-%d %H:%M:%S JST")

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '')
except:
    print('')
" 2>/dev/null)

[ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ] && exit 0

LAST_RESPONSE=$(python3 <<PYEOF 2>/dev/null || echo ""
import json, pathlib
p = pathlib.Path("$TRANSCRIPT_PATH")
for line in reversed(p.read_text(errors="replace").splitlines()):
    if not line.strip(): continue
    try: d = json.loads(line)
    except: continue
    if d.get("type") == "assistant":
        m = d.get("message", {}); c = m.get("content", [])
        if isinstance(c, list):
            ts = [b.get("text","") for b in c if isinstance(b,dict) and b.get("type")=="text"]
            print("\n".join(ts)[:6000]); break
PYEOF
)

[ -z "$LAST_RESPONSE" ] && exit 0

# 引用主張 pattern
CITATION_PATTERNS=$(echo "$LAST_RESPONSE" | grep -cE "(によると|と書いてある|公式には|出典|documented|stated)" || echo 0)

# evidence: quote (```block or "quoted") or file:line reference
HAS_EVIDENCE=0
if echo "$LAST_RESPONSE" | grep -qE "(\`\`\`|[a-zA-Z_]+\.(py|md|sh|json|yaml):[0-9]+|/[a-zA-Z_/]+\.(py|md|sh|json|yaml))"; then
  HAS_EVIDENCE=1
fi

if [ "$CITATION_PATTERNS" -ge 2 ] && [ "$HAS_EVIDENCE" -eq 0 ]; then
  echo "[$TS] WARN citation_without_evidence | citation_count=$CITATION_PATTERNS" >> "$LOG"
fi

exit 0
