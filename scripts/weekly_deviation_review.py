#!/usr/bin/env python3
"""Weekly Deviation Review — 土曜10:00 JST自動実行

deviation_scanner を直近7日分で実行し、結果を Pushover [SYS/DEVIATION] に送信。
Challenger型事故予防（Normalization of Deviance 早期発見）。
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from urllib import request, parse

BASE = Path(__file__).resolve().parents[1]

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "a5rb9ipb3yrdanv3vk4n8x28qt7io9")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "u2cevk8nktib3sr148rw2hs78ecvux")


def send_pushover(title: str, message: str, priority: int = 0):
    data = parse.urlencode({
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message[:1020],
        "priority": priority,
    }).encode()
    try:
        req = request.Request("https://api.pushover.net/1/messages.json", data=data)
        with request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[pushover] {e}", file=sys.stderr)
        return False


def main():
    scanner = BASE / "scripts" / "deviation_scanner.py"
    result = subprocess.run(
        ["python3", str(scanner), "--days", "7", "--threshold", "10", "--dashboard"],
        capture_output=True, text=True, cwd=str(BASE),
    )
    output = result.stdout.strip() or "(no output)"
    has_normalized = result.returncode == 1

    # カテゴリを整形
    if has_normalized:
        title = "[SYS/DEVIATION] 常態化検知"
        priority = 1
        msg = f"Challenger型予防アラート\n\n{output}\n\n詳細: data/deviation_dashboard.md"
    else:
        title = "[SYS/DEVIATION] 週次レビュー正常"
        priority = 0
        msg = f"常態化検知なし\n\n{output}"

    send_pushover(title, msg, priority)
    print(f"[weekly_review] sent: {title}")


if __name__ == "__main__":
    main()
