#!/usr/bin/env python3
"""session_end_instruction_check.sh — Stop hook

セッション終了時に user_instruction_ledger.jsonl の pending 件数を警告する。
block (exit 2) はしない — informational のみ。

asyncio 禁止 (B16 遵守)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path("/Users/yuusakuichio/trading")
LEDGER = ROOT / "data" / "user_instruction_ledger.jsonl"
JST = timezone(timedelta(hours=9))


def load_entries() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def main() -> int:
    if os.environ.get("INSTRUCTION_CHECK_BYPASS") == "1":
        return 0

    try:
        sys.stdin.read()
    except Exception:
        pass

    entries = load_entries()
    pending = [e for e in entries if e.get("status") in ("pending", "in_progress")]

    if not pending:
        return 0

    high = [e for e in pending if e.get("priority") == "high"]

    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    sys.stderr.write(f"\n[INSTRUCTION_CHECK] {ts}\n")
    sys.stderr.write(
        f"[INSTRUCTION_CHECK] user_instruction_ledger: pending={len(pending)} "
        f"(high={len(high)})\n"
    )

    recent = sorted(pending, key=lambda e: e.get("timestamp", ""), reverse=True)[:5]
    for e in recent:
        iid = e.get("instruction_id", "?")[:12]
        action = e.get("parsed_action", "?")
        priority = e.get("priority", "?")
        text = e.get("exact_text", "")[:60].replace("\n", " ")
        sys.stderr.write(
            f"[INSTRUCTION_CHECK]   id={iid} [{priority}] {action}: {text!r}\n"
        )

    if len(pending) > 5:
        sys.stderr.write(f"[INSTRUCTION_CHECK]   ... 他 {len(pending)-5} 件\n")

    sys.stderr.write(
        "[INSTRUCTION_CHECK] 上記は実装完了まで未解決のまま。"
        "scripts/check_user_instructions.sh --mark-done <id> <commit> で更新。\n\n"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
