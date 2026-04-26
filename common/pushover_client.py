#!/usr/bin/env python3
"""
common/pushover_client.py — Pushover 共通クライアント (Sora Lab)

全スクリプトはこのモジュール経由で Pushover へ送信する。
backoff / queue / retry を一元管理し、1スクリプトの429連鎖から
他スクリプトを守る（SPOF解消）。

state:  data/pushover_client_state.json
queue:  data/pushover_client_queue.jsonl

API:
    from common.pushover_client import send, flush_queue

    # 基本送信
    send("[Atlas] エントリー", "SPY CS +$42", priority=0)

    # 明示的にトークン指定
    send("title", "msg", priority=1, token=os.environ["PUSHOVER_ALERT_TOKEN"])

    # キュー再送（LaunchAgent から定期実行）
    python3 -m common.pushover_client --flush

backoff policy:
    - 連続3回 429 or レスポンスに "banned" → 30分沈黙
    - ban 中の send() はキューへ追記して即 return False
    - 30分後に自動再試行（先頭1件テスト送信）
    - 成功 → キュー全flush / 失敗 → さらに30分延長

queue 管理:
    - JSONL 追記型。各エントリ: {ts, title, message, priority, token, app_tag}
    - 24時間以上古いエントリは drop（stale drop）
    - サイズ上限 10MB → 古いエントリから drop
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore

# ── パス設定 ─────────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parents[1]  # trading/

STATE_PATH = _BASE_DIR / "data" / "pushover_client_state.json"
QUEUE_PATH = _BASE_DIR / "data" / "pushover_client_queue.jsonl"

# ── 定数 ─────────────────────────────────────────────────────────────────────
_429_MAX_CONSECUTIVE: int   = 3       # 連続この回数で ban 扱い
_BACKOFF_DURATION_SEC: int  = 1800    # 30分
_STALE_DROP_SEC: int        = 86400   # 24時間以上古いエントリを破棄
_QUEUE_MAX_BYTES: int       = 10 * 1024 * 1024  # 10MB
_PUSHOVER_URL               = "https://api.pushover.net/1/messages.json"
_PUSHOVER_TIMEOUT_SEC       = 10

# ── ロガー ───────────────────────────────────────────────────────────────────
log = logging.getLogger("pushover_client")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] pushover_client: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

# ── 環境変数デフォルト ────────────────────────────────────────────────────────
_DEFAULT_TOKEN = (
    os.environ.get("PUSHOVER_OPS_TOKEN")
    or os.environ.get("PUSHOVER_TOKEN")
    or ""
)
_DEFAULT_USER = os.environ.get("PUSHOVER_USER", "")


# ─────────────────────────────────────────────────────────────────────────────
# 内部: state ファイル管理
# ─────────────────────────────────────────────────────────────────────────────

def _load_state() -> dict[str, Any]:
    """STATE_PATH から backoff 状態を読み込む。ファイルがなければデフォルト返却。"""
    try:
        if STATE_PATH.exists():
            raw = STATE_PATH.read_text(encoding="utf-8")
            obj = json.loads(raw)
            return {
                "consecutive_429": int(obj.get("consecutive_429", 0)),
                "backoff_until":   float(obj.get("backoff_until", 0.0)),
            }
    except Exception as e:
        log.warning("state load error: %s", e)
    return {"consecutive_429": 0, "backoff_until": 0.0}


def _save_state(consecutive_429: int, backoff_until: float) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps({"consecutive_429": consecutive_429, "backoff_until": backoff_until}),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("state save error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# 内部: queue 管理
# ─────────────────────────────────────────────────────────────────────────────

def _queue_entry(
    title: str,
    message: str,
    priority: int,
    token: str,
    app_tag: str,
) -> None:
    """キューへ1件追記する。サイズ上限チェックは別途 _trim_queue() で行う。"""
    try:
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps(
            {
                "ts":       time.time(),
                "title":    title,
                "message":  message[:1024],
                "priority": priority,
                "token":    token,
                "app_tag":  app_tag,
            },
            ensure_ascii=False,
        )
        with QUEUE_PATH.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
        log.info("[QUEUE] enqueued: %s", title)
    except Exception as e:
        log.warning("[QUEUE] write error: %s", e)


def _load_queue() -> list[dict[str, Any]]:
    """QUEUE_PATH の全エントリをリストで返す。存在しなければ空リスト。"""
    if not QUEUE_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    except Exception as e:
        log.warning("[QUEUE] read error: %s", e)
    return entries


def _rewrite_queue(entries: list[dict[str, Any]]) -> None:
    """キューをエントリ群で上書き（trim 後の書き戻し用）。"""
    try:
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e, ensure_ascii=False) for e in entries]
        QUEUE_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    except Exception as e:
        log.warning("[QUEUE] rewrite error: %s", e)


def _trim_queue() -> int:
    """
    1. 24時間以上古いエントリを drop（stale drop）
    2. 10MB 超過時は古い方から drop
    戻り値: drop 件数
    """
    entries = _load_queue()
    if not entries:
        return 0

    now = time.time()
    before = len(entries)

    # stale drop
    entries = [e for e in entries if now - float(e.get("ts", 0)) < _STALE_DROP_SEC]

    # サイズ drop
    while entries:
        size = sum(len(json.dumps(e, ensure_ascii=False).encode()) for e in entries)
        if size <= _QUEUE_MAX_BYTES:
            break
        entries.pop(0)

    dropped = before - len(entries)
    if dropped:
        log.info("[QUEUE] trimmed %d entries (stale/size)", dropped)
        _rewrite_queue(entries)

    return dropped


# ─────────────────────────────────────────────────────────────────────────────
# 内部: 実際の HTTP 送信
# ─────────────────────────────────────────────────────────────────────────────

def _http_post(
    token: str,
    user: str,
    title: str,
    message: str,
    priority: int,
) -> tuple[bool, bool]:
    """
    HTTP POST を1回実行する。
    戻り値: (success: bool, is_429_or_banned: bool)
    """
    if _requests is None:
        log.warning("[HTTP] requests library not available")
        return False, False
    try:
        data: dict[str, Any] = {
            "token":   token,
            "user":    user,
            "title":   title,
            "message": message[:1024],
            "priority": priority,
        }
        if priority >= 2:
            data["retry"]  = 30
            data["expire"] = 3600
        resp = _requests.post(_PUSHOVER_URL, data=data, timeout=_PUSHOVER_TIMEOUT_SEC)

        if resp.status_code == 429:
            log.warning("[HTTP] 429 rate limited")
            return False, True

        body = resp.text or ""
        if "banned" in body.lower():
            log.warning("[HTTP] banned detected in response: %s", body[:200])
            return False, True

        if resp.ok:
            return True, False

        log.warning("[HTTP] error status=%s body=%s", resp.status_code, body[:200])
        return False, False

    except Exception as e:
        log.warning("[HTTP] exception: %s", e)
        return False, False


# ─────────────────────────────────────────────────────────────────────────────
# 公開 API
# ─────────────────────────────────────────────────────────────────────────────

def send(
    title: str,
    message: str,
    priority: int = 0,
    *,
    token: str | None = None,
    app_tag: str = "SYS",
) -> bool:
    """
    Pushover に通知を送信する。

    Parameters
    ----------
    title:    通知タイトル
    message:  通知本文（1024文字で自動切り捨て）
    priority: -2〜2。2の場合 retry/expire を自動付与
    token:    使用する Pushover アプリトークン。省略時は環境変数から自動選択
    app_tag:  ログ・キュー識別用タグ（例: "Atlas", "Chronos", "SYS"）

    Returns
    -------
    bool: True = 送信成功 / False = 送信失敗（ban中はキュー追記）
    """
    tok  = token or _DEFAULT_TOKEN
    user = _DEFAULT_USER

    # state 読み込み
    state = _load_state()
    consecutive_429 = state["consecutive_429"]
    backoff_until   = state["backoff_until"]
    now = time.time()

    # ── ban 中はキュー追記して終了 ──────────────────────────────────────────
    if now < backoff_until:
        remaining = backoff_until - now
        log.info("[BACKOFF] active (%.0fs remaining) — queuing: %s", remaining, title)
        _queue_entry(title, message, priority, tok, app_tag)
        return False

    # ── token / user チェック ────────────────────────────────────────────────
    if not tok or not user:
        log.warning("[SEND] missing token/user. title=%s", title)
        return False

    # ── HTTP 送信 ────────────────────────────────────────────────────────────
    success, is_429 = _http_post(tok, user, title, message, priority)

    if is_429:
        consecutive_429 += 1
        log.warning("[BACKOFF] 429/banned (%d/%d)", consecutive_429, _429_MAX_CONSECUTIVE)
        if consecutive_429 >= _429_MAX_CONSECUTIVE:
            new_backoff = now + _BACKOFF_DURATION_SEC
            log.warning("[BACKOFF] entering 30min silence until %.0f", new_backoff)
            _save_state(consecutive_429, new_backoff)
            _queue_entry(title, message, priority, tok, app_tag)
        else:
            _save_state(consecutive_429, backoff_until)
        return False

    if success:
        if consecutive_429 > 0:
            _save_state(0, 0.0)
        return True

    # その他エラー（非 429）はカウンタを変えない
    return False


def flush_queue() -> int:
    """
    ban 解除後のキュー再送。

    1. state を読んで ban 中なら即 return 0
    2. キューの先頭1件をテスト送信
    3. 成功 → 残り全件送信してキュークリア
    4. 失敗(429) → backoff をさらに30分延長 / その他失敗 → 件数だけ返す

    Returns
    -------
    int: 送信成功件数
    """
    state = _load_state()
    consecutive_429 = state["consecutive_429"]
    backoff_until   = state["backoff_until"]
    now = time.time()

    if now < backoff_until:
        log.info("[FLUSH] backoff active (%.0fs remaining) — skip", backoff_until - now)
        return 0

    _trim_queue()
    entries = _load_queue()
    if not entries:
        log.info("[FLUSH] queue empty")
        return 0

    user = _DEFAULT_USER

    # テスト送信（先頭1件）
    first = entries[0]
    tok = first.get("token") or _DEFAULT_TOKEN
    success, is_429 = _http_post(
        tok, user,
        first.get("title", ""),
        first.get("message", ""),
        int(first.get("priority", 0)),
    )

    if is_429:
        new_backoff = time.time() + _BACKOFF_DURATION_SEC
        log.warning("[FLUSH] 429 on test send — extending backoff to %.0f", new_backoff)
        _save_state(consecutive_429 + 1, new_backoff)
        return 0

    if not success:
        log.warning("[FLUSH] test send failed (non-429) — aborting flush")
        return 0

    # 先頭1件成功 → 残り全件送信
    sent = 1
    remaining = entries[1:]
    failed: list[dict[str, Any]] = []

    for entry in remaining:
        tok_e = entry.get("token") or _DEFAULT_TOKEN
        ok, is_429_e = _http_post(
            tok_e, user,
            entry.get("title", ""),
            entry.get("message", ""),
            int(entry.get("priority", 0)),
        )
        if is_429_e:
            # 途中で 429 → 残りをキューに戻す
            new_backoff = time.time() + _BACKOFF_DURATION_SEC
            _save_state(consecutive_429 + 1, new_backoff)
            failed.extend(remaining[remaining.index(entry):])
            break
        elif ok:
            sent += 1
        else:
            failed.append(entry)

    _rewrite_queue(failed)
    if not failed:
        _save_state(0, 0.0)
        log.info("[FLUSH] completed: sent=%d", sent)
    else:
        log.info("[FLUSH] partial: sent=%d failed=%d", sent, len(failed))

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# CLI エントリポイント
# ─────────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Pushover 共通クライアント CLI")
    parser.add_argument("--flush", action="store_true", help="キュー再送を実行")
    parser.add_argument("--status", action="store_true", help="state / queue 状態を表示")
    parser.add_argument("--send", nargs=2, metavar=("TITLE", "MSG"), help="テスト送信")
    args = parser.parse_args()

    if args.flush:
        n = flush_queue()
        print(f"[flush] sent={n}")
    elif args.status:
        s = _load_state()
        q = _load_queue()
        now = time.time()
        ban_remaining = max(0.0, s["backoff_until"] - now)
        print(f"consecutive_429 : {s['consecutive_429']}")
        print(f"backoff_until   : {s['backoff_until']} (remaining {ban_remaining:.0f}s)")
        print(f"queue entries   : {len(q)}")
    elif args.send:
        title, msg = args.send
        ok = send(title, msg, priority=0, app_tag="CLI")
        print(f"send: {'ok' if ok else 'failed/queued'}")
    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
