#!/usr/bin/env python3
"""session_end_interactive_propose.py — Stop hook

session 終了時に:
1. pending > 10 件 && 今 session で対応済候補 >= 3 件で warn
2. candidate を data/pending_proposals.jsonl に記録
3. 次 session 冒頭で bulk mark-done できるよう保持
"""
from __future__ import annotations

import json
import datetime as _dt
import subprocess
import sys
from pathlib import Path

LEDGER = Path("/Users/yuusakuichio/trading/data/user_instruction_ledger.jsonl")
PROPOSALS = Path("/Users/yuusakuichio/trading/data/pending_proposals.jsonl")
THRESHOLD_PENDING = 10
THRESHOLD_CANDIDATES = 3
SESSION_HOURS = 2  # 直近 N 時間の対応候補対象


def _load_entries() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text(encoding='utf-8').splitlines() if l.strip()]


def _recent_commits_in_session(hours: int = SESSION_HOURS) -> list[tuple[str, str]]:
    """直近 N 時間の commit list (hash, msg)。"""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={hours} hours ago", "--pretty=%H|%s"],
            cwd="/Users/yuusakuichio/trading",
            capture_output=True, text=True, timeout=5,
        )
        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2:
                commits.append((parts[0][:12], parts[1]))
        return commits
    except Exception:
        return []


def _match_candidates(entries: list[dict], commits: list[tuple[str, str]]) -> list[dict]:
    """pending の text に含まれる keyword と commit msg の keyword マッチで候補抽出。"""
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9)))
    cutoff = now - _dt.timedelta(hours=SESSION_HOURS)
    candidates = []
    for e in entries:
        if e.get("status") != "pending":
            continue
        try:
            ts = _dt.datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
            if ts < cutoff:
                continue
        except Exception:
            continue
        text_lower = e["exact_text"].lower()
        # keyword マッチ: text の最初の 30 文字に含まれる単語と commit msg 照合
        text_keywords = set()
        for word in text_lower.split():
            if len(word) >= 3 and word.isascii() and word.isalpha():
                text_keywords.add(word)
        for h, msg in commits:
            msg_lower = msg.lower()
            match_count = sum(1 for kw in text_keywords if kw in msg_lower)
            if match_count >= 2:
                candidates.append({
                    "instruction_id": e["instruction_id"],
                    "text_snippet": e["exact_text"][:80],
                    "matched_commit": h,
                    "matched_commit_msg": msg[:60],
                    "match_count": match_count,
                })
                break
    return candidates


def main() -> int:
    entries = _load_entries()
    pending = [e for e in entries if e.get("status") == "pending"]
    if len(pending) < THRESHOLD_PENDING:
        return 0

    commits = _recent_commits_in_session(SESSION_HOURS)
    candidates = _match_candidates(entries, commits)

    if len(candidates) < THRESHOLD_CANDIDATES:
        return 0

    # propose に記録
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))).isoformat(timespec="seconds")
    with open(PROPOSALS, "a", encoding="utf-8") as f:
        for c in candidates:
            c["proposed_at"] = now
            c["status"] = "proposed"
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # warn
    sys.stderr.write(
        f"\n[INSTRUCTION LEDGER] pending={len(pending)}・session 内 mark-done 候補 {len(candidates)} 件\n"
    )
    sys.stderr.write(f"[INSTRUCTION LEDGER] proposals 記録先: {PROPOSALS}\n")
    sys.stderr.write(f"[INSTRUCTION LEDGER] 次 session 冒頭で確認を推奨\n")
    for c in candidates[:5]:
        sys.stderr.write(
            f"  candidate: {c['instruction_id']} ↔ commit {c['matched_commit']} "
            f"({c['match_count']} keyword match)\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
