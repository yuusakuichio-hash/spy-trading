#!/bin/bash
# pronoun_guard.sh — 一人称規律の物理ガード
#
# 2026-04-22 ゆうさくさん指摘「ソラさん女性なのに『僕』使った」を物理化。
# memory feedback_language.md「俺・僕禁止・私で統一」を hook で強制。
#
# 動作:
#   Stop hook で stdin の応答 text を受領
#   以下の禁句を検出したら exit 2 で block:
#     - 「僕」「俺」（一人称禁止）
#     - 「俺たち」「僕たち」（複数形も）
#   ただし以下は許可:
#     - 引用内（「」or `` で囲まれている）の「僕」「俺」（ユーザー発言や agent 報告の引用）
#     - コード内の var 名等
#
# Bypass: PRONOUN_GUARD_BYPASS=1
#
# 既存規律 memory:
#   - feedback_language.md（一人称「私」固定）
#   - feedback_tone.md / feedback_tone_language.md（言葉遣い）

set -u

if [ "${PRONOUN_GUARD_BYPASS:-}" = "1" ]; then
    exit 0
fi

# Stop hook は stdin に JSON or 応答 text を受け取る
INPUT=$(cat 2>/dev/null || true)

if [ -z "$INPUT" ]; then
    exit 0
fi

# JSON か plain text か判定
TEXT=""
if echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(type(d).__name__)" 2>/dev/null | grep -q "dict"; then
    # JSON の場合、応答 text を抽出
    TEXT=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    # Stop hook の payload structure 候補
    text = d.get('response', '') or d.get('text', '') or d.get('content', '') or d.get('message', '')
    if not text:
        # transcript_path から最後の assistant 応答を取る方法もあるが省略
        text = json.dumps(d)
    print(text)
except Exception:
    print('')
" 2>/dev/null)
else
    TEXT="$INPUT"
fi

if [ -z "$TEXT" ]; then
    exit 0
fi

# Python に env var で渡す（stdin は heredoc に使うため衝突回避）
RESULT=$(PRONOUN_INPUT="$TEXT" python3 - <<'PYEOF'
import os, re
text = os.environ.get("PRONOUN_INPUT", "")

# 引用内除去（ネストは考慮しない・単純化）
text_no_quote = re.sub(r'「[^」]*」', '', text)
text_no_quote = re.sub(r'```[\s\S]*?```', '', text_no_quote)
text_no_quote = re.sub(r'`[^`]*`', '', text_no_quote)
text_no_quote = re.sub(r'^>.*$', '', text_no_quote, flags=re.MULTILINE)
text_no_quote = re.sub(r'"[^"]*"', '', text_no_quote)

# 一人称検出
# negative lookbehind は「漢字 + ASCII」のみ:
#   ひらがな除外しない（「と僕」「は俺」のような自然な日本語を検出）
#   漢字除外（「公僕」「下僕」等の誤検出回避）
#   ASCII 除外（var 名等の誤検出回避・コードブロック除去後も保険）
patterns = [
    (r'(?<![一-龥A-Za-z])僕', '僕'),
    (r'(?<![一-龥A-Za-z])俺', '俺'),
]
hits = []
for pattern, label in patterns:
    matches = list(re.finditer(pattern, text_no_quote))
    if matches:
        first = matches[0]
        start = max(0, first.start() - 30)
        end = min(len(text_no_quote), first.end() + 30)
        context = text_no_quote[start:end]
        hits.append(f'{label} ({len(matches)}回・例: "...{context}...")')

if hits:
    print('VIOLATION', '; '.join(hits))
else:
    print('OK')
PYEOF
)

if echo "$RESULT" | grep -q "^VIOLATION"; then
    cat >&2 <<EOF
[PRONOUN_GUARD] 一人称規律違反検出:
  $RESULT

memory/feedback_language.md「俺・僕禁止・一人称は『私』で統一」違反。
ソラ（女性設定）として「私」を使用してください。

引用内（「」内・コードブロック内）は許可されています。

緊急 bypass: PRONOUN_GUARD_BYPASS=1（audit log 必要）
EOF
    exit 2
fi

exit 0
