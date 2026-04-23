#!/usr/bin/env python3
"""
scripts/morning_digest_send.py — 朝4:00 モーニングダイジェスト送信

静穏時間 (JST 22:00-4:00) 中に保留した通知を集約して1通で送信する。
LaunchAgent com.soralab.morning_digest から毎日 04:00 JST に起動される。

data/pushover_morning_queue.jsonl を読み込み:
  - 自己修復成功系: 件数のみ
  - 検知のみ(file-only): 件数のみ
  - P1相当の保留: 各件タイトル + 抜粋

まとめて1通のPushover通知(title="[SYS朝] 昨夜の動き"・priority=0)を送信し、
送信後にキューを空にする。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# ── パス設定 ──────────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE_DIR))

from common.pushover_client import (
    _load_morning_queue,
    _clear_morning_queue,
    _DEFAULT_TOKEN,
    _DEFAULT_USER,
    _http_post,
    MORNING_QUEUE_PATH,
)

log = logging.getLogger("morning_digest")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] morning_digest: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


def _categorize(entries: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """エントリをカテゴリ分けする。

    Returns:
        (自己修復系, 検知のみ系, P1相当保留系)
    """
    auto_fix: list[dict] = []
    detect_only: list[dict] = []
    high_priority: list[dict] = []

    for e in entries:
        title = e.get("title", "")
        priority = int(e.get("priority", 0))

        # 自己修復成功系キーワード
        if any(kw in title for kw in ("自己修復", "recovered", "recovery", "restored", "restart")):
            auto_fix.append(e)
        # 検知のみ系キーワード
        elif any(kw in title for kw in ("検知", "detect", "WARN", "WARNING", "監視")):
            detect_only.append(e)
        # P1以上の保留（ALERT/HALT/CRITICAL 相当）
        elif priority >= 1 or any(kw in title for kw in ("ALERT", "HALT", "CRITICAL", "ERROR", "エラー", "失敗")):
            high_priority.append(e)
        else:
            detect_only.append(e)

    return auto_fix, detect_only, high_priority


def _build_auth_budget_section() -> str:
    """認証試行予算サマリー行を返す（失敗や残少ない場合のみ）"""
    try:
        from common.auth_budget import AuthBudget
        summary = AuthBudget.get_summary()
        alerts = []
        for svc, info in summary.items():
            if info["count"] == 0:
                continue
            if info["remaining"] == 0:
                alerts.append(f"  {svc}: 上限到達 {info['count']}/{info['max']}")
            elif info["remaining"] <= 1:
                alerts.append(f"  {svc}: 残り{info['remaining']}回")
            elif info["success_rate"] < 50 and info["count"] >= 2:
                alerts.append(f"  {svc}: 成功率{info['success_rate']}% ({info['count']}回)")
        if not alerts:
            return ""
        return "認証試行:\n" + "\n".join(alerts)
    except Exception:
        return ""


def send_morning_digest() -> bool:
    """モーニングダイジェストを送信する。

    Returns:
        True = 送信成功またはキューが空（スキップ） / False = 送信失敗
    """
    entries = _load_morning_queue()

    if not entries:
        log.info("morning queue empty — skip digest")
        return True

    log.info("morning queue: %d entries — composing digest", len(entries))

    auto_fix, detect_only, high_priority = _categorize(entries)

    lines: list[str] = []

    if auto_fix:
        lines.append(f"自己修復成功: {len(auto_fix)}件")

    if detect_only:
        lines.append(f"検知・監視ログ: {len(detect_only)}件")

    if high_priority:
        lines.append(f"--- 要確認 {len(high_priority)}件 ---")
        for e in high_priority:
            ts_str = time.strftime("%H:%M", time.localtime(float(e.get("ts", 0))))
            title = e.get("title", "")[:50]
            msg = e.get("message", "")[:60]
            lines.append(f"[{ts_str}] {title}: {msg}")

    lines.append(f"\n合計 {len(entries)}件を静穏時間中に保留")

    auth_section = _build_auth_budget_section()
    if auth_section:
        lines.append(auth_section)

    digest_title = "[SYS朝] 昨夜の動き"
    digest_message = "\n".join(lines)[:1024]

    tok = _DEFAULT_TOKEN
    user = _DEFAULT_USER

    if not tok or not user:
        log.warning("missing PUSHOVER_TOKEN/PUSHOVER_USER — writing digest to log only")
        log.info("DIGEST:\n%s\n%s", digest_title, digest_message)
        _clear_morning_queue()
        return True

    success, is_429 = _http_post(tok, user, digest_title, digest_message, priority=0)

    if success:
        log.info("morning digest sent: %d entries", len(entries))
        _clear_morning_queue()
        return True
    elif is_429:
        log.warning("morning digest 429 — queue preserved for retry")
        return False
    else:
        log.warning("morning digest send failed — queue preserved")
        return False


if __name__ == "__main__":
    ok = send_morning_digest()
    sys.exit(0 if ok else 1)
