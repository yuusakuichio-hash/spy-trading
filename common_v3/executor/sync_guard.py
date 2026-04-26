"""common_v3/executor/sync_guard.py — @sync_only runtime contract decorator.

C-001 Sprint 1 実装: AST 静的解析の補助として runtime guard を主防御に格上げ。

設計根拠:
- kill_switch / idempotency 等のクリティカル関数は file I/O + fcntl.flock を使用。
- asyncio event loop 内（main thread 上で動く coroutine）から直接呼ぶと
  flock が event loop を blocking し、全 coroutine が凍結する。
- 別 thread からの呼出は fcntl.flock の thread-safety に依存しているが、
  KillSwitch/IdempotencyStore の内部 threading.Lock との二重ロックで
  デッドロックリスクが生じる。
- よって「main thread の sync コードからのみ呼び出す」契約を runtime で強制する。

asyncio 呼出が必要な場合の正しいパターン:
    # asyncio context → sync_only 関数を呼ぶ場合は common_v3/executor/async_impl.py 経由で:
    result = await asyncio.to_thread(kill_switch.is_active)
    # または
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, kill_switch.is_active)

注意: asyncio coroutine は main thread 上で動くため current_thread() is main_thread() が
True になることがある。本 guard は sys.modules 経由で asyncio を遅延参照して
実行中のイベントループを検出する（B16 規律に従い asyncio を直接 import しない）。
"""
from __future__ import annotations

import sys
import threading
from functools import wraps
from typing import Any, Callable, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])


def _is_asyncio_loop_running() -> bool:
    """asyncio event loop が main thread 上で実行中かを sys.modules 経由で確認する。

    B16 規律: asyncio を直接 import しない（async_impl.py 外禁止）。
    sys.modules["asyncio"] は asyncio がロード済みの場合のみ存在する。
    asyncio がロードされていない環境では False を返す（安全側）。

    Returns:
        True  — asyncio event loop が現在 running 状態（coroutine 内から呼ばれた可能性）
        False — asyncio 未ロード、または loop が running でない
    """
    asyncio_mod = sys.modules.get("asyncio")
    if asyncio_mod is None:
        # asyncio 自体がロードされていない → event loop 実行中ではない
        return False

    get_running_loop = getattr(asyncio_mod, "get_running_loop", None)
    if get_running_loop is None:
        return False

    try:
        loop = get_running_loop()
        return loop is not None and loop.is_running()
    except RuntimeError:
        # RuntimeError: no running event loop — loop 実行中でない
        return False


def sync_only(fn: _F) -> _F:
    """デコレートした関数を「main thread かつ asyncio event loop 外」でのみ実行可能にする。

    以下のどちらかに該当する場合に RuntimeError を送出する:
        1. current_thread() is not main_thread()
           (別 thread からの呼び出し)
        2. main thread 上で asyncio event loop が running 状態
           (async def / coroutine 内からの直接呼び出し)

    asyncio context からどうしても呼びたい場合は common_v3/executor/async_impl.py の
    asyncio.to_thread() / run_in_executor() ラッパーを経由すること。

    Args:
        fn: ラップする callable

    Returns:
        ラップされた callable（シグネチャ・__name__ 等は保持）

    Raises:
        RuntimeError: sync-only contract violation
    """

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        # --- guard 1: non-main thread ---
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                f"sync-only contract violation: {fn.__qualname__!r} called from "
                f"non-main thread ({threading.current_thread().name!r}). "
                "Use common_v3/executor/async_impl.py wrappers "
                "(asyncio.to_thread / run_in_executor) to call sync-only "
                "functions from async context."
            )

        # --- guard 2: asyncio event loop running on main thread ---
        if _is_asyncio_loop_running():
            raise RuntimeError(
                f"sync-only contract violation: {fn.__qualname__!r} called from "
                "asyncio event loop (coroutine/async context). "
                "Use 'await asyncio.to_thread(fn, ...)' via "
                "common_v3/executor/async_impl.py instead."
            )

        return fn(*args, **kwargs)

    # タグ: テストや hook が sync_only 適用済みかを確認するために使う
    wrapper.__sync_only__ = True  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]
