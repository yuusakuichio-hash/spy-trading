#!/usr/bin/env python3
"""ledger_audit_run.py — 1h cron で pending_proposals.jsonl の auto mark-done を実行

pending_proposals.jsonl の status="proposed" エントリを走査し、
keyword 率 80% 以上かつ related_commit が存在する場合は auto done に更新。

1h launchd (com.soralab.ledger-auditor.plist) から呼ばれる想定。
手動実行も可: python3 scripts/ledger_audit_run.py

asyncio 禁止 (B16 遵守)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path("/Users/yuusakuichio/trading")
PROPOSALS = ROOT / "data" / "pending_proposals.jsonl"
LEDGER = ROOT / "data" / "user_instruction_ledger.jsonl"
LOG_DIR = ROOT / "data" / "logs"
JST = timezone(timedelta(hours=9))

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.]+|[぀-鿿]+")
_STOP_WORDS = {
    "を", "に", "で", "が", "は", "と", "の", "へ", "から", "より", "まで",
    "する", "した", "して", "します", "できる", "ある", "いる", "なる",
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "of", "for",
    "is", "are", "was", "be", "by", "with", "this", "that",
}
AUTO_DONE_KEYWORD_RATE = 0.80   # 80% 以上で auto done


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def rewrite_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    tmp.replace(path)


def extract_keywords(text: str, min_len: int = 3) -> list[str]:
    tokens = _TOKEN_RE.findall(text)
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        tl = t.lower()
        if len(tl) < min_len:
            continue
        if tl in _STOP_WORDS:
            continue
        if tl not in seen:
            seen.add(tl)
            out.append(tl)
    return out[:30]


def get_recent_commits(n: int = 20) -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", f"-{n}", "--pretty=format:%H %s"],
            capture_output=True, text=True, timeout=10
        )
        commits: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            commits.append({"hash": parts[0], "msg": parts[1] if len(parts) > 1 else ""})
        return commits
    except Exception:
        return []


def verify_commit_exists(commit_hash: str) -> bool:
    """commit hash が実在するか確認。"""
    if not commit_hash:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", commit_hash],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "ledger_audit_run.log"

    proposals = load_jsonl(PROPOSALS)
    ledger = load_jsonl(LEDGER)
    commits = get_recent_commits()
    commit_msgs = " ".join(c.get("msg", "") for c in commits)

    proposed = [p for p in proposals if p.get("status") == "proposed"]
    auto_done_ids: list[str] = []
    proposals_updated = list(proposals)

    for prop in proposed:
        inst_id = prop.get("instruction_id", "")
        text_snippet = prop.get("text_snippet", "")
        related_commit = prop.get("matched_commit", "")

        # 1. commit 実在確認
        if not verify_commit_exists(related_commit):
            continue

        # 2. keyword 率チェック
        keywords = extract_keywords(text_snippet)
        if not keywords:
            continue

        matched = [kw for kw in keywords if kw.lower() in commit_msgs.lower()]
        rate = len(matched) / len(keywords)
        if rate < AUTO_DONE_KEYWORD_RATE:
            continue

        # auto done に更新
        auto_done_ids.append(inst_id)
        for i, p in enumerate(proposals_updated):
            if p.get("instruction_id") == inst_id and p.get("status") == "proposed":
                updated = dict(p)
                updated["status"] = "auto_done"
                updated["auto_done_at"] = now_jst_iso()
                updated["auto_done_reason"] = (
                    f"keyword_rate={rate:.2f} matched={matched[:5]} commit={related_commit[:12]}"
                )
                proposals_updated[i] = updated

        # ledger 側の対応エントリも更新
        for i, e in enumerate(ledger):
            if e.get("instruction_id") == inst_id and e.get("status") == "pending":
                updated_e = dict(e)
                updated_e["status"] = "done"
                updated_e["verified_by"] = "ledger_audit_run"
                updated_e["verified_at"] = now_jst_iso()
                updated_e["related_commit"] = related_commit[:12]
                old_notes = updated_e.get("notes") or ""
                tag = f"[ledger_audit_run] keyword_rate={rate:.2f} commit={related_commit[:12]}"
                updated_e["notes"] = (old_notes + " | " + tag).lstrip(" | ")
                ledger[i] = updated_e

    if auto_done_ids:
        rewrite_jsonl(PROPOSALS, proposals_updated)
        rewrite_jsonl(LEDGER, ledger)

    ts = now_jst_iso()
    summary = (
        f"[{ts}] ledger_audit_run: "
        f"proposed={len(proposed)} auto_done={len(auto_done_ids)} "
        f"ids={auto_done_ids}"
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(summary + "\n")

    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
