#!/usr/bin/env bash
# proposal_bottleneck_stop_guard.sh
# Stop hook: 直接チャット応答に含まれる「プラン/選択肢/推奨/案」パターンを検知し、
# 事前調査（data/research/*.md 直近30分 or premortem）の証拠がなければ
# 違反ログ記録+Pushover通知+次プロンプト注入用strikeファイル生成
#
# 2026-04-21 導入
# 背景: 既存 recommendation_guard.sh は Agent tool のみ・チャット直接応答は素通り
# → Plan A/B 提案時に仕様未確認で実環境auth失敗→ゆうさくさん時間浪費パターン繰返し
#
# 検出対象: Stop hook on assistant response to user

set -uo pipefail

LOG_DIR="/Users/yuusakuichio/trading/data/logs"
STRIKE_DIR="/Users/yuusakuichio/trading/data/logs/proposal_strikes"
PUSHOVER_TOKEN="a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER="u2cevk8nktib3sr148rw2hs78ecvux"
mkdir -p "$LOG_DIR" "$STRIKE_DIR"

TS=$(date "+%Y-%m-%d %H:%M:%S JST")

# Bypass
if [ "${PROPOSAL_BOTTLENECK_GUARD_BYPASS:-0}" = "1" ]; then
  exit 0
fi

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '')
except:
    print('')
" 2>/dev/null || echo "")

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# 最後のアシスタント応答 text を抽出
LAST_RESPONSE=$(python3 <<PYEOF 2>/dev/null || echo ""
import json, pathlib
p = pathlib.Path("$TRANSCRIPT_PATH")
lines = p.read_text(errors="replace").splitlines()
for line in reversed(lines):
    if not line.strip():
        continue
    try:
        d = json.loads(line)
    except:
        continue
    if d.get("type") == "assistant":
        msg = d.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            texts = [b.get("text","") for b in content if isinstance(b, dict) and b.get("type")=="text"]
            print("\n".join(texts)[:8000])
            break
PYEOF
)

if [ -z "$LAST_RESPONSE" ]; then
  exit 0
fi

# 提案パターン検出
# - プランA/B, 選択肢, 推奨, 案, 購入, 契約, ~がおすすめ, ~が最適
PROPOSAL_PATTERN='(プラン[A-Z]|プラン[0-9]|選択肢|推奨|お勧め|おすすめ|最適|案[ABC]|購入してください|契約してください|sign up|subscribe|enroll|払って|買って|購入推奨|推奨案)'

if ! echo "$LAST_RESPONSE" | grep -qE "$PROPOSAL_PATTERN"; then
  # 提案パターンなし → OK
  exit 0
fi

# 提案パターンあり → 事前調査証拠チェック
HAS_EVIDENCE=0
EVIDENCE_DETAIL=""

RESEARCH_RECENT=$(find /Users/yuusakuichio/trading/data/research -name "*.md" -mmin -30 2>/dev/null | wc -l | tr -d ' ')
if [ "$RESEARCH_RECENT" -gt 0 ]; then
  HAS_EVIDENCE=1
  EVIDENCE_DETAIL="research_recent_${RESEARCH_RECENT}"
fi

PREMORTEM_RECENT=$(find /Users/yuusakuichio/trading/data/premortem_reports -name "*.md" -mmin -30 2>/dev/null | wc -l | tr -d ' ')
if [ "$PREMORTEM_RECENT" -gt 0 ]; then
  HAS_EVIDENCE=1
  EVIDENCE_DETAIL="${EVIDENCE_DETAIL}+premortem_${PREMORTEM_RECENT}"
fi

# 応答内に「未確認」「不明」「調査中」等の honest hedge があれば証拠扱い
if echo "$LAST_RESPONSE" | grep -qE '(未確認|不明|調査中|要確認|確認待ち|要調査|verification needed|unverified)'; then
  HAS_EVIDENCE=1
  EVIDENCE_DETAIL="${EVIDENCE_DETAIL}+honest_hedge"
fi

# 応答内に「ボトルネック」「仕様確認済」「公式ソース」等の明示があれば証拠扱い
if echo "$LAST_RESPONSE" | grep -qE '(ボトルネック特定|仕様確認済|公式確認済|出典|ソース:|エビデンス)'; then
  HAS_EVIDENCE=1
  EVIDENCE_DETAIL="${EVIDENCE_DETAIL}+inline_evidence"
fi

if [ "$HAS_EVIDENCE" -eq 1 ]; then
  echo "[$TS] PASSED proposal_pattern | evidence=$EVIDENCE_DETAIL" >> "$LOG_DIR/proposal_passed.log"
  exit 0
fi

# 違反検知 → log + Pushover + strike
STRIKE_COUNT=$(ls -1 "$STRIKE_DIR" 2>/dev/null | wc -l | tr -d ' ')
STRIKE_COUNT=$((STRIKE_COUNT + 1))
STRIKE_FILE="$STRIKE_DIR/strike_$(date +%Y%m%d_%H%M%S).txt"

cat > "$STRIKE_FILE" <<EOF
[$TS] Proposal bottleneck violation #$STRIKE_COUNT
Pattern matched in assistant response without research evidence.
Response excerpt (first 500 chars):
$(echo "$LAST_RESPONSE" | head -c 500)
...
EOF

echo "[$TS] VIOLATION proposal_without_research | strike=$STRIKE_COUNT" >> "$LOG_DIR/proposal_violations.log"

# Pushover通知 (priority=1 alert)
curl -s -X POST https://api.pushover.net/1/messages.json \
  -F "token=$PUSHOVER_TOKEN" \
  -F "user=$PUSHOVER_USER" \
  -F "title=[ALERT] Proposal without bottleneck research" \
  -F "priority=1" \
  -F "message=Claude proposed action/plan without research evidence. Strike #$STRIKE_COUNT. Check: $LOG_DIR/proposal_violations.log" \
  > /dev/null 2>&1 || true

# 次セッションのUserPromptSubmit hook 用に strike 注入ファイル作成
INJECT_FILE="/Users/yuusakuichio/trading/data/logs/pending_proposal_violations.md"
cat >> "$INJECT_FILE" <<EOF
## [$TS] 規律違反 strike #$STRIKE_COUNT: 事前調査なしで提案
抜粋: $(echo "$LAST_RESPONSE" | head -c 200 | tr '\n' ' ')
→ 次回プラン/推奨/購入系提案時は必ず data/research/*.md 作成後に出すこと

EOF

exit 0  # Stop hook なのでblockはできない・記録+通知のみ
