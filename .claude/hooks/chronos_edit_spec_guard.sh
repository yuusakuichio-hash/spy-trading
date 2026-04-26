#!/usr/bin/env bash
# .claude/hooks/chronos_edit_spec_guard.sh — Chronos/Atlas 時間帯混同防止ガード
#
# PreToolUse フック: Edit/Write/MultiEdit ツールが
#   chronos_*.py / chronos_*.yaml / atlas_*.py / spy_bot*.py
# を対象にするとき、時間っぽい文字列 (HH:MM 形式) が含まれる場合に
# 市場仕様リマインダーを表示する。
#
# 強制ブロックではなく「警告表示のみ」。
# バイパス: export MARKET_GUARD_BYPASS=1

set -euo pipefail

LOG_DIR="/Users/yuusakuichio/trading/data/logs"
LOG_FILE="${LOG_DIR}/market_spec_guard.log"
mkdir -p "$LOG_DIR"

# ── 入力 JSON を stdin から読む ───────────────────────────────────────────────
INPUT="$(cat)"

# ── バイパスフラグ確認 ────────────────────────────────────────────────────────
if [ "${MARKET_GUARD_BYPASS:-0}" = "1" ]; then
    echo "$INPUT"
    exit 0
fi

# ── ツール名取得 ──────────────────────────────────────────────────────────────
TOOL_NAME="$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null || echo "")"

# Edit / Write / MultiEdit 以外はスキップ
case "$TOOL_NAME" in
    Edit|Write|MultiEdit) ;;
    *) echo "$INPUT"; exit 0 ;;
esac

# ── 対象ファイルパス取得 ──────────────────────────────────────────────────────
FILE_PATH="$(echo "$INPUT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
inp = d.get('tool_input', {})
print(inp.get('file_path', inp.get('path', '')))
" 2>/dev/null || echo "")"

BASENAME="$(basename "$FILE_PATH")"

# ── 対象ファイル判定 ──────────────────────────────────────────────────────────
IS_CHRONOS=0
IS_ATLAS=0
IS_SPY=0

case "$BASENAME" in
    chronos_*.py|chronos_*.yaml|chronos*.py) IS_CHRONOS=1 ;;
    atlas_*.py|atlas*.py)                    IS_ATLAS=1 ;;
    spy_bot*.py|spy_*.py)                    IS_SPY=1 ;;
esac

# 対象外ならスキップ
if [ "$IS_CHRONOS" = "0" ] && [ "$IS_ATLAS" = "0" ] && [ "$IS_SPY" = "0" ]; then
    echo "$INPUT"
    exit 0
fi

# ── 時間文字列の有無を確認 ────────────────────────────────────────────────────
CONTENT="$(echo "$INPUT" | python3 -c "
import sys, json, re
d = json.load(sys.stdin)
inp = d.get('tool_input', {})
text = inp.get('new_string', inp.get('content', inp.get('old_string', '')))
matches = re.findall(r'\b\d{1,2}:\d{2}\b', text)
matches += re.findall(r'\(\s*\d{1,2}\s*,\s*\d{1,2}\s*\)', text)
matches += re.findall(r'(?:HOUR|MINUTE|_START|_END)\s*=\s*\d+', text, re.IGNORECASE)
print(' '.join(matches) if matches else '')
" 2>/dev/null || echo "")"

# 時間文字列がなければスキップ
if [ -z "$CONTENT" ]; then
    echo "$INPUT"
    exit 0
fi

# ── 警告表示 ──────────────────────────────────────────────────────────────────
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"

if [ "$IS_CHRONOS" = "1" ]; then
    BOT_LABEL="Chronos (先物)"
    MARKET_LABEL="CME E-mini (ES/MES/NQ/MNQ)"
    SESSION_LABEL="月 07:00 〜 土 06:00 JST (EDT)"
    BREAK_LABEL="毎日 06:00-07:00 JST (EDT)"
    COPY_WARN="Atlas(SPX)からコピーした時間帯は間違いです。即中止。"
elif [ "$IS_ATLAS" = "1" ]; then
    BOT_LABEL="Atlas (SPX/SPY オプション)"
    MARKET_LABEL="CBOE SPX 0DTE / SPY オプション"
    SESSION_LABEL="22:20 〜 05:10 JST (EDT) 平日のみ"
    BREAK_LABEL="土日クローズ"
    COPY_WARN="Chronos(先物)からコピーした時間帯は間違いです。即中止。"
else
    BOT_LABEL="spy_bot (SPY オプション)"
    MARKET_LABEL="SPY オプション"
    SESSION_LABEL="22:20 〜 05:10 JST (EDT) 平日のみ"
    BREAK_LABEL="土日クローズ"
    COPY_WARN="先物の時間帯と混同しないでください。"
fi

echo "" >&2
echo "╔══════════════════════════════════════════════════════════╗" >&2
echo "║  [MARKET SPEC GUARD]  時間帯混同防止チェック              ║" >&2
echo "╠══════════════════════════════════════════════════════════╣" >&2
echo "║  対象: $BASENAME" >&2
echo "║  Bot:  $BOT_LABEL" >&2
echo "║  市場: $MARKET_LABEL" >&2
echo "║  セッション: $SESSION_LABEL" >&2
echo "║  休止: $BREAK_LABEL" >&2
echo "╠══════════════════════════════════════════════════════════╣" >&2
echo "║  検出時間文字列: $CONTENT" >&2
echo "╠══════════════════════════════════════════════════════════╣" >&2
echo "║  確認事項:" >&2
echo "║  1. common/market_specs.yaml を参照しましたか？          ║" >&2
echo "║  2. $COPY_WARN" >&2
echo "║  3. DST切替(3/8, 11/1)の影響を考慮しましたか？          ║" >&2
echo "║  バイパス: export MARKET_GUARD_BYPASS=1                  ║" >&2
echo "╚══════════════════════════════════════════════════════════╝" >&2
echo "" >&2

# ログ記録
{
    echo "[$TIMESTAMP] GUARD TRIGGERED"
    echo "  file: $FILE_PATH"
    echo "  bot:  $BOT_LABEL"
    echo "  time_strings: $CONTENT"
    echo "---"
} >> "$LOG_FILE"

# ブロックせず通過させる（警告のみ）
echo "$INPUT"
exit 0
