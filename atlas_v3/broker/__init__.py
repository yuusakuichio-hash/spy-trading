"""atlas_v3.broker — broker 抽象化 layer (本実装)

Public API:
- BrokerProtocol: place_order interface 定義 (Protocol)
- build_broker(mode, quote_ctx, paper_acc_id): mode → broker 切替 factory
- 既存 provider を re-export
"""
from __future__ import annotations

from typing import Literal, Optional, Protocol, runtime_checkable

from atlas_v3.core.engine import OrderRequest, OrderResult


@runtime_checkable
class BrokerProtocol(Protocol):
    """全 broker 実装が満たすべき interface."""

    def place_order(self, request: OrderRequest) -> OrderResult: ...


def build_broker(
    mode: Literal["paper", "live", "dry", "test-connect"],
    trade_ctx=None,
    paper_acc_id: Optional[int] = None,
):
    """mode に応じた broker を返す factory.

    Args:
        mode: 動作 mode
        trade_ctx: futu.OpenSecTradeContext (paper / live で必要)
        paper_acc_id: paper account ID (paper で必要)

    Returns:
        BrokerProtocol 実装または None (caller で stub fallback)
    """
    if mode == "paper":
        if trade_ctx is None or paper_acc_id is None:
            return None
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker
        return MoomooPaperBroker(trade_ctx, paper_acc_id=paper_acc_id)
    if mode == "live":
        # live broker は未実装 (safety で None 返却)
        return None
    return None  # dry / test-connect は stub fallback


__all__ = ["BrokerProtocol", "build_broker", "OrderRequest", "OrderResult"]


def __getattr__(name):
    if name == "MoomooPaperBroker":
        from atlas_v3.broker.moomoo_paper import MoomooPaperBroker
        return MoomooPaperBroker
    if name == "MoomooMetricProvider":
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        return MoomooMetricProvider
    if name == "YFinanceMetricProvider":
        from atlas_v3.ops.yfinance_provider import YFinanceMetricProvider
        return YFinanceMetricProvider
    raise AttributeError(f"module 'atlas_v3.broker' has no attribute {name!r}")
