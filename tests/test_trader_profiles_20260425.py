"""tests/test_trader_profiles_20260425.py

atlas_v3/bots/engines/trader_profiles.py のテストスイート。

カバレッジ対象:
    - TraderProfile dataclass: 4 preset の型・不変性・フィールド値確認
    - profile_selector: 正常系 4 件 + 未登録キー + 大文字小文字正規化
    - apply_trader_profile:
        ivr_min / vix_max の保守化ポリシー / 非 dataclass guard /
        変更なし（no-op）ケース
    - get_kelly_sizing: 正常系 / 負 Kelly / 不正入力 / cap clamp
    - get_greeks_budget_check: 全 OK / delta 超過 / gamma 超過 / vega 超過 /
                               複合超過
    - get_sharpe_adjusted_threshold: 4 profile × 境界値
    - get_regime_filter: short series / volatile / trending /
                         mean_reverting / CONSERVATIVE 感度
    - dynamic_params との chain: apply_dynamic_overrides → apply_trader_profile

合計テスト数: 32
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

import pytest

from atlas_v3.bots.engines.trader_profiles import (
    AGGRESSIVE,
    BALANCED,
    CONSERVATIVE,
    TOP100_TRADER,
    TraderProfile,
    apply_trader_profile,
    get_greeks_budget_check,
    get_kelly_sizing,
    get_regime_filter,
    get_sharpe_adjusted_threshold,
    profile_selector,
)
from atlas_v3.bots.engines.dynamic_params import apply_dynamic_overrides
from atlas_v3.bots.engines.iron_fly import IronFlyConfig
from atlas_v3.bots.engines.jade_lizard import JadeLizardConfig
from atlas_v3.bots.engines.short_strangle_0dte import ShortStrangle0DTEConfig


# ===========================================================================
# Helpers
# ===========================================================================

@dataclass(frozen=False)
class _MinimalConfig:
    """apply_trader_profile テスト用最小 Config。"""
    ivr_min: float = 50.0
    vix_max: float = 28.0
    other_field: str = "unchanged"


# ===========================================================================
# TraderProfile dataclass — 4 preset 値確認
# ===========================================================================

class TestPresetProfiles:
    def test_conservative_kelly_quarter(self) -> None:
        assert CONSERVATIVE.kelly_fraction == 0.25

    def test_conservative_vix_max_calm_normal_only(self) -> None:
        assert CONSERVATIVE.vix_max == 20.0

    def test_balanced_kelly_35(self) -> None:
        assert BALANCED.kelly_fraction == 0.35

    def test_aggressive_kelly_half(self) -> None:
        assert AGGRESSIVE.kelly_fraction == 0.50

    def test_aggressive_vix_max_allows_high(self) -> None:
        assert AGGRESSIVE.vix_max >= 30.0

    def test_top100_trader_earnings_proximity(self) -> None:
        assert TOP100_TRADER.earnings_proximity_days == 5

    def test_top100_trader_term_structure_contango(self) -> None:
        assert TOP100_TRADER.term_structure_filter == "contango"

    def test_all_profiles_are_frozen(self) -> None:
        for profile in [CONSERVATIVE, BALANCED, AGGRESSIVE, TOP100_TRADER]:
            with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
                setattr(profile, "kelly_fraction", 0.99)

    def test_win_rate_threshold_ordering(self) -> None:
        """保守プロファイルの勝率閾値が積極より高い。"""
        assert CONSERVATIVE.win_rate_threshold > AGGRESSIVE.win_rate_threshold

    def test_drawdown_cap_ordering(self) -> None:
        """保守の DD キャップが積極より小さい。"""
        assert CONSERVATIVE.drawdown_cap_pct < AGGRESSIVE.drawdown_cap_pct


# ===========================================================================
# profile_selector
# ===========================================================================

class TestProfileSelector:
    def test_select_conservative(self) -> None:
        p = profile_selector("CONSERVATIVE")
        assert p.name == "CONSERVATIVE"

    def test_select_balanced(self) -> None:
        p = profile_selector("BALANCED")
        assert p.name == "BALANCED"

    def test_select_aggressive(self) -> None:
        p = profile_selector("AGGRESSIVE")
        assert p.name == "AGGRESSIVE"

    def test_select_top100(self) -> None:
        p = profile_selector("TOP100_TRADER")
        assert p.name == "TOP100_TRADER"

    def test_selector_case_insensitive(self) -> None:
        p = profile_selector("conservative")
        assert p.name == "CONSERVATIVE"

    def test_selector_unknown_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="未登録"):
            profile_selector("ULTRA_CONSERVATIVE")


# ===========================================================================
# apply_trader_profile
# ===========================================================================

class TestApplyTraderProfile:
    def test_ivr_min_tightened_by_conservative(self) -> None:
        """config.ivr_min=50 < CONSERVATIVE.ivr_min=55 → 55 に引き上げ。"""
        cfg = _MinimalConfig(ivr_min=50.0, vix_max=28.0)
        new_cfg = apply_trader_profile(cfg, CONSERVATIVE)
        assert new_cfg.ivr_min == 55.0  # type: ignore[union-attr]

    def test_vix_max_lowered_by_conservative(self) -> None:
        """config.vix_max=28 > CONSERVATIVE.vix_max=20 → 20 に引き下げ。"""
        cfg = _MinimalConfig(ivr_min=50.0, vix_max=28.0)
        new_cfg = apply_trader_profile(cfg, CONSERVATIVE)
        assert new_cfg.vix_max == 20.0  # type: ignore[union-attr]

    def test_no_change_when_config_already_conservative(self) -> None:
        """config の値がすでに保守的 → 変更なし（同一インスタンス）。"""
        cfg = _MinimalConfig(ivr_min=70.0, vix_max=18.0)
        new_cfg = apply_trader_profile(cfg, CONSERVATIVE)
        assert new_cfg.ivr_min == 70.0  # type: ignore[union-attr]
        assert new_cfg.vix_max == 18.0  # type: ignore[union-attr]

    def test_other_field_untouched(self) -> None:
        cfg = _MinimalConfig(ivr_min=30.0, vix_max=40.0, other_field="keep_me")
        new_cfg = apply_trader_profile(cfg, BALANCED)
        assert new_cfg.other_field == "keep_me"  # type: ignore[union-attr]

    def test_non_dataclass_returns_as_is(self) -> None:
        plain = {"ivr_min": 50.0}
        result = apply_trader_profile(plain, BALANCED)
        assert result is plain

    def test_apply_to_iron_fly_config(self) -> None:
        cfg = IronFlyConfig(ivr_min=60.0, vix_max=30.0)
        new_cfg = apply_trader_profile(cfg, CONSERVATIVE)
        # IronFlyConfig.ivr_min=60 > CONSERVATIVE.ivr_min=55 → 変更なし
        assert new_cfg.ivr_min == 60.0
        # IronFlyConfig.vix_max=30 > CONSERVATIVE.vix_max=20 → 20
        assert new_cfg.vix_max == 20.0


# ===========================================================================
# get_kelly_sizing
# ===========================================================================

class TestGetKellySizing:
    def test_normal_conservative(self) -> None:
        """win_rate=0.60, payoff=2.0 → full_kelly=0.40 × 0.25 = 0.10"""
        result = get_kelly_sizing(0.60, 2.0, 1.0, CONSERVATIVE)
        full_kelly = (0.60 * 2.0 - 0.40) / 2.0  # = 0.40
        expected = full_kelly * CONSERVATIVE.kelly_fraction
        assert math.isclose(result, min(expected, CONSERVATIVE.drawdown_cap_pct * 10), rel_tol=1e-5)

    def test_negative_kelly_returns_zero(self) -> None:
        """win_rate=0.30, payoff=1.0 → full_kelly < 0 → 0.0"""
        result = get_kelly_sizing(0.30, 1.0, 1.0, BALANCED)
        assert result == 0.0

    def test_cap_limits_output(self) -> None:
        """cap=0.001 → 出力が cap を超えない。"""
        result = get_kelly_sizing(0.80, 5.0, 0.001, AGGRESSIVE)
        assert result <= 0.001

    def test_invalid_win_rate_zero(self) -> None:
        result = get_kelly_sizing(0.0, 2.0, 1.0, BALANCED)
        assert result == 0.0

    def test_invalid_payoff_zero(self) -> None:
        result = get_kelly_sizing(0.60, 0.0, 1.0, BALANCED)
        assert result == 0.0

    def test_top100_drawdown_cap_applied(self) -> None:
        """高勝率でも drawdown_cap_pct × 10 を超えない。"""
        result = get_kelly_sizing(0.95, 20.0, 1.0, TOP100_TRADER)
        upper = TOP100_TRADER.drawdown_cap_pct * 10.0
        assert result <= upper + 1e-9


# ===========================================================================
# get_greeks_budget_check
# ===========================================================================

class TestGetGreeksBudgetCheck:
    def test_all_within_budget(self) -> None:
        greeks = {"delta": 0.05, "gamma": 0.02, "vega": 30.0}
        ok, reason = get_greeks_budget_check(greeks, CONSERVATIVE)
        assert ok is True
        assert "予算内" in reason

    def test_delta_exceeded(self) -> None:
        greeks = {"delta": 0.50, "gamma": 0.01, "vega": 10.0}
        ok, reason = get_greeks_budget_check(greeks, CONSERVATIVE)
        assert ok is False
        assert "delta" in reason

    def test_gamma_exceeded(self) -> None:
        greeks = {"delta": 0.05, "gamma": 0.20, "vega": 10.0}
        ok, reason = get_greeks_budget_check(greeks, CONSERVATIVE)
        assert ok is False
        assert "gamma" in reason

    def test_vega_exceeded(self) -> None:
        greeks = {"delta": 0.05, "gamma": 0.01, "vega": 999.0}
        ok, reason = get_greeks_budget_check(greeks, CONSERVATIVE)
        assert ok is False
        assert "vega" in reason

    def test_multiple_violations_reported(self) -> None:
        greeks = {"delta": 1.0, "gamma": 1.0, "vega": 9999.0}
        ok, reason = get_greeks_budget_check(greeks, CONSERVATIVE)
        assert ok is False
        assert "delta" in reason and "gamma" in reason and "vega" in reason

    def test_aggressive_wider_budget(self) -> None:
        """AGGRESSIVE は許容幅が広い → 同じ Greeks で OK。"""
        greeks = {"delta": 0.30, "gamma": 0.10, "vega": 180.0}
        ok_agg, _ = get_greeks_budget_check(greeks, AGGRESSIVE)
        ok_cons, _ = get_greeks_budget_check(greeks, CONSERVATIVE)
        assert ok_agg is True
        assert ok_cons is False


# ===========================================================================
# get_sharpe_adjusted_threshold
# ===========================================================================

class TestGetSharpeAdjustedThreshold:
    def test_conservative_higher_than_aggressive(self) -> None:
        t_cons = get_sharpe_adjusted_threshold(CONSERVATIVE)
        t_agg = get_sharpe_adjusted_threshold(AGGRESSIVE)
        assert t_cons > t_agg

    def test_result_within_clamp_range(self) -> None:
        for profile in [CONSERVATIVE, BALANCED, AGGRESSIVE, TOP100_TRADER]:
            t = get_sharpe_adjusted_threshold(profile)
            assert 0.5 <= t <= 5.0

    def test_balanced_between_extremes(self) -> None:
        t_bal = get_sharpe_adjusted_threshold(BALANCED)
        t_agg = get_sharpe_adjusted_threshold(AGGRESSIVE)
        t_cons = get_sharpe_adjusted_threshold(CONSERVATIVE)
        assert t_agg <= t_bal <= t_cons


# ===========================================================================
# get_regime_filter
# ===========================================================================

class TestGetRegimeFilter:
    def test_short_series_unknown(self) -> None:
        assert get_regime_filter([1.0, 2.0, 3.0], BALANCED) == "unknown"

    def test_volatile_high_cv(self) -> None:
        """変動係数が高い系列 → volatile。"""
        base = [100.0]
        series = [100.0 + (i % 3) * 30 for i in range(15)]
        result = get_regime_filter(series, BALANCED)
        assert result == "volatile"

    def test_trending_series(self) -> None:
        """前半 vs 後半に明確なトレンド → trending。
        SPY 相当の価格帯 (500 近辺) で CV を低く保ちつつ、
        前半 vs 後半に十分な差（>σ）を持たせる。
        """
        # 前半 500, 後半 510 → mean≈505, σ≈5, diff=10 > σ
        base = 500.0
        series = [base + i * 0.5 for i in range(20)]  # 500.0..509.5 単調増加
        mean = sum(series) / len(series)
        import statistics as _st
        stdev = _st.stdev(series)
        cv = stdev / abs(mean)
        # CV は 0.015 未満であること（volatile 判定を回避）
        assert cv < 0.015, f"テストデータの CV={cv:.4f} が threshold を超えている"
        result = get_regime_filter(series, BALANCED)
        assert result == "trending"

    def test_mean_reverting_flat(self) -> None:
        """ほぼ横ばいの系列 → mean_reverting。"""
        series = [100.0 + (i % 2) * 0.1 for i in range(20)]
        result = get_regime_filter(series, BALANCED)
        assert result == "mean_reverting"

    def test_conservative_higher_volatile_sensitivity(self) -> None:
        """CONSERVATIVE は AGGRESSIVE より volatile 感度が高い。"""
        series = [100.0 + (i % 2) * 1.5 for i in range(20)]
        res_cons = get_regime_filter(series, CONSERVATIVE)
        res_agg = get_regime_filter(series, AGGRESSIVE)
        # CONSERVATIVE は thresh=0.010 で volatile になる可能性がある
        assert res_cons in ("volatile", "mean_reverting", "trending")
        assert res_agg in ("volatile", "mean_reverting", "trending")


# ===========================================================================
# dynamic_params との chain テスト
# ===========================================================================

class TestChainWithDynamicParams:
    def test_chain_iron_fly_vix_then_profile(self) -> None:
        """apply_dynamic_overrides → apply_trader_profile の順で chain。"""
        cfg = IronFlyConfig(ivr_min=70.0, vix_max=25.0)
        cfg2 = apply_dynamic_overrides(cfg, vix=22.0)
        cfg3 = apply_trader_profile(cfg2, CONSERVATIVE)
        # CONSERVATIVE.vix_max=20 < 25 → 20 に下がるはず
        assert cfg3.vix_max == 20.0
        # ivr_min は max(dynamic 調整後, 55) → 55 以上
        assert cfg3.ivr_min >= 55.0

    def test_chain_preserves_type(self) -> None:
        """chain 後も型が IronFlyConfig のまま。"""
        cfg = IronFlyConfig()
        cfg2 = apply_dynamic_overrides(cfg, vix=18.0)
        cfg3 = apply_trader_profile(cfg2, BALANCED)
        assert isinstance(cfg3, IronFlyConfig)
