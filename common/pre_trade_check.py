"""Pre-Trade Check — Defense-in-Depth 4層防護

全 place_order 呼び出し直前にcheck_order()を呼ぶ。
違反時は発注拒否（ログのみ）+通知ポリシーに従って必要時のみPushover。
"""
from __future__ import annotations
import collections
import datetime
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

from common.risk_limits import RiskLimits, load_limits
from common import kill_switch, portfolio_aggregator

try:
    from common.quote_context_manager import get_global_manager as _qcm_get
    _QCM_AVAILABLE = True
except ImportError:
    _QCM_AVAILABLE = False


# 発注頻度トラッキング（プロセス内メモリ）
_recent_orders: collections.deque = collections.deque(maxlen=100)
_recent_keys: collections.deque = collections.deque(maxlen=20)


@dataclass
class OrderContext:
    """発注前の全情報"""
    symbol: str
    strike: float
    side: str            # "SELL" / "BUY"
    qty: int
    option_price: float
    bid: float = 0
    ask: float = 0
    est_margin: float = 0      # 必要証拠金
    capital_usd: float = 0     # 現口座資本
    open_positions: int = 0
    open_margin_total: float = 0
    symbol_margin: float = 0   # 同銘柄の既存証拠金合計
    paper: bool = True


@dataclass
class CheckResult:
    allow: bool
    layer: str
    reason: str
    severity: str = "low"   # low / medium / high / critical
    notify_required: bool = False


def _bas_pct(bid: float, ask: float) -> float:
    if ask <= 0:
        return 1.0
    return (ask - bid) / ask


def check_order(ctx: OrderContext, limits: Optional[RiskLimits] = None) -> CheckResult:
    """全4層check + Kill Switch + Loss Gate"""
    if limits is None:
        limits = load_limits(capital_usd=ctx.capital_usd, paper=ctx.paper)

    # Kill Switch最優先
    if kill_switch.is_active():
        return CheckResult(False, "KILL", f"Kill Switch発動中: {kill_switch.reason()}", "critical", True)

    # Quote Context level check (段階的フェイルオーバー)
    if _QCM_AVAILABLE:
        qcm = _qcm_get()
        if not qcm.allow_new_entry():
            return CheckResult(
                False, "QCM",
                f"Quote context level={qcm.get_level()} — 新規エントリー停止中(既存exitは許可)",
                "high", False,
            )
        # level 1-2 は margin_scale で est_margin を事実上縮小判定
        scale = qcm.margin_scale()
        if scale < 1.0 and ctx.est_margin > 0:
            # スケール適用: est_margin / scale で実効判定
            ctx.est_margin = ctx.est_margin / max(scale, 0.01)

    # Layer 1: Pre-trade Sanity
    if ctx.symbol not in limits.symbol_whitelist:
        return CheckResult(False, "L1", f"Symbol not in whitelist: {ctx.symbol}", "high", True)

    if ctx.qty <= 0 or ctx.qty > limits.max_qty_per_order:
        return CheckResult(False, "L1", f"qty out of range: {ctx.qty} (max={limits.max_qty_per_order})", "high", True)

    if ctx.option_price >= limits.max_option_price:
        return CheckResult(False, "L1",
                           f"Deep ITM価格発注拒否: ${ctx.option_price:.2f} >= ${limits.max_option_price:.0f}",
                           "critical", True)

    if ctx.capital_usd > 0 and ctx.est_margin > ctx.capital_usd * limits.max_margin_pct_per_trade:
        return CheckResult(False, "L1",
                           f"単一発注margin超過: ${ctx.est_margin:.0f} > {limits.max_margin_pct_per_trade:.0%}×${ctx.capital_usd:.0f}",
                           "high", True)

    if ctx.bid > 0 and ctx.ask > 0:
        spread = _bas_pct(ctx.bid, ctx.ask)
        if spread > limits.max_bid_ask_spread_pct:
            return CheckResult(False, "L1",
                               f"bid-ask spread過大: {spread:.1%} > {limits.max_bid_ask_spread_pct:.0%}",
                               "medium", False)

    # Layer 2: Portfolio Aggregate
    if ctx.open_positions >= limits.max_positions:
        return CheckResult(False, "L2",
                           f"同時ポジ上限: {ctx.open_positions} >= {limits.max_positions}",
                           "medium", False)

    if ctx.capital_usd > 0:
        total_with_new = (ctx.open_margin_total + ctx.est_margin) / ctx.capital_usd
        if total_with_new > limits.max_margin_pct_total:
            return CheckResult(False, "L2",
                               f"合計証拠金超過: {total_with_new:.1%} > {limits.max_margin_pct_total:.0%}",
                               "high", True)

        conc_with_new = (ctx.symbol_margin + ctx.est_margin) / ctx.capital_usd
        if conc_with_new > limits.max_concentration_pct:
            return CheckResult(False, "L2",
                               f"{ctx.symbol} 集中超過: {conc_with_new:.1%} > {limits.max_concentration_pct:.0%}",
                               "medium", False)

    # Layer 3: Loss Gates (日次/週次/月次)
    allow, reason = portfolio_aggregator.check_loss_gates(ctx.capital_usd, limits)
    if not allow:
        # 月次DD超過はKill Switch自動発動
        if "monthly_loss_gate" in reason and not kill_switch.is_active():
            kill_switch.activate(f"monthly_loss_gate_auto: {reason}")
            log.critical("[L3] 月次DDゲート → Kill Switch自動発動: %s", reason)
        return CheckResult(False, "L3", reason, "high", True)

    # Layer 3b: Cross-Bot portfolio limits
    cross_allow, cross_reason = portfolio_aggregator.check_cross_bot_limits(
        ctx.capital_usd, limits
    )
    if not cross_allow:
        return CheckResult(False, "L3B", cross_reason, "high", True)

    # Layer 4: Frequency & Duplicate
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(minutes=1)
    recent_in_window = sum(1 for t in _recent_orders if t >= cutoff)
    if recent_in_window >= limits.orders_per_minute_limit:
        return CheckResult(False, "L4",
                           f"発注頻度超過: {recent_in_window}/min >= {limits.orders_per_minute_limit}",
                           "critical", True)

    key = (ctx.symbol, round(ctx.strike, 2), ctx.side)
    duplicate_count = sum(1 for k in _recent_keys if k == key)
    if duplicate_count >= 3:
        return CheckResult(False, "L4",
                           f"重複発注疑い: {key} 直近{duplicate_count}回",
                           "high", True)

    # All passed — record
    _recent_orders.append(now)
    _recent_keys.append(key)
    return CheckResult(True, "PASS", "all checks passed", "low", False)
