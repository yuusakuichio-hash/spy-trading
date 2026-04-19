#!/usr/bin/env python3
"""
check_violation_repetition.py
violation_registry.jsonl を読み込み、繰り返し違反の状態をレポートする。
同一パターン2回以上: Pushover CRITICAL
同一パターン3回以上: session_halt_flag.txt に書き込み
"""
import json, os, sys, subprocess
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
REGISTRY_PATH = f"{BASE}/data/violation_registry.jsonl"
HALT_FLAG_PATH = f"{BASE}/data/session_halt_flag.txt"
LOG_PATH = f"{BASE}/data/logs/discipline_violations.log"
JST = timezone(timedelta(hours=9))

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")

CRED_PATHS = [
    f"{BASE}/.claude/skills/credentials.md",
    os.path.expanduser("~/.claude/agents/credentials.md"),
]
for cp in CRED_PATHS:
    if os.path.exists(cp):
        try:
            import re
            with open(cp) as f:
                ctext = f.read()
            m = re.search(r"PUSHOVER_(?:API_)?TOKEN[:\\s]+([a-zA-Z0-9_-]+)", ctext)
            if m and not PUSHOVER_TOKEN:
                PUSHOVER_TOKEN = m.group(1).strip()
            m2 = re.search(r"PUSHOVER_USER(?:_KEY)?[:\\s]+([a-zA-Z0-9_-]+)", ctext)
            if m2 and not PUSHOVER_USER:
                PUSHOVER_USER = m2.group(1).strip()
        except Exception:
            pass
    if PUSHOVER_TOKEN and PUSHOVER_USER:
        break

def log_event(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    with open(LOG_PATH, "a") as f:
        f.write(f"[VIOLATION_REP] {ts} {msg}\n")

def load_jsonl(path):
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries

def send_pushover(title, message, priority=1):
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        return False
    try:
        cmd = [
            "curl", "-s", "-X", "POST",
            "https://api.pushover.net/1/messages.json",
            "-d", f"token={PUSHOVER_TOKEN}",
            "-d", f"user={PUSHOVER_USER}",
            "-d", f"title={title}",
            "-d", f"message={message}",
            "-d", f"priority={priority}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except Exception:
        return False

def main():
    registry = load_jsonl(REGISTRY_PATH)
    if not registry:
        print("No violations in registry.")
        return

    critical_found = False
    halt_needed = False

    for e in registry:
        count = e.get("occurrence_count", 1)
        title = e.get("title", "?")

        if count >= 3:
            halt_needed = True
            critical_found = True
            msg = f"同一違反 {count} 回: {title}"
            print(f"[CRITICAL x{count}] {msg}")
            log_event(f"TRIPLE_VIOLATION: {msg}")
            send_pushover(
                f"[CRITICAL] REPEATED VIOLATION x{count}",
                f"同一パターンが {count} 回繰り返されています。\n{title}\nセッション終了ブロック発動。",
                priority=2
            )
        elif count >= 2:
            critical_found = True
            msg = f"同一違反 {count} 回: {title}"
            print(f"[CRITICAL x{count}] {msg}")
            log_event(f"DOUBLE_VIOLATION: {msg}")
            send_pushover(
                f"[CRITICAL] REPEATED VIOLATION x{count}",
                f"同一パターンが {count} 回繰り返されています。\n{title}\n次回で自動ブロック発動。",
                priority=2
            )
        else:
            print(f"[VIOLATION x{count}] {title}")

    if halt_needed:
        ts = datetime.now(JST).isoformat()
        os.makedirs(os.path.dirname(HALT_FLAG_PATH), exist_ok=True)
        with open(HALT_FLAG_PATH, "w") as f:
            f.write(f"HALT at {ts}\n")
            for e in registry:
                if e.get("occurrence_count", 1) >= 3:
                    f.write(f"  {e.get('title','?')}: {e.get('occurrence_count','?')}回\n")
        print(f"[HALT FLAG] Written to {HALT_FLAG_PATH}")
        log_event(f"HALT_FLAG_WRITTEN")

if __name__ == "__main__":
    main()
