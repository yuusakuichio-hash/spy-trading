"""
tests/test_qty_calculator.py — Self-Checking Pair qty計算の property-based tests

Hypothesis による property-based testing で100ケース以上を自動生成して検証する。
"""

import math
import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.qty_calculator import (
    calc_qty_pure_python,
    calc_qty_numpy,
    calc_qty_verified,
    QtyMismatchError,
)

# ── ストラテジー定義 ──────────────────────────────────────────────────────────

# 実運用の現実的な範囲を含む広いレンジでテスト
CASH_STRAT        = st.floats(min_value=0.0, max_value=1_000_000_000.0, allow_nan=False, allow_infinity=False)
PREMIUM_STRAT     = st.floats(min_value=0.01, max_value=100_000.0, allow_nan=False, allow_infinity=False)
RISK_PCT_STRAT    = st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False)
MIN_QTY_STRAT     = st.integers(min_value=1, max_value=10)
MAX_QTY_STRAT     = st.one_of(st.none(), st.integers(min_value=1, max_value=100))


# ── Property 1: 両経路が常に一致 ─────────────────────────────────────────────

@given(
    cash=CASH_STRAT,
    premium=PREMIUM_STRAT,
    max_risk_pct=RISK_PCT_STRAT,
)
@settings(max_examples=100)
def test_both_paths_agree(cash, premium, max_risk_pct):
    """pure_python と numpy が常に同じ結果を返す"""
    qty_py = calc_qty_pure_python(cash, premium, max_risk_pct)
    qty_np = calc_qty_numpy(cash, premium, max_risk_pct)
    assert qty_py == qty_np, (
        f"Mismatch: pure_python={qty_py} numpy={qty_np} "
        f"(cash={cash}, premium={premium}, risk={max_risk_pct})"
    )


# ── Property 2: verified は両者一致時に正常に返す ────────────────────────────

@given(
    cash=CASH_STRAT,
    premium=PREMIUM_STRAT,
    max_risk_pct=RISK_PCT_STRAT,
)
@settings(max_examples=100)
def test_verified_returns_int(cash, premium, max_risk_pct):
    """calc_qty_verified は正常時に int を返す"""
    result = calc_qty_verified(cash, premium, max_risk_pct)
    assert isinstance(result, int)
    assert result >= 1


# ── Property 3: 最低枚数保証 ─────────────────────────────────────────────────

@given(
    cash=CASH_STRAT,
    premium=PREMIUM_STRAT,
    max_risk_pct=RISK_PCT_STRAT,
    min_qty=MIN_QTY_STRAT,
)
@settings(max_examples=100)
def test_min_qty_guaranteed(cash, premium, max_risk_pct, min_qty):
    """結果は常に min_qty 以上"""
    result = calc_qty_verified(cash, premium, max_risk_pct, min_qty=min_qty)
    assert result >= min_qty


# ── Property 4: 上限枚数遵守 ─────────────────────────────────────────────────

@given(
    cash=CASH_STRAT,
    premium=PREMIUM_STRAT,
    max_risk_pct=RISK_PCT_STRAT,
    max_qty=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=100)
def test_max_qty_respected(cash, premium, max_risk_pct, max_qty):
    """結果は常に max_qty 以下"""
    result = calc_qty_verified(cash, premium, max_risk_pct, max_qty=max_qty)
    assert result <= max_qty


# ── Property 5: 単調増加 (cash が増えれば qty も増加 or 同一) ─────────────────

@given(
    cash_base=st.floats(min_value=1000.0, max_value=500_000_000.0, allow_nan=False, allow_infinity=False),
    premium=PREMIUM_STRAT,
    max_risk_pct=RISK_PCT_STRAT,
    multiplier=st.floats(min_value=1.01, max_value=10.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_monotonic_in_cash(cash_base, premium, max_risk_pct, multiplier):
    """資金が増えれば枚数は増加するか同一（単調非減少）"""
    qty_low  = calc_qty_verified(cash_base, premium, max_risk_pct)
    qty_high = calc_qty_verified(cash_base * multiplier, premium, max_risk_pct)
    assert qty_high >= qty_low, (
        f"Not monotonic: cash={cash_base} → qty={qty_low}, "
        f"cash={cash_base * multiplier} → qty={qty_high}"
    )


# ── Property 6: 単調減少 (premium が増えれば qty は減少 or 同一) ──────────────

@given(
    cash=st.floats(min_value=1000.0, max_value=500_000_000.0, allow_nan=False, allow_infinity=False),
    premium_base=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
    max_risk_pct=RISK_PCT_STRAT,
    multiplier=st.floats(min_value=1.01, max_value=100.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_monotonic_in_premium(cash, premium_base, max_risk_pct, multiplier):
    """プレミアムが高くなれば枚数は減少するか同一（単調非増加）"""
    premium_high = premium_base * multiplier
    assume(premium_high <= 100_000.0)

    qty_low_prem  = calc_qty_verified(cash, premium_base, max_risk_pct)
    qty_high_prem = calc_qty_verified(cash, premium_high, max_risk_pct)
    assert qty_high_prem <= qty_low_prem, (
        f"Not monotonic: premium={premium_base} → qty={qty_low_prem}, "
        f"premium={premium_high} → qty={qty_high_prem}"
    )


# ── Property 7: 境界条件 — cash=0 でも min_qty が返る ────────────────────────

@given(
    premium=PREMIUM_STRAT,
    max_risk_pct=RISK_PCT_STRAT,
)
@settings(max_examples=50)
def test_zero_cash_returns_min_qty(premium, max_risk_pct):
    """cash=0 のとき risk_budget=0 → qty_by_risk=0 → min_qty=1 が返る"""
    result = calc_qty_verified(0.0, premium, max_risk_pct)
    assert result == 1


# ── 決定論的ユニットテスト ────────────────────────────────────────────────────

class TestDeterministic:

    def test_basic_calculation(self):
        """基本ケース: cash=100000, premium=1.0, risk=5% → qty=50"""
        # risk_budget = 100000 * 0.05 = 5000
        # contract_cost = 1.0 * 100 = 100
        # qty = 5000 / 100 = 50
        assert calc_qty_verified(100_000, 1.0, 0.05) == 50

    def test_small_account_single_contract(self):
        """少額口座: cash=500, premium=2.0, risk=10% → qty=1 (floor → 0 → min_qty)"""
        # risk_budget = 500 * 0.10 = 50
        # contract_cost = 2.0 * 100 = 200
        # qty_by_risk = 0 → min_qty = 1
        assert calc_qty_verified(500, 2.0, 0.10) == 1

    def test_max_qty_cap(self):
        """max_qty制限: 計算上は100枚でもmax_qty=5なら5を返す"""
        result = calc_qty_verified(10_000_000, 0.01, 0.99, max_qty=5)
        assert result == 5

    def test_min_qty_floor(self):
        """min_qty制限: min_qty=3指定時は最低3を返す"""
        result = calc_qty_verified(100, 1.0, 0.01, min_qty=3)
        assert result == 3

    def test_typical_spy_premium(self):
        """典型的SPYプレミアム: cash=1,500,000 JPY, premium=0.50, risk=2%"""
        # risk_budget = 1,500,000 * 0.02 = 30,000
        # contract_cost = 0.50 * 100 = 50
        # qty = 30,000 / 50 = 600 → max_qty=10 → 10
        result = calc_qty_verified(1_500_000, 0.50, 0.02, max_qty=10)
        assert result == 10

    def test_high_premium_single_contract(self):
        """高プレミアム: cash=100000, premium=50.0, risk=5% → qty=1"""
        # risk_budget = 100000 * 0.05 = 5000
        # contract_cost = 50.0 * 100 = 5000
        # qty = 5000 / 5000 = 1
        assert calc_qty_verified(100_000, 50.0, 0.05) == 1


class TestValidation:

    def test_negative_cash_raises(self):
        with pytest.raises(ValueError, match="cash must be >= 0"):
            calc_qty_verified(-1.0, 1.0, 0.05)

    def test_zero_premium_raises(self):
        with pytest.raises(ValueError, match="premium must be > 0"):
            calc_qty_verified(100_000, 0.0, 0.05)

    def test_negative_premium_raises(self):
        with pytest.raises(ValueError, match="premium must be > 0"):
            calc_qty_verified(100_000, -1.0, 0.05)

    def test_zero_risk_pct_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct must be in"):
            calc_qty_verified(100_000, 1.0, 0.0)

    def test_over_100pct_risk_raises(self):
        with pytest.raises(ValueError, match="max_risk_pct must be in"):
            calc_qty_verified(100_000, 1.0, 1.01)

    def test_max_qty_less_than_min_raises(self):
        with pytest.raises(ValueError, match="max_qty"):
            calc_qty_verified(100_000, 1.0, 0.05, min_qty=5, max_qty=3)
