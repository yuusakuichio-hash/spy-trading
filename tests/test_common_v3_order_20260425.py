"""tests/test_common_v3_order_20260425.py — common_v3.order helper の単体テスト"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestPrepareOrderCtx:
    def test_ensures_us_prefix(self):
        from common_v3.order import prepare_order_ctx
        ctx = prepare_order_ctx(symbol="SPY", qty=1, side="BUY", is_long=True, capital_usd=10000.0)
        assert ctx.symbol == "US.SPY"

    def test_keeps_us_prefix_when_already_present(self):
        from common_v3.order import prepare_order_ctx
        ctx = prepare_order_ctx(symbol="US.QQQ", qty=2, side="SELL", is_long=False, capital_usd=10000.0)
        assert ctx.symbol == "US.QQQ"

    def test_est_margin_from_legs(self):
        from common_v3.order import prepare_order_ctx
        leg1 = MagicMock(quantity=2)
        leg2 = MagicMock(quantity=-1)
        leg3 = MagicMock(quantity=1)
        ctx = prepare_order_ctx(
            symbol="US.SPY", qty=1, side="BUY", is_long=True,
            capital_usd=10000.0, legs=[leg1, leg2, leg3],
        )
        # |2| + |-1| + |1| = 4 → 4 × 100 = 400
        assert ctx.est_margin == 400

    def test_est_margin_from_qty_when_no_legs(self):
        from common_v3.order import prepare_order_ctx
        ctx = prepare_order_ctx(
            symbol="US.SPY", qty=3, side="BUY", is_long=True, capital_usd=10000.0,
        )
        assert ctx.est_margin == 300

    def test_capital_usd_passed(self):
        from common_v3.order import prepare_order_ctx
        ctx = prepare_order_ctx(
            symbol="US.SPY", qty=1, side="BUY", is_long=True, capital_usd=25000.0,
        )
        assert ctx.capital_usd == 25000.0


class TestCheckPreTrade:
    def test_passes_normal_order(self):
        from common_v3.order import prepare_order_ctx, check_pre_trade
        # KillSwitch 非アクティブ前提で normal order
        import common_v3.risk.kill_switch as ks
        ks.deactivate(activator="test")
        ctx = prepare_order_ctx(
            symbol="SPY", qty=1, side="SELL", is_long=False,
            capital_usd=100000.0, option_price=10.0,
        )
        result = check_pre_trade(ctx)
        assert result.allowed is True

    def test_blocks_when_capital_zero(self):
        from common_v3.order import prepare_order_ctx, check_pre_trade
        ctx = prepare_order_ctx(
            symbol="SPY", qty=1, side="SELL", is_long=False,
            capital_usd=0.0, option_price=10.0,
        )
        result = check_pre_trade(ctx)
        assert not result.allowed
