#!/usr/bin/env python3
"""
Stop hook: stop_pending_check
セッション終了前に pending_completions.jsonl に未解決エントリがあれば stderr に警告。
同一パターン 3回以上の繰り返し違反がある場合のみ exit 2 でブロック。
"""
import sys, json, os
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
PENDING_PATH = f"{BASE}/data/pending_completions.jsonl"
REGISTRY_PATH = f"{BASE}/data/violation_registry.jsonl"
LOG_PATH = f"{BASE}/data/logs/discipline_violations.log"
JST = timezone(timedelta(hours=9))

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

def log_event(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    with open(LOG_PATH, "a") as f:
        f.write(f"[STOP_CHECK] {ts} {msg}\n")

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

pending = load_jsonl(PENDING_PATH)
unresolved = [e for e in pending if not e.get("resolved", False)]

registry = load_jsonl(REGISTRY_PATH)
repeated = [e for e in registry if e.get("occurrence_count", 1) >= 2]

if not unresolved and not repeated:
    sys.exit(0)

sys.stderr.write("\n[STOP_CHECK] === セッション終了前チェック ===\n")

if unresolved:
    sys.stderr.write(f"[STOP_CHECK] 未実装 pending: {len(unresolved)} 件\n")
    for e in unresolved:
        title = e.get("title", e.get("memory_path", "?"))
        deadline = e.get("deadline_ts", "?")
        sys.stderr.write(f"[STOP_CHECK]   - {title} (deadline: {deadline})\n")
    sys.stderr.write("[STOP_CHECK] メモリ保存だけでは完了ではない。コードをcommitすること。\n")

if repeated:
    sys.stderr.write(f"[STOP_CHECK] 繰り返し違反: {len(repeated)} 件\n")
    for e in repeated:
        count = e.get("occurrence_count", "?")
        title = e.get("title", "?")
        sys.stderr.write(f"[STOP_CHECK]   - {title}: {count}回目\n")

sys.stderr.write("\n")
log_event(f"SESSION_END_CHECK: unresolved={len(unresolved)} repeated={len(repeated)}")

max_count = max((e.get("occurrence_count", 1) for e in repeated), default=0)
if max_count >= 3:
    sys.stderr.write(f"[STOP_CHECK] BLOCK: 同一違反が {max_count} 回繰り返されています。\n")
    sys.stderr.write(f"[STOP_CHECK] scripts/mark_completion.py で実装コミットを証明してから終了してください。\n\n")
    sys.exit(2)

sys.exit(0)
