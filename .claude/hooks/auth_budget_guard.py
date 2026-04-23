#!/usr/bin/env python3
"""
auth_budget_guard.py -- PreToolUse hook
Bash tool 実行時に認証試行パターンを検出し、予算超過なら HARD BLOCK する。
exit 0: 通過, exit 2: HARD BLOCK
緊急解除: AUTH_BUDGET_BYPASS=1 環境変数
"""

import sys
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

LOG = "/Users/yuusakuichio/trading/data/logs/auth_budget_guard.log"
os.makedirs(os.path.dirname(LOG), exist_ok=True)

BYPASS = os.environ.get("AUTH_BUDGET_BYPASS", "") == "1"

def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass

AUTH_PATTERNS = [
    (r"tradovate_client\.py", "tradovate_demo", "Tradovate client 実行"),
    (r"curl.*tradovateapi\.com.*auth", "tradovate_demo", "Tradovate認証エンドポイント直叩き"),
    (r"tradovate.*authenticate|authenticate.*tradovate", "tradovate_demo", "Tradovate authenticate呼び出し"),
    (r"TRADOVATE_ENV=LIVE.*tradovate_client", "tradovate_live", "Tradovate LIVE 認証"),
    (r"live\.tradovateapi\.com.*auth", "tradovate_live", "Tradovate Live認証エンドポイント"),
    (r"futu.*login|login.*futu|opend.*login|ftapi.*login", "opend", "FutuOpenD ログイン"),
    (r"RET_create_app_conn|SetLoginInfo|do_login", "opend", "OpenD SDK ログイン API"),
    (r"curl.*moomoo|moomoo.*auth|moomoo.*login", "moomoo", "moomoo API認証"),
    (r"gmail_monitor\.py\s+--auth|InstalledAppFlow|run_local_server", "gmail_oauth", "Gmail OAuth新規フロー"),
    (r"python3.*gmail.*--auth", "gmail_oauth", "Gmail認証スクリプト"),
]

def _load_budget():
    try:
        sys.path.insert(0, "/Users/yuusakuichio/trading")
        from common.auth_budget import AuthBudget, AuthBudgetExceeded, SERVICES
        return AuthBudget, AuthBudgetExceeded, SERVICES
    except ImportError as e:
        _log(f"[WARN] common.auth_budget import failed: {e}")
        return None, None, {}

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

if tool_name != "Bash":
    sys.exit(0)

command = tool_input.get("command", "")
if not command:
    sys.exit(0)

if BYPASS:
    _log(f"[BYPASS] command={command[:100]}")
    sys.exit(0)

detected_services = []
for pattern, service, desc in AUTH_PATTERNS:
    if re.search(pattern, command, re.IGNORECASE):
        detected_services.append((service, desc))

if not detected_services:
    sys.exit(0)

AuthBudget, AuthBudgetExceeded, SERVICES = _load_budget()

if AuthBudget is None:
    _log("[WARN] auth_budget unavailable -- allowing command")
    sys.exit(0)

blocked_services = []
warning_services = []

for service, desc in detected_services:
    allowed, remaining, reason = AuthBudget.check_budget(service)
    _log(f"[CHECK] service={service} allowed={allowed} remaining={remaining} cmd={command[:80]}")
    if not allowed:
        blocked_services.append((service, desc, reason))
    elif remaining <= 1:
        warning_services.append((service, desc, remaining))

if blocked_services:
    sys.stderr.write("\n" + "=" * 70 + "\n")
    sys.stderr.write("[AUTH BUDGET GUARD] HARD BLOCK -- 認証試行予算超過\n")
    sys.stderr.write("=" * 70 + "\n")
    for service, desc, reason in blocked_services:
        spec = SERVICES.get(service, {})
        window_min = spec.get("window_sec", 3600) // 60
        sys.stderr.write(f"\n  サービス: {service}\n")
        sys.stderr.write(f"  検出理由: {desc}\n")
        sys.stderr.write(f"  ブロック: {reason}\n")
        sys.stderr.write(f"  制限窓:  {window_min}分\n")
    sys.stderr.write("\n[AUTH BUDGET GUARD] 緊急解除が必要な場合:\n")
    sys.stderr.write("  AUTH_BUDGET_BYPASS=1 環境変数をセットして再実行\n")
    sys.stderr.write("  または window_sec 経過後に自動リセット\n")
    sys.stderr.write("=" * 70 + "\n\n")
    _log(f"[HARD BLOCK] services={[s for s,_,_ in blocked_services]} cmd={command[:100]}")
    sys.exit(2)

if warning_services:
    sys.stderr.write("\n[AUTH BUDGET GUARD] WARNING -- 残り試行回数が少ない\n")
    for service, desc, remaining in warning_services:
        sys.stderr.write(f"  {service}: 残り{remaining}回 ({desc})\n")
    sys.stderr.write("  次の試行で上限に達します。\n\n")
    _log(f"[WARN] low_remaining services={[s for s,_,_ in warning_services]}")

sys.exit(0)
