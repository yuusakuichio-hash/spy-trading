#!/usr/bin/env python3
"""
atlas_watchdog.py - 最小動作版
condor.log を監視し、異常パターンを検知したら Pushover で即通知
"""

import time
import re
import os
import requests
from collections import defaultdict, deque
from datetime import datetime

LOG_PATH = "/Users/yuusakuichio/trading/data/logs/condor.log"
WATCHDOG_LOG = "/Users/yuusakuichio/trading/data/logs/watchdog.log"
CHECK_INTERVAL = 10  # 秒

PUSHOVER_TOKEN = "a5rb9ipb3yrdanv3vk4n8x28qt7io9"
PUSHOVER_USER = "u2cevk8nktib3sr148rw2hs78ecvux"

# 検知パターン
ALERT_PATTERNS = [
    (re.compile(r'\bERROR\b', re.IGNORECASE), "ERROR"),
    (re.compile(r'\bWARNING\b', re.IGNORECASE), "WARNING"),
    (re.compile(r'strike.*不整合|strike mismatch|invalid strike', re.IGNORECASE), "strike不整合"),
    (re.compile(r'gamma_early_exit|early.?exit.*gamma|gamma.*early.?exit', re.IGNORECASE), "gamma_early_exit"),
]

# パターン別タイムスタンプキュー (直近5分)
pattern_times: dict[str, deque] = defaultdict(lambda: deque())
WINDOW_SECONDS = 300  # 5分
THRESHOLD = 10  # 件数

# 送信済みアラートのデdup (同パターンを60秒以内に再送しない)
last_alert_sent: dict[str, float] = {}
ALERT_COOLDOWN = 60  # 秒

def wlog(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(WATCHDOG_LOG, "a") as f:
        f.write(line + "\n")

def pushover_send(title: str, message: str, priority: int = 1):
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "title": title,
                "message": message,
                "priority": priority,
                "retry": 30 if priority >= 2 else 0,
                "expire": 3600 if priority >= 2 else 0,
            },
            timeout=10,
        )
        wlog(f"Pushover sent: status={resp.status_code} title={title}")
    except Exception as e:
        wlog(f"Pushover error: {e}")

def tail_new_lines(filepath: str, last_pos: int) -> tuple[list[str], int]:
    """ファイルの追記分だけ読む"""
    try:
        size = os.path.getsize(filepath)
    except FileNotFoundError:
        return [], last_pos

    if size < last_pos:
        # ログローテート検知
        wlog("Log rotated, resetting position.")
        last_pos = 0

    if size == last_pos:
        return [], last_pos

    with open(filepath, "r", errors="replace") as f:
        f.seek(last_pos)
        new_lines = f.readlines()
        new_pos = f.tell()

    return new_lines, new_pos

def check_patterns(lines: list[str]):
    now = time.time()

    for line in lines:
        line = line.rstrip()
        for pattern, label in ALERT_PATTERNS:
            if pattern.search(line):
                q = pattern_times[label]
                q.append((now, line))

                # 5分より古いエントリを削除
                while q and now - q[0][0] > WINDOW_SECONDS:
                    q.popleft()

                count = len(q)
                wlog(f"Pattern [{label}] detected ({count}/{THRESHOLD}): {line[:120]}")

                if count >= THRESHOLD:
                    last_sent = last_alert_sent.get(label, 0)
                    if now - last_sent > ALERT_COOLDOWN:
                        last_alert_sent[label] = now
                        # 直近5件のログ行を抜粋
                        recent = [entry[1] for entry in list(q)[-5:]]
                        excerpt = "\n".join(recent)
                        msg = (
                            f"パターン: {label}\n"
                            f"5分以内に {count} 件検知\n\n"
                            f"直近ログ:\n{excerpt[:800]}"
                        )
                        pushover_send(f"[Atlas/WATCHDOG] {label}", msg, priority=1)

def main():
    wlog("=== atlas_watchdog started ===")
    wlog(f"監視対象: {LOG_PATH}")
    wlog(f"チェック間隔: {CHECK_INTERVAL}秒 / 閾値: {WINDOW_SECONDS}秒で{THRESHOLD}件")

    # 初回は現在のファイル末尾位置から開始 (過去ログはスキップ)
    try:
        last_pos = os.path.getsize(LOG_PATH)
        wlog(f"初期ファイルサイズ: {last_pos} bytes (過去ログスキップ)")
    except FileNotFoundError:
        last_pos = 0
        wlog(f"ログファイル未存在。作成を待機: {LOG_PATH}")

    # 起動通知
    pushover_send(
        "[Atlas/BUILDER] watchdog起動",
        f"atlas_watchdog.py 起動完了\n{LOG_PATH} 監視開始\n{CHECK_INTERVAL}秒間隔・{WINDOW_SECONDS}秒/{THRESHOLD}件で通知",
        priority=0,
    )

    while True:
        new_lines, last_pos = tail_new_lines(LOG_PATH, last_pos)
        if new_lines:
            check_patterns(new_lines)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
