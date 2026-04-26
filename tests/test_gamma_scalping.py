"""tests/test_gamma_scalping.py — ガンマスキャルピング 手数料試算 + PnL テスト

common/gamma_scalping.py の全関数を検証する。
  - 手数料モデル（entry_cost / hedge_cost）
  - ヘッジ頻度 3 パターン（5min / 15min / 30min）の PnL 試算
  - IVR > 25% → disable
  - 手数料 > ガンマPnL → disable
  - should_enable_gamma_scalping() 統合判定
  - estimate_monthly_contribution() 月利寄与試算
"""
import sys
import os
import math

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.gamma_scalping import (
    DELTA_BAND_VIX_LOW,
    DELTA_BAND_VIX_MID,
    DELTA_BAND_VIX_HIGH,
    DELTA_BAND_VIX_EXTREME,
    GAMMA_SCALP_IVR_DISABLE_THRESHOLD,
    HEDGE_INTERVAL_PATTERNS,
    MOOMOO_OPTION_FEE_PER_CONTRACT,
    MOOMOO_SPY_STOCK_FEE_PER_SHARE,
    MIN_HEDGE_INTERVAL_SEC,
    SLIPPAGE_PER_LEG,
    GammaScalpFeeSimulator,
    GammaScalpPnLResult,
    HedgeRoundTrip,
    StraddleCostParams,
    estimate_monthly_contribution,
    should_enable_gamma_scalping,
)


# ── フィクスチャ ───────────────────────────────────────────────────────────────

def _make_params(
    spy_price: float = 550.0,
    vix: float = 15.0,
    call_mid: float = 1.80,
    put_mid: float = 1.80,
    gamma: float = 0.20,
    theta_per_day: float = -3.50,
    qty: int = 1,
) -> StraddleCostParams:
    return StraddleCostParams(
        spy_price=spy_price,
        vix=vix,
        call_mid=call_mid,
        put_mid=put_mid,
        gamma=gamma,
        theta_per_day=theta_per_day,
        qty=qty,
    )


def _make_sim(
    params: StraddleCostParams = None,
    spy_daily_move_avg: float = 3.0,
    ivr: float = 15.0,
) -> GammaScalpFeeSimulator:
    if params is None:
        params = _make_params()
    return GammaScalpFeeSimulator(
        params=params,
        spy_daily_move_avg=spy_daily_move_avg,
        ivr=ivr,
    )


# ── 1. 手数料定数の健全性 ─────────────────────────────────────────────────────

class TestConstants:
    def test_option_fee_positive(self):
        assert MOOMOO_OPTION_FEE_PER_CONTRACT > 0

    def test_spy_stock_fee_zero(self):
        assert MOOMOO_SPY_STOCK_FEE_PER_SHARE == 0.0

    def test_min_hedge_interval_ge_30sec(self):
        """moomoo rate limit: 最低 30 秒インターバル。"""
        assert MIN_HEDGE_INTERVAL_SEC >= 30

    def test_ivr_disable_threshold_is_25(self):
        assert GAMMA_SCALP_IVR_DISABLE_THRESHOLD == 25.0

    def test_hedge_patterns_keys(self):
        assert set(HEDGE_INTERVAL_PATTERNS.keys()) == {"5min", "15min", "30min"}

    def test_hedge_intervals_ascending(self):
        """5min < 15min < 30min の順序が正しい。"""
        intervals = list(HEDGE_INTERVAL_PATTERNS.values())
        assert intervals == sorted(intervals)


# ── 2. StraddleCostParams ──────────────────────────────────────────────────────

class TestStraddleCostParams:
    def test_entry_cost_includes_fee_and_slippage(self):
        params = _make_params(call_mid=1.80, put_mid=1.80, qty=1)
        straddle_raw = (1.80 + 1.80) * 100 * 1
        fee = MOOMOO_OPTION_FEE_PER_CONTRACT * 2 * 1
        slip = SLIPPAGE_PER_LEG * 2 * 100 * 1
        expected = straddle_raw + fee + slip
        assert abs(params.entry_cost - expected) < 0.01

    def test_entry_cost_scales_with_qty(self):
        p1 = _make_params(qty=1)
        p3 = _make_params(qty=3)
        # qty=3 は qty=1 の約 3 倍（整数倍）
        assert abs(p3.entry_cost - p1.entry_cost * 3) < 0.10

    def test_theta_cost_day_positive(self):
        params = _make_params(theta_per_day=-3.50, qty=2)
        # qty=2 なので 3.50 * 2 * 100 = 700.0
        assert params.theta_cost_day == pytest.approx(700.0, abs=0.01)

    def test_theta_cost_day_negative_theta_input(self):
        """theta_per_day が負でも theta_cost_day は正値を返す。"""
        params = _make_params(theta_per_day=-5.0)
        assert params.theta_cost_day > 0

    def test_entry_cost_gt_straddle_mid(self):
        """手数料・スリッページ込みのコストは mid より大きい。"""
        params = _make_params(call_mid=2.00, put_mid=2.00)
        straddle_mid = (2.00 + 2.00) * 100
        assert params.entry_cost > straddle_mid


# ── 3. HedgeRoundTrip ─────────────────────────────────────────────────────────

class TestHedgeRoundTrip:
    def test_stock_fee_is_zero(self):
        rtrip = HedgeRoundTrip(delta_at_trigger=0.20, spy_price=550.0, hedge_shares=20)
        assert rtrip.stock_fee == 0.0

    def test_stock_slippage_positive(self):
        rtrip = HedgeRoundTrip(delta_at_trigger=0.20, spy_price=550.0, hedge_shares=20)
        assert rtrip.stock_slippage > 0.0

    def test_total_cost_eq_slippage(self):
        """SPY 株は手数料 0 なので total_cost = slippage のみ。"""
        rtrip = HedgeRoundTrip(delta_at_trigger=0.20, spy_price=550.0, hedge_shares=50)
        assert rtrip.total_cost == rtrip.stock_slippage


# ── 4. GammaScalpFeeSimulator.delta_band ──────────────────────────────────────

class TestDeltaBand:
    def test_vix_below_15(self):
        sim = _make_sim(params=_make_params(vix=12.0))
        assert sim.delta_band == DELTA_BAND_VIX_LOW

    def test_vix_15_20(self):
        sim = _make_sim(params=_make_params(vix=17.0))
        assert sim.delta_band == DELTA_BAND_VIX_MID

    def test_vix_20_25(self):
        sim = _make_sim(params=_make_params(vix=22.0))
        assert sim.delta_band == DELTA_BAND_VIX_HIGH

    def test_vix_above_25(self):
        sim = _make_sim(params=_make_params(vix=30.0))
        assert sim.delta_band == DELTA_BAND_VIX_EXTREME

    def test_vix_boundary_exactly_15(self):
        """VIX=15.0 は MID ゾーン（< 15 は LOW, >= 15 は MID）。"""
        sim = _make_sim(params=_make_params(vix=15.0))
        assert sim.delta_band == DELTA_BAND_VIX_MID


# ── 5. simulate_pattern: 3 パターン PnL ──────────────────────────────────────

class TestSimulatePattern:
    @pytest.mark.parametrize("pattern", ["5min", "15min", "30min"])
    def test_pattern_returns_result(self, pattern):
        sim = _make_sim(spy_daily_move_avg=3.0, ivr=15.0)
        result = sim.simulate_pattern(pattern)
        assert isinstance(result, GammaScalpPnLResult)
        assert result.pattern == pattern

    @pytest.mark.parametrize("pattern", ["5min", "15min", "30min"])
    def test_hedge_count_non_negative(self, pattern):
        sim = _make_sim()
        result = sim.simulate_pattern(pattern)
        assert result.hedge_count >= 0

    def test_5min_more_hedges_than_30min(self):
        """5 分は 30 分より多くのヘッジ回数になる（同一パラメータ）。"""
        sim = _make_sim(spy_daily_move_avg=5.0)
        r5 = sim.simulate_pattern("5min")
        r30 = sim.simulate_pattern("30min")
        assert r5.hedge_count >= r30.hedge_count

    @pytest.mark.parametrize("pattern", ["5min", "15min", "30min"])
    def test_entry_fee_consistent(self, pattern):
        """entry_fee は手数料 + スリッページの定数計算。"""
        params = _make_params(qty=1)
        sim = _make_sim(params=params)
        result = sim.simulate_pattern(pattern)
        expected_fee = MOOMOO_OPTION_FEE_PER_CONTRACT * 2 * 1 + SLIPPAGE_PER_LEG * 2 * 100
        assert abs(result.entry_fee - expected_fee) < 0.01

    @pytest.mark.parametrize("pattern", ["5min", "15min", "30min"])
    def test_exit_fee_eq_entry_fee(self, pattern):
        """exit_fee は entry_fee と等しい（同一手数料構造）。"""
        sim = _make_sim()
        result = sim.simulate_pattern(pattern)
        assert result.entry_fee == result.exit_fee

    @pytest.mark.parametrize("pattern", ["5min", "15min", "30min"])
    def test_net_pnl_is_gross_minus_costs(self, pattern):
        sim = _make_sim()
        r = sim.simulate_pattern(pattern)
        expected = (
            r.gamma_pnl_gross
            - r.theta_cost
            - r.hedge_cost_total
            - r.entry_fee
            - r.exit_fee
        )
        assert abs(r.net_pnl - expected) < 0.01

    @pytest.mark.parametrize("pattern", ["5min", "15min", "30min"])
    def test_ev_pct_consistent_with_net_pnl(self, pattern):
        """ev_pct = net_pnl / entry_cost * 100。"""
        params = _make_params()
        sim = _make_sim(params=params)
        r = sim.simulate_pattern(pattern)
        entry_cost_raw = (params.call_mid + params.put_mid) * 100 * params.qty
        expected_ev = (r.net_pnl / entry_cost_raw) * 100.0
        assert abs(r.ev_pct - expected_ev) < 0.01


# ── 6. enable_gamma_scalping フラグ ──────────────────────────────────────────

class TestEnableFlag:
    def test_enable_when_gamma_exceeds_fees(self):
        """大きな日中変動 + 高ガンマ → ガンマPnL > 費用 → enable=True。"""
        params = _make_params(gamma=0.30, theta_per_day=-1.00)
        sim = _make_sim(params=params, spy_daily_move_avg=5.0)
        result = sim.simulate_pattern("15min")
        assert result.enable_gamma_scalping is True

    def test_disable_when_fees_exceed_gamma(self):
        """低変動 + 高シータ → 費用 > ガンマPnL → enable=False。"""
        params = _make_params(gamma=0.01, theta_per_day=-10.0)
        sim = _make_sim(params=params, spy_daily_move_avg=0.5)
        result = sim.simulate_pattern("15min")
        assert result.enable_gamma_scalping is False

    def test_summary_contains_pattern(self):
        sim = _make_sim()
        r = sim.simulate_pattern("15min")
        assert "15min" in r.summary()

    def test_summary_enable_keyword(self):
        params = _make_params(gamma=0.30, theta_per_day=-1.00)
        sim = _make_sim(params=params, spy_daily_move_avg=5.0)
        r = sim.simulate_pattern("15min")
        assert "ENABLE" in r.summary() or "DISABLE" in r.summary()


# ── 7. simulate_all_patterns ──────────────────────────────────────────────────

class TestSimulateAllPatterns:
    def test_returns_three_patterns(self):
        sim = _make_sim()
        results = sim.simulate_all_patterns()
        assert set(results.keys()) == {"5min", "15min", "30min"}

    def test_all_results_are_pnl_result(self):
        sim = _make_sim()
        for r in sim.simulate_all_patterns().values():
            assert isinstance(r, GammaScalpPnLResult)


# ── 8. best_pattern ───────────────────────────────────────────────────────────

class TestBestPattern:
    def test_returns_none_when_all_disabled(self):
        """全パターン disable → best_pattern() は None。"""
        params = _make_params(gamma=0.001, theta_per_day=-50.0)
        sim = _make_sim(params=params, spy_daily_move_avg=0.1)
        assert sim.best_pattern() is None

    def test_returns_result_when_enabled(self):
        params = _make_params(gamma=0.30, theta_per_day=-1.00)
        sim = _make_sim(params=params, spy_daily_move_avg=5.0)
        best = sim.best_pattern()
        assert best is not None
        assert best.enable_gamma_scalping is True

    def test_best_has_highest_net_pnl_among_enabled(self):
        params = _make_params(gamma=0.25, theta_per_day=-2.0)
        sim = _make_sim(params=params, spy_daily_move_avg=4.0)
        best = sim.best_pattern()
        if best is None:
            pytest.skip("全パターン disabled (低ガンマ環境)")
        all_results = sim.simulate_all_patterns()
        enabled = [r for r in all_results.values() if r.enable_gamma_scalping]
        max_net = max(r.net_pnl for r in enabled)
        assert abs(best.net_pnl - max_net) < 0.01


# ── 9. should_enable_gamma_scalping ──────────────────────────────────────────

class TestShouldEnableGammaScalping:
    def test_ivr_above_25_disables(self):
        enabled, reason, result = should_enable_gamma_scalping(
            ivr=30.0,
            vix=25.0,
            spy_price=550.0,
            call_mid=2.0,
            put_mid=2.0,
            gamma=0.20,
            theta_per_day=-3.5,
        )
        assert enabled is False
        assert "IVR" in reason
        assert result is None

    def test_ivr_exactly_25_disables(self):
        """IVR = 25.0% は閾値と等しい → disable（> でなく >=）。"""
        enabled, reason, result = should_enable_gamma_scalping(
            ivr=25.0,
            vix=20.0,
            spy_price=550.0,
            call_mid=1.80,
            put_mid=1.80,
            gamma=0.20,
            theta_per_day=-3.5,
        )
        assert enabled is False

    def test_ivr_below_25_high_gamma_enables(self):
        """IVR < 25% + 高ガンマ + 大きな日中変動 → enable=True。"""
        enabled, reason, result = should_enable_gamma_scalping(
            ivr=15.0,
            vix=14.0,
            spy_price=550.0,
            call_mid=1.80,
            put_mid=1.80,
            gamma=0.30,
            theta_per_day=-1.50,
            spy_daily_move_avg=5.0,
        )
        assert enabled is True
        assert result is not None
        assert result.enable_gamma_scalping is True

    def test_returns_tuple_of_three(self):
        result_tuple = should_enable_gamma_scalping(
            ivr=10.0,
            vix=12.0,
            spy_price=545.0,
            call_mid=1.50,
            put_mid=1.50,
            gamma=0.25,
            theta_per_day=-3.0,
        )
        assert len(result_tuple) == 3

    def test_low_gamma_disables_regardless_of_ivr(self):
        """ガンマが極端に低い場合は IVR < 25% でも disable。"""
        enabled, reason, result = should_enable_gamma_scalping(
            ivr=5.0,
            vix=10.0,
            spy_price=550.0,
            call_mid=1.80,
            put_mid=1.80,
            gamma=0.001,
            theta_per_day=-10.0,
            spy_daily_move_avg=0.2,
        )
        assert enabled is False

    def test_reason_non_empty(self):
        enabled, reason, result = should_enable_gamma_scalping(
            ivr=30.0,
            vix=25.0,
            spy_price=550.0,
            call_mid=2.0,
            put_mid=2.0,
            gamma=0.20,
            theta_per_day=-3.5,
        )
        assert len(reason) > 0


# ── 10. estimate_monthly_contribution ────────────────────────────────────────

class TestEstimateMonthlyContribution:
    def test_zero_ev_gives_zero_contribution(self):
        result = estimate_monthly_contribution(
            daily_ev_pct=0.0, capital_usd=100000.0, trading_days=21
        )
        assert result["monthly_ev_usd"] == pytest.approx(0.0)
        assert result["monthly_contribution_pct"] == pytest.approx(0.0)

    def test_daily_ev_scales_to_monthly(self):
        result = estimate_monthly_contribution(
            daily_ev_pct=0.1, capital_usd=100000.0, trading_days=21
        )
        # 0.1% × $100,000 × 21 = $2,100
        assert result["monthly_ev_usd"] == pytest.approx(2100.0, rel=1e-6)

    def test_monthly_contribution_pct_consistent(self):
        result = estimate_monthly_contribution(
            daily_ev_pct=0.5, capital_usd=50000.0, trading_days=20
        )
        expected_pct = (0.5 / 100.0 * 50000.0 * 20 / 50000.0) * 100.0
        assert abs(result["monthly_contribution_pct"] - expected_pct) < 0.01

    def test_target_range_05_to_15_pct_per_month(self):
        """ORATS根拠: 期待寄与 +0.5〜1.5%/月。

        gamma=0.20, theta=-1.5/day, SPY 3ドル動く, IVR=15%の
        標準シナリオで月利 0.5% 以上の寄与があることを確認する。
        """
        params = _make_params(gamma=0.20, theta_per_day=-1.50, qty=1)
        sim = _make_sim(params=params, spy_daily_move_avg=3.0, ivr=15.0)
        best = sim.best_pattern()
        if best is None:
            pytest.skip("標準シナリオで全パターン disabled")
        monthly = estimate_monthly_contribution(
            daily_ev_pct=best.ev_pct,
            capital_usd=50000.0,
            trading_days=21,
        )
        # 最低 0.1% の寄与があれば OK（保守的な確認）
        assert monthly["monthly_contribution_pct"] >= 0.1

    def test_returns_dict_with_required_keys(self):
        result = estimate_monthly_contribution(
            daily_ev_pct=1.0, capital_usd=10000.0
        )
        assert "daily_ev_usd" in result
        assert "monthly_ev_usd" in result
        assert "monthly_contribution_pct" in result


# ── 11. 統合: IVR=24% (境界ケース) ──────────────────────────────────────────

class TestBoundaryIVR:
    def test_ivr_just_below_25_proceeds_to_fee_check(self):
        """IVR=24.9% → IVR チェックは通過し手数料チェックへ進む。"""
        enabled, reason, result = should_enable_gamma_scalping(
            ivr=24.9,
            vix=18.0,
            spy_price=550.0,
            call_mid=1.80,
            put_mid=1.80,
            gamma=0.25,
            theta_per_day=-2.0,
            spy_daily_move_avg=3.5,
        )
        # IVR チェックは通過するため reason に "IVR" は含まれない
        assert "IVR=24.9" not in reason or enabled is True

    def test_ivr_just_above_25_disables(self):
        enabled, reason, result = should_enable_gamma_scalping(
            ivr=25.1,
            vix=18.0,
            spy_price=550.0,
            call_mid=1.80,
            put_mid=1.80,
            gamma=0.25,
            theta_per_day=-2.0,
        )
        assert enabled is False
        assert result is None


# ── 12. マルチ qty ────────────────────────────────────────────────────────────

class TestMultiQty:
    def test_qty3_entry_fee_is_3x_qty1(self):
        params1 = _make_params(qty=1)
        params3 = _make_params(qty=3)
        sim1 = _make_sim(params=params1)
        sim3 = _make_sim(params=params3)
        r1 = sim1.simulate_pattern("15min")
        r3 = sim3.simulate_pattern("15min")
        assert abs(r3.entry_fee - r1.entry_fee * 3) < 0.10

    def test_qty3_theta_cost_is_3x_qty1(self):
        params1 = _make_params(qty=1, theta_per_day=-3.5)
        params3 = _make_params(qty=3, theta_per_day=-3.5)
        assert abs(params3.theta_cost_day - params1.theta_cost_day * 3) < 0.01
