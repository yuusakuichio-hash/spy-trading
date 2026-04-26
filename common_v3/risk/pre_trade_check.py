"""common_v3/risk/pre_trade_check.py — 4-Layer Pre-Trade Gate (v3)

4/17 事故 (SPXW 5400C 裸 LONG $169K) 再現阻止。
全 place_order / build_order 呼び出し直前に check_order() を通す。

4 Layer 構成:
  Layer 1 — Deep ITM 裸 LONG 拒否 ($50+ / option_price 閾値)
  Layer 2 — Symbol Whitelist (未登録銘柄即拒否)
  Layer 3 — Margin% Cap (単一発注 + 合計保有証拠金)
  Layer 4 — Fat Finger qty sanity (0 < qty <= max_qty_per_order)

設計規律:
- 純 sync / 副作用なし (ファイル I/O・外部 API 呼出を持たない)
- Kill Switch との協調: check_order() 冒頭で common_v3.risk.kill_switch.is_active() 確認
- KillSwitch 発動は呼出側の責務 (PreTradeGate は判定のみ)
- deepcopy で ctx 破壊的変更を防ぐ
- 各 Layer は独立して呼び出し可能 (テスト容易性)
- CC <= 10 per method

公開 API:
    PreTradeConfig   — 設定 dataclass (frozen=True)
    OrderCtx         — 発注コンテキスト dataclass (frozen=True)
    GateResult       — 判定結果 dataclass (frozen=True)
    check_order()    — メイン gate エントリポイント
"""
from __future__ import annotations

import copy
import dataclasses
import logging
from typing import Sequence

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: Layer 1: Deep ITM 裸 LONG ブロック閾値 (USD)
#: 4/17 事故: SPXW 5400C を $169/contract = $169K で発注 → $50 以上を即ブロック
_DEFAULT_DEEP_ITM_PRICE_THRESHOLD: float = 50.0

#: Layer 2: デフォルト Symbol Whitelist
#: common/risk_limits.py P0_paper と同列の高流動銘柄 (2026-04-22 確定)
_DEFAULT_SYMBOL_WHITELIST: frozenset[str] = frozenset([
    "US.SPY", "US.QQQ", "US.META", "US.SPXW", "US.SPX",
    "US.TSLA", "US.NVDA", "US.AAPL", "US.MSFT",
    "US.AMZN", "US.GOOGL", "US.IWM",
])

#: Layer 3: デフォルト単一発注証拠金上限 (資本比)
_DEFAULT_MARGIN_PCT_PER_TRADE: float = 0.03

#: Layer 3: デフォルト合計保有証拠金上限 (資本比)
_DEFAULT_MARGIN_PCT_TOTAL: float = 0.50

#: Layer 4: デフォルト最大発注数量
_DEFAULT_MAX_QTY_PER_ORDER: int = 100

# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PreTradeConfig:
    """PreTradeGate 設定。

    Args:
        deep_itm_price_threshold: Layer 1 Deep ITM 拒否閾値 (USD)。
            option_price >= この値なら裸 LONG を即ブロック。
            4/17 事故再現阻止のため $50 をデフォルト。
        symbol_whitelist:         Layer 2 許可銘柄 frozenset。
            frozenset 以外 (list/set) も受け付けて内部で変換する。
        max_margin_pct_per_trade: Layer 3 単一発注証拠金上限 (0.0–1.0)。
        max_margin_pct_total:     Layer 3 合計保有証拠金上限 (0.0–1.0)。
        max_qty_per_order:        Layer 4 最大発注数量 (正整数)。
    """
    deep_itm_price_threshold: float = _DEFAULT_DEEP_ITM_PRICE_THRESHOLD
    symbol_whitelist: frozenset[str] = dataclasses.field(
        default_factory=lambda: _DEFAULT_SYMBOL_WHITELIST
    )
    max_margin_pct_per_trade: float = _DEFAULT_MARGIN_PCT_PER_TRADE
    max_margin_pct_total: float = _DEFAULT_MARGIN_PCT_TOTAL
    max_qty_per_order: int = _DEFAULT_MAX_QTY_PER_ORDER

    def __post_init__(self) -> None:
        if self.deep_itm_price_threshold <= 0:
            raise ValueError(
                f"deep_itm_price_threshold must be > 0, got {self.deep_itm_price_threshold}"
            )
        if not (0.0 < self.max_margin_pct_per_trade <= 1.0):
            raise ValueError(
                f"max_margin_pct_per_trade must be in (0.0, 1.0], "
                f"got {self.max_margin_pct_per_trade}"
            )
        if not (0.0 < self.max_margin_pct_total <= 1.0):
            raise ValueError(
                f"max_margin_pct_total must be in (0.0, 1.0], "
                f"got {self.max_margin_pct_total}"
            )
        if self.max_qty_per_order <= 0:
            raise ValueError(
                f"max_qty_per_order must be > 0, got {self.max_qty_per_order}"
            )
        # symbol_whitelist が frozenset 以外なら変換 (list/set 受け入れ)
        if not isinstance(self.symbol_whitelist, frozenset):
            object.__setattr__(
                self, "symbol_whitelist", frozenset(self.symbol_whitelist)
            )


@dataclasses.dataclass(frozen=True)
class OrderCtx:
    """発注コンテキスト。全 check_order() に渡す最小情報セット。

    Args:
        symbol:             原資産銘柄 (例: "US.SPY")
        qty:                発注数量 (正整数必須)
        option_price:       オプション 1 contract の現在価格 (USD/contract)
                            裸 LONG の場合はプレミアム。0.0 = 価格不明 (Layer1 スキップ)
        side:               "BUY" または "SELL"
        is_long:            True = 裸 LONG (Layer 1 の Deep ITM 判定対象)
                            False = Spread 脚など (Layer 1 スキップ)
        est_margin:         この発注の推定必要証拠金 (USD)。0.0 = 不明 (L3 スキップ)
        capital_usd:        口座資本 (USD)。0.0 = 不明 (L3 スキップ)
        open_margin_total:  既存全ポジションの証拠金合計 (USD)
    """
    symbol: str
    qty: int
    option_price: float = 0.0
    side: str = "BUY"
    is_long: bool = True
    est_margin: float = 0.0
    capital_usd: float = 0.0
    open_margin_total: float = 0.0


@dataclasses.dataclass(frozen=True)
class GateResult:
    """PreTradeGate 判定結果。

    Args:
        allowed:         True = 発注許可 / False = 発注拒否
        layer:           ブロックした Layer 名 ("L1"/"L2"/"L3"/"L4"/"KILL"/"PASS")
        reason:          拒否理由 (allowed=True の場合は空文字)
        severity:        "low" / "medium" / "high" / "critical"
    """
    allowed: bool
    layer: str
    reason: str
    severity: str = "low"


# ---------------------------------------------------------------------------
# Gate implementation (layer 関数群)
# ---------------------------------------------------------------------------


def _check_layer1_deep_itm(ctx: OrderCtx, config: PreTradeConfig) -> GateResult | None:
    """Layer 1: Deep ITM 裸 LONG $50+ 拒否。

    4/17 事故再現阻止:
    - is_long=True かつ option_price >= deep_itm_price_threshold → 即ブロック
    - is_long=True かつ option_price <= 0.0 → fail-closed (B-1 fix 2026-04-25)

    B-1 fix (2026-04-25 Redteam CRITICAL):
    旧: option_price <=0.0 で return None (pass) → 価格不明で Deep ITM 裸 LONG 通過
    新: 裸 LONG で価格不明は fail-closed (Deep ITM 判定不能=安全側で拒否)

    Returns:
        GateResult (block) or None (pass)
    """
    if not ctx.is_long:
        return None
    if ctx.option_price <= 0.0:
        reason = (
            f"[L1] B-1 fail-closed: 裸 LONG 発注で option_price={ctx.option_price} (<=0=価格不明)。"
            f" Deep ITM 判定不能のため拒否。symbol={ctx.symbol} qty={ctx.qty}"
        )
        log.critical("[PreTradeGate] %s", reason)
        return GateResult(allowed=False, layer="L1", reason=reason, severity="critical")
    if ctx.option_price >= config.deep_itm_price_threshold:
        reason = (
            f"[L1] Deep ITM 裸 LONG 拒否: option_price=${ctx.option_price:.2f} "
            f">= threshold=${config.deep_itm_price_threshold:.2f} "
            f"(symbol={ctx.symbol} qty={ctx.qty} side={ctx.side})"
        )
        log.critical("[PreTradeGate] %s", reason)
        return GateResult(allowed=False, layer="L1", reason=reason, severity="critical")
    return None


def _check_layer2_whitelist(ctx: OrderCtx, config: PreTradeConfig) -> GateResult | None:
    """Layer 2: Symbol Whitelist 未登録銘柄拒否。

    Returns:
        GateResult (block) or None (pass)
    """
    if ctx.symbol not in config.symbol_whitelist:
        reason = (
            f"[L2] Symbol whitelist 違反: {ctx.symbol!r} は許可銘柄リストに存在しない "
            f"(whitelist={sorted(config.symbol_whitelist)})"
        )
        log.error("[PreTradeGate] %s", reason)
        return GateResult(allowed=False, layer="L2", reason=reason, severity="high")
    return None


def _check_layer3_margin(ctx: OrderCtx, config: PreTradeConfig) -> GateResult | None:
    """Layer 3: Margin% Cap。

    B-2 fix (2026-04-25 Redteam CRITICAL):
    旧: capital/margin <=0.0 で return None (pass) → margin 不明で発注通過 = Layer 3 完全ザル
    新: capital/margin <=0.0 は fail-closed (margin 可視性なしで発注は危険)

    チェック 1: 単一発注 margin / capital >= max_margin_pct_per_trade
    チェック 2: (open_margin_total + est_margin) / capital >= max_margin_pct_total

    Returns:
        GateResult (block) or None (pass)
    """
    if ctx.capital_usd <= 0.0 or ctx.est_margin <= 0.0:
        reason = (
            f"[L3] B-2 fail-closed: capital_usd={ctx.capital_usd} est_margin={ctx.est_margin}"
            f" のいずれかが <=0 (margin 可視性なし)。margin cap 判定不能のため拒否。"
        )
        log.error("[PreTradeGate] %s", reason)
        return GateResult(allowed=False, layer="L3", reason=reason, severity="critical")

    single_pct = ctx.est_margin / ctx.capital_usd
    if single_pct > config.max_margin_pct_per_trade:
        reason = (
            f"[L3] 単一発注 margin 超過: est_margin=${ctx.est_margin:.0f} / "
            f"capital=${ctx.capital_usd:.0f} = {single_pct:.1%} "
            f"> limit={config.max_margin_pct_per_trade:.0%}"
        )
        log.error("[PreTradeGate] %s", reason)
        return GateResult(allowed=False, layer="L3", reason=reason, severity="high")

    total_pct = (ctx.open_margin_total + ctx.est_margin) / ctx.capital_usd
    if total_pct > config.max_margin_pct_total:
        reason = (
            f"[L3] 合計保有 margin 超過: (open=${ctx.open_margin_total:.0f} + "
            f"new=${ctx.est_margin:.0f}) / capital=${ctx.capital_usd:.0f} "
            f"= {total_pct:.1%} > limit={config.max_margin_pct_total:.0%}"
        )
        log.error("[PreTradeGate] %s", reason)
        return GateResult(allowed=False, layer="L3", reason=reason, severity="high")

    return None


def _check_layer4_qty(ctx: OrderCtx, config: PreTradeConfig) -> GateResult | None:
    """Layer 4: Fat Finger qty sanity。

    0 < qty <= max_qty_per_order の範囲外は即ブロック。

    Returns:
        GateResult (block) or None (pass)
    """
    if not isinstance(ctx.qty, int) or ctx.qty <= 0 or ctx.qty > config.max_qty_per_order:
        reason = (
            f"[L4] Fat finger qty 異常: qty={ctx.qty!r} "
            f"(must be int in range (0, {config.max_qty_per_order}])"
        )
        log.critical("[PreTradeGate] %s", reason)
        return GateResult(allowed=False, layer="L4", reason=reason, severity="critical")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = PreTradeConfig()


def check_order(
    ctx: OrderCtx,
    config: PreTradeConfig | None = None,
) -> GateResult:
    """4-Layer Pre-Trade Gate メインエントリポイント。

    処理順 (順序変更禁止・短絡評価):
    0. Kill Switch (ARMED なら即拒否)
    1. Layer 1: Deep ITM 裸 LONG $50+ 拒否
    2. Layer 2: Symbol Whitelist
    3. Layer 3: Margin% Cap (単一 + 合計)
    4. Layer 4: Fat Finger qty sanity

    Args:
        ctx:    発注コンテキスト (OrderCtx)
        config: gate 設定 (None のときデフォルト設定)

    Returns:
        GateResult (allowed=True/False)

    注: ctx を deepcopy して作業するため呼出側の ctx は変更されない。
    """
    cfg = config or _DEFAULT_CONFIG
    # deepcopy: ctx の破壊的変更を防ぐ (legacy pre_trade_check H-2 と同規律)
    ctx = copy.deepcopy(ctx)

    # 0. Kill Switch (最優先)
    try:
        from common_v3.risk.kill_switch import is_active as _ks_is_active
        if _ks_is_active():
            reason = "[KILL] Kill Switch ARMED: 全発注をブロック"
            log.critical("[PreTradeGate] %s", reason)
            return GateResult(
                allowed=False, layer="KILL", reason=reason, severity="critical"
            )
    except Exception as exc:
        # kill_switch import 失敗 → fail-closed: ブロック
        reason = f"[KILL] Kill Switch 確認失敗 (fail-closed): {exc}"
        log.error("[PreTradeGate] %s", reason)
        return GateResult(
            allowed=False, layer="KILL", reason=reason, severity="critical"
        )

    # Layer 1
    result = _check_layer1_deep_itm(ctx, cfg)
    if result is not None:
        return result

    # Layer 2
    result = _check_layer2_whitelist(ctx, cfg)
    if result is not None:
        return result

    # Layer 3
    result = _check_layer3_margin(ctx, cfg)
    if result is not None:
        return result

    # Layer 4
    result = _check_layer4_qty(ctx, cfg)
    if result is not None:
        return result

    log.debug(
        "[PreTradeGate] PASS: symbol=%s qty=%s side=%s option_price=%.2f",
        ctx.symbol,
        ctx.qty,
        ctx.side,
        ctx.option_price,
    )
    return GateResult(allowed=True, layer="PASS", reason="", severity="low")


def check_order_critical_only(
    ctx: OrderCtx,
    config: PreTradeConfig | None = None,
) -> GateResult:
    """Critical-only Pre-Trade Gate (Kill Switch + L1 + L2 + L3 + L4)。

    B-3 fix (2026-04-25 Redteam CRITICAL): 旧実装は L2/L3 を「AtlasEngine 経由の
    full check_order() に委ねる」設計だったが、AtlasEngine が dead code (本番経路で
    instantiate されない) のため L2/L3 が**実質完全スキップ**されていた。
    本 fix では check_order_critical_only でも L2/L3 fall-through を必須化。

    Args:
        ctx:    発注コンテキスト (OrderCtx)
        config: gate 設定 (None のときデフォルト設定)

    Returns:
        GateResult (allowed=True/False)
    """
    cfg = config or _DEFAULT_CONFIG
    ctx = copy.deepcopy(ctx)

    # 0. Kill Switch (最優先)
    try:
        from common_v3.risk.kill_switch import is_active as _ks_is_active
        if _ks_is_active():
            reason = "[KILL] Kill Switch ARMED: 全発注をブロック"
            log.critical("[PreTradeGate] %s", reason)
            return GateResult(
                allowed=False, layer="KILL", reason=reason, severity="critical"
            )
    except Exception as exc:
        reason = f"[KILL] Kill Switch 確認失敗 (fail-closed): {exc}"
        log.error("[PreTradeGate] %s", reason)
        return GateResult(
            allowed=False, layer="KILL", reason=reason, severity="critical"
        )

    # Layer 1: Deep ITM 裸 LONG — 4/17 事故直接防止
    result = _check_layer1_deep_itm(ctx, cfg)
    if result is not None:
        return result

    # Layer 2: Symbol Whitelist (B-3 fix: critical_only でも必須化)
    result = _check_layer2_whitelist(ctx, cfg)
    if result is not None:
        return result

    # Layer 3: Margin% Cap (B-3 fix: critical_only でも必須化)
    result = _check_layer3_margin(ctx, cfg)
    if result is not None:
        return result

    # Layer 4: Fat Finger qty sanity
    result = _check_layer4_qty(ctx, cfg)
    if result is not None:
        return result

    log.debug(
        "[PreTradeGate] PASS (critical-only): symbol=%s qty=%s side=%s option_price=%.2f",
        ctx.symbol,
        ctx.qty,
        ctx.side,
        ctx.option_price,
    )
    return GateResult(allowed=True, layer="PASS", reason="", severity="low")
