#!/usr/bin/env python3
"""backfill_user_instructions_20260425.sh
~/.claude/projects/-Users-yuusakuichio-trading/*.jsonl から
USER role メッセージを抽出して user_instruction_ledger.jsonl に初期登録する。

- 重複 instruction_id はスキップ
- デフォルト: 最新 5 ファイル × 各最大 300 件 → 合計 20+ 件を目標
- --all-files フラグで全ファイルスキャン

asyncio 禁止 (B16 遵守)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path("/Users/yuusakuichio/trading")
LEDGER = ROOT / "data" / "user_instruction_ledger.jsonl"
SESSIONS_DIR = Path("/Users/yuusakuichio/.claude/projects/-Users-yuusakuichio-trading")
JST = timezone(timedelta(hours=9))

# --- 分類 (user_prompt_ledger.py と同一ロジック) ----------------------------
_QUESTION_RE = re.compile(
    r"(?:なぜ|なに|何|どう|どこ|いつ|誰|なん|どれ|どの|教えて|わかる|確認|？|\?)", re.IGNORECASE
)
_CORRECTION_RE = re.compile(
    r"(?:違う|ちがう|間違|まちが|直して|直す|修正|訂正|そうじゃない|違います)", re.IGNORECASE
)
_FEEDBACK_RE = re.compile(
    r"(?:いい|よい|ダメ|だめ|良い|悪い|問題|指摘|叱|怒|残念|感謝|ありがとう|よくない|最悪|最高)",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(
    r"(?:進めていい|やっていい|確認して|承認|問題ない|OKです|OKでしょうか|よろしい)", re.IGNORECASE
)


def classify_action(text: str) -> str:
    if _CORRECTION_RE.search(text):
        return "訂正"
    if _CONFIRM_RE.search(text):
        return "確認要求"
    if _QUESTION_RE.search(text):
        return "質問"
    if _FEEDBACK_RE.search(text):
        return "フィードバック"
    return "指示"


def infer_priority(text: str, action: str) -> str:
    if action in ("訂正", "フィードバック"):
        return "high"
    if action == "確認要求":
        return "low"
    if action == "質問":
        return "medium"
    urgent = re.compile(r"(?:すぐ|今すぐ|急いで|至急|緊急|最優先)", re.IGNORECASE)
    if urgent.search(text):
        return "high"
    return "medium"


def make_id(timestamp: str, text: str) -> str:
    raw = (timestamp + text).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def load_existing_ids() -> set[str]:
    if not LEDGER.exists():
        return set()
    ids: set[str] = set()
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if iid := e.get("instruction_id"):
                ids.add(iid)
        except json.JSONDecodeError:
            continue
    return ids


def append_entries(entries: list[dict[str, Any]]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def extract_user_messages_from_file(path: Path, max_per_file: int = 300) -> list[tuple[str, str]]:
    """(timestamp, text) のリストを返す。"""
    results: list[tuple[str, str]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(results) >= max_per_file:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if ev.get("type") != "user":
                    continue

                ts = ev.get("timestamp", "")
                msg = ev.get("message", {})
                content = msg.get("content", [])

                texts: list[str] = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            texts.append(part.get("text", ""))
                        elif isinstance(part, str):
                            texts.append(part)

                combined = " ".join(texts).strip()
                # tool_result だけのメッセージは無視 (中身のテキストがない)
                if len(combined) < 5:
                    continue
                # hook injection / system messages / task-notification は除外
                if (
                    combined.startswith("=== ")
                    or combined.startswith("[SESSION")
                    or combined.startswith("<task-notification>")
                    or combined.startswith("<system-reminder>")
                ):
                    continue

                results.append((ts, combined))
    except Exception:
        pass
    return results


def main() -> None:
    all_files = "--all-files" in sys.argv

    jsonl_files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jsonl_files:
        print(f"ERROR: no .jsonl files in {SESSIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    files_to_scan = jsonl_files if all_files else jsonl_files[:5]
    print(f"scanning {len(files_to_scan)} file(s) ...", flush=True)

    existing_ids = load_existing_ids()
    print(f"existing ledger entries: {len(existing_ids)}", flush=True)

    new_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set(existing_ids)

    for fpath in files_to_scan:
        msgs = extract_user_messages_from_file(fpath)
        for ts, text in msgs:
            iid = make_id(ts, text)
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            action = classify_action(text)
            priority = infer_priority(text, action)
            new_entries.append({
                "instruction_id": iid,
                "timestamp": ts,
                "exact_text": text[:2000],
                "parsed_action": action,
                "status": "pending",
                "related_task_id": None,
                "related_commit": None,
                "verified_by": None,
                "verified_at": None,
                "priority": priority,
                "notes": "backfill_20260425",
            })

    if not new_entries:
        print("no new entries to add (all already in ledger)")
        return

    append_entries(new_entries)
    print(f"backfill complete: added {len(new_entries)} entries")

    # stats
    from collections import Counter
    action_cnt: Counter[str] = Counter(e["parsed_action"] for e in new_entries)
    priority_cnt: Counter[str] = Counter(e["priority"] for e in new_entries)
    print("\naction breakdown:")
    for k, v in sorted(action_cnt.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print("priority breakdown:")
    for k, v in sorted(priority_cnt.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print(f"\ntotal ledger size: {len(existing_ids) + len(new_entries)} entries")


if __name__ == "__main__":
    main()
