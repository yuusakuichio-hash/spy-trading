"""tests/test_gex_estimator_20260425.py — GEX 推定の単体テスト"""
from __future__ import annotations

import math
from unittest.mock import MagicMock

import pandas as pd
import pytest


# ===========================================================================
# 1. calc_bs_gamma — 純粋計算
# ===========================================================================

class TestCalcBsGamma:
    def test_gamma_at_atm_is_positive(self):
        from atlas_v3.ops.gex_estimator import calc_bs_gamma
        # ATM (spot=strike), 30 日 (T=30/365), IV=0.20 (20%)
        gamma = calc_bs_gamma(spot=560.0, strike=560.0, T=30/365.0, sigma=0.20)
        assert gamma > 0

    def test_gamma_returns_zero_for_invalid_inputs(self):
        from atlas_v3.ops.gex_estimator import calc_bs_gamma
        assert calc_bs_gamma(0, 560, 30/365, 0.2) == 0.0
        assert calc_bs_gamma(560, 0, 30/365, 0.2) == 0.0
        assert calc_bs_gamma(560, 560, 0, 0.2) == 0.0
        assert calc_bs_gamma(560, 560, 30/365, 0) == 0.0

    def test_gamma_decreases_far_from_atm(self):
        from atlas_v3.ops.gex_estimator import calc_bs_gamma
        T = 30/365.0
        sigma = 0.20
        atm_gamma = calc_bs_gamma(560.0, 560.0, T, sigma)
        otm_gamma = calc_bs_gamma(560.0, 600.0, T, sigma)
        assert atm_gamma > otm_gamma  # ATM の方が gamma 大


# ===========================================================================
# 2. estimate_gex_from_moomoo — quote_ctx 連携
# ===========================================================================

class TestEstimateGexFromMoomoo:
    def test_returns_none_when_quote_ctx_is_none(self):
        from atlas_v3.ops.gex_estimator import estimate_gex_from_moomoo
        assert estimate_gex_from_moomoo(None) is None

    def test_returns_none_when_spy_price_zero(self):
        import futu as ft
        from atlas_v3.ops.gex_estimator import estimate_gex_from_moomoo
        ctx = MagicMock()
        ctx.get_market_snapshot = MagicMock(return_value=(ft.RET_OK, pd.DataFrame([{
            "code": "US.SPY", "last_price": 0.0,
        }])))
        result = estimate_gex_from_moomoo(ctx)
        assert result is None
