#!/usr/bin/env python3
"""
state_safety_guard: PreToolUse hook
Write/Edit が data/*state*.json や config.yaml を対象とするとき、
安全装置系キーワードを含む変更内容を検出して警告する。
bypass: STATE_SAFETY_BYPASS=1 環境変数をセットすると通過
"""
import sys
import json
import os
import re
from datetime import datetime

LOG = "/Users/yuusakuichio/trading/data/logs/state_safety_violations.log"
os.makedirs(os.path.dirname(LOG), exist_ok=True)

if os.environ.get("STATE_SAFETY_BYPASS", "") == "1":
    sys.exit(0)

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

if tool_name not in ("Write", "Edit"):
    sys.exit(0)

file_path = tool_input.get("file_path", "")

TARGET_PATTERNS = [
    r"data/.*state.*\.json$",
    r"data/.*config.*\.json$",
    r"config\.yaml$",
    r"config\.yml$",
    r"atlas_state\.json$",
    r"watchdog.*state.*\.json$",
    r"chronos.*state.*\.json$",
    # キャッシュ系ファイルも監視対象（2026-04-21 Tradovate hardening redteam指摘）
    r".*_auth_cache\.json$",
    r".*_token\.json$",
    r"tradovate_auth_cache\.json$",
]

is_target = any(re.search(p, file_path, re.IGNORECASE) for p in TARGET_PATTERNS)
if not is_target:
    sys.exit(0)

content_parts = []
if "content" in tool_input:
    content_parts.append(str(tool_input["content"]))
if "new_string" in tool_input:
    content_parts.append(str(tool_input["new_string"]))
if "old_string" in tool_input:
    content_parts.append(str(tool_input["old_string"]))
check_text = "\n".join(content_parts)

if not check_text.strip():
    sys.exit(0)

SAFETY_KEYWORDS = [
    (r"pdt_constrained",   "CRITICAL", "PDT制約フラグ — derived のため state.json への手書き禁止"),
    (r"pdt_bypass",        "CRITICAL", "PDTバイパスフラグ"),
    (r"kill_switch",       "CRITICAL", "キルスイッチ"),
    (r"force_",            "CRITICAL", "強制上書き系キーワード"),
    (r"override_",         "CRITICAL", "オーバーライド系キーワード"),
    (r"trading_mode",      "HIGH",     "トレードモードフィールド — 環境変数 ATLAS_MODE で切り替える"),
    (r'"mode"\s*:',        "HIGH",     "モードフィールド"),
    (r"_constrained",      "HIGH",     "制約フラグ系サフィックス"),
    (r"_bypass",           "HIGH",     "バイパスフラグ系サフィックス"),
    (r"_blocked",          "HIGH",     "ブロックフラグ系サフィックス"),
    (r"_enabled",          "MEDIUM",   "有効化フラグ — 意図的な変更か確認"),
    (r"_disabled",         "MEDIUM",   "無効化フラグ — 意図的な変更か確認"),
    (r'"recovered"\s*:',   "HIGH",     "回復フラグ — watchdog が管理するフィールド"),
]

detected = []
for pattern, severity, description in SAFETY_KEYWORDS:
    if re.search(pattern, check_text, re.IGNORECASE):
        detected.append((severity, pattern, description))

if not detected:
    sys.exit(0)

ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S JST")
has_critical = any(s == "CRITICAL" for s, _, _ in detected)

with open(LOG, "a", encoding="utf-8") as lf:
    lf.write(f"=== STATE SAFETY VIOLATION [{ts}] ===\n")
    lf.write(f"Tool: {tool_name}  File: {file_path}\n")
    for severity, pattern, desc in detected:
        lf.write(f"  [{severity}] pattern={pattern!r}: {desc}\n")
    lf.write("  bypass: STATE_SAFETY_BYPASS=1 環境変数で通過可\n")
    lf.write("---\n")

sys.stderr.write(f"\n[STATE SAFETY GUARD] 安全装置系フィールドを含む書き込みを検知\n")
sys.stderr.write(f"  対象ファイル: {file_path}\n")
for severity, pattern, description in detected:
    sys.stderr.write(f"  [{severity}] {pattern!r} — {description}\n")
sys.stderr.write("\n[STATE SAFETY GUARD] WARNING: Safety-critical field detected.\n")
sys.stderr.write("  Confirm this is derived, not hand-written.\n")
sys.stderr.write("  - CRITICALフィールドはコードで derived するべき (state.json に書かない)\n")
sys.stderr.write("  - 意図的な変更: STATE_SAFETY_BYPASS=1 を環境変数にセットして再実行\n")
sys.stderr.write(f"  ログ: {LOG}\n\n")

if has_critical:
    sys.stderr.write("[STATE SAFETY GUARD] HARD BLOCK: CRITICAL フィールドのため書き込みをブロック\n")
    sys.exit(2)

sys.exit(0)
