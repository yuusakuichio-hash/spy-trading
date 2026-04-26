#!/usr/bin/env python3
"""migrate_ledger_auto_filter_20260425.py

user_instruction_ledger.jsonl の既存エントリを走査し、
auto-noise filter パターンにマッチするものを status="auto_filtered" に更新する
migration script。

--dry-run で実際には書き換えず、統計だけ出力。
--apply  で実際に書き換え (原子的 tmp -> rename)。

asyncio 禁止 (B16 遵守)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path("/Users/yuusakuichio/trading")
LEDGER = ROOT / "data" / "user_instruction_ledger.jsonl"
JST = timezone(timedelta(hours=9))

# --- ノイズパターン (user_prompt_ledger.py と同期維持) --------------------
_NOISE_PATTERNS: list[tuple[str, str]] = [
    ("<task-notification>", "startswith"),
    ("Sora Lab discipline checker", "contains"),
    ("Output formatter", "contains"),
    ("You are an LLM", "startswith"),
    ("# /loop", "startswith"),
    ("Stop hook feedback:", "startswith"),
]


def is_noise(text: str) -> bool:
    for pattern, mode in _NOISE_PATTERNS:
        if mode == "startswith" and text.startswith(pattern):
            return True
        if mode == "contains" and pattern in text:
            return True
    return False


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def load_ledger() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def rewrite_ledger(entries: list[dict[str, Any]]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    tmp.replace(LEDGER)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate ledger: set auto_filtered status")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="Show statistics without writing (default)")
    parser.add_argument("--apply", action="store_true", default=False,
                        help="Apply changes to ledger")
    args = parser.parse_args()

    if not args.apply:
        args.dry_run = True

    entries = load_ledger()
    total = len(entries)
    skipped_done = 0
    already_filtered = 0
    to_filter: list[str] = []
    pattern_counts: dict[str, int] = {}

    updated_entries: list[dict[str, Any]] = []

    for entry in entries:
        status = entry.get("status", "pending")
        # done / wontfix / deferred は変更しない
        if status in ("done", "wontfix", "deferred"):
            skipped_done += 1
            updated_entries.append(entry)
            continue
        # 既に auto_filtered のものはスキップ
        if status == "auto_filtered":
            already_filtered += 1
            updated_entries.append(entry)
            continue

        text = entry.get("exact_text", "")
        noise = False
        matched_pattern = ""
        for pattern, mode in _NOISE_PATTERNS:
            if mode == "startswith" and text.startswith(pattern):
                noise = True
                matched_pattern = pattern[:40]
                break
            if mode == "contains" and pattern in text:
                noise = True
                matched_pattern = pattern[:40]
                break

        if noise:
            to_filter.append(entry.get("instruction_id", "?"))
            pattern_counts[matched_pattern] = pattern_counts.get(matched_pattern, 0) + 1
            if not args.dry_run:
                new_entry = dict(entry)
                new_entry["status"] = "auto_filtered"
                old_notes = new_entry.get("notes") or ""
                tag = f"[auto_noise_filter migration 2026-04-25] matched={matched_pattern}"
                new_entry["notes"] = (old_notes + " | " + tag).lstrip(" | ")
                new_entry["verified_by"] = "migrate_ledger_auto_filter_20260425"
                new_entry["verified_at"] = now_jst_iso()
                updated_entries.append(new_entry)
            else:
                updated_entries.append(entry)
        else:
            updated_entries.append(entry)

    print(f"Migration statistics (dry_run={args.dry_run}):")
    print(f"  total entries       : {total}")
    print(f"  skipped (done/wontfix/deferred): {skipped_done}")
    print(f"  already auto_filtered: {already_filtered}")
    print(f"  to be filtered      : {len(to_filter)}")
    print(f"  pattern breakdown   :")
    for pat, cnt in sorted(pattern_counts.items(), key=lambda x: -x[1]):
        print(f"    '{pat}': {cnt}")

    if args.dry_run:
        print("\n[DRY RUN] No changes written. Use --apply to apply.")
        return 0

    rewrite_ledger(updated_entries)
    print(f"\n[APPLY] Updated {len(to_filter)} entries to auto_filtered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
