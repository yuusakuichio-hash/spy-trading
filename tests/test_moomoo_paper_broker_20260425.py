"""tests/test_moomoo_paper_broker_20260425.py — MoomooPaperBroker 単体テスト"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest


def _make_request(side="buy", qty=1, symbol="US.SPY", tactic="iron_fly"):
    from atlas_v3.core.engine import OrderRequest
    return OrderRequest(
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type="market",
        tactic_name=tactic,
        idempotency_key="test_idem_001",
    )


# ===========================================================================
# 1. BUY 成功 → submitted
# ===========================================================================

class TestMoomooPaperBrokerBuy:
    def test_buy_market_order_submitted(self):
        import futu as ft
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker

        ctx = MagicMock()
        ctx.place_order = MagicMock(return_value=(
            ft.RET_OK,
            pd.DataFrame([{"order_id": "ORDER_12345"}]),
        ))
        broker = MoomooPaperBroker(ctx, paper_acc_id=1173421)
        result = broker.place_order(_make_request(side="buy", qty=1))

        assert result.status == "submitted"
        assert result.order_id == "ORDER_12345"
        assert result.symbol == "US.SPY"

        # acc_id / trd_env=SIMULATE / trd_side=BUY が渡されること
        kwargs = ctx.place_order.call_args.kwargs
        assert kwargs["acc_id"] == 1173421
        assert kwargs["trd_env"] == ft.TrdEnv.SIMULATE
        assert kwargs["trd_side"] == ft.TrdSide.BUY
        assert kwargs["qty"] == 1
        assert kwargs["code"] == "US.SPY"


# ===========================================================================
# 2. SELL 成功 → trd_side=SELL
# ===========================================================================

class TestMoomooPaperBrokerSell:
    def test_sell_market_order_submitted(self):
        import futu as ft
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker

        ctx = MagicMock()
        ctx.place_order = MagicMock(return_value=(
            ft.RET_OK, pd.DataFrame([{"order_id": "ORDER_99"}]),
        ))
        broker = MoomooPaperBroker(ctx, paper_acc_id=1173421)
        result = broker.place_order(_make_request(side="sell"))

        assert result.status == "submitted"
        kwargs = ctx.place_order.call_args.kwargs
        assert kwargs["trd_side"] == ft.TrdSide.SELL


# ===========================================================================
# 3. moomoo error → rejected
# ===========================================================================

class TestMoomooPaperBrokerReject:
    def test_returns_rejected_on_moomoo_error(self):
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker

        ctx = MagicMock()
        ctx.place_order = MagicMock(return_value=(-1, "insufficient buying power"))
        broker = MoomooPaperBroker(ctx, paper_acc_id=1173421)
        result = broker.place_order(_make_request())

        assert result.status == "rejected"
        assert "moomoo ret=-1" in result.detail

    def test_returns_rejected_on_exception(self):
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker

        ctx = MagicMock()
        ctx.place_order = MagicMock(side_effect=RuntimeError("connection lost"))
        broker = MoomooPaperBroker(ctx, paper_acc_id=1173421)
        result = broker.place_order(_make_request())

        assert result.status == "rejected"
        assert "RuntimeError" in result.detail


# ===========================================================================
# 4. unsupported side → rejected
# ===========================================================================

class TestMoomooPaperBrokerInvalidSide:
    def test_returns_rejected_on_invalid_side(self):
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker
        broker = MoomooPaperBroker(MagicMock(), paper_acc_id=1173421)
        result = broker.place_order(_make_request(side="xxx"))
        assert result.status == "rejected"
        assert "unsupported side" in result.detail


# ===========================================================================
# 5. idempotency_key が remark に渡される
# ===========================================================================

class TestMoomooPaperBrokerIdempotency:
    def test_idempotency_key_passed_as_remark(self):
        import futu as ft
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker

        ctx = MagicMock()
        ctx.place_order = MagicMock(return_value=(
            ft.RET_OK, pd.DataFrame([{"order_id": "X"}]),
        ))
        broker = MoomooPaperBroker(ctx, paper_acc_id=1173421)
        broker.place_order(_make_request())

        kwargs = ctx.place_order.call_args.kwargs
        assert kwargs["remark"] == "test_idem_001"
