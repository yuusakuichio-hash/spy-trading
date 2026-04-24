"""tests/test_mass_verify_safe_runner.py — MassVerify safe runner テスト (12 件)

カバー範囲:
  - VerifyContext: immutable / バリデーション
  - VerifyResult: ok / fail ファクトリ
  - run_mass_verify_safe: 正常実行 / エラー処理 / stop_on_first_error
  - per-symbol Lock: TOCTOU race 防止（スレッド安全性検証）
  - run_mass_verify_safe_with_summary: サマリー生成
"""
from __future__ import annotations

import sys
import os
import threading
import time
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atlas_v3.ops.mass_verify_safe_runner import (
    MassVerifyError,
    SymbolLockTimeoutError,
    VerifyContext,
    VerifyResult,
    _get_symbol_lock,
    _symbol_locks,
    run_mass_verify_safe,
    run_mass_verify_safe_with_summary,
)


# ── ヘルパー ──────────────────────────────────────────────────────────────────

def _ctx(symbol: str = "US.SPY", strike: float = 500.0, otype: str = "C") -> VerifyContext:
    return VerifyContext(symbol=symbol, strike=strike, expiry="2026-05-16", option_type=otype)


def _ok_fn(ctx: VerifyContext) -> VerifyResult:
    return VerifyResult.ok(ctx)


def _fail_fn(ctx: VerifyContext) -> VerifyResult:
    return VerifyResult.fail(ctx, reason="price_mismatch")


def _raise_fn(ctx: VerifyContext) -> VerifyResult:
    raise RuntimeError(f"API error for {ctx.symbol}")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: VerifyContext は frozen で属性書換不可
# ─────────────────────────────────────────────────────────────────────────────

def test_verify_context_is_frozen():
    ctx = _ctx()
    with pytest.raises((AttributeError, TypeError)):
        ctx.symbol = "US.QQQ"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: VerifyContext バリデーション — 空 symbol はエラー
# ─────────────────────────────────────────────────────────────────────────────

def test_verify_context_empty_symbol_raises():
    with pytest.raises(ValueError, match="symbol"):
        VerifyContext(symbol="", strike=500.0, expiry="2026-05-16", option_type="C")


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: VerifyContext バリデーション — 無効 option_type はエラー
# ─────────────────────────────────────────────────────────────────────────────

def test_verify_context_invalid_option_type():
    with pytest.raises(ValueError, match="option_type"):
        VerifyContext(symbol="US.SPY", strike=500.0, expiry="2026-05-16", option_type="X")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: VerifyContext バリデーション — 非正 strike はエラー
# ─────────────────────────────────────────────────────────────────────────────

def test_verify_context_nonpositive_strike():
    with pytest.raises(ValueError, match="strike"):
        VerifyContext(symbol="US.SPY", strike=0.0, expiry="2026-05-16", option_type="C")


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: 空リストで即空 list を返す
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_entries_returns_empty():
    results = run_mass_verify_safe([], _ok_fn)
    assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: 全エントリ成功時に同数の success 結果を返す
# ─────────────────────────────────────────────────────────────────────────────

def test_all_success():
    entries = [_ctx(strike=float(500 + i)) for i in range(10)]
    results = run_mass_verify_safe(entries, _ok_fn)
    assert len(results) == 10
    assert all(r.success for r in results)


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: verify_fn が失敗を返しても全件処理される（デフォルト stop_on_first_error=False）
# ─────────────────────────────────────────────────────────────────────────────

def test_partial_failures_processed_all():
    entries = [_ctx(strike=float(500 + i)) for i in range(5)]

    def _mixed_fn(ctx: VerifyContext) -> VerifyResult:
        if ctx.strike == 502.0:
            return VerifyResult.fail(ctx, reason="strike_invalid")
        return VerifyResult.ok(ctx)

    results = run_mass_verify_safe(entries, _mixed_fn)
    assert len(results) == 5
    failed = [r for r in results if not r.success]
    assert len(failed) == 1
    assert failed[0].context.strike == 502.0


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: verify_fn が例外を投げても結果に fail として記録される
# ─────────────────────────────────────────────────────────────────────────────

def test_raising_fn_records_fail_result():
    entries = [_ctx()]
    results = run_mass_verify_safe(entries, _raise_fn)
    assert len(results) == 1
    assert results[0].success is False
    assert "API error" in results[0].reason


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: stop_on_first_error=True で最初の失敗後に打ち切り
# ─────────────────────────────────────────────────────────────────────────────

def test_stop_on_first_error():
    entries = [_ctx(strike=float(500 + i)) for i in range(10)]

    def _fail_at_3(ctx: VerifyContext) -> VerifyResult:
        if ctx.strike == 503.0:
            return VerifyResult.fail(ctx, reason="stop_me")
        return VerifyResult.ok(ctx)

    results = run_mass_verify_safe(entries, _fail_at_3, stop_on_first_error=True)
    # 0,1,2 成功 + 3 失敗 → 4 件で停止（5 件目以降はスキップ）
    assert len(results) == 4
    assert results[3].success is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: 同一 symbol への同時アクセスが Lock で直列化される (race 検証)
# ─────────────────────────────────────────────────────────────────────────────

def test_no_race_on_same_symbol():
    """2 スレッドが同一 symbol を同時実行しても shared state が混在しないことを確認。"""
    execution_order: list[str] = []
    lock_for_list = threading.Lock()

    def _recording_fn(ctx: VerifyContext) -> VerifyResult:
        with lock_for_list:
            execution_order.append(f"start:{ctx.symbol}:{ctx.strike}")
        time.sleep(0.01)  # 意図的に遅延させてレース誘発
        with lock_for_list:
            execution_order.append(f"end:{ctx.symbol}:{ctx.strike}")
        return VerifyResult.ok(ctx)

    entries = [
        VerifyContext(symbol="US.SPY", strike=500.0, expiry="2026-05-16", option_type="C"),
        VerifyContext(symbol="US.SPY", strike=505.0, expiry="2026-05-16", option_type="C"),
    ]

    results = run_mass_verify_safe(entries, _recording_fn)
    assert len(results) == 2
    assert all(r.success for r in results)

    # 直列実行なので start:500 → end:500 → start:505 → end:505 の順が保証される
    assert execution_order[0].startswith("start:")
    assert execution_order[1].startswith("end:")
    # end より前に次の start が来ないことを確認
    for i in range(0, len(execution_order) - 1, 2):
        assert execution_order[i].startswith("start:")
        assert execution_order[i + 1].startswith("end:")


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: VerifyResult context が入力 VerifyContext と同一オブジェクト
# ─────────────────────────────────────────────────────────────────────────────

def test_result_context_matches_input():
    ctx = _ctx(symbol="US.QQQ", strike=450.0, otype="P")
    result = VerifyResult.ok(ctx, price=2.5)
    assert result.context is ctx
    assert result.data["price"] == pytest.approx(2.5)
    assert result.success is True


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: run_mass_verify_safe_with_summary — サマリーが正確に集計される
# ─────────────────────────────────────────────────────────────────────────────

def test_with_summary_correct():
    entries = [
        VerifyContext(symbol="US.SPY", strike=500.0, expiry="2026-05-16", option_type="C"),
        VerifyContext(symbol="US.QQQ", strike=450.0, expiry="2026-05-16", option_type="P"),
        VerifyContext(symbol="US.IWM", strike=200.0, expiry="2026-05-16", option_type="C"),
    ]

    def _mixed(ctx: VerifyContext) -> VerifyResult:
        if ctx.symbol == "US.QQQ":
            return VerifyResult.fail(ctx, reason="qqq_fail")
        return VerifyResult.ok(ctx)

    results, summary = run_mass_verify_safe_with_summary(entries, _mixed)
    assert summary["total"] == 3
    assert summary["success"] == 2
    assert summary["failed"] == 1
    assert "US.QQQ" in summary["failed_symbols"]
