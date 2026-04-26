"""tests/test_bulkhead_20260425.py — BulkheadPool 15 件テスト

カバレッジ対象:
  T01  デフォルトサイズ確認 (tradovate=2 / moomoo=4 / pushover=1)
  T02  upstream_names() 返却値
  T03  submit: 正常ケース — 戻り値が Future で result() が得られる
  T04  submit: moomoo pool 分離 — tradovate hang 中に moomoo submit が成功する
  T05  submit: 未登録 upstream → BulkheadUpstreamNotFound
  T06  submit: shutdown 後 → BulkheadAlreadyShutdown
  T07  register: 新 upstream を追加して submit 成功
  T08  register: 重複 upstream → ValueError
  T09  register: size < 1 → ValueError
  T10  pool_size: 正常取得
  T11  pool_size: 未登録 upstream → BulkheadUpstreamNotFound
  T12  shutdown: is_shutdown() が True になる
  T13  shutdown: 2 回呼出しても例外なし (冪等)
  T14  get_global_pool: シングルトン同一インスタンスが返る
  T15  reset_global_pool: shutdown 後に get_global_pool() で新 pool が生成される
  T16  bulkhead 隔壁: moomoo pool 全スレッド占有中でも pushover submit は受け付ける
  T17  submit: worker 内例外は Future.exception() で取得可能
  T18  moomoo_provider: bulkhead import が成功している (統合チェック)
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from common_v3.self_healing.bulkhead import (
    BulkheadAlreadyShutdown,
    BulkheadPool,
    BulkheadUpstreamNotFound,
    get_global_pool,
    reset_global_pool,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def pool():
    """テスト用独立 BulkheadPool (デフォルトサイズ)."""
    p = BulkheadPool()
    yield p
    if not p.is_shutdown():
        p.shutdown(wait=False, cancel_futures=True)


@pytest.fixture(autouse=True)
def reset_global():
    """各テスト前後にグローバル pool をリセットする."""
    reset_global_pool(None)
    yield
    reset_global_pool(None)


# ---------------------------------------------------------------------------
# T01  デフォルトサイズ確認
# ---------------------------------------------------------------------------

def test_t01_default_sizes(pool: BulkheadPool) -> None:
    assert pool.pool_size("tradovate") == 2
    assert pool.pool_size("moomoo") == 4
    assert pool.pool_size("pushover") == 1


# ---------------------------------------------------------------------------
# T02  upstream_names() 返却値
# ---------------------------------------------------------------------------

def test_t02_upstream_names(pool: BulkheadPool) -> None:
    names = pool.upstream_names()
    assert set(names) == {"tradovate", "moomoo", "pushover"}
    # ソート済みであること
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# T03  submit 正常ケース — 戻り値 Future で result() 取得
# ---------------------------------------------------------------------------

def test_t03_submit_normal(pool: BulkheadPool) -> None:
    fut = pool.submit("moomoo", lambda: 42)
    assert isinstance(fut, Future)
    assert fut.result(timeout=5.0) == 42


# ---------------------------------------------------------------------------
# T04  submit: tradovate hang 中に moomoo submit が成功
# ---------------------------------------------------------------------------

def test_t04_moomoo_isolated_from_tradovate_hang(pool: BulkheadPool) -> None:
    """tradovate pool がハングしても moomoo pool は独立して動作する."""
    barrier = threading.Event()
    done = threading.Event()

    def long_running():
        barrier.wait(timeout=10.0)

    # tradovate pool の全スレッド(2本)をブロック
    futs = [pool.submit("tradovate", long_running) for _ in range(2)]

    # moomoo は別 pool なので即座に結果が取れる
    result_fut = pool.submit("moomoo", lambda: "moomoo_ok")
    result = result_fut.result(timeout=5.0)
    assert result == "moomoo_ok"

    # tradovate 解放
    barrier.set()
    for f in futs:
        f.result(timeout=5.0)


# ---------------------------------------------------------------------------
# T05  submit: 未登録 upstream → BulkheadUpstreamNotFound
# ---------------------------------------------------------------------------

def test_t05_submit_unknown_upstream_raises(pool: BulkheadPool) -> None:
    with pytest.raises(BulkheadUpstreamNotFound):
        pool.submit("nonexistent_upstream", lambda: None)


# ---------------------------------------------------------------------------
# T06  submit: shutdown 後 → BulkheadAlreadyShutdown
# ---------------------------------------------------------------------------

def test_t06_submit_after_shutdown_raises(pool: BulkheadPool) -> None:
    pool.shutdown(wait=True)
    with pytest.raises(BulkheadAlreadyShutdown):
        pool.submit("moomoo", lambda: None)


# ---------------------------------------------------------------------------
# T07  register: 新 upstream 追加して submit 成功
# ---------------------------------------------------------------------------

def test_t07_register_new_upstream(pool: BulkheadPool) -> None:
    pool.register("new_service", size=3)
    assert pool.pool_size("new_service") == 3
    assert "new_service" in pool.upstream_names()

    fut = pool.submit("new_service", lambda: "hello")
    assert fut.result(timeout=5.0) == "hello"


# ---------------------------------------------------------------------------
# T08  register: 重複 upstream → ValueError
# ---------------------------------------------------------------------------

def test_t08_register_duplicate_raises(pool: BulkheadPool) -> None:
    with pytest.raises(ValueError, match="既に登録済み"):
        pool.register("moomoo", size=2)


# ---------------------------------------------------------------------------
# T09  register: size < 1 → ValueError
# ---------------------------------------------------------------------------

def test_t09_register_size_zero_raises(pool: BulkheadPool) -> None:
    with pytest.raises(ValueError, match="1 以上"):
        pool.register("zero_pool", size=0)


# ---------------------------------------------------------------------------
# T10  pool_size: 正常取得
# ---------------------------------------------------------------------------

def test_t10_pool_size_normal(pool: BulkheadPool) -> None:
    assert pool.pool_size("tradovate") == 2


# ---------------------------------------------------------------------------
# T11  pool_size: 未登録 upstream → BulkheadUpstreamNotFound
# ---------------------------------------------------------------------------

def test_t11_pool_size_unknown_raises(pool: BulkheadPool) -> None:
    with pytest.raises(BulkheadUpstreamNotFound):
        pool.pool_size("not_registered")


# ---------------------------------------------------------------------------
# T12  shutdown: is_shutdown() が True になる
# ---------------------------------------------------------------------------

def test_t12_shutdown_flag(pool: BulkheadPool) -> None:
    assert not pool.is_shutdown()
    pool.shutdown(wait=True)
    assert pool.is_shutdown()


# ---------------------------------------------------------------------------
# T13  shutdown: 2 回呼出しても例外なし (冪等)
# ---------------------------------------------------------------------------

def test_t13_shutdown_idempotent(pool: BulkheadPool) -> None:
    pool.shutdown(wait=True)
    pool.shutdown(wait=True)  # 例外なし
    assert pool.is_shutdown()


# ---------------------------------------------------------------------------
# T14  get_global_pool: シングルトン同一インスタンスが返る
# ---------------------------------------------------------------------------

def test_t14_get_global_pool_singleton() -> None:
    p1 = get_global_pool()
    p2 = get_global_pool()
    assert p1 is p2


# ---------------------------------------------------------------------------
# T15  reset_global_pool: shutdown 後に get_global_pool() で新 pool 生成
# ---------------------------------------------------------------------------

def test_t15_reset_global_pool_recreates() -> None:
    old = get_global_pool()
    reset_global_pool(None)
    new = get_global_pool()
    # 新インスタンスであること
    assert new is not old
    assert not new.is_shutdown()


# ---------------------------------------------------------------------------
# T16  bulkhead 隔壁: moomoo pool 全スレッド占有中でも pushover submit は受け付ける
# ---------------------------------------------------------------------------

def test_t16_pushover_isolated_from_moomoo_saturation(pool: BulkheadPool) -> None:
    """moomoo pool (size=4) 全スレッド詰まり中に pushover submit が通る."""
    barrier = threading.Event()

    def blocker():
        barrier.wait(timeout=10.0)

    # moomoo 全スレッド(4本)をブロック
    futs = [pool.submit("moomoo", blocker) for _ in range(4)]

    # pushover は別 pool (size=1) なので即座に動く
    pf = pool.submit("pushover", lambda: "pushover_ok")
    result = pf.result(timeout=5.0)
    assert result == "pushover_ok"

    barrier.set()
    for f in futs:
        f.result(timeout=5.0)


# ---------------------------------------------------------------------------
# T17  submit: worker 内例外は Future.exception() で取得可能
# ---------------------------------------------------------------------------

def test_t17_worker_exception_propagated(pool: BulkheadPool) -> None:
    def bad_fn():
        raise ValueError("intentional error")

    fut = pool.submit("tradovate", bad_fn)
    # result() は例外を re-raise する
    with pytest.raises(ValueError, match="intentional error"):
        fut.result(timeout=5.0)

    # exception() でも取得可能
    exc = fut.exception(timeout=1.0)
    assert isinstance(exc, ValueError)


# ---------------------------------------------------------------------------
# T18  moomoo_provider: bulkhead import が成功 (統合チェック)
# ---------------------------------------------------------------------------

def test_t18_moomoo_provider_uses_bulkhead() -> None:
    """moomoo_provider が get_global_pool を import 済みであることを確認."""
    import atlas_v3.ops.moomoo_provider as mp
    # get_global_pool が module namespace に存在する
    assert hasattr(mp, "get_global_pool"), (
        "moomoo_provider.py に get_global_pool import が存在しない"
    )
