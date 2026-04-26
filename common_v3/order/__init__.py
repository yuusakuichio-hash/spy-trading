"""common_v3.order — 11 戦術共通発注ロジック (本実装)

Why
---
9 engine が PreTradeGate `_Ctx(...)` 個別 inline で書いていたため、本日 (2026-04-25)
1 件の修正 (capital_usd / est_margin / US.prefix) を 9 箇所コピペする事態が発生。
Knight Capital 2012 ($440M) 型「同一バグの多箇所コピペ」を構造的に防止する。

Public API
----------
- prepare_order_ctx(symbol, qty, side, is_long, capital_usd, legs=None, option_price=None) -> OrderCtx
  -> US.prefix 補完 + est_margin proxy + capital_usd / option_price 設定済 OrderCtx を返す
- check_pre_trade(ctx) -> GateResult
  -> PreTradeGate L1-L4 を一括チェック (check_order_critical_only の wrapper)
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from common_v3.risk.pre_trade_check import (
    OrderCtx,
    GateResult,
    check_order,
    check_order_critical_only,
)


def _ensure_us_prefix(symbol: str) -> str:
    """US.XXX 形式に補完 (PreTradeGate L2 whitelist 要件)."""
    if symbol.startswith("US."):
        return symbol
    return f"US.{symbol}"


def _proxy_est_margin(qty: int, legs: Optional[Iterable[Any]] = None) -> int:
    """est_margin proxy: legs 数量合計 × 100 (1 contract = 100 shares).

    legs 不在の engine は qty × 100 で代替。
    """
    if legs:
        try:
            return sum(abs(leg.quantity) for leg in legs) * 100
        except (AttributeError, TypeError):
            return abs(qty) * 100
    return abs(qty) * 100


def prepare_order_ctx(
    symbol: str,
    qty: int,
    side: str,
    is_long: bool,
    capital_usd: float = 0.0,
    legs: Optional[Iterable[Any]] = None,
    option_price: float = 0.0,
    open_margin_total: float = 0.0,
) -> OrderCtx:
    """11 戦術共通の OrderCtx 構築 helper.

    Args:
        symbol: "SPY" / "US.SPY" のいずれも可 (US.prefix 自動補完)
        qty: 注文数量
        side: "BUY" / "SELL"
        is_long: True=BUY long / False=SELL short
        capital_usd: 口座資金額 (PreTradeGate L3 必須・>0)
        legs: 多 leg engine 用 (sum(abs(leg.quantity)) * 100 で margin 算出)
        option_price: option 単価 (BUY の deep ITM L1 chk 用)
        open_margin_total: 既存 open margin 合計 (default 0)

    Returns:
        OrderCtx (PreTradeGate L1-L4 通過可能な形式)
    """
    return OrderCtx(
        symbol=_ensure_us_prefix(symbol),
        qty=qty,
        side=side,
        is_long=is_long,
        option_price=option_price,
        est_margin=_proxy_est_margin(qty, legs),
        capital_usd=capital_usd,
        open_margin_total=open_margin_total,
    )


def check_pre_trade(ctx: OrderCtx) -> GateResult:
    """PreTradeGate L1-L4 + KillSwitch 一括チェック wrapper."""
    return check_order_critical_only(ctx)


__all__ = [
    "OrderCtx",
    "GateResult",
    "prepare_order_ctx",
    "check_pre_trade",
    "check_order",
    "check_order_critical_only",
]
