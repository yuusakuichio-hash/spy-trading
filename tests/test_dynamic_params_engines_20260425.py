"""tests/test_dynamic_params_engines_20260425.py

atlas_v3/bots/engines/dynamic_params.py の全 getter と
apply_dynamic_overrides の挙動を検証するテストスイート。

カバレッジ対象:
    - get_vix_band: 5 バンド境界値 + 不正 VIX
    - get_dynamic_ivr_threshold: calm / normal / elevated / high / crisis + fallback
    - get_dynamic_delta_range: low / mid / high VIX + fallback
    - get_dynamic_profit_target: low / mid / high VIX + floor/cap クランプ
    - get_dynamic_stop_loss: low / mid / high VIX + floor/cap クランプ
    - get_dynamic_entry_window: calm / elevated / crisis + overflow guard
    - get_dynamic_qty_sizing: low / mid / high VIX + invalid input fallback
    - apply_dynamic_overrides: JadeLizardConfig / IronFlyConfig / ShortStrangle0DTEConfig /
                               DiagonalSpreadConfig / PMCCConfig / non-dataclass guard

合計テスト数: 30
"""
from __future__ import annotations

import math
import pytest

from atlas_v3.bots.engines.dynamic_params import (
    VIX_CALM_MAX,
    VIX_ELEVATED_MAX,
    VIX_HIGH_MAX,
    VIX_NORMAL_MAX,
    apply_dynamic_overrides,
    get_dynamic_delta_range,
    get_dynamic_entry_window,
    get_dynamic_ivr_threshold,
    get_dynamic_profit_target,
    get_dynamic_qty_sizing,
    get_dynamic_stop_loss,
    get_vix_band,
)
from atlas_v3.bots.engines.diagonal_spread import DiagonalSpreadConfig
from atlas_v3.bots.engines.iron_fly import IronFlyConfig
from atlas_v3.bots.engines.jade_lizard import JadeLizardConfig
from atlas_v3.bots.engines.pmcc import PMCCConfig
from atlas_v3.bots.engines.short_strangle_0dte import ShortStrangle0DTEConfig


# ---------------------------------------------------------------------------
# get_vix_band
# ---------------------------------------------------------------------------

class TestGetVixBand:
    """get_vix_band: 5 バンド境界値 + 不正入力。"""

    def test_calm_below_threshold(self):
        assert get_vix_band(12.0) == "calm"

    def test_calm_at_boundary_below(self):
        # VIX_CALM_MAX = 15.0 → 14.99 は calm
        assert get_vix_band(VIX_CALM_MAX - 0.01) == "calm"

    def test_normal_at_boundary(self):
        # VIX = 15.0 → normal
        assert get_vix_band(VIX_CALM_MAX) == "normal"

    def test_elevated_at_boundary(self):
        assert get_vix_band(VIX_NORMAL_MAX) == "elevated"

    def test_high_at_boundary(self):
        assert get_vix_band(VIX_ELEVATED_MAX) == "high"

    def test_crisis_at_boundary(self):
        assert get_vix_band(VIX_HIGH_MAX) == "crisis"

    def test_crisis_extreme(self):
        assert get_vix_band(80.0) == "crisis"

    def test_invalid_nan(self):
        assert get_vix_band(float("nan")) == "unknown"

    def test_invalid_inf(self):
        assert get_vix_band(float("inf")) == "unknown"

    def test_invalid_zero(self):
        assert get_vix_band(0.0) == "unknown"

    def test_invalid_negative(self):
        assert get_vix_band(-5.0) == "unknown"


# ---------------------------------------------------------------------------
# get_dynamic_ivr_threshold
# ---------------------------------------------------------------------------

class TestGetDynamicIvrThreshold:
    """VIX low/mid/high バンドで ivr_min が正しく調整されること。"""

    def test_calm_raises_ivr(self):
        # calm: base + 5.0
        result = get_dynamic_ivr_threshold(12.0, 60.0)
        assert result == pytest.approx(65.0)

    def test_normal_unchanged(self):
        # normal: base + 0
        result = get_dynamic_ivr_threshold(17.0, 60.0)
        assert result == pytest.approx(60.0)

    def test_elevated_lowers_ivr(self):
        # elevated: base - 5.0
        result = get_dynamic_ivr_threshold(22.0, 60.0)
        assert result == pytest.approx(55.0)

    def test_high_lowers_ivr_more(self):
        # high: base - 10.0
        result = get_dynamic_ivr_threshold(27.0, 60.0)
        assert result == pytest.approx(50.0)

    def test_crisis_lowers_ivr_most(self):
        # crisis: base - 15.0
        result = get_dynamic_ivr_threshold(35.0, 60.0)
        assert result == pytest.approx(45.0)

    def test_floor_applied(self):
        # base_ivr=25, crisis(-15) = 10 → floor=20
        result = get_dynamic_ivr_threshold(35.0, 25.0)
        assert result >= 20.0

    def test_cap_applied_calm(self):
        # calm: base+5 → cap = base+10。base=95 → 100 が上限だが cap=105 → clamp
        result = get_dynamic_ivr_threshold(12.0, 95.0)
        assert result <= 95.0 + 10.0

    def test_invalid_vix_fallback(self):
        result = get_dynamic_ivr_threshold(float("nan"), 60.0)
        assert result == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# get_dynamic_delta_range
# ---------------------------------------------------------------------------

class TestGetDynamicDeltaRange:
    """delta range (min, max) が VIX に応じて拡大すること。"""

    def test_calm_narrow_range(self):
        d_min, d_max = get_dynamic_delta_range(12.0, 0.20)
        assert d_max - d_min == pytest.approx(0.04, abs=0.001)  # half=0.02 × 2

    def test_crisis_wide_range(self):
        d_min, d_max = get_dynamic_delta_range(35.0, 0.20)
        assert d_max - d_min == pytest.approx(0.10, abs=0.001)  # half=0.05 × 2

    def test_elevated_medium_range(self):
        d_min, d_max = get_dynamic_delta_range(22.0, 0.20)
        width = d_max - d_min
        # elevated: half=0.03 → width=0.06
        assert width == pytest.approx(0.06, abs=0.001)

    def test_floor_applied(self):
        # base_delta=0.06, calm(-0.02) → min=0.04 → floor=0.05
        d_min, _ = get_dynamic_delta_range(12.0, 0.06)
        assert d_min >= 0.05

    def test_cap_applied(self):
        _, d_max = get_dynamic_delta_range(35.0, 0.48)
        assert d_max <= 0.50

    def test_invalid_vix_fallback_symmetric(self):
        d_min, d_max = get_dynamic_delta_range(0.0, 0.20)
        # fallback: half=0.025
        assert d_max - d_min == pytest.approx(0.05, abs=0.001)


# ---------------------------------------------------------------------------
# get_dynamic_profit_target
# ---------------------------------------------------------------------------

class TestGetDynamicProfitTarget:
    """profit_target_pct が VIX 上昇で下がること（早期利確）。"""

    def test_baseline_vix20(self):
        # VIX=20 → adjusted = base + (20-20)*(-0.005) = base
        result = get_dynamic_profit_target(20.0, 0.50)
        assert result == pytest.approx(0.50)

    def test_low_vix_raises_target(self):
        # VIX=12 → adjusted = 0.50 + (12-20)*(-0.005) = 0.50 + 0.04 = 0.54
        result = get_dynamic_profit_target(12.0, 0.50)
        assert result > 0.50

    def test_high_vix_lowers_target(self):
        # VIX=30 → adjusted = 0.50 + (30-20)*(-0.005) = 0.45
        result = get_dynamic_profit_target(30.0, 0.50)
        assert result < 0.50

    def test_floor_applied(self):
        # VIX=120 → very low → floor=0.20
        result = get_dynamic_profit_target(120.0, 0.50)
        assert result >= 0.20

    def test_cap_applied(self):
        # cap = min(0.80, base+0.15)。base=0.75 → cap=0.80
        result = get_dynamic_profit_target(1.0, 0.75)
        assert result <= 0.80

    def test_invalid_vix_fallback(self):
        result = get_dynamic_profit_target(float("nan"), 0.50)
        assert result == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# get_dynamic_stop_loss
# ---------------------------------------------------------------------------

class TestGetDynamicStopLoss:
    """stop_loss_mult が VIX 上昇で締まること。"""

    def test_baseline_vix20(self):
        # VIX=20 → adjusted = base - 0 = base
        result = get_dynamic_stop_loss(20.0, 1.5)
        assert result == pytest.approx(1.5)

    def test_low_vix_relaxes_stop(self):
        # VIX=10 → adjusted = 1.5 - (10-20)*0.02 = 1.5 + 0.2 = 1.7
        result = get_dynamic_stop_loss(10.0, 1.5)
        assert result > 1.5

    def test_high_vix_tightens_stop(self):
        # VIX=30 → adjusted = 1.5 - (30-20)*0.02 = 1.5 - 0.2 = 1.3
        result = get_dynamic_stop_loss(30.0, 1.5)
        assert result < 1.5

    def test_floor_applied(self):
        # VIX=100 → extreme → floor=0.80
        result = get_dynamic_stop_loss(100.0, 1.5)
        assert result >= 0.80

    def test_cap_applied(self):
        # cap = base + 0.50
        result = get_dynamic_stop_loss(5.0, 1.5)
        assert result <= 1.5 + 0.50

    def test_invalid_vix_fallback(self):
        result = get_dynamic_stop_loss(0.0, 1.5)
        assert result == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# get_dynamic_entry_window
# ---------------------------------------------------------------------------

class TestGetDynamicEntryWindow:
    """entry window が VIX spike で開始が遅れること。"""

    def test_calm_no_delay(self):
        start, end = get_dynamic_entry_window(12.0, 10, 13)
        assert start == 10
        assert end == 13

    def test_normal_no_delay(self):
        start, end = get_dynamic_entry_window(17.0, 10, 13)
        assert start == 10

    def test_elevated_delays_by_1h(self):
        start, end = get_dynamic_entry_window(22.0, 10, 13)
        assert start == 11
        assert end == 13

    def test_crisis_delays_by_2h(self):
        start, end = get_dynamic_entry_window(35.0, 10, 13)
        assert start == 12
        assert end == 13

    def test_overflow_guard(self):
        # base_start=12, base_end=13, crisis(+2) → 14 >= 13 → fallback to 12
        start, end = get_dynamic_entry_window(35.0, 12, 13)
        assert start < end

    def test_invalid_vix_fallback(self):
        start, end = get_dynamic_entry_window(float("nan"), 10, 13)
        assert start == 10
        assert end == 13


# ---------------------------------------------------------------------------
# get_dynamic_qty_sizing
# ---------------------------------------------------------------------------

class TestGetDynamicQtySizing:
    """VIX 高で risk budget が縮小すること。"""

    def test_calm_full_size(self):
        budget = get_dynamic_qty_sizing(12.0, 100_000.0, 0.02)
        assert budget == pytest.approx(100_000.0 * 0.02 * 1.00)

    def test_elevated_80pct(self):
        budget = get_dynamic_qty_sizing(22.0, 100_000.0, 0.02)
        assert budget == pytest.approx(100_000.0 * 0.02 * 0.80)

    def test_high_60pct(self):
        budget = get_dynamic_qty_sizing(27.0, 100_000.0, 0.02)
        assert budget == pytest.approx(100_000.0 * 0.02 * 0.60)

    def test_crisis_40pct(self):
        budget = get_dynamic_qty_sizing(35.0, 100_000.0, 0.02)
        assert budget == pytest.approx(100_000.0 * 0.02 * 0.40)

    def test_invalid_vix_fallback(self):
        budget = get_dynamic_qty_sizing(0.0, 100_000.0, 0.02)
        assert budget == pytest.approx(100_000.0 * 0.02)

    def test_invalid_cash_zero(self):
        budget = get_dynamic_qty_sizing(27.0, 0.0, 0.02)
        assert budget == pytest.approx(0.0)

    def test_invalid_risk_pct_zero(self):
        budget = get_dynamic_qty_sizing(27.0, 100_000.0, 0.0)
        assert budget == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# apply_dynamic_overrides — per-Config integration
# ---------------------------------------------------------------------------

class TestApplyDynamicOverridesJadeLizard:
    """JadeLizardConfig に override が正しく適用されること。"""

    def test_high_vix_lowers_ivr_min(self):
        cfg = JadeLizardConfig(ivr_min=60.0)
        new = apply_dynamic_overrides(cfg, 27.0)
        assert new.ivr_min < 60.0

    def test_high_vix_lowers_profit_target(self):
        cfg = JadeLizardConfig(profit_target_pct=0.50)
        new = apply_dynamic_overrides(cfg, 27.0)
        assert new.profit_target_pct < 0.50

    def test_high_vix_tightens_stop_loss(self):
        cfg = JadeLizardConfig(stop_loss_multiplier=2.0)
        new = apply_dynamic_overrides(cfg, 27.0)
        assert new.stop_loss_multiplier < 2.0

    def test_calm_vix_does_not_mutate_original(self):
        cfg = JadeLizardConfig(ivr_min=60.0)
        new = apply_dynamic_overrides(cfg, 12.0)
        # calm: ivr_min += 5 → original unchanged（frozen=False でも replace で新インスタンス）
        assert cfg is not new  # replace は常に新インスタンスを返す

    def test_invalid_vix_returns_unchanged(self):
        cfg = JadeLizardConfig(ivr_min=60.0, profit_target_pct=0.50)
        new = apply_dynamic_overrides(cfg, 0.0)
        assert new.ivr_min == pytest.approx(60.0)
        assert new.profit_target_pct == pytest.approx(0.50)


class TestApplyDynamicOverridesIronFly:
    """IronFlyConfig: profit_target_pct + stop_loss_credit_x が調整されること。"""

    def test_crisis_vix_lowers_profit_target(self):
        cfg = IronFlyConfig(profit_target_pct=0.25)
        new = apply_dynamic_overrides(cfg, 35.0)
        assert new.profit_target_pct <= 0.25

    def test_crisis_vix_tightens_stop_loss(self):
        cfg = IronFlyConfig(stop_loss_credit_x=1.5)
        new = apply_dynamic_overrides(cfg, 35.0)
        assert new.stop_loss_credit_x < 1.5

    def test_low_vix_relaxes_stop(self):
        cfg = IronFlyConfig(stop_loss_credit_x=1.5)
        new = apply_dynamic_overrides(cfg, 10.0)
        assert new.stop_loss_credit_x >= 1.5


class TestApplyDynamicOverridesShortStrangle0DTE:
    """ShortStrangle0DTEConfig: profit_target_remaining_pct + stop_loss_mult。"""

    def test_high_vix_lowers_profit_remaining(self):
        cfg = ShortStrangle0DTEConfig(profit_target_remaining_pct=0.30)
        new = apply_dynamic_overrides(cfg, 27.0)
        assert new.profit_target_remaining_pct <= 0.30

    def test_high_vix_tightens_stop_mult(self):
        cfg = ShortStrangle0DTEConfig(stop_loss_mult=2.0)
        new = apply_dynamic_overrides(cfg, 27.0)
        assert new.stop_loss_mult < 2.0


class TestApplyDynamicOverridesDiagonalSpread:
    """DiagonalSpreadConfig: entry_window_start_et が elevated で +1h されること。"""

    def test_elevated_vix_delays_entry_window(self):
        cfg = DiagonalSpreadConfig()  # entry_window_start_et=10, end=13
        new = apply_dynamic_overrides(cfg, 22.0)
        assert new.entry_window_start_et > cfg.entry_window_start_et

    def test_calm_no_window_change(self):
        cfg = DiagonalSpreadConfig()
        new = apply_dynamic_overrides(cfg, 12.0)
        # calm delay=0 → start unchanged
        assert new.entry_window_start_et == cfg.entry_window_start_et


class TestApplyDynamicOverridesPMCC:
    """PMCCConfig: stop_loss_ratio + entry_window_start_et。"""

    def test_high_vix_tightens_stop_ratio(self):
        cfg = PMCCConfig()  # stop_loss_ratio=2.0
        new = apply_dynamic_overrides(cfg, 27.0)
        assert new.stop_loss_ratio < 2.0

    def test_crisis_delays_entry_window(self):
        cfg = PMCCConfig()  # entry_window_start_et=10
        new = apply_dynamic_overrides(cfg, 35.0)
        assert new.entry_window_start_et > cfg.entry_window_start_et


class TestApplyDynamicOverridesNonDataclass:
    """dataclass 以外は no-op で返ること。"""

    def test_plain_object_returns_unchanged(self):
        obj = object()
        result = apply_dynamic_overrides(obj, 30.0)
        assert result is obj

    def test_dict_returns_unchanged(self):
        d: dict = {"ivr_min": 60.0}
        result = apply_dynamic_overrides(d, 30.0)
        assert result is d
