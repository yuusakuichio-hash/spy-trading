#!/usr/bin/env python3
"""user_prompt_ledger.py — UserPromptSubmit hook

ゆうさくさんの発言を受けて user_instruction_ledger.jsonl に自動エントリ追加する。

スキーマ:
  {instruction_id, timestamp, exact_text, parsed_action, status,
   related_task_id, related_commit, verified_by, verified_at, priority, notes}

parsed_action 分類 (LLM-free keyword match):
  質問 / 指示 / 確認要求 / フィードバック / 訂正

auto-noise filter (2026-04-25 追加):
  以下パターンにマッチする入力は status="auto_filtered" で登録 (pending にしない):
  - <task-notification> 始まり (agent 完了通知)
  - "Sora Lab discipline checker" 含む (discipline hook system prompt)
  - "Output formatter" 含む (formatter system prompt)
  - "You are an LLM" 始まり (system LLM directive)
  - "# /loop" 始まり (slash command metadata)
  - "Stop hook feedback:" 始まり (hook system feedback)

制御:
  USER_PROMPT_LEDGER_BYPASS=1 で無効化

asyncio 禁止 (B16 遵守) — 同期 I/O のみ使用
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

# ---------------------------------------------------------------------------
ROOT = Path("/Users/yuusakuichio/trading")
LEDGER = ROOT / "data" / "user_instruction_ledger.jsonl"
JST = timezone(timedelta(hours=9))

# --- auto-noise filter パターン -------------------------------------------
# マッチした場合は status="auto_filtered" で登録し pending には入れない
# 各要素: (pattern, match_mode)  match_mode = "startswith" | "contains"
_NOISE_PATTERNS: list[tuple[str, str]] = [
    ("<task-notification>", "startswith"),
    ("Sora Lab discipline checker", "contains"),
    ("Output formatter", "contains"),
    ("You are an LLM", "startswith"),
    ("# /loop", "startswith"),
    ("Stop hook feedback:", "startswith"),
]


def is_noise(text: str) -> bool:
    """ノイズパターンにマッチするか判定。"""
    for pattern, mode in _NOISE_PATTERNS:
        if mode == "startswith" and text.startswith(pattern):
            return True
        if mode == "contains" and pattern in text:
            return True
    return False


# --- 分類キーワード ---------------------------------------------------------
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
    urgent_words = re.compile(r"(?:すぐ|今すぐ|急いで|至急|緊急|最優先)", re.IGNORECASE)
    if urgent_words.search(text):
        return "high"
    return "medium"


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def make_instruction_id(timestamp: str, text: str) -> str:
    raw = (timestamp + text).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:12]


def ensure_ledger() -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    if not LEDGER.exists():
        LEDGER.touch()


def append_entry(entry: dict[str, Any]) -> None:
    ensure_ledger()
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def extract_user_text(payload: dict[str, Any]) -> str:
    """UserPromptSubmit payload から user テキストを取得。"""
    prompt = payload.get("prompt", "")
    if isinstance(prompt, str):
        return prompt.strip()
    if isinstance(prompt, list):
        parts = []
        for p in prompt:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
            elif isinstance(p, str):
                parts.append(p)
        return " ".join(parts).strip()
    return ""


def main() -> int:
    if os.environ.get("USER_PROMPT_LEDGER_BYPASS") == "1":
        return 0

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        payload = json.loads(raw)
    except Exception:
        return 0

    text = extract_user_text(payload)
    if not text:
        return 0

    # 極端に短いもの (1-2 文字) は操作ミスの可能性が高いので無視
    if len(text) < 3:
        return 0

    ts = now_jst_iso()
    instruction_id = make_instruction_id(ts, text)
    action = classify_action(text)
    priority = infer_priority(text, action)

    # auto-noise filter: ノイズパターンにマッチしたら auto_filtered で登録
    if is_noise(text):
        status = "auto_filtered"
        notes = "[auto_noise_filter] system/hook generated content"
    else:
        status = "pending"
        notes = ""

    entry: dict[str, Any] = {
        "instruction_id": instruction_id,
        "timestamp": ts,
        "exact_text": text[:2000],
        "parsed_action": action,
        "status": status,
        "related_task_id": None,
        "related_commit": None,
        "verified_by": None,
        "verified_at": None,
        "priority": priority,
        "notes": notes,
    }

    append_entry(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
