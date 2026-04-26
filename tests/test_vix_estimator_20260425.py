"""tests/test_vix_estimator_20260425.py — SPY ATM straddle IV から VIX 推定の単体テスト

検証対象:
  atlas_v3/ops/vix_estimator.py::estimate_vix_from_spy_atm()

設計:
  spy_bot.py:3254-3383 _get_vix_from_atm_straddle() の atlas_v3 移植版。
  moomoo OpenD で VIX index 配信なし (Do not support US stock index series) のため、
  SPY 0DTE ATM の Call/Put IV 平均 × 100 で VIX 近似値を算出する。
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ===========================================================================
# 1. quote_ctx=None なら None 返却
# ===========================================================================

class TestEstimateVixNoQuoteCtx:
    def test_returns_none_when_quote_ctx_is_none(self):
        from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm
        result = estimate_vix_from_spy_atm(None)
        assert result is None


# ===========================================================================
# 2. snapshot で IV が直接取れる場合 (option_implied_volatility > 0)
# ===========================================================================

class TestEstimateVixDirectIV:
    def _make_quote_ctx(self, call_iv: float, put_iv: float, spy_price: float = 560.0):
        """SPY 価格 + ATM Call/Put IV を返す quote_ctx mock を生成"""
        import futu as ft
        ctx = MagicMock()

        # SPY snapshot (last_price 取得)
        spy_snap_df = pd.DataFrame([{
            "code": "US.SPY",
            "last_price": spy_price,
        }])

        # option chain (Call / Put 各 1 strike)
        call_chain_df = pd.DataFrame([{
            "code": "US.SPY260425C560000",
            "strike_price": 560.0,
        }])
        put_chain_df = pd.DataFrame([{
            "code": "US.SPY260425P560000",
            "strike_price": 560.0,
        }])

        # option snapshot (option_implied_volatility 直接取得)
        call_opt_snap = pd.DataFrame([{
            "code": "US.SPY260425C560000",
            "option_implied_volatility": call_iv,
            "bid_price": 1.0, "ask_price": 1.1,
        }])
        put_opt_snap = pd.DataFrame([{
            "code": "US.SPY260425P560000",
            "option_implied_volatility": put_iv,
            "bid_price": 1.0, "ask_price": 1.1,
        }])

        # get_market_snapshot は引数で symbol が決まる
        def _snap(symbols):
            if symbols == ["US.SPY"]:
                return (ft.RET_OK, spy_snap_df)
            if symbols == ["US.SPY260425C560000"]:
                return (ft.RET_OK, call_opt_snap)
            if symbols == ["US.SPY260425P560000"]:
                return (ft.RET_OK, put_opt_snap)
            return (-1, "Unknown")

        ctx.get_market_snapshot = MagicMock(side_effect=_snap)

        # get_option_chain は option_type で切替
        def _chain(code, start, end, option_type):
            if option_type == ft.OptionType.CALL:
                return (ft.RET_OK, call_chain_df)
            if option_type == ft.OptionType.PUT:
                return (ft.RET_OK, put_chain_df)
            return (-1, pd.DataFrame())

        ctx.get_option_chain = MagicMock(side_effect=_chain)
        return ctx

    def test_calculates_vix_from_decimal_iv(self):
        """小数形式 IV (例: 0.18 = 18%) → 自動検出して × 100 で % 化"""
        from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm
        # call IV=0.18 (=18%), put IV=0.20 (=20%) → avg=19.0%
        ctx = self._make_quote_ctx(call_iv=0.18, put_iv=0.20)
        result = estimate_vix_from_spy_atm(ctx, underlying_code="US.SPY")
        assert result is not None
        assert 18.5 < result < 19.5  # 19.0 ± 0.5

    def test_calculates_vix_from_percent_iv_moomoo_format(self):
        """moomoo 配信は % 値そのまま (例: 11.87 = 11.87%) → そのまま使用"""
        from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm
        # call IV=11.87 (% 形式), put IV=12.13 → avg=12.0%
        ctx = self._make_quote_ctx(call_iv=11.87, put_iv=12.13)
        result = estimate_vix_from_spy_atm(ctx, underlying_code="US.SPY")
        assert result is not None
        # 旧バグでは × 100 で 1200 になっていた・修正後は 12.0 期待
        assert 11.0 < result < 13.0  # 12.0 ± 1.0
        assert result < 100  # 二重 × 100 バグ再発防止 (1200 にならない)

    def test_returns_call_only_when_put_iv_zero(self):
        from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm
        ctx = self._make_quote_ctx(call_iv=0.20, put_iv=0.20)
        result = estimate_vix_from_spy_atm(ctx, underlying_code="US.SPY")
        assert result is not None
        assert 19.5 < result < 20.5


# ===========================================================================
# 3. SPY snapshot 失敗 → None
# ===========================================================================

class TestEstimateVixSpySnapshotFail:
    def test_returns_none_when_spy_price_zero(self):
        import futu as ft
        from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm
        ctx = MagicMock()
        ctx.get_market_snapshot = MagicMock(return_value=(ft.RET_OK, pd.DataFrame([{
            "code": "US.SPY", "last_price": 0.0,
        }])))
        result = estimate_vix_from_spy_atm(ctx, underlying_code="US.SPY")
        assert result is None


# ===========================================================================
# 4. option chain が空 → None (ATM が取れない)
# ===========================================================================

class TestEstimateVixChainEmpty:
    def test_returns_none_when_chain_empty(self):
        import futu as ft
        from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm
        ctx = MagicMock()
        ctx.get_market_snapshot = MagicMock(return_value=(ft.RET_OK, pd.DataFrame([{
            "code": "US.SPY", "last_price": 560.0,
        }])))
        ctx.get_option_chain = MagicMock(return_value=(ft.RET_OK, pd.DataFrame()))
        result = estimate_vix_from_spy_atm(ctx, underlying_code="US.SPY")
        assert result is None
