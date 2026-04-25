"""tests/test_realized_volatility_20260425.py — HV 計算の単体テスト"""
from __future__ import annotations

import math
from unittest.mock import MagicMock

import pandas as pd
import pytest


# ===========================================================================
# 1. calc_hv_from_closes — 純粋計算
# ===========================================================================

class TestCalcHvFromCloses:
    def test_returns_none_when_too_few_closes(self):
        from atlas_v3.ops.realized_volatility import calc_hv_from_closes
        assert calc_hv_from_closes([]) is None
        assert calc_hv_from_closes([100.0]) is None
        assert calc_hv_from_closes([100.0, 101.0]) is None  # log_returns 1 件 → ddof=1 で計算不能

    def test_zero_volatility_constant_price(self):
        """価格が一定なら log return が全て 0 → HV ≈ 0"""
        from atlas_v3.ops.realized_volatility import calc_hv_from_closes
        closes = [100.0] * 10
        hv = calc_hv_from_closes(closes)
        assert hv is not None
        assert hv < 0.01  # ほぼゼロ

    def test_known_hv_value(self):
        """既知の入力で年率 HV を検算する。

        日次 1% std (= 0.01) → 年率 HV = 0.01 × sqrt(252) × 100 ≈ 15.87%
        """
        from atlas_v3.ops.realized_volatility import calc_hv_from_closes
        # 日次 +1% / -1% を交互に 30 日 → log return std ≈ 0.01
        closes = [100.0]
        for i in range(30):
            if i % 2 == 0:
                closes.append(closes[-1] * 1.01)  # +1%
            else:
                closes.append(closes[-1] * 0.99)  # -1%
        hv = calc_hv_from_closes(closes)
        assert hv is not None
        # 0.01 × sqrt(252) × 100 = 15.87% ± 1.0
        expected = 0.01 * math.sqrt(252) * 100
        assert abs(hv - expected) < 1.5

    def test_skips_invalid_prices(self):
        """0 / 負価格は log return 計算スキップ"""
        from atlas_v3.ops.realized_volatility import calc_hv_from_closes
        closes = [100.0, 101.0, 0.0, 102.0, 101.0]
        # 価格 > 0 のペアだけ計算
        hv = calc_hv_from_closes(closes)
        assert hv is not None
        assert hv > 0


# ===========================================================================
# 2. estimate_hv_from_moomoo — quote_ctx 連携
# ===========================================================================

class TestEstimateHvFromMoomoo:
    def test_returns_none_when_quote_ctx_is_none(self):
        from atlas_v3.ops.realized_volatility import estimate_hv_from_moomoo
        assert estimate_hv_from_moomoo(None) is None

    def test_calculates_hv_from_kline(self):
        import futu as ft
        from atlas_v3.ops.realized_volatility import estimate_hv_from_moomoo
        # mock kline DataFrame (30 日分・日次 +0.5% trend)
        closes = [100.0 * (1.005 ** i) for i in range(30)]
        kline_df = pd.DataFrame({
            "close": closes,
            "time_key": [f"2026-{m+1:02d}-01" for m in range(30)],
        })
        ctx = MagicMock()
        ctx.request_history_kline = MagicMock(return_value=(ft.RET_OK, kline_df, None))

        hv = estimate_hv_from_moomoo(ctx, underlying_code="US.SPY")
        assert hv is not None
        assert hv > 0

    def test_returns_none_on_kline_failure(self):
        import futu as ft
        from atlas_v3.ops.realized_volatility import estimate_hv_from_moomoo
        ctx = MagicMock()
        ctx.request_history_kline = MagicMock(return_value=(-1, None, None))
        assert estimate_hv_from_moomoo(ctx) is None
