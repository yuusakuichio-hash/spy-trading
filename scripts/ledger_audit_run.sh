#!/bin/bash
# ledger_audit_run.sh — 1h cron wrapper for ledger_audit_run.py
#
# 使用方法: bash scripts/ledger_audit_run.sh
# launchd: com.soralab.ledger-auditor.plist (StartInterval=3600)
set -u

PYTHON="/usr/bin/python3"
SCRIPT="/Users/yuusakuichio/trading/scripts/ledger_audit_run.py"
LOG="/Users/yuusakuichio/trading/data/logs/ledger_audit_run.log"

mkdir -p "$(dirname "$LOG")"
cd /Users/yuusakuichio/trading

exec "$PYTHON" "$SCRIPT" 2>> "$LOG"
