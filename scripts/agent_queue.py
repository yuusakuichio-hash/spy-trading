#!/usr/bin/env python3
"""Agent dispatch queue — 秘書ソラが dispatch 予定の agent をキューに登録・表示するユーティリティ。

設計:
  - data/agent_queue.jsonl に予定 agent を記録
  - 各エントリ: {kind, task, waits_for, added_ts}
  - ダッシュボード (sora_status_server.py) が読んで UPCOMING セクション表示

使い方:
  push:    python3 scripts/agent_queue.py push redteam "Redteam r5 最終敵対レビュー" --waits-for "Builder r5 完了"
  pop:     python3 scripts/agent_queue.py pop <kind>  # 先頭の該当 kind を 1 件 consume
  list:    python3 scripts/agent_queue.py list         # 現在のキュー表示
  clear:   python3 scripts/agent_queue.py clear        # 全削除
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = PROJECT_ROOT / "data" / "agent_queue.jsonl"


def _load() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        with QUEUE_FILE.open("r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_FILE.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def push(kind: str, task: str, waits_for: str = "") -> None:
    items = _load()
    items.append({
        "kind": kind,
        "task": task,
        "waits_for": waits_for,
        "added_ts": datetime.now(timezone.utc).isoformat(),
    })
    _save(items)
    print(f"[queue] pushed: {kind} — {task}")


def pop(kind: str) -> dict | None:
    items = _load()
    for i, item in enumerate(items):
        if item["kind"] == kind:
            removed = items.pop(i)
            _save(items)
            print(f"[queue] popped: {removed['kind']} — {removed['task']}")
            return removed
    print(f"[queue] not found: {kind}", file=sys.stderr)
    return None


def list_cmd() -> None:
    items = _load()
    if not items:
        print("[queue] empty")
        return
    for i, item in enumerate(items):
        waits = f" (waits: {item['waits_for']})" if item.get("waits_for") else ""
        print(f"  {i+1}. {item['kind']:12s} — {item['task']}{waits}")


def clear() -> None:
    _save([])
    print("[queue] cleared")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_push = sub.add_parser("push")
    p_push.add_argument("kind")
    p_push.add_argument("task")
    p_push.add_argument("--waits-for", default="")

    p_pop = sub.add_parser("pop")
    p_pop.add_argument("kind")

    sub.add_parser("list")
    sub.add_parser("clear")

    args = parser.parse_args()

    if args.cmd == "push":
        push(args.kind, args.task, args.waits_for)
    elif args.cmd == "pop":
        pop(args.kind)
    elif args.cmd == "list":
        list_cmd()
    elif args.cmd == "clear":
        clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
