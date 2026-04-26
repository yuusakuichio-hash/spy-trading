#!/usr/bin/env python3
"""
PostToolUse hook: memory_completion_tracker
Write ツールで違反関連メモリファイルを作成した場合、pending_completions.jsonl に登録する。
30分以内に対応コード commit がなければ Pushover priority=1 で通知。
"""
import sys, json, os, hashlib, re
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
PENDING_PATH = f"{BASE}/data/pending_completions.jsonl"
PATTERNS_PATH = f"{BASE}/data/violation_patterns.json"
LOG_PATH = f"{BASE}/data/logs/discipline_violations.log"
JST = timezone(timedelta(hours=9))


def load_deadline_minutes(now):
    """時間帯別deadlineをviolation_patterns.jsonから取得。
    場中 22:30-05:00 JST は10分 / 場外 05:00-22:30 は20分 /
    メンテ 06:00-07:00 は60分 / 週末 土06:00-月07:00 は120分。
    2026-04-20 analyst推奨（data/pending_threshold_optimization_20260420.md）。
    """
    try:
        with open(PATTERNS_PATH, "r") as f:
            patterns = json.load(f)
        cfg = patterns.get("memory_as_completion", {})
        weekday = now.weekday()
        h, m = now.hour, now.minute
        if weekday == 5 and (h, m) >= (6, 0):
            return cfg.get("deadline_minutes_weekend", 120)
        if weekday == 6:
            return cfg.get("deadline_minutes_weekend", 120)
        if weekday == 0 and (h, m) < (7, 0):
            return cfg.get("deadline_minutes_weekend", 120)
        if (h, m) >= (6, 0) and (h, m) < (7, 0):
            return cfg.get("deadline_minutes_maintenance", 60)
        if (h, m) >= (22, 30) or (h, m) < (5, 0):
            return cfg.get("deadline_minutes_market_hours", 10)
        return cfg.get("deadline_minutes_daytime", 20)
    except Exception:
        return 30

TRIGGER_PATTERNS = [
    r"memory/feedback_.*violations?.*\.md",
    r"memory/feedback_.*_gap.*\.md",
    r"memory/feedback_.*_lessons.*\.md",
    r"memory/feedback_.*_failure.*\.md",
    r"memory/feedback_.*_fix.*\.md",
    r"memory/project_.*violations?.*\.md",
    r"memory/project_.*_gap.*\.md",
    r"memory/project_.*_failure.*\.md",
    r"memory/project_.*_fix.*\.md",
    r"memory/project_.*_bug.*\.md",
    r"memory/feedback_.*_bias.*\.md",
    r"memory/feedback_.*_mistake.*\.md",
    r"memory/project_.*_redteam.*\.md",
    r"memory/project_.*_critical.*\.md",
]

def should_trigger(file_path):
    for pat in TRIGGER_PATTERNS:
        if re.search(pat, file_path):
            return True
    return False

def compute_fingerprint(file_path, content=""):
    raw = f"{os.path.basename(file_path)}:{content[:500]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def load_pending():
    if not os.path.exists(PENDING_PATH):
        return []
    entries = []
    with open(PENDING_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries

def append_pending(entry):
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    with open(PENDING_PATH, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def log_event(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    with open(LOG_PATH, "a") as f:
        f.write(f"[MEMORY_TRACKER] {ts} {msg}\n")

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_name = data.get("tool_name", "")
tool_input = data.get("tool_input", {})

if tool_name != "Write":
    sys.exit(0)

file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
content = tool_input.get("content", "") if isinstance(tool_input, dict) else ""

if not should_trigger(file_path):
    sys.exit(0)

now = datetime.now(JST)
deadline_min = load_deadline_minutes(now)
deadline = now + timedelta(minutes=deadline_min)
fingerprint = compute_fingerprint(file_path, content)

existing = load_pending()
for e in existing:
    if e.get("memory_path") == file_path and not e.get("resolved", False):
        sys.exit(0)

entry = {
    "ts": now.isoformat(),
    "memory_path": file_path,
    "fingerprint": fingerprint,
    "deadline_ts": deadline.isoformat(),
    "resolved": False,
    "title": os.path.basename(file_path),
}

append_pending(entry)
log_event(f"PENDING_REGISTERED: {os.path.basename(file_path)} deadline={deadline.strftime('%H:%M JST')}")

sys.stderr.write(f"\n[MEMORY_TRACKER] 違反メモリ登録: {os.path.basename(file_path)}\n")
sys.stderr.write(f"[MEMORY_TRACKER] deadline: {deadline.strftime('%Y-%m-%d %H:%M JST')}\n")
sys.stderr.write(f"[MEMORY_TRACKER] {deadline_min}分以内に対応コードをcommitしないとPushover[ALERT]が発火する。\n")
sys.stderr.write(f"[MEMORY_TRACKER] メモリ保存=対策完了ではない。コード実装まで完了とみなすな。\n\n")

sys.exit(0)
