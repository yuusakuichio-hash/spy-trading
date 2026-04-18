#!/usr/bin/env python3
"""
common/idempotency.py — 発注冪等性キー管理

moomoo place_order の remark フィールド（64バイト上限）を使って
重複発注を防止する。プロセス再起動後もファイルで永続化。

使い方:
    from common.idempotency import IdempotencyStore

    store = IdempotencyStore()
    key = store.make_key(signal_id="2026-04-18_CS_PUT_10:30", label="SHORT_PUT")
    if store.check_and_register(key):
        # 新規発注 OK → place_order に remark=key を渡す
        trade_ctx.place_order(..., remark=key)
    else:
        # 重複発注 → スキップ
        log.warning(f"Duplicate order blocked: {key}")
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 冪等性キーの保持期間（秒）。1営業日 = 86400秒でリセット
_TTL_SEC: int = 86400
# 永続化ファイルパス
_DEFAULT_STORE_PATH = Path(__file__).resolve().parents[1] / "data" / "idempotency_keys.json"


class IdempotencyStore:
    """発注冪等性キーのファイル永続化ストア。

    スレッドセーフではないが、Atlas は単一プロセス・単一スレッドで
    place_order を呼ぶ設計なので許容する。
    """

    def __init__(self, store_path: Optional[Path] = None, ttl_sec: int = _TTL_SEC) -> None:
        self._path = store_path or _DEFAULT_STORE_PATH
        self._ttl = ttl_sec
        self._cache: dict[str, float] = {}  # key -> registered_at
        self._load()

    def _load(self) -> None:
        """ファイルからキーを読み込む。"""
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                now = time.time()
                # TTL 切れのキーは捨てる
                self._cache = {k: ts for k, ts in data.items() if now - ts < self._ttl}
        except Exception as e:
            log.warning(f"IdempotencyStore._load: {e} — empty store")
            self._cache = {}

    def _save(self) -> None:
        """キーをファイルに保存する。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._cache, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"IdempotencyStore._save: {e}")

    @staticmethod
    def make_key(signal_id: str, label: str) -> str:
        """64バイト以内の冪等性キーを生成する。

        remark フィールドの上限に合わせて短縮する。
        フォーマット: "idm_{hash8}" (len=13)
        hash8 は signal_id + label の SHA256 先頭8文字。

        Args:
            signal_id: エントリー識別子 (e.g., "2026-04-18_standard_PUT_10:30")
            label:     レグラベル (e.g., "SHORT_PUT", "LONG_PUT")

        Returns:
            str: 64バイト以内の冪等性キー
        """
        raw = f"{signal_id}|{label}"
        h = hashlib.sha256(raw.encode()).hexdigest()[:12]
        key = f"idm_{h}"
        assert len(key.encode("utf-8")) <= 64, f"key too long: {key}"
        return key

    def check_and_register(self, key: str) -> bool:
        """キーが未登録なら登録してTrueを返す。登録済みならFalseを返す。

        Returns:
            True  → 新規発注 OK
            False → 重複発注 → ブロックすべき
        """
        now = time.time()
        # TTL 切れのキーを掃除
        self._cache = {k: ts for k, ts in self._cache.items() if now - ts < self._ttl}

        if key in self._cache:
            log.warning(
                f"[Idempotency] 重複発注ブロック: key={key} "
                f"(registered {int(now - self._cache[key])}秒前)"
            )
            return False

        self._cache[key] = now
        self._save()
        log.debug(f"[Idempotency] 新規キー登録: {key}")
        return True

    def clear_key(self, key: str) -> None:
        """発注失敗時にキーを削除して再試行を許可する。"""
        if key in self._cache:
            del self._cache[key]
            self._save()
            log.debug(f"[Idempotency] キー削除（再試行許可）: {key}")


# モジュールレベルのシングルトン（spy_bot.py からインポートして使う）
_global_store: Optional[IdempotencyStore] = None


def get_store() -> IdempotencyStore:
    """グローバルIdempotencyStoreを返す（初回のみ初期化）。"""
    global _global_store
    if _global_store is None:
        _global_store = IdempotencyStore()
    return _global_store
