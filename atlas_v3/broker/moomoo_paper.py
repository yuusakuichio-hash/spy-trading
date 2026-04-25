"""atlas_v3/broker/moomoo_paper.py — moomoo SIMULATE paper broker

Why
---
β-2 完成の最終段: _StubBroker (発注 skip) → moomoo SIMULATE 経由 paper 発注経路
への差替え。これで paper 30 日成績の実数値計測が可能になる。

設計:
- OpenSecTradeContext で moomoo paper account (acc_id) に SIMULATE 発注
- BrokerClient Protocol (atlas_v3.core.engine) を実装
- spy_bot.py:4688 の trade_ctx.place_order signature に準拠
- 現状 market only (limit は OrderRequest 拡張後に対応)

安全性:
- TrdEnv.SIMULATE 固定 (本番 REAL は別 broker class で実装)
- acc_id は明示注入 (環境変数経由でなく constructor で固定)
- 失敗時は OrderResult(status="rejected") で graceful return
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from atlas_v3.core.engine import OrderRequest, OrderResult

log = logging.getLogger(__name__)


class MoomooPaperBroker:
    """moomoo SIMULATE 経由の paper broker (BrokerClient Protocol 実装).

    Args:
        trade_ctx: futu.OpenSecTradeContext (paper account 接続済)
        paper_acc_id: moomoo paper account ID (例: 1173421)
    """

    def __init__(self, trade_ctx, paper_acc_id: int) -> None:
        self._trade_ctx = trade_ctx
        self._acc_id = paper_acc_id

    def place_order(self, request: OrderRequest) -> OrderResult:
        """OrderRequest を moomoo SIMULATE 注文として発注する."""
        try:
            import futu as ft
        except ImportError:
            return OrderResult(
                order_id="", symbol=request.symbol,
                status="rejected", tactic_name=request.tactic_name,
                detail="futu module not available",
            )

        # side mapping
        if request.side.lower() == "buy":
            trd_side = ft.TrdSide.BUY
        elif request.side.lower() == "sell":
            trd_side = ft.TrdSide.SELL
        else:
            return OrderResult(
                order_id="", symbol=request.symbol,
                status="rejected", tactic_name=request.tactic_name,
                detail=f"unsupported side: {request.side}",
            )

        # order_type mapping (現状 market only・limit は OrderRequest 拡張後)
        if request.order_type.lower() == "market":
            order_type = ft.OrderType.MARKET
            price = 0.0
        else:
            # 未対応 order_type は market にフォールバック
            log.warning(
                "[MoomooPaperBroker] order_type=%r 未対応・market にフォールバック",
                request.order_type,
            )
            order_type = ft.OrderType.MARKET
            price = 0.0

        try:
            ret, data = self._trade_ctx.place_order(
                price=price,
                qty=request.quantity,
                code=request.symbol,
                trd_side=trd_side,
                order_type=order_type,
                trd_env=ft.TrdEnv.SIMULATE,
                acc_id=self._acc_id,
                time_in_force=ft.TimeInForce.DAY,
                remark=request.idempotency_key or "",
            )
            if ret != ft.RET_OK:
                log.warning(
                    "[MoomooPaperBroker] place_order failed: ret=%s data=%s",
                    ret, str(data)[:200],
                )
                return OrderResult(
                    order_id="", symbol=request.symbol,
                    status="rejected", tactic_name=request.tactic_name,
                    detail=f"moomoo ret={ret}: {str(data)[:120]}",
                )

            order_id = ""
            try:
                if hasattr(data, "empty") and not data.empty:
                    order_id = str(data.iloc[0].get("order_id", "") or "")
            except Exception:
                order_id = ""

            log.info(
                "[MoomooPaperBroker] SIMULATE submitted: symbol=%s side=%s qty=%d "
                "tactic=%s order_id=%s",
                request.symbol, request.side, request.quantity,
                request.tactic_name, order_id,
            )
            return OrderResult(
                order_id=order_id or f"moomoo-{request.idempotency_key}",
                symbol=request.symbol,
                status="submitted",
                tactic_name=request.tactic_name,
                detail="moomoo SIMULATE order submitted",
            )
        except Exception as e:
            log.warning("[MoomooPaperBroker] EXC: %s", e)
            return OrderResult(
                order_id="", symbol=request.symbol,
                status="rejected", tactic_name=request.tactic_name,
                detail=f"exception: {type(e).__name__}: {str(e)[:120]}",
            )
