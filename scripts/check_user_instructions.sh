#!/usr/bin/env python3
# check_user_instructions.sh — user_instruction_ledger.jsonl 操作 CLI
#
# 使い方:
#   scripts/check_user_instructions.sh               # pending+in_progress 最新20件
#   scripts/check_user_instructions.sh --all         # 全件
#   scripts/check_user_instructions.sh --mark-done <id> <commit_hash>
#   scripts/check_user_instructions.sh --verify <id>
#   scripts/check_user_instructions.sh --stats
#
# asyncio 禁止 (B16 遵守) — 同期 I/O のみ

"""
This file is actually a Python script despite the .sh extension
(shebang line above handles execution).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path("/Users/yuusakuichio/trading")
LEDGER = ROOT / "data" / "user_instruction_ledger.jsonl"
JST = timezone(timedelta(hours=9))

STATUS_COLORS = {
    "pending": "\033[33m",      # yellow
    "in_progress": "\033[36m",  # cyan
    "done": "\033[32m",         # green
    "deferred": "\033[90m",     # gray
    "wontfix": "\033[90m",      # gray
}
RESET = "\033[0m"


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def load_ledger() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def save_ledger(entries: list[dict[str, Any]]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def find_entry(entries: list[dict[str, Any]], id_prefix: str) -> dict[str, Any] | None:
    for e in entries:
        if e.get("instruction_id", "").startswith(id_prefix):
            return e
    return None


def colorize(status: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    color = STATUS_COLORS.get(status, "")
    return f"{color}{text}{RESET}"


def fmt_entry(e: dict[str, Any], index: int) -> str:
    status = e.get("status", "?")
    iid = e.get("instruction_id", "?")[:12]
    ts = e.get("timestamp", "?")[:16]
    action = e.get("parsed_action", "?")
    priority = e.get("priority", "?")
    text = e.get("exact_text", "")[:80].replace("\n", " ")
    commit = e.get("related_commit") or "-"
    task = e.get("related_task_id") or "-"
    status_str = colorize(status, f"[{status}]")
    return (
        f"  {index:>3}. {status_str} id={iid} ts={ts}\n"
        f"       action={action} priority={priority}\n"
        f"       text={text!r}\n"
        f"       commit={commit} task={task}"
    )


# --- サブコマンド -----------------------------------------------------------

def cmd_list(args: list[str]) -> None:
    show_all = "--all" in args
    entries = load_ledger()
    if show_all:
        targets = entries[-50:]
        label = f"全件 (最新50件 / total={len(entries)})"
    else:
        targets = [e for e in entries if e.get("status") in ("pending", "in_progress")]
        targets = targets[-20:]
        label = f"pending + in_progress (最新20件 / total={len(targets)})"

    print(f"\n=== user_instruction_ledger: {label} ===\n")
    if not targets:
        print("  (件なし)")
        return
    for i, e in enumerate(targets, 1):
        print(fmt_entry(e, i))
    print()


def cmd_mark_done(id_prefix: str, commit_hash: str) -> None:
    entries = load_ledger()
    entry = find_entry(entries, id_prefix)
    if entry is None:
        print(f"ERROR: id={id_prefix!r} not found", file=sys.stderr)
        sys.exit(1)
    entry["status"] = "done"
    entry["related_commit"] = commit_hash
    entry["verified_by"] = "manual"
    entry["verified_at"] = now_jst_iso()
    save_ledger(entries)
    print(f"OK: {entry['instruction_id']} marked done (commit={commit_hash})")


def cmd_verify(id_prefix: str) -> None:
    entries = load_ledger()
    entry = find_entry(entries, id_prefix)
    if entry is None:
        print(f"ERROR: id={id_prefix!r} not found", file=sys.stderr)
        sys.exit(1)

    print(f"\n=== verify: {entry['instruction_id']} ===")
    print(f"  status       : {entry.get('status')}")
    print(f"  text         : {entry.get('exact_text','')[:100]!r}")
    print(f"  related_commit: {entry.get('related_commit')}")
    print(f"  related_task_id: {entry.get('related_task_id')}")
    print(f"  verified_by  : {entry.get('verified_by')}")
    print(f"  verified_at  : {entry.get('verified_at')}")

    commit = entry.get("related_commit")
    if commit:
        print(f"\n--- git log for commit {commit} ---")
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--oneline", "-1", commit],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"  {result.stdout.strip()}")
        else:
            print(f"  (git error: {result.stderr.strip()[:100]})")

    task = entry.get("related_task_id")
    if task:
        print(f"\n--- related_task_id: {task} ---")
        # agent_tasks.json / agent_queue.jsonl を簡易検索
        tasks_path = ROOT / "data" / "agent_tasks.json"
        if tasks_path.exists():
            try:
                tasks = json.loads(tasks_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(tasks, list):
                    for t in tasks:
                        if str(t.get("id", "")) == str(task) or str(t.get("task_id", "")) == str(task):
                            print(f"  found in agent_tasks.json: {json.dumps(t, ensure_ascii=False)[:200]}")
            except Exception:
                pass
    print()


def cmd_stats() -> None:
    entries = load_ledger()
    if not entries:
        print("(ledger empty)")
        return

    from collections import Counter
    status_cnt: Counter[str] = Counter()
    action_cnt: Counter[str] = Counter()
    priority_cnt: Counter[str] = Counter()
    for e in entries:
        status_cnt[e.get("status", "?")] += 1
        action_cnt[e.get("parsed_action", "?")] += 1
        priority_cnt[e.get("priority", "?")] += 1

    print(f"\n=== user_instruction_ledger stats (total={len(entries)}) ===\n")
    print("status:")
    for k, v in sorted(status_cnt.items(), key=lambda x: -x[1]):
        bar = "#" * min(v, 30)
        print(f"  {k:<12} {v:>4}  {bar}")
    print("\nparsed_action:")
    for k, v in sorted(action_cnt.items(), key=lambda x: -x[1]):
        bar = "#" * min(v, 30)
        print(f"  {k:<12} {v:>4}  {bar}")
    print("\npriority:")
    for k, v in sorted(priority_cnt.items(), key=lambda x: -x[1]):
        bar = "#" * min(v, 30)
        print(f"  {k:<12} {v:>4}  {bar}")
    print()


# --- entrypoint ------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    if "--mark-done" in args:
        idx = args.index("--mark-done")
        if len(args) < idx + 3:
            print("Usage: check_user_instructions.sh --mark-done <id> <commit_hash>", file=sys.stderr)
            sys.exit(1)
        cmd_mark_done(args[idx + 1], args[idx + 2])
        return

    if "--verify" in args:
        idx = args.index("--verify")
        if len(args) < idx + 2:
            print("Usage: check_user_instructions.sh --verify <id>", file=sys.stderr)
            sys.exit(1)
        cmd_verify(args[idx + 1])
        return

    if "--stats" in args:
        cmd_stats()
        return

    # デフォルト: pending + in_progress 一覧
    cmd_list(args)


if __name__ == "__main__":
    main()
