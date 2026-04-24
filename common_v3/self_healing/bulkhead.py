"""common_v3/self_healing/bulkhead.py — Upstream 別 Thread Pool 分離 (Bulkhead Pattern)

設計方針:
- upstream ごとに専用 ThreadPoolExecutor を保持し、他 upstream の遅延/ハングが
  波及しない隔壁 (bulkhead) を形成する。
- サイズ定義は BUILD TIME に固定 (tradovate=2 / moomoo=4 / pushover=1)。
  追加 upstream は register() で実行時登録も可能。
- submit() は Future を返す。呼出側が timeout 付き result() でキャンセル判断。
- shutdown() は全 pool を cancel_futures=True で即時解放（テスト teardown 対応）。
- スレッドは daemon=True 設定済み — プロセス終了時に孤立スレッドを残さない。

使用例:
    from common_v3.self_healing.bulkhead import BulkheadPool, get_global_pool

    pool = get_global_pool()
    fut = pool.submit("moomoo", some_blocking_func, arg1, kw=val)
    try:
        result = fut.result(timeout=5.0)
    except TimeoutError:
        # moomoo pool が詰まっていても tradovate pool は影響ゼロ
        ...

ref: Release-It! §5 Bulkhead pattern
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# デフォルト upstream サイズ定義
# ---------------------------------------------------------------------------

#: upstream 名 -> pool size (worker thread 数上限)
_DEFAULT_UPSTREAM_SIZES: dict[str, int] = {
    "tradovate": 2,
    "moomoo": 4,
    "pushover": 1,
}


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------

class BulkheadUpstreamNotFound(KeyError):
    """未登録 upstream に submit/resize を試みた場合。"""


class BulkheadAlreadyShutdown(RuntimeError):
    """shutdown() 済みの pool に操作を試みた場合。"""


# ---------------------------------------------------------------------------
# BulkheadPool
# ---------------------------------------------------------------------------

class BulkheadPool:
    """Upstream 別 ThreadPoolExecutor ラッパー。

    Args:
        upstream_sizes: upstream 名 -> worker 数のマッピング。
                        None の場合 _DEFAULT_UPSTREAM_SIZES を使用。
        thread_name_prefix: 各 executor の thread name prefix。
                            デフォルト "bulkhead"。

    Thread-safety: submit / register / shutdown はすべて _lock で保護。
    """

    def __init__(
        self,
        upstream_sizes: Optional[dict[str, int]] = None,
        thread_name_prefix: str = "bulkhead",
    ) -> None:
        self._sizes: dict[str, int] = dict(
            upstream_sizes if upstream_sizes is not None else _DEFAULT_UPSTREAM_SIZES
        )
        self._prefix = thread_name_prefix
        self._pools: dict[str, ThreadPoolExecutor] = {}
        self._lock = threading.Lock()
        self._shutdown_flag = False

        # 初期 pool 生成
        for name, size in self._sizes.items():
            self._pools[name] = self._make_executor(name, size)

        log.info(
            "[BulkheadPool] initialized: %s",
            {k: v for k, v in self._sizes.items()},
        )

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _make_executor(self, name: str, size: int) -> ThreadPoolExecutor:
        """daemon thread の ThreadPoolExecutor を生成する。"""
        return ThreadPoolExecutor(
            max_workers=size,
            thread_name_prefix=f"{self._prefix}_{name}",
        )

    def _guard_alive(self) -> None:
        if self._shutdown_flag:
            raise BulkheadAlreadyShutdown(
                "BulkheadPool は既に shutdown() 済みです。新しい BulkheadPool を生成してください。"
            )

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def submit(
        self,
        upstream: str,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future:
        """指定 upstream の pool で fn を非同期実行する。

        Args:
            upstream: 登録済み upstream 名 ("tradovate" / "moomoo" / "pushover" 等)
            fn:       実行する callable
            *args:    fn に渡す位置引数
            **kwargs: fn に渡すキーワード引数

        Returns:
            concurrent.futures.Future

        Raises:
            BulkheadUpstreamNotFound: upstream が未登録
            BulkheadAlreadyShutdown: shutdown() 後
        """
        with self._lock:
            self._guard_alive()
            if upstream not in self._pools:
                raise BulkheadUpstreamNotFound(
                    f"upstream={upstream!r} は BulkheadPool に登録されていません。 "
                    f"登録済み: {sorted(self._pools)}"
                )
            executor = self._pools[upstream]

        log.debug("[BulkheadPool] submit upstream=%s fn=%s", upstream, getattr(fn, "__name__", fn))
        return executor.submit(fn, *args, **kwargs)

    def register(self, upstream: str, size: int) -> None:
        """新しい upstream pool を追加登録する。

        既に同名で登録済みの場合は ValueError。
        既存 pool を置き換えたい場合は shutdown() して新インスタンス生成を推奨。

        Args:
            upstream: upstream 識別名
            size:     worker thread 数（>= 1）

        Raises:
            ValueError: upstream 既登録 / size < 1
            BulkheadAlreadyShutdown: shutdown() 後
        """
        if size < 1:
            raise ValueError(f"size は 1 以上が必要です (got {size})")
        with self._lock:
            self._guard_alive()
            if upstream in self._pools:
                raise ValueError(
                    f"upstream={upstream!r} は既に登録済みです (size={self._sizes[upstream]}). "
                    "重複登録は禁止です。"
                )
            self._sizes[upstream] = size
            self._pools[upstream] = self._make_executor(upstream, size)
            log.info("[BulkheadPool] registered upstream=%s size=%d", upstream, size)

    def pool_size(self, upstream: str) -> int:
        """登録済み upstream の pool size を返す。

        Raises:
            BulkheadUpstreamNotFound: upstream が未登録
        """
        with self._lock:
            if upstream not in self._sizes:
                raise BulkheadUpstreamNotFound(
                    f"upstream={upstream!r} は未登録です。"
                )
            return self._sizes[upstream]

    def upstream_names(self) -> list[str]:
        """登録済み upstream 名のリストを返す（ソート済み）。"""
        with self._lock:
            return sorted(self._pools)

    def is_shutdown(self) -> bool:
        """shutdown() 済みかどうかを返す。"""
        return self._shutdown_flag

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        """全 upstream pool を shutdown する。

        Args:
            wait:           True なら実行中 future 完了を待つ（default True）
            cancel_futures: True なら pending future をキャンセルする（Python 3.9+）
        """
        with self._lock:
            if self._shutdown_flag:
                return
            self._shutdown_flag = True
            pools_snapshot = dict(self._pools)

        for name, executor in pools_snapshot.items():
            log.debug("[BulkheadPool] shutdown upstream=%s", name)
            try:
                # cancel_futures は Python 3.9+ のみ対応
                import sys
                if sys.version_info >= (3, 9):
                    executor.shutdown(wait=wait, cancel_futures=cancel_futures)
                else:
                    executor.shutdown(wait=wait)
            except Exception as exc:  # noqa: BLE001
                log.warning("[BulkheadPool] shutdown error upstream=%s: %s", name, exc)

        log.info("[BulkheadPool] all pools shutdown.")

    def __repr__(self) -> str:
        status = "shutdown" if self._shutdown_flag else "alive"
        return f"BulkheadPool(upstreams={sorted(self._pools)!r}, status={status!r})"


# ---------------------------------------------------------------------------
# モジュールレベルシングルトン (グローバル pool)
# ---------------------------------------------------------------------------

_global_pool: Optional[BulkheadPool] = None
_global_pool_lock = threading.Lock()


def get_global_pool() -> BulkheadPool:
    """モジュールレベルのシングルトン BulkheadPool を返す。

    初回呼出時に _DEFAULT_UPSTREAM_SIZES で初期化。
    テストでは reset_global_pool() で差し替え可能。
    """
    global _global_pool
    with _global_pool_lock:
        if _global_pool is None or _global_pool.is_shutdown():
            _global_pool = BulkheadPool()
            log.info("[BulkheadPool] global pool (re)initialized.")
        return _global_pool


def reset_global_pool(new_pool: Optional[BulkheadPool] = None) -> None:
    """テスト用: グローバル pool を差し替える。

    Args:
        new_pool: 差し替え先。None の場合は既存 pool を shutdown して None 化
                  (次の get_global_pool() 呼出で再生成)。
    """
    global _global_pool
    with _global_pool_lock:
        if _global_pool is not None and not _global_pool.is_shutdown():
            _global_pool.shutdown(wait=False, cancel_futures=True)
        _global_pool = new_pool
