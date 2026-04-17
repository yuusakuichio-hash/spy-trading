#!/usr/bin/env python3
"""PreToolUse hook: Red Team 並行起動 (環境変数 ATLAS_REDTEAM=1 時のみ)

Agent呼び出しを検知したら、並行して redteam_review.py を起動する。
既存ガード（discipline_guard）と非競合（両方exit 0なら通過）。
"""
import sys, json, os, subprocess, time

if os.environ.get("ATLAS_REDTEAM") != "1":
    sys.exit(0)

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
if tool_name != "Agent":
    sys.exit(0)

tool_input = data.get("tool_input", {}) if isinstance(data.get("tool_input"), dict) else {}
prompt = tool_input.get("prompt", "") or ""
description = tool_input.get("description", "") or ""

# 大規模タスク（500文字以上）のみRed Team並行起動
if len(prompt) < 500:
    sys.exit(0)

review_script = "/Users/yuusakuichio/trading/scripts/redteam_review.py"
if not os.path.exists(review_script):
    sys.exit(0)

# 非ブロッキングで Red Team 起動（結果はdata/redteam_reports/へ）
label = description.replace(" ", "_")[:40] or "agent_review"
try:
    subprocess.Popen(
        ["python3", review_script, "--text", prompt[:4000], "--label", label, "--timeout", "120"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sys.stderr.write(f"[redteam_prehook] 起動: {label}\n")
except Exception as e:
    sys.stderr.write(f"[redteam_prehook] 起動失敗: {e}\n")

sys.exit(0)
