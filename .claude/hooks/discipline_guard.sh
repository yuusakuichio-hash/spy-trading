#!/usr/bin/env python3
"""PreToolUse hook: 規律違反パターン検知。違反時はstderr+exit 2でClaudeに反省促す。"""
import sys, json, re, os
from datetime import datetime

log_file = "/Users/yuusakuichio/trading/data/logs/discipline_violations.log"
timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

# Read系はスキップ
if tool_name in ("Read", "Grep", "Glob"):
    sys.exit(0)

# 規律/メモリ関連ファイル編集時はbypass（文書内に規律語彙を記載するのが正当用途）
file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
bypass_paths = [
    "/memory/",
    "/.claude/hooks/",
    "/CLAUDE.md",
    "discipline_",
    "recent_corrections",
    "/data/discipline_",
    "/data/violation_",
    "/data/pending_",
    "feedback_cognitive",
    "feedback_no_schedule",
    "feedback_market_hours",
    "violation_patterns.json",
    "violation_physical_prevention",
    "/scripts/",
    "/tests/test_violation",
]
if file_path and any(p in file_path for p in bypass_paths):
    sys.exit(0)

check_text = json.dumps(tool_input, ensure_ascii=False).lower()
violations = []

# 先延ばし語彙
procrastination = [
    "月曜から稼働", "月曜から", "週末に", "週末まで",
    "後日別タスク", "後日実装", "後日対応", "後日",
    "クローズ後に修正", "クローズ後に",
    "本番移行前に", "本番移行後に",
    "翌営業日に", "翌日やる",
    "来週", "明日やる", "明日対応",
    "後で", "あとで",
]
for p in procrastination:
    if p.lower() in check_text:
        violations.append(f"[先延ばし] \"{p}\"")
        break

# 不要な確認質問
unnecessary_confirm = [
    "進めていい？", "進めていいですか",
    "どれからやる？", "どれからやりますか",
    "実施してもよろしいでしょうか", "承認をお願い",
    "判断くれ", "判断してください",
    "続ける？", "続けますか",
    "それとも",
    "どっちする？", "どっちにする", "どれにする",
    "どっちから", "どちらから",
    "や、する？", "で進める？",
    "どうする？", "何やる？", "何する？",
    "ok？", "okでしょうか", "大丈夫ですか",
    "実行していい", "発動していい",
]
for p in unnecessary_confirm:
    if p.lower() in check_text:
        violations.append(f"[不要確認] \"{p}\"")
        break

# 場中停止提案（ET market hours: JST 22:30-05:00 相当）
now = datetime.now()
current_min = now.hour * 60 + now.minute
if current_min >= 22 * 60 + 30 or current_min <= 5 * 60:
    for p in ["bot停止", "停止を推奨", "停止してください", "botを止めて", "停止を提案"]:
        if p.lower() in check_text:
            violations.append(f"[場中停止提案] \"{p}\" (市場時間中)")
            break

# 銘柄専用化
if tool_name in ("Write", "Edit", "Bash"):
    symbol_lock_patterns = [
        r'if\s+symbol\s*!=\s*["\']us\.spy["\']',
        r'if\s+ticker\s*!=\s*["\']spy["\']',
        r'return\s+#.*spy.*専用',
    ]
    for p in symbol_lock_patterns:
        if re.search(p, check_text, re.IGNORECASE):
            violations.append(f"[銘柄専用化] Atlasはマルチ銘柄")
            break

# 違反があれば記録+stderr出力+exit 2
if violations:
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"=== DISCIPLINE VIOLATION ===\n")
        f.write(f"Timestamp: {timestamp}\n")
        f.write(f"Tool: {tool_name}\n")
        for v in violations:
            f.write(f"  {v}\n")
        f.write(f"  Input(first 200): {check_text[:200]}\n")
        f.write("---\n")
    sys.stderr.write(f"\n[DISCIPLINE GUARD] 規律違反パターン検知:\n")
    for v in violations:
        sys.stderr.write(f"  {v}\n")
    sys.stderr.write(f"[DISCIPLINE GUARD] 「なぜ今やらない？」に答えられないなら今やれ。\n")
    sys.stderr.write(f"[DISCIPLINE GUARD] ログ: {log_file}\n\n")
    sys.exit(2)

sys.exit(0)
