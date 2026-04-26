#!/usr/bin/env python3
"""auto_mark_done_on_stop.py — Stop hook

session 内で touch されたファイル / tool_use 履歴 / 最新 N commit msg を scan し、
user_instruction_ledger.jsonl の pending エントリ（直近 2h 以内）と照合して
confidence 閾値超のものを自動 mark-done する。

設計方針:
  - keyword >= 2 一致 OR commit hash/INST-id 一致 → auto mark-done（confidence: high）
  - keyword 1 一致 → low-confidence: stderr に候補リスト出力のみ
  - 変更は ledger の status / verified_by / verified_at / notes を更新（JSONL 全行書き直し）
  - B16 遵守: asyncio 禁止・同期 I/O のみ

スキーマ維持: user_prompt_ledger.py の既存フィールドを変更しない。
  追加フィールド: notes に "[auto_mark_done] ..." を追記。

制御:
  AUTO_MARK_DONE_BYPASS=1 で無効化
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

# ---------------------------------------------------------------------------
ROOT = Path("/Users/yuusakuichio/trading")
LEDGER = ROOT / "data" / "user_instruction_ledger.jsonl"
LOG_DIR = ROOT / "data" / "logs"
JST = timezone(timedelta(hours=9))

BYPASS_ENV = "AUTO_MARK_DONE_BYPASS"
PENDING_WINDOW_H = 2        # 直近 2h 以内の pending が対象
RECENT_COMMITS = 10         # git log 取得件数
HIGH_CONF_THRESHOLD = 2     # keyword 一致数しきい値（以上で auto mark-done）
INST_ID_RE = re.compile(r"INST-([0-9a-f]{12})", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.]+|[぀-鿿]+")
_STOP_WORDS = {
    "を", "に", "で", "が", "は", "と", "の", "へ", "から", "より", "まで",
    "する", "した", "して", "します", "できる", "ある", "いる", "なる",
    "the", "a", "an", "and", "or", "in", "on", "at", "to", "of", "for",
    "is", "are", "was", "be", "by", "with", "this", "that",
}


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def now_jst() -> datetime:
    return datetime.now(JST)


def now_jst_iso() -> str:
    return now_jst().isoformat(timespec="seconds")


def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

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
    """ledger 全行を上書き。原子性: tmp -> rename。"""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    tmp.replace(LEDGER)


# ---------------------------------------------------------------------------
# pending entry 抽出（直近 2h 以内）
# ---------------------------------------------------------------------------

def get_recent_pending(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = now_jst() - timedelta(hours=PENDING_WINDOW_H)
    result: list[dict[str, Any]] = []
    for e in entries:
        if e.get("status") != "pending":
            continue
        ts_raw = e.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=JST)
            if ts >= cutoff:
                result.append(e)
        except (ValueError, TypeError):
            continue
    return result


# ---------------------------------------------------------------------------
# Transcript 解析
# ---------------------------------------------------------------------------

def read_transcript(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def extract_touched_files(events: list[dict[str, Any]]) -> list[str]:
    """Write/Edit/NotebookEdit tool_use から file_path を収集。"""
    paths: list[str] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for b in ev.get("message", {}).get("content", []) or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name", "")
            inp = b.get("input", {}) or {}
            if name in ("Write", "Edit", "NotebookEdit"):
                fp = inp.get("file_path") or inp.get("path") or ""
                if fp:
                    paths.append(str(fp))
    return paths


def extract_tool_texts(events: list[dict[str, Any]]) -> list[str]:
    """全 tool_use の input text を concat して返す（keyword 照合に使用）。"""
    parts: list[str] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for b in ev.get("message", {}).get("content", []) or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            inp = b.get("input", {}) or {}
            for v in inp.values():
                if isinstance(v, str):
                    parts.append(v)
    return parts


def extract_assistant_texts(events: list[dict[str, Any]]) -> list[str]:
    """全 assistant text block を返す。"""
    parts: list[str] = []
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for b in ev.get("message", {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text", "")
                if t:
                    parts.append(t)
    return parts


def extract_inst_ids_from_events(events: list[dict[str, Any]]) -> set[str]:
    """transcript 全体から INST-<12hex> を収集。"""
    found: set[str] = set()
    for ev in events:
        for b in (ev.get("message", {}).get("content", []) or []):
            if isinstance(b, dict):
                for val in b.values():
                    if isinstance(val, str):
                        for m in INST_ID_RE.finditer(val):
                            found.add(m.group(1).lower())
    return found


# ---------------------------------------------------------------------------
# git commit scan
# ---------------------------------------------------------------------------

def get_recent_commit_info(n: int = RECENT_COMMITS) -> list[dict[str, str]]:
    """直近 n commit の {hash, msg} を返す。失敗時は []。"""
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
            commits.append({
                "hash": parts[0],
                "msg": parts[1] if len(parts) > 1 else "",
            })
        return commits
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def extract_keywords(text: str, min_len: int = 3) -> list[str]:
    """instruction text からキーワードリストを作成（重複除去）。"""
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


def count_keyword_matches(keywords: list[str], corpus: str) -> tuple[int, list[str]]:
    """corpus 中に何キーワード出現するか。(count, matched_list) を返す。"""
    corpus_lower = corpus.lower()
    matched: list[str] = []
    for kw in keywords:
        if kw in corpus_lower:
            matched.append(kw)
    return len(matched), matched


# ---------------------------------------------------------------------------
# Mark-done logic
# ---------------------------------------------------------------------------

def build_corpus(
    touched_files: list[str],
    tool_texts: list[str],
    assistant_texts: list[str],
    commits: list[dict[str, str]],
) -> str:
    """照合用の大コーパスを結合。"""
    parts: list[str] = []
    parts.extend(touched_files)
    parts.extend(tool_texts)
    parts.extend(assistant_texts)
    for c in commits:
        parts.append(c.get("msg", ""))
    return " ".join(parts)


def mark_done_entry(
    entry: dict[str, Any],
    reason: str,
    confidence: str,
    related_commit: str | None = None,
) -> dict[str, Any]:
    """entry を更新して返す（元 dict を変更しない）。"""
    updated = dict(entry)
    updated["status"] = "done"
    updated["verified_by"] = "auto_mark_done_on_stop"
    updated["verified_at"] = now_jst_iso()
    old_notes = updated.get("notes") or ""
    tag = f"[auto_mark_done confidence={confidence}] {reason}"
    updated["notes"] = (old_notes + " | " + tag).lstrip(" | ")
    if related_commit and not updated.get("related_commit"):
        updated["related_commit"] = related_commit
    return updated


def find_related_commit(
    entry_id: str,
    corpus_inst_ids: set[str],
    commits: list[dict[str, str]],
) -> str | None:
    """entry に関連する commit hash を探す。見つからなければ None。"""
    for c in commits:
        msg = c.get("msg", "")
        for m in INST_ID_RE.finditer(msg):
            if m.group(1).lower() in corpus_inst_ids:
                return c["hash"][:12]
        if entry_id in msg:
            return c["hash"][:12]
    return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    if os.environ.get(BYPASS_ENV) == "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    transcript_path = payload.get("transcript_path", "") or ""
    if not transcript_path:
        return 0

    events = read_transcript(transcript_path)
    if not events:
        return 0

    # --- データ収集 ---
    touched_files = extract_touched_files(events)
    tool_texts = extract_tool_texts(events)
    assistant_texts = extract_assistant_texts(events)
    commits = get_recent_commit_info()
    corpus_inst_ids = extract_inst_ids_from_events(events)
    corpus = build_corpus(touched_files, tool_texts, assistant_texts, commits)

    # --- ledger 読み込み ---
    all_entries = load_ledger()
    pending = get_recent_pending(all_entries)
    if not pending:
        return 0

    # --- 照合 ---
    auto_done_ids: set[str] = set()
    auto_done_reasons: dict[str, str] = {}
    low_conf_candidates: list[tuple[dict[str, Any], list[str]]] = []

    for entry in pending:
        entry_id = entry.get("instruction_id", "")
        exact_text = entry.get("exact_text", "")
        keywords = extract_keywords(exact_text)

        count, matched = count_keyword_matches(keywords, corpus)
        related_commit = find_related_commit(entry_id, corpus_inst_ids, commits)
        inst_match = entry_id in corpus_inst_ids

        if inst_match or related_commit or count >= HIGH_CONF_THRESHOLD:
            reason_parts: list[str] = []
            if inst_match:
                reason_parts.append(f"INST-id match={entry_id}")
            if related_commit:
                reason_parts.append(f"commit={related_commit}")
            if count >= HIGH_CONF_THRESHOLD:
                reason_parts.append(f"keywords={matched[:5]}")
            reason = "; ".join(reason_parts)
            auto_done_ids.add(entry_id)
            auto_done_reasons[entry_id] = reason

            for i, e in enumerate(all_entries):
                if e.get("instruction_id") == entry_id and e.get("status") == "pending":
                    all_entries[i] = mark_done_entry(e, reason, "high", related_commit)
        elif count == 1:
            low_conf_candidates.append((entry, matched))

    # --- ledger 書き戻し ---
    if auto_done_ids:
        rewrite_ledger(all_entries)
        _ensure_log_dir()
        log_path = LOG_DIR / "auto_mark_done.log"
        ts = now_jst_iso()
        with log_path.open("a", encoding="utf-8") as f:
            for eid in sorted(auto_done_ids):
                f.write(f"[{ts}] AUTO_MARK_DONE instruction_id={eid} reason={auto_done_reasons.get(eid,'')}\n")

    # --- stderr 出力 ---
    if auto_done_ids:
        sys.stderr.write(
            f"[AUTO_MARK_DONE] {len(auto_done_ids)} pending instruction(s) marked done "
            f"(high-confidence): {sorted(auto_done_ids)}\n"
        )

    if low_conf_candidates:
        sys.stderr.write("[AUTO_MARK_DONE] Low-confidence candidates (manual confirm required):\n")
        for e, matched in low_conf_candidates:
            eid = e.get("instruction_id", "?")
            preview = (e.get("exact_text") or "")[:80].replace("\n", " ")
            sys.stderr.write(f"  - [{eid}] matched={matched} | {preview}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
