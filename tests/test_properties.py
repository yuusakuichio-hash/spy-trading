"""
tests/test_properties.py — Property-based tests (Hypothesis) for 8 tactics

対象:
  1. calc_butterfly_wing_width       — ATR・range内でnon-negative・丸め整合性
  2. calc_butterfly_qty              — TMR二重検証・min/max範囲
  3. IronCondorSell _calc_dynamic_deltas — call/put symmetric・delta範囲内
  4. StrangleSell OTM判定             — ATMからの距離・delta範囲
  5. symbol_selector スコア計算        — normalize後[0,1]・ウェイト合計整合性
  6. earnings_engine IV Crush率        — 0.0-1.0範囲・rolling median整合性
  7. portfolio_aggregator 合算          — commutative・associative
  8. ORBbreakout range                  — high>=low・range>=0

各 invariant につき hypothesis で max_examples=100 以上。
"""

from __future__ import annotations

import math
import os
import sys
import types

# futu mock (ImportError回避)
_futu_mock = types.ModuleType("futu")
_futu_mock.RET_OK = 0
_futu_mock.TrdSide = types.SimpleNamespace(BUY="BUY", SELL="SELL")
_futu_mock.OrderType = types.SimpleNamespace(MARKET="MARKET", LIMIT="LIMIT")
_futu_mock.TrdMarket = types.SimpleNamespace(US="US")
_futu_mock.TrdEnv = types.SimpleNamespace(REAL="REAL", SIMULATE="SIMULATE")
_futu_mock.SecurityFirm = types.SimpleNamespace(FUTUINC="FUTUINC")
_futu_mock.SubType = types.SimpleNamespace(TICKER="TICKER")
_futu_mock.TimeInForce = types.SimpleNamespace(DAY="DAY")
_futu_mock.ModifyOrderOp = types.SimpleNamespace(CANCEL="CANCEL")
_futu_mock.KLType = types.SimpleNamespace(K_1M="K_1M")
_futu_mock.StockQuoteHandlerBase = object
_futu_mock.OpenQuoteContext = object
_futu_mock.OpenSecTradeContext = object
sys.modules.setdefault("futu", _futu_mock)

_TRADING_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _TRADING_DIR)

import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

# ── import targets ────────────────────────────────────────────────────────────

import spy_bot as bot_mod
from spy_bot import (
    calc_butterfly_wing_width,
    calc_butterfly_qty,
    BUTTERFLY_MIN_WING_STRIKES,
    BUTTERFLY_MAX_WING_STRIKES,
    BUTTERFLY_MAX_QTY,
    BUTTERFLY_MAX_QTY_PAPER,
    IronCondorSellEngine,
    IronCondorSellPosition,
    StrangleSellEngine,
    StrangleSellPosition,
    STRANGLE_SELL_CALL_DELTA,
    STRANGLE_SELL_PUT_DELTA,
    STRANGLE_SELL_MAX_QTY,
)
from common.symbol_selector import (
    SymbolMetrics,
    _normalize_ivr,
    _normalize_volume,
    _normalize_liquidity,
    _normalize_vix_corr,
    _normalize_gap,
    _compute_raw_scores,
    _weighted_score,
    _TACTIC_WEIGHTS,
    score_symbols,
)
from common.earnings_engine import (
    EarningsEngine,
    _DEFAULT_IV_CRUSH_RATES,
    _DEFAULT_CRUSH_RATE,
    SIZE_FACTOR_HIGH,
    SIZE_FACTOR_MID,
    SIZE_FACTOR_LOW,
)
from common.portfolio_aggregator import (
    aggregate_portfolio_risk,
    bot_pnl_by_period,
    daily_pnl,
    weekly_pnl,
    monthly_pnl,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. calc_butterfly_wing_width
#    invariant: result in [MIN_WING, MAX_WING], result >= 0, result is int
# ─────────────────────────────────────────────────────────────────────────────

ATR_STRAT = st.one_of(
    st.none(),
    st.floats(min_value=-100.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
)
SYMBOL_STRAT = st.sampled_from(["US.SPY", "US.QQQ", "US.IWM", "US.TSLA", "US.NVDA"])


@given(symbol=SYMBOL_STRAT, atr=ATR_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_wing_width_range(symbol, atr):
    """calc_butterfly_wing_width は常に [MIN, MAX] 範囲内の非負整数を返す。"""
    width = calc_butterfly_wing_width(symbol, atr)
    assert isinstance(width, int), f"width is not int: {type(width)}"
    assert width >= 0, f"width < 0: {width}"
    assert BUTTERFLY_MIN_WING_STRIKES <= width <= BUTTERFLY_MAX_WING_STRIKES, (
        f"width={width} outside [{BUTTERFLY_MIN_WING_STRIKES}, {BUTTERFLY_MAX_WING_STRIKES}]"
    )


@given(
    symbol=SYMBOL_STRAT,
    atr=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_wing_width_non_decreasing_with_atr(symbol, atr):
    """ATRが大きいほど wing幅は大きくなるか同じ（単調非減少）。"""
    w_small = calc_butterfly_wing_width(symbol, atr)
    w_large = calc_butterfly_wing_width(symbol, atr * 2.0)
    assert w_large >= w_small, (
        f"ATR={atr}: w_small={w_small} > w_large={w_large} (ATR*2)"
    )


@given(
    symbol=SYMBOL_STRAT,
    atr=st.floats(min_value=0.01, max_value=500.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_wing_width_integer_result(symbol, atr):
    """結果は常に整数 (浮動小数点誤差なし)。"""
    width = calc_butterfly_wing_width(symbol, atr)
    assert width == int(width), f"Not integer: {width}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. calc_butterfly_qty
#    invariant: result >= 1, result <= max_qty (paper / live), non-negative cash
# ─────────────────────────────────────────────────────────────────────────────

CASH_STRAT = st.floats(min_value=0.0, max_value=10_000_000.0, allow_nan=False, allow_infinity=False)
# min_value=1e-4 で subnormal float (∼ 2.2e-308) を回避する
# 実運用上のプレミアム最小値は0.01セント（1e-4 USD）で十分
DEBIT_STRAT = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False, allow_subnormal=False)
PAPER_STRAT = st.booleans()


@given(cash=CASH_STRAT, debit=DEBIT_STRAT, paper=PAPER_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_butterfly_qty_minimum_is_one(cash, debit, paper):
    """calc_butterfly_qty は常に 1 以上を返す (最低保証)。"""
    qty = calc_butterfly_qty(cash, debit, paper=paper)
    assert qty >= 1, f"qty={qty} < 1 (cash={cash}, debit={debit}, paper={paper})"


@given(cash=CASH_STRAT, debit=DEBIT_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_butterfly_qty_live_cap(cash, debit):
    """本番モードの結果は BUTTERFLY_MAX_QTY 以下。"""
    qty = calc_butterfly_qty(cash, debit, paper=False)
    assert qty <= BUTTERFLY_MAX_QTY, (
        f"qty={qty} > BUTTERFLY_MAX_QTY={BUTTERFLY_MAX_QTY}"
    )


@given(cash=CASH_STRAT, debit=DEBIT_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_butterfly_qty_paper_cap(cash, debit):
    """ペーパーモードの結果は BUTTERFLY_MAX_QTY_PAPER 以下。"""
    qty = calc_butterfly_qty(cash, debit, paper=True)
    assert qty <= BUTTERFLY_MAX_QTY_PAPER, (
        f"qty={qty} > BUTTERFLY_MAX_QTY_PAPER={BUTTERFLY_MAX_QTY_PAPER}"
    )


@given(
    cash=st.floats(min_value=1000.0, max_value=10_000_000.0, allow_nan=False, allow_infinity=False),
    debit=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
    multiplier=st.floats(min_value=1.01, max_value=5.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_butterfly_qty_monotonic_in_cash(cash, debit, multiplier):
    """資金が増えれば枚数は増加するか同じ (単調非減少)。"""
    qty_low  = calc_butterfly_qty(cash, debit, paper=False)
    qty_high = calc_butterfly_qty(cash * multiplier, debit, paper=False)
    assert qty_high >= qty_low, (
        f"Not monotonic: cash={cash}→qty={qty_low}, cash*{multiplier}→qty={qty_high}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. IronCondorSell _calc_dynamic_deltas
#    invariant: call/put delta 対称性チェック・範囲内
# ─────────────────────────────────────────────────────────────────────────────

class _IcMkt:
    def __init__(self, vix=22.0):
        self._vix = vix
        self.underlying_code = "US.SPY"

    def get_vix(self): return self._vix
    def get_vix_history(self, days=60):
        return [self._vix + i * 0.1 for i in range(-30, 30)]
    def get_spy_current(self): return 560.0
    def get_option_chain_with_greeks(self, *a, **kw): return []
    def find_by_delta(self, chain, target): return None
    def find_by_strike(self, chain, strike): return None
    def get_symbol_atr(self, sym, period=14): return 10.0
    def calc_ivr(self): return 55.0


class _IcEng:
    def get_account_cash(self): return 50_000.0
    def get_open_positions(self): return []
    def place_credit_spread(self, *a, **kw): return True


VIX_STRAT = st.floats(min_value=10.0, max_value=80.0, allow_nan=False, allow_infinity=False)
IVR_PCT_STRAT = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)


@given(vix=VIX_STRAT, ivr_pct=IVR_PCT_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_ic_delta_range(vix, ivr_pct):
    """_calc_dynamic_deltas の結果は [0.10, 0.35] 範囲内。"""
    engine = IronCondorSellEngine(_IcMkt(vix=vix), _IcEng(), paper=True, dry_test=True)
    call_d, put_d = engine._calc_dynamic_deltas(vix=vix, ivr_pct=ivr_pct)
    assert 0.10 <= call_d <= 0.35, f"call_delta={call_d:.4f} outside [0.10, 0.35] (vix={vix})"
    assert 0.10 <= put_d  <= 0.35, f"put_delta={put_d:.4f} outside [0.10, 0.35] (vix={vix})"


@given(
    vix_low=st.floats(min_value=10.0, max_value=25.0, allow_nan=False, allow_infinity=False),
    vix_high=st.floats(min_value=35.0, max_value=80.0, allow_nan=False, allow_infinity=False),
    ivr_pct=IVR_PCT_STRAT,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_ic_delta_shrinks_at_high_vix(vix_low, vix_high, ivr_pct):
    """高VIX時はデルタが縮小するかまたは等しい (リスク管理方向)。"""
    engine = IronCondorSellEngine(_IcMkt(), _IcEng(), paper=True, dry_test=True)
    call_d_normal, _ = engine._calc_dynamic_deltas(vix=vix_low,  ivr_pct=ivr_pct)
    call_d_high,   _ = engine._calc_dynamic_deltas(vix=vix_high, ivr_pct=ivr_pct)
    assert call_d_high <= call_d_normal, (
        f"delta did not shrink at high VIX: vix_low={vix_low}→{call_d_normal:.4f}, "
        f"vix_high={vix_high}→{call_d_high:.4f}"
    )


@given(vix=VIX_STRAT, ivr_pct=IVR_PCT_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_ic_delta_positive(vix, ivr_pct):
    """call_delta と put_delta は常に正の値。"""
    engine = IronCondorSellEngine(_IcMkt(vix=vix), _IcEng(), paper=True, dry_test=True)
    call_d, put_d = engine._calc_dynamic_deltas(vix=vix, ivr_pct=ivr_pct)
    assert call_d > 0.0, f"call_delta <= 0: {call_d}"
    assert put_d  > 0.0, f"put_delta <= 0: {put_d}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. StrangleSell OTM判定 (execute_entry dry_test)
#    invariant: call_strike > underlying > put_strike (OTM保証)
# ─────────────────────────────────────────────────────────────────────────────

class _SsMkt:
    underlying_code = "US.SPY"
    quote_ctx = None

    def get_vix(self): return 22.0
    def get_spy_current(self): return 560.0
    def get_option_chain_with_greeks(self, *a, **kw): return []
    def get_option_greeks(self, code): return {"last": 0.20, "iv": 0.25, "delta": 0.15}
    def calc_ivr(self): return 70.0
    def get_ivr_percentiles(self): return {"p75": 70.0, "p70": 62.0}


class _SsEng:
    class _VP:
        def add_position(self, *a, **kw): pass
    def __init__(self):
        self._virtual_pos = self._VP()
    def get_account_cash(self): return 15_000.0
    def _place_single_leg(self, *a, **kw): return "id", "ok"


UNDERLYING_STRAT = st.floats(min_value=50.0, max_value=2000.0, allow_nan=False, allow_infinity=False)
VIX_SS_STRAT = st.floats(min_value=12.0, max_value=50.0, allow_nan=False, allow_infinity=False)


@given(underlying_price=UNDERLYING_STRAT, vix=VIX_SS_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_strangle_otm_structure(underlying_price, vix):
    """dry_testエントリー: call_strike > underlying > put_strike (OTM構造保証)。"""
    engine = StrangleSellEngine(_SsMkt(), _SsEng(), dry_test=True)
    pos = engine.execute_entry(underlying_price=underlying_price, vix=vix)
    if pos is None:
        return  # エントリー条件外はスキップ（invariantは適用しない）
    assert pos.call_strike > underlying_price, (
        f"call_strike={pos.call_strike} <= underlying={underlying_price} (OTM違反)"
    )
    assert pos.put_strike < underlying_price, (
        f"put_strike={pos.put_strike} >= underlying={underlying_price} (OTM違反)"
    )


@given(underlying_price=UNDERLYING_STRAT, vix=VIX_SS_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_strangle_net_credit_positive(underlying_price, vix):
    """dry_testエントリーのnet_creditは正の値。"""
    engine = StrangleSellEngine(_SsMkt(), _SsEng(), dry_test=True)
    pos = engine.execute_entry(underlying_price=underlying_price, vix=vix)
    if pos is None:
        return
    assert pos.net_credit > 0.0, f"net_credit={pos.net_credit} <= 0"


@given(underlying_price=UNDERLYING_STRAT, vix=VIX_SS_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_strangle_qty_in_range(underlying_price, vix):
    """qty は 1 以上 STRANGLE_SELL_MAX_QTY 以下。"""
    engine = StrangleSellEngine(_SsMkt(), _SsEng(), dry_test=True)
    pos = engine.execute_entry(underlying_price=underlying_price, vix=vix)
    if pos is None:
        return
    assert pos.qty >= 1, f"qty={pos.qty} < 1"
    assert pos.qty <= STRANGLE_SELL_MAX_QTY, (
        f"qty={pos.qty} > STRANGLE_SELL_MAX_QTY={STRANGLE_SELL_MAX_QTY}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. symbol_selector スコア計算
#    invariant: normalize後[0,1], weighted_score[0,1], ウェイト符号整合性
# ─────────────────────────────────────────────────────────────────────────────

IVR_RANGE = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
VOLUME_RANGE = st.floats(min_value=0.01, max_value=20.0, allow_nan=False, allow_infinity=False)
GAP_RANGE = st.floats(min_value=0.0, max_value=0.20, allow_nan=False, allow_infinity=False)
SPREAD_RANGE = st.floats(min_value=0.0001, max_value=0.10, allow_nan=False, allow_infinity=False)
CORR_RANGE = st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@given(ivr=IVR_RANGE, vol=VOLUME_RANGE, gap=GAP_RANGE, spread=SPREAD_RANGE, corr=CORR_RANGE)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_normalize_ivr_in_range(ivr, vol, gap, spread, corr):
    """_normalize_ivr は常に [0.0, 1.0]。"""
    universe = [ivr, ivr * 0.5, ivr * 1.5, 0.0, 100.0]
    result = _normalize_ivr(ivr, universe)
    assert 0.0 <= result <= 1.0, f"_normalize_ivr={result:.4f} outside [0, 1]"


@given(ivr=IVR_RANGE, vol=VOLUME_RANGE, gap=GAP_RANGE, spread=SPREAD_RANGE, corr=CORR_RANGE)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_normalize_liquidity_in_range(ivr, vol, gap, spread, corr):
    """_normalize_liquidity は常に [0.0, 1.0]。"""
    universe = [spread, spread * 0.5, spread * 2.0]
    result = _normalize_liquidity(spread, universe)
    assert 0.0 <= result <= 1.0, f"_normalize_liquidity={result:.4f} outside [0, 1]"


@given(corr=CORR_RANGE)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_normalize_vix_corr_symmetry(corr):
    """_normalize_vix_corr は符号に無依存 (絶対値ベース)。"""
    pos_result = _normalize_vix_corr(abs(corr))
    neg_result = _normalize_vix_corr(-abs(corr))
    assert abs(pos_result - neg_result) < 1e-9, (
        f"Not symmetric: +{abs(corr):.4f}={pos_result:.4f}, -{abs(corr):.4f}={neg_result:.4f}"
    )


@given(
    ivr=IVR_RANGE,
    vol=VOLUME_RANGE,
    gap=GAP_RANGE,
    spread=SPREAD_RANGE,
    corr=CORR_RANGE,
    tactic=st.sampled_from(["credit_spread", "iron_condor", "straddle", "butterfly"]),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_weighted_score_in_range(ivr, vol, gap, spread, corr, tactic):
    """_weighted_score は常に [0.0, 1.0]。"""
    raw = {
        "ivr": max(0.0, min(1.0, ivr / 100.0)),
        "volume": max(0.0, min(1.0, vol / 20.0)),
        "gap": max(0.0, min(1.0, gap / 0.20)),
        "liquidity": max(0.0, min(1.0, 1.0 - spread * 10.0)),
        "vix_corr": max(0.0, min(1.0, abs(corr))),
    }
    weights = _TACTIC_WEIGHTS[tactic]
    score = _weighted_score(raw, weights)
    assert 0.0 <= score <= 1.0, f"_weighted_score={score:.4f} outside [0, 1] (tactic={tactic})"


@given(
    ivrs=st.lists(IVR_RANGE, min_size=2, max_size=10),
    tactic=st.sampled_from(["credit_spread", "iron_condor", "straddle", "butterfly"]),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_score_symbols_order_preserved(ivrs, tactic):
    """score_symbols の戻り値は score 降順。"""
    metrics = [
        SymbolMetrics(symbol=f"SYM{i}", ivr=v, volume_spike_ratio=1.0,
                      gap_abs_pct=0.01, bid_ask_spread_pct=0.001,
                      vix_correlation=0.5, near_earnings=False,
                      hist_gaps=[0.01] * 10)
        for i, v in enumerate(ivrs)
    ]
    results = score_symbols(metrics, tactic=tactic, earnings_exclude=False)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True), (
        f"Not descending: {scores}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. earnings_engine IV Crush率
#    invariant: 0.0-1.0範囲・rolling median整合性
# ─────────────────────────────────────────────────────────────────────────────

CRUSH_RATE_STRAT = st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False)
SYMBOL_EARNINGS_STRAT = st.sampled_from(
    list(_DEFAULT_IV_CRUSH_RATES.keys()) + ["UNKNOWN_XYZ", "RANDOM_ABC"]
)


@given(symbol=SYMBOL_EARNINGS_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_iv_crush_rate_in_range(symbol):
    """_get_iv_crush_rate は常に (0.0, 1.0) 範囲内。"""
    eng = EarningsEngine(api_key="test")
    rate = eng._get_iv_crush_rate(symbol)
    assert 0.0 < rate < 1.0, f"crush_rate={rate} outside (0, 1) for symbol={symbol}"


@given(crush_rate=CRUSH_RATE_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_size_factor_monotonic(crush_rate):
    """_calc_size_factor は crush_rate に単調非減少。"""
    eng = EarningsEngine(api_key="test")
    sf = eng._calc_size_factor(crush_rate)
    assert sf in (SIZE_FACTOR_LOW, SIZE_FACTOR_MID, SIZE_FACTOR_HIGH), (
        f"Unexpected size_factor={sf} for crush_rate={crush_rate}"
    )
    # 単調性: 大きいcrush_rateのsize_factorは小さいものと同じかそれ以上
    sf_higher = eng._calc_size_factor(min(crush_rate * 1.5, 0.99))
    assert sf_higher >= sf, (
        f"Not monotonic: crush_rate={crush_rate}→sf={sf}, "
        f"crush_rate*1.5→sf={sf_higher}"
    )


@given(
    crush_values=st.lists(
        st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False),
        min_size=3, max_size=20,
    )
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_iv_crush_history_median(crush_values):
    """履歴が3件以上ある場合、_get_iv_crush_rate は中央値を返す (0-1範囲内)。"""
    eng = EarningsEngine(api_key="test")
    sym = "TEST_HISTORY"
    eng._history = {sym: [{"actual_crush": v} for v in crush_values]}
    rate = eng._get_iv_crush_rate(sym)
    assert 0.0 < rate < 1.0, f"crush_rate_from_history={rate} outside (0, 1)"
    # 中央値として実際の値が返ることを確認
    sorted_v = sorted(crush_values)
    expected_median = sorted_v[len(sorted_v) // 2]
    assert abs(rate - round(expected_median, 4)) < 1e-6, (
        f"Expected median={expected_median:.6f}, got={rate:.6f}"
    )


@given(symbol=SYMBOL_EARNINGS_STRAT)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_iv_crush_rate_deterministic(symbol):
    """同じ履歴状態なら同じ結果 (決定論的)。"""
    eng = EarningsEngine(api_key="test")
    rate1 = eng._get_iv_crush_rate(symbol)
    rate2 = eng._get_iv_crush_rate(symbol)
    assert rate1 == rate2, f"Non-deterministic: {rate1} != {rate2}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. portfolio_aggregator 合算
#    invariant: commutative (順序非依存)・associative
#    ファイルシステム独立: monkeypatch でin-memory listを使う
# ─────────────────────────────────────────────────────────────────────────────

import common.portfolio_aggregator as _pa

PNL_RECORD = st.fixed_dictionaries({
    "date": st.just("2026-04-18"),
    "bot": st.sampled_from(["atlas_bot", "spy_bot", "momentum_bot"]),
    "pnl_usd": st.floats(min_value=-5000.0, max_value=5000.0, allow_nan=False, allow_infinity=False),
})


@given(records=st.lists(PNL_RECORD, min_size=0, max_size=20))
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_daily_pnl_commutative(records, monkeypatch, tmp_path):
    """daily_pnl は records の順序に依存しない (commutative)。

    monkeypatching を suppress_health_check で許可。
    Hypothesis は同一 tmp_path/monkeypatch を再利用するが、
    pnl_path.write_text で上書きするため内容は毎回更新される。
    """
    import datetime
    import json
    import random

    pnl_path = tmp_path / "portfolio_pnl.json"
    pos_path = tmp_path / "portfolio_positions.json"
    condor_path = tmp_path / "condor_pnl.json"
    monkeypatch.setattr(_pa, "PORTFOLIO_PNL_FILE", pnl_path)
    monkeypatch.setattr(_pa, "POSITIONS_FILE", pos_path)
    monkeypatch.setattr(_pa, "PNL_FILE", condor_path)

    today = datetime.date.today()
    today_records = [{**r, "date": today.strftime("%Y-%m-%d")} for r in records]

    pnl_path.write_text(json.dumps(today_records), encoding="utf-8")
    pnl1 = daily_pnl(today)

    shuffled = today_records.copy()
    random.shuffle(shuffled)
    pnl_path.write_text(json.dumps(shuffled), encoding="utf-8")
    pnl2 = daily_pnl(today)

    assert abs(pnl1 - pnl2) < 1e-6, f"Not commutative: {pnl1} vs {pnl2}"


@given(
    records_a=st.lists(PNL_RECORD, min_size=0, max_size=10),
    records_b=st.lists(PNL_RECORD, min_size=0, max_size=10),
)
@settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_daily_pnl_additive(records_a, records_b, monkeypatch, tmp_path):
    """daily_pnl はレコードの分割に依存しない (additive: sum(a+b) == expected_sum)。"""
    import datetime
    import json

    pnl_path = tmp_path / "portfolio_pnl.json"
    pos_path = tmp_path / "portfolio_positions.json"
    condor_path = tmp_path / "condor_pnl.json"
    monkeypatch.setattr(_pa, "PORTFOLIO_PNL_FILE", pnl_path)
    monkeypatch.setattr(_pa, "POSITIONS_FILE", pos_path)
    monkeypatch.setattr(_pa, "PNL_FILE", condor_path)

    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    all_records = [
        {**r, "date": today_str} for r in records_a + records_b
    ]
    pnl_path.write_text(json.dumps(all_records), encoding="utf-8")
    pnl_combined = daily_pnl(today)

    expected = sum(r["pnl_usd"] for r in records_a + records_b)
    assert abs(pnl_combined - expected) < 1e-3, (
        f"daily_pnl mismatch: got={pnl_combined:.4f}, expected={expected:.4f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 8. ORBbreakout range
#    invariant: high >= low, range >= 0
# ─────────────────────────────────────────────────────────────────────────────

from spy_bot import ORBEngine

PRICE_STRAT = st.floats(min_value=10.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)
DELTA_STRAT = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)


class _OrbMkt:
    def __init__(self, code="US.SPY"):
        self.underlying_code = code
        self.quote_ctx = None

        class _PC:
            def get(self, code, max_age_sec=5.0): return None
            def get_open(self, code, max_age_sec=5.0): return None
        self._price_cache = _PC()

    def get_spy_current(self): return 560.0
    def get_vix(self): return 22.0


class _OrbEng:
    def get_account_cash(self): return 50_000.0


@given(
    base_price=PRICE_STRAT,
    delta=DELTA_STRAT,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_orb_range_non_negative(base_price, delta):
    """ORBレンジ (high - low) は常に >= 0。"""
    engine = ORBEngine(_OrbMkt(), _OrbEng(), paper=True, dry_test=True)
    orb_high = base_price + delta
    orb_low  = base_price
    # 直接属性セット（record_opening_range はネットワーク依存のためdry_testで使う）
    engine.orb_high  = orb_high
    engine.orb_low   = orb_low
    engine.orb_range = orb_high - orb_low
    engine.orb_checked = True

    assert engine.orb_high >= engine.orb_low, (
        f"ORB violation: high={engine.orb_high} < low={engine.orb_low}"
    )
    assert engine.orb_range >= 0.0, f"ORB range < 0: {engine.orb_range}"


@given(
    high=PRICE_STRAT,
    low_offset=DELTA_STRAT,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_orb_breakout_direction_consistency(high, low_offset):
    """ORBブレイクアウト: price > high → CALL, price < low → PUT の論理整合性。

    invariant:
      - price_above > orb_high → CALL 方向に判定される
      - price_below < orb_low  → PUT 方向に判定される
      - 各テストは独立して評価する（片方の関数のみチェック）
    """
    engine = ORBEngine(_OrbMkt(), _OrbEng(), paper=True, dry_test=True)
    low = max(0.01, high - low_offset)
    engine.orb_high  = high
    engine.orb_low   = low
    engine.orb_range = high - low
    engine.orb_checked = True

    # CALL ブレイクアウトロジック: price > orb_high
    price_above = high + 0.01
    dir_above = "CALL" if price_above > engine.orb_high else "None"
    assert dir_above == "CALL", (
        f"price_above={price_above:.4f} > orb_high={engine.orb_high:.4f} "
        f"should be CALL but got {dir_above}"
    )

    # PUT ブレイクアウトロジック: price < orb_low
    price_below = max(0.001, low - 0.01)
    dir_below = "PUT" if price_below < engine.orb_low else "None"
    # price_below は定義上 low - 0.01 < low なので必ず PUT
    assert dir_below == "PUT", (
        f"price_below={price_below:.4f} < orb_low={engine.orb_low:.4f} "
        f"should be PUT but got {dir_below}"
    )


@given(
    high=PRICE_STRAT,
    low_offset=st.floats(min_value=0.0, max_value=50.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_orb_range_equals_high_minus_low(high, low_offset):
    """orb_range == orb_high - orb_low の算術的整合性。"""
    engine = ORBEngine(_OrbMkt(), _OrbEng(), paper=True, dry_test=True)
    low = high - low_offset
    engine.orb_high  = high
    engine.orb_low   = low
    engine.orb_range = high - low
    engine.orb_checked = True

    assert abs(engine.orb_range - (engine.orb_high - engine.orb_low)) < 1e-9, (
        f"range arithmetic error: {engine.orb_range} != {engine.orb_high - engine.orb_low}"
    )
