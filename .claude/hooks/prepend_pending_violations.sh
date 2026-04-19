#!/usr/bin/env python3
"""
UserPromptSubmit hook: prepend_pending_violations
ユーザーが新規プロンプトを入力するたびに未解決 pending と繰り返し違反を先頭に注入する。
Claude main が無視できない構造にする。
"""
import sys, json, os
from datetime import datetime, timezone, timedelta

BASE = "/Users/yuusakuichio/trading"
PENDING_PATH = f"{BASE}/data/pending_completions.jsonl"
REGISTRY_PATH = f"{BASE}/data/violation_registry.jsonl"
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

lines = ["", "=== PENDING VIOLATIONS REMINDER ==="]

if unresolved:
    lines.append(f"Pending memory-as-completion: {len(unresolved)}件（未実装）")
    for i, e in enumerate(unresolved[:5], 1):
        title = e.get("title", e.get("memory_path", "?"))
        deadline = e.get("deadline_ts", "?")
        lines.append(f"  {i}. {title} (deadline: {deadline})")
    if len(unresolved) > 5:
        lines.append(f"  ... 他 {len(unresolved)-5} 件")

if repeated:
    lines.append(f"Repeated violations (2回以上): {len(repeated)}件")
    for i, e in enumerate(repeated[:5], 1):
        count = e.get("occurrence_count", "?")
        title = e.get("title", "?")
        lines.append(f"  {i}. {title} x {count}回")

lines.append("メモリ保存=完了ではない。上記はコード実装commitまで解消されない。")
lines.append("===================================")
lines.append("")

injection = "\n".join(lines)

print(json.dumps({"additionalSystemPrompt": injection}))
sys.exit(0)
