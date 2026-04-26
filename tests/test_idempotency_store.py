"""
tests/test_idempotency_store.py — common_v3/idempotency/store.py のテスト

カバー範囲:
  1. check_and_mark — 新規キーは True
  2. check_and_mark — 同キーを再呼出すると False（重複）
  3. check_and_mark — TTL 失効後は True（新規扱い）
  4. make_job_key  — 同引数で同一キー / 異引数で異なるキー
  5. with_idempotency — 新規時は func 呼出 / 重複時は func スキップ
  6. concurrent write race — 複数スレッドから同一キーを競合登録しても 1 回のみ True
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from common_v3.idempotency.store import (
    IdempotencyStore,
    OrderNotSentError,
    make_job_key,
    with_idempotency,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> IdempotencyStore:
    """一時ディレクトリに store ファイルを作成する。"""
    return IdempotencyStore(path=tmp_path / "idem.json")


# ---------------------------------------------------------------------------
# 1. 新規キーは True
# ---------------------------------------------------------------------------


def test_new_key_returns_true(store: IdempotencyStore) -> None:
    result = store.check_and_mark("key_new", ttl_sec=300)
    assert result is True


# ---------------------------------------------------------------------------
# 2. 同キー再呼出は False（重複）
# ---------------------------------------------------------------------------


def test_duplicate_key_returns_false(store: IdempotencyStore) -> None:
    store.check_and_mark("key_dup", ttl_sec=300)
    result = store.check_and_mark("key_dup", ttl_sec=300)
    assert result is False


# ---------------------------------------------------------------------------
# 3. TTL 失効後は True（新規扱い）
# ---------------------------------------------------------------------------


def test_expired_key_becomes_new(tmp_path: Path) -> None:
    store = IdempotencyStore(path=tmp_path / "idem.json")
    # TTL=1秒で登録
    assert store.check_and_mark("key_expire", ttl_sec=1) is True
    # 1秒以上待って再チェック
    time.sleep(1.1)
    # 失効済みなので True（新規扱い）
    assert store.check_and_mark("key_expire", ttl_sec=1) is True


# ---------------------------------------------------------------------------
# 4. make_job_key — 一意性・同引数で同一値
# ---------------------------------------------------------------------------


def test_make_job_key_deterministic() -> None:
    dt = datetime(2026, 4, 23, 9, 30, 0, tzinfo=timezone.utc)
    k1 = make_job_key("ORB_1DTE", "SPY", dt)
    k2 = make_job_key("ORB_1DTE", "SPY", dt)
    assert k1 == k2
    assert k1.startswith("v3_")


def test_make_job_key_differs_by_strategy() -> None:
    dt = datetime(2026, 4, 23, 9, 30, 0, tzinfo=timezone.utc)
    k1 = make_job_key("ORB_1DTE", "SPY", dt)
    k2 = make_job_key("IC_1DTE", "SPY", dt)
    assert k1 != k2


def test_make_job_key_differs_by_symbol() -> None:
    dt = datetime(2026, 4, 23, 9, 30, 0, tzinfo=timezone.utc)
    k1 = make_job_key("ORB_1DTE", "SPY", dt)
    k2 = make_job_key("ORB_1DTE", "QQQ", dt)
    assert k1 != k2


def test_make_job_key_differs_by_time() -> None:
    dt1 = datetime(2026, 4, 23, 9, 30, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 4, 23, 9, 31, 0, tzinfo=timezone.utc)
    k1 = make_job_key("ORB_1DTE", "SPY", dt1)
    k2 = make_job_key("ORB_1DTE", "SPY", dt2)
    assert k1 != k2


# ---------------------------------------------------------------------------
# 5. with_idempotency — 冪等性
# ---------------------------------------------------------------------------


def test_with_idempotency_calls_func_once(store: IdempotencyStore) -> None:
    counter = {"n": 0}

    def job() -> str:
        counter["n"] += 1
        return "done"

    key = "job_once"
    result1 = with_idempotency(store, key, job, ttl_sec=300)
    result2 = with_idempotency(store, key, job, ttl_sec=300)

    assert result1 == "done"
    assert result2 is None          # 重複 → スキップ → None
    assert counter["n"] == 1        # func は 1 回だけ呼ばれる


def test_with_idempotency_different_keys_call_func(store: IdempotencyStore) -> None:
    counter = {"n": 0}

    def job() -> None:
        counter["n"] += 1

    with_idempotency(store, "key_a", job, ttl_sec=300)
    with_idempotency(store, "key_b", job, ttl_sec=300)

    assert counter["n"] == 2


# ---------------------------------------------------------------------------
# 6. concurrent write race — @sync_only により別スレッドからの呼出は RuntimeError
# ---------------------------------------------------------------------------
# Sprint 1 C-001: check_and_mark に @sync_only が統合された。
# 別スレッドからの呼出は RuntimeError を送出し、results に追加されない。
# flock による 1 True 保証は @sync_only により不要（非 main thread 呼出自体が禁止）。


def test_concurrent_write_single_winner(tmp_path: Path) -> None:
    """別スレッドから check_and_mark() を呼ぶと @sync_only が RuntimeError を送出する。

    Sprint 1 C-001 以前: 複数スレッドから check_and_mark → flock で 1 True のみ
    Sprint 1 C-001 以降: 別スレッドからの呼出は RuntimeError → results には追加されない
    """
    store = IdempotencyStore(path=tmp_path / "race.json")
    results: list[bool] = []
    errors: list[RuntimeError] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            r = store.check_and_mark("race_key", ttl_sec=300)
            with lock:
                results.append(r)
        except RuntimeError as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, name=f"idem-race-{i}") for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # @sync_only により全 10 スレッドが RuntimeError を送出する
    assert len(errors) == 10, f"全スレッドが RuntimeError のはず: errors={len(errors)}"
    assert len(results) == 0, "非 main thread からは results に追加されない"
    for e in errors:
        assert "sync-only contract violation" in str(e)


# ---------------------------------------------------------------------------
# 7. C1: デフォルトパスが trading/data/ を指すこと（parents[2] 検証）
# ---------------------------------------------------------------------------


def test_default_path_points_to_trading_data() -> None:
    """_DEFAULT_STORE_PATH が /Users/.../trading/data/idempotency_keys.json を指す。"""
    from common_v3.idempotency.store import _DEFAULT_STORE_PATH

    # parents[2] = trading/ であることを確認
    assert _DEFAULT_STORE_PATH.parts[-1] == "idempotency_keys.json"
    assert _DEFAULT_STORE_PATH.parts[-2] == "data"
    # trading/ 直下の data/ であること（parents[3] = Users/ ではない）
    trading_dir = _DEFAULT_STORE_PATH.parent.parent
    assert trading_dir.name == "trading", (
        f"expected .../trading/data/idempotency_keys.json, got {_DEFAULT_STORE_PATH}"
    )


# ---------------------------------------------------------------------------
# 8. C2: with_idempotency — OrderNotSentError の時のみキーがロールバックされること
# ---------------------------------------------------------------------------


def test_with_idempotency_rollback_on_func_exception(tmp_path: Path) -> None:
    """func() が OrderNotSentError を送出した場合、キーが _unmark されて再試行が通ること。

    R2-C1 対応: 送信前例外（OrderNotSentError）のみロールバック対象。
    """
    store = IdempotencyStore(path=tmp_path / "idem.json")
    key = "rollback_key"

    def not_sent_job() -> None:
        raise OrderNotSentError("broker 未送信・バリデーション失敗")

    # 1回目: OrderNotSentError が伝播すること
    with pytest.raises(OrderNotSentError, match="broker 未送信"):
        with_idempotency(store, key, not_sent_job, ttl_sec=300)

    # ロールバック後: 同じキーで再試行が通ること（キーが残っていると False になる）
    counter = {"n": 0}

    def success_job() -> str:
        counter["n"] += 1
        return "ok"

    result = with_idempotency(store, key, success_job, ttl_sec=300)
    assert result == "ok", "OrderNotSentError ロールバック後の再試行が成功すべき"
    assert counter["n"] == 1


# ---------------------------------------------------------------------------
# 10. R2-C1: with_idempotency — generic Exception では unmark されず同キー二度発注不可
# ---------------------------------------------------------------------------


def test_with_idempotency_no_unmark_on_generic_exception(tmp_path: Path) -> None:
    """func() が generic Exception（RuntimeError 等）を送出しても unmark されないこと。

    broker 送信後の副作用例外（ログ失敗・通知失敗等）では unmark が起きないため、
    同一キーで再度 func() が呼ばれることはない（Knight Capital 型二重発注防止）。
    """
    store = IdempotencyStore(path=tmp_path / "idem.json")
    key = "no_unmark_key"

    counter = {"n": 0}

    def post_send_side_effect_failure() -> None:
        counter["n"] += 1
        # broker 送信後の副作用失敗を模擬（ログ失敗・通知エラー等）
        raise RuntimeError("送信後の副作用失敗（ログエラー等）")

    # 1回目: RuntimeError が伝播すること
    with pytest.raises(RuntimeError, match="送信後の副作用失敗"):
        with_idempotency(store, key, post_send_side_effect_failure, ttl_sec=300)

    assert counter["n"] == 1

    # キーはロールバックされていないため、同じキーで呼んでも func は実行されない
    result = with_idempotency(store, key, post_send_side_effect_failure, ttl_sec=300)
    assert result is None, "unmark されていないので重複判定で None になるべき"
    assert counter["n"] == 1, "func は 2 回目に呼ばれてはいけない"


# ---------------------------------------------------------------------------
# 11. R2-C1: _unmark は公開 API でないこと（ImportError または AttributeError）
# ---------------------------------------------------------------------------


def test_unmark_not_in_public_api() -> None:
    """from common_v3.idempotency import unmark が ImportError になること。

    _unmark はプライベートメソッドであり、モジュールレベルの公開シンボルではない。
    """
    import importlib

    mod = importlib.import_module("common_v3.idempotency")
    assert not hasattr(mod, "unmark"), (
        "'unmark' は common_v3.idempotency の公開 API にあってはならない"
    )
    # __all__ にも含まれていないこと
    assert "unmark" not in mod.__all__, (
        "'unmark' は __all__ に含まれてはならない"
    )


# ---------------------------------------------------------------------------
# 9. C3: ttl_sec=0 は ValueError を送出すること
# ---------------------------------------------------------------------------


def test_ttl_sec_zero_raises(store: IdempotencyStore) -> None:
    """check_and_mark に ttl_sec=0 を渡すと ValueError。"""
    with pytest.raises(ValueError, match="ttl_sec must be positive"):
        store.check_and_mark("key_zero_ttl", ttl_sec=0)


def test_ttl_sec_negative_raises(store: IdempotencyStore) -> None:
    """check_and_mark に ttl_sec=-1 を渡すと ValueError。"""
    with pytest.raises(ValueError, match="ttl_sec must be positive"):
        store.check_and_mark("key_neg_ttl", ttl_sec=-1)


def test_with_idempotency_ttl_zero_raises(store: IdempotencyStore) -> None:
    """with_idempotency に ttl_sec=0 を渡すと ValueError。"""
    with pytest.raises(ValueError, match="ttl_sec must be positive"):
        with_idempotency(store, "key_zero", lambda: None, ttl_sec=0)


def test_with_idempotency_ttl_negative_raises(store: IdempotencyStore) -> None:
    """with_idempotency に ttl_sec=-1 を渡すと ValueError。"""
    with pytest.raises(ValueError, match="ttl_sec must be positive"):
        with_idempotency(store, "key_neg", lambda: None, ttl_sec=-1)
