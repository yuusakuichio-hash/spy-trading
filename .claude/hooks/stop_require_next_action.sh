#!/bin/bash
# stop_require_next_action.sh
#
# Stop hook: assistant response 終了時に tool_use 数を確認し、
# 0 件だったら「説明のみで止まる」規律違反パターンとして次 action 催促を stdout 注入。
# stdout は次 user turn の context に system message として現れるため、
# Claude が次 response 冒頭で必ず認識する。
#
# 2026-04-24 策定（ゆうさくさん指示「違反で止まらない仕組み」）
# 根拠: memory/feedback_declaration_execution_unified_20260424.md
#
# Bypass: STOP_REQUIRE_ACTION_BYPASS=1 (ゆうさくさん向け説明応答等で一時無効化可)

set -u

if [ "${STOP_REQUIRE_ACTION_BYPASS:-0}" = "1" ]; then
    exit 0
fi

INPUT=$(cat)

# hook 入力から transcript_path を取得（Claude Code stop hook の仕様）
TRANSCRIPT=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', ''))
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then
    exit 0
fi

# 直近の assistant turn で tool_use が何件あったかカウント
TOOL_USE_COUNT=$(python3 << PYEOF
import json
from pathlib import Path

transcript = Path("$TRANSCRIPT")
if not transcript.exists():
    print(-1)
    exit()

# jsonl を tail から読んで直近の assistant turn を識別
# assistant turn = user turn の間までの連続 assistant メッセージ群
lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()

# 末尾から遡って最新の assistant turn を探す
count = 0
in_last_assistant_turn = False
for line in reversed(lines[-200:]):
    line = line.strip()
    if not line:
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        continue
    role = rec.get("type", "")
    if role == "user":
        # user turn に当たったら stop（最新 assistant turn の境界）
        if in_last_assistant_turn:
            break
        continue
    if role != "assistant":
        continue
    in_last_assistant_turn = True
    msg = rec.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                count += 1

print(count)
PYEOF
)

# tool_use 0 件 = 説明のみで止まったパターン
if [ "$TOOL_USE_COUNT" = "0" ]; then
    cat <<'EOF'

─── [STOP_REQUIRE_ACTION] 規律違反検知 ───

直近の応答で tool_use 0 件 = 説明・宣言のみで止まった可能性あり。
memory/feedback_declaration_execution_unified_20260424.md 違反（同一 response 内で
宣言 → 実行が原則）。

次の user turn では:
1. 「今 response は説明のみでよい」と user が明示的に示しているか確認
2. 示していなければ、並行可能な他 task（carryover / pytest / test 作成 等）を即着手
3. 末尾に「いかがしますか？」等の質問を付けず、推奨案で自動着手宣言して終わる

止まらない・走り続ける規律（ゆうさくさん明示方針）。
─────────────────────────────────────

EOF
fi

exit 0
