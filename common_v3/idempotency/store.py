#!/usr/bin/env python3
"""
common_v3/idempotency/store.py — 発注冪等性キー管理 (v3)

spec B6 準拠。file-based 実装で二重発注を物理防止する。
B15 StorageBackend 経由化は Sprint 1 (C-008) で対応予定。

公開 API:
    IdempotencyStore        — ファイル永続化ストア
    OrderNotSentError       — broker 送信前例外マーカー（with_idempotency でのみ unmark される）
    make_job_key            — strategy/symbol/trigger_time からキー生成
    with_idempotency        — デコレータライクなラッパー
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from common_v3.executor.sync_guard import sync_only

log = logging.getLogger(__name__)


class OrderNotSentError(Exception):
    """broker 送信前の例外を示すマーカー。

    with_idempotency はこの例外を捕捉した場合のみキーを _unmark する。
    broker への送信が完了した後の副作用例外（ログ失敗・通知失敗等）では
    絶対にこの例外を raise してはいけない（Knight Capital 型二重発注を防ぐ）。

    Usage:
        def submit_order(symbol: str) -> OrderResult:
            if not validate(symbol):
                raise OrderNotSentError("validation failed")   # 送信前 → unmark OK
            result = broker.send(symbol)                        # 送信完了
            # ここ以降では OrderNotSentError を raise しない
            notify(result)  # 失敗しても OrderNotSentError は使わない
            return result
    """


_DEFAULT_STORE_PATH: Path = (
    Path(__file__).resolve().parents[2] / "data" / "idempotency_keys.json"
)


class IdempotencyStore:
    """file-based 冪等性キーストア。

    - fcntl.flock による concurrent write 保護
    - threading.Lock によるスレッドセーフ
    - TTL 失効済みキーは check_and_mark 呼出時に自動削除
    - 既存 data/idempotency_keys.json フォーマット（{key: timestamp_float}）と互換

    Args:
        path:    永続化ファイルパス。None のとき data/idempotency_keys.json を使用。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path or _DEFAULT_STORE_PATH
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    @sync_only
    def check_and_mark(self, key: str, ttl_sec: int = 300) -> bool:
        """キーが未登録なら登録して True を返す。登録済みなら False を返す。

        Args:
            key:     冪等性キー
            ttl_sec: TTL 秒数（デフォルト 300 秒）。失効済みキーは新規扱い。

        Returns:
            True  — 新規（発注続行して良い）
            False — 重複（発注ブロックすべき）

        Raises:
            ValueError: ttl_sec が 0 以下の場合
        """
        if ttl_sec <= 0:
            raise ValueError(f"ttl_sec must be positive, got {ttl_sec}")
        with self._lock:
            return self._check_and_mark_locked(key, ttl_sec)

    @sync_only
    def _unmark(self, key: str) -> None:
        """登録済みキーを削除する（OrderNotSentError ロールバック専用・内部用）。

        外部から直接呼び出してはいけない。broker 送信前に失敗した場合に
        with_idempotency が内部で呼び出す専用メソッド。
        送信後の副作用例外でこのメソッドを呼ぶと Knight Capital 型二重発注が発生する。

        キーが存在しない場合は無操作（冪等）。

        Args:
            key: 削除する冪等性キー
        """
        with self._lock:
            self._unmark_locked(key)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _check_and_mark_locked(self, key: str, ttl_sec: int) -> bool:
        """threading.Lock 取得済み状態で呼ぶ内部実装。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # ファイルを open してから flock — 存在しない場合は新規作成
        with open(self._path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read().strip()
                store: dict[str, float] = json.loads(raw) if raw else {}

                now = time.time()

                # TTL 失効済みキーを削除
                store = {k: ts for k, ts in store.items() if now - ts < ttl_sec}

                if key in store:
                    log.warning(
                        "[IdempotencyStore] 重複ブロック: key=%s (登録から %ds 経過)",
                        key,
                        int(now - store[key]),
                    )
                    return False

                # 新規登録してファイルに書き戻し
                store[key] = now
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(store, ensure_ascii=False))
                log.debug("[IdempotencyStore] 新規キー登録: %s", key)
                return True
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _unmark_locked(self, key: str) -> None:
        """threading.Lock 取得済み状態でキーを削除する内部実装。"""
        if not self._path.exists():
            return

        with open(self._path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                raw = fh.read().strip()
                store: dict[str, float] = json.loads(raw) if raw else {}

                if key not in store:
                    return

                del store[key]
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps(store, ensure_ascii=False))
                log.debug("[IdempotencyStore] キー削除（ロールバック）: %s", key)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# module-level helpers
# ---------------------------------------------------------------------------


def make_job_key(strategy: str, symbol: str, trigger_time: datetime) -> str:
    """strategy / symbol / trigger_time から一意な冪等性キーを生成する。

    フォーマット: "v3_{sha256[:12]}"（常に 16 バイト・URL-safe ASCII）

    Args:
        strategy:     戦略名 (e.g., "ORB_1DTE")
        symbol:       銘柄 (e.g., "SPY")
        trigger_time: トリガー時刻（UTC 推奨。tzinfo 付きで渡すこと）

    Returns:
        str: 冪等性キー
    """
    raw = f"{strategy}|{symbol}|{trigger_time.isoformat()}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"v3_{digest}"


def with_idempotency(
    store: IdempotencyStore,
    key: str,
    func: Callable[[], Any],
    ttl_sec: int = 300,
) -> Any:
    """func を冪等に実行する。

    - 新規キー: func() を呼んで結果を返す
    - 重複キー: func() を呼ばず None を返す
    - func() が OrderNotSentError を送出した場合のみ: キーをロールバック（_unmark）して例外を再送出
    - func() がその他の例外を送出した場合: キーはロールバックしない（TTL 自然失効で再試行）
      これにより broker 送信後の副作用例外による Knight Capital 型二重発注を防ぐ。

    Args:
        store:   IdempotencyStore インスタンス
        key:     冪等性キー（make_job_key で生成を推奨）
        func:    実行する callable（引数なし）
        ttl_sec: TTL 秒数（1 以上の正整数）

    Returns:
        func() の戻り値、または None（重複の場合）

    Raises:
        ValueError:         ttl_sec が 0 以下の場合
        OrderNotSentError:  func() が送出（キーロールバック後に再送出）
        Exception:          func() が送出したその他の例外（キーはロールバックしない）
    """
    if ttl_sec <= 0:
        raise ValueError(f"ttl_sec must be positive, got {ttl_sec}")
    if not store.check_and_mark(key, ttl_sec=ttl_sec):
        log.info("[with_idempotency] スキップ（重複）: key=%s", key)
        return None
    try:
        return func()
    except OrderNotSentError:
        store._unmark(key)
        log.warning(
            "[with_idempotency] OrderNotSentError のためキーロールバック: key=%s", key
        )
        raise
