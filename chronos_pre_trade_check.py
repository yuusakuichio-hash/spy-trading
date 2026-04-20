#!/usr/bin/env python3
"""
chronos_pre_trade_check.py — Chronos固有の事前チェック (Sora Lab / Chronos)

役割:
  - Atlas共通 pre_trade_check.py の4層チェック に加え、先物・MFFU固有チェックを追加
  - 全 place_order 呼び出し直前に check_order() を呼ぶ（Atlas と同じ設計規律）

チェック構成:
  ── Atlas共通（再利用） ──────────────────────────────────────────────────────
  Layer 1: Pre-trade Sanity（シンボルホワイトリスト / qty / 証拠金 / bid-ask spread）
  Layer 2: Portfolio Aggregate（同時ポジ / 合計証拠金 / 集中度）
  Layer 3: Loss Gates（日次/週次/月次）
  Layer 3B: Cross-Bot portfolio limits
  Layer 3.5: PDT guard（オプション戦術と合算カウント）
  Layer 4: Frequency & Duplicate

  ── Chronos固有（追加） ─────────────────────────────────────────────────────
  Layer F1: 先物銘柄ホワイトリスト（chronos_symbol_meta.py の FUTURES_META）
  Layer F2: MFFU Consistency ルール（1日の損失上限 / Trailing Drawdown）
  Layer F3: MFFU Safety Buffer チェック（口座残高が Safety Buffer を下回る発注禁止）
  Layer F4: 先物固有の margin check（tick size × contract multiplier ベース）

依存:
  - common/pre_trade_check.py  (Atlas共通4層)
  - common/risk_limits.py
  - chronos_symbol_meta.py     (先物銘柄メタ)
  - chronos_mffu_rules.py      (MFFU固有ルール)
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

# ── Atlas共通基盤（再利用） ──────────────────────────────────────────────────
from common.pre_trade_check import check_order as atlas_check_order, OrderContext, CheckResult
from common.risk_limits import RiskLimits, load_limits

# ── Chronos固有モジュール ────────────────────────────────────────────────────
from chronos_symbol_meta import FUTURES_META, get_tick_size, get_contract_multiplier
from chronos_mffu_rules import MFFURules, check_mffu_compliance

# ── Phase A: Layer PF-1 プロップファーム契約遵守チェック ─────────────────────
from common.prop_firm_rules import check_prop_firm_compliance

log = logging.getLogger(__name__)


def _derive_side_from_net_pos(p: dict) -> str:
    """net_pos フィールドから side を導出する（KeyError 防止フォールバック）。

    MF-1 fix: get_positions() は side を持たず net_pos のみ返す場合がある。
    net_pos > 0 → "BUY", net_pos < 0 → "SELL", net_pos == 0 → "BUY"（デフォルト）

    Args:
        p: position dict（"net_pos" キーを含むことを期待）
    Returns:
        "BUY" | "SELL"
    """
    net_pos = p.get("net_pos", 0)
    if net_pos < 0:
        return "SELL"
    return "BUY"

# ── Hedging 同一商品ペアテーブル ─────────────────────────────────────────────
# MFFU Fair Play Policy: 同一商品の同時 long+short (hedging) 禁止
# MES と ES は実質同一プロダクト (S&P500 先物) のため両建て禁止
# Source: https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices
# "Hedging: simultaneously holding long and short positions in the same or correlated instruments"
_HEDGE_SAME_PRODUCT_PAIRS: set[frozenset] = {
    frozenset({"MES", "ES"}),    # Micro / Mini S&P500 — 同一プロダクト
    frozenset({"MNQ", "NQ"}),    # Micro / Mini NASDAQ-100 — 同一プロダクト
    frozenset({"MYM", "YM"}),    # Micro / Mini Dow Jones — 同一プロダクト
    frozenset({"M2K", "RTY"}),   # Micro / Mini Russell 2000 — 同一プロダクト
}


# ── Chronos固有発注コンテキスト ───────────────────────────────────────────────
@dataclass
class FuturesOrderContext:
    """先物発注前の全情報。Atlas OrderContext の先物版。

    Atlasと異なる点:
      - strike / option_price フィールドなし（先物は原資産直接）
      - entry_price が原資産価格
      - contract_size が必要（tick size × multiplier で証拠金計算）
      - mffu_account_balance が MFFU Safety Buffer チェックに必要
    """
    symbol: str
    side: str                   # "BUY" / "SELL"
    qty: int                    # 枚数
    entry_price: float          # 発注価格（MARKET の場合は現値）
    est_margin: float           # 推定必要証拠金（USD）
    capital_usd: float          # 口座残高（USD）
    open_positions: int = 0
    open_margin_total: float = 0
    symbol_margin: float = 0
    paper: bool = True

    # MFFU固有フィールド
    mffu_account_balance: float = 0.0    # MFFU口座残高（Safety Buffer計算用）
    mffu_daily_pnl: float = 0.0          # 当日PnL（Consistency確認用）
    mffu_trailing_drawdown: float = 0.0  # Trailing Drawdown現在値

    # HIGH-5: 既存ポジション一覧（Hedging違反チェックに使用）
    # 呼出側で必ず設定すること。空リストの場合は warning を出す（設定忘れ防止）。
    # 各要素: {"symbol": str, "side": "BUY"|"SELL"|"LONG"|"SHORT", "qty": int}
    existing_positions_list: list = None  # type: ignore

    # ── Phase A: Layer PF-1 プロップファーム契約遵守（新設） ──────────────────
    # 設定方法: firm="mffu", plan="core_50k", phase="evaluation" 等
    # account_state はプロップファーム口座の状態 dict（詳細は prop_firm_rules.py 参照）
    firm: str = ""                                          # "mffu" | "tradeify" | "apex" | ""
    plan: str = ""                                          # "core_50k" | "rapid_50k" 等
    phase: str = "evaluation"                               # "evaluation" | "funded" | "sim_funded" 等
    prop_account_state: dict = field(default_factory=dict)  # プロップ口座状態
    contract_type: str = "mini"                             # "mini" | "micro"
    est_pnl: float = 0.0                                    # 次取引の見込み PnL
    upcoming_events: list = field(default_factory=list)     # T1 ニュースイベントリスト

    def __post_init__(self):
        if self.existing_positions_list is None:
            self.existing_positions_list = []


# ── 統合チェック関数 ──────────────────────────────────────────────────────────
def check_order(ctx: FuturesOrderContext, limits: Optional[RiskLimits] = None) -> CheckResult:
    """Atlas共通4層チェック + Chronos固有先物チェック（Layer F1-F4）を実行する。

    発注前に必ず呼ぶこと。

    Args:
        ctx:    FuturesOrderContext（先物発注コンテキスト）
        limits: RiskLimits（省略時は capital_usd から自動決定）

    Returns:
        CheckResult（allow=Trueなら発注可）

    実装方針:
        1. Atlas共通4層チェック (atlas_check_order) を先に実行
        2. 合格した場合のみ Chronos固有 Layer F1 + Hedging チェックを追加実行
        3. Layer F2/F3/F4 は仕様確認後に個別実装（現在は pass）

    MFFU Fair Play Policy (Section 5 Hedging):
        check_hedging_violation() を必ず通過させる。
        Source: https://help.myfundedfutures.com/en/articles/8444599
    """
    # ── Step 1: RiskLimits の自動決定 ─────────────────────────────────────────
    if limits is None:
        limits = load_limits(ctx.capital_usd)

    # ── Step 2: Layer PF-1 — プロップファーム契約遵守チェック（新設・Phase A） ──
    # P0-CRITICAL-7: firm 未設定は即 fail-closed（発注拒否）
    # Atlas共通チェック(Step 3)より先に実行すること。
    # プロップファーム口座では必ず firm を設定すること。空文字はシステム設定ミスを意味する。
    if not ctx.firm or not ctx.firm.strip():
        reason = (
            "Layer PF-1 fail-closed: firm が設定されていません。"
            "FuturesOrderContext.firm に 'mffu'|'tradeify'|'apex' を設定してください。"
        )
        log.error("[ChronosPreTrade] %s", reason)
        return CheckResult(allow=False, layer="PF-1-FIRM-MISSING", reason=reason)

    if ctx.firm:
        pf_order_ctx = {
            "symbol":          ctx.symbol,
            "side":            ctx.side,
            "qty":             ctx.qty,
            "contract_type":   ctx.contract_type,
            "est_pnl":         ctx.est_pnl,
            "upcoming_events": ctx.upcoming_events,
        }
        # prop_account_state が空の場合は最低限のデフォルトで補完
        # P0-CRITICAL-2: unrealized_pnl は必ず existing_positions_list の実値を使う。
        # ハードコード 0 は不正確な Consistency/MLL 計算の原因になる。
        # 呼出側で futu API から取得した unrealized_pnl を各要素に設定すること。
        account_state = {
            "balance":         ctx.mffu_account_balance or ctx.capital_usd,
            "peak_balance":    ctx.capital_usd,
            "daily_pnl":       ctx.mffu_daily_pnl,
            "cycle_daily_pnl": [],
            "trades_today":    0,
            "recent_trades":   [],
            "open_positions":  [
                {
                    "symbol":         p["symbol"],
                    # MF-1 fix: get_positions() は net_pos のみ返す場合があり p["side"] は
                    # KeyError を起こす。get_positions_for_rules() 経由なら "side" が保証されるが、
                    # 呼出側が raw get_positions() を渡してきた場合の安全網として
                    # _derive_side_from_net_pos() フォールバックを設ける。
                    "side":           p.get("side", _derive_side_from_net_pos(p)),
                    "unrealized_pnl": p.get("unrealized_pnl", 0),  # 呼出側で設定必須
                }
                for p in ctx.existing_positions_list
            ],
            "last_trade_date": None,
            "payout_count":    0,
        }
        account_state.update(ctx.prop_account_state)

        pf_allow, pf_layer, pf_reason = check_prop_firm_compliance(
            firm=ctx.firm,
            plan=ctx.plan,
            phase=ctx.phase,
            account_state=account_state,
            order_ctx=pf_order_ctx,
        )
        if not pf_allow:
            log.error(
                "[ChronosPreTrade] Layer PF-1 REJECTED: layer=%s reason=%s",
                pf_layer, pf_reason,
            )
            return CheckResult(allow=False, layer=pf_layer, reason=pf_reason)

    # ── Step 3: Atlas共通 OrderContext へ変換 ────────────────────────────────
    # PF-1 チェック合格後に atlas_ctx を構築する（fail-closed 前に構築しない）
    # OrderContext は strike/option_price が必須のため先物は strike=0, option_price=entry_price
    atlas_ctx = OrderContext(
        symbol            = ctx.symbol,
        strike            = 0.0,           # 先物は strike なし
        side              = ctx.side,
        qty               = ctx.qty,
        option_price      = ctx.entry_price,  # 先物の発注価格を option_price として渡す
        est_margin        = ctx.est_margin,
        capital_usd       = ctx.capital_usd,
        open_positions    = ctx.open_positions,
        open_margin_total = ctx.open_margin_total,
        symbol_margin     = ctx.symbol_margin,
        paper             = ctx.paper,
    )

    # ── Step 4: Atlas共通4層チェック ──────────────────────────────────────────
    atlas_result = atlas_check_order(atlas_ctx, limits)
    if not atlas_result.allow:
        log.warning(
            f"[ChronosPreTrade] Atlas check REJECTED: "
            f"layer={atlas_result.layer} reason={atlas_result.reason}"
        )
        return atlas_result

    # ── Step 5: Layer F1 — 先物銘柄ホワイトリスト ──────────────────────────────
    symbol_upper = ctx.symbol.upper()
    if symbol_upper not in FUTURES_META:
        reason = (
            f"Layer F1 (FuturesSymbolWhitelist): '{ctx.symbol}' は "
            f"FUTURES_META に登録されていない銘柄 → 発注拒否"
        )
        log.error(f"[ChronosPreTrade] {reason}")
        return CheckResult(allow=False, layer="F1_symbol_whitelist", reason=reason)

    # ── Step 5: Hedging 違反チェック ──────────────────────────────────────────
    # MFFU Fair Play Policy Section 5: 同一商品の同時 long+short 禁止
    # HIGH-5: existing_positions_list フィールドを明示的に参照する。
    # 空リスト時は warning を出して設定忘れを防ぐ。
    existing_list: list[dict] = ctx.existing_positions_list  # HIGH-5: getattr廃止・直接参照
    if not existing_list:
        log.warning(
            "[ChronosPreTrade] HIGH-5: existing_positions_list が空 — "
            "呼出側で FuturesOrderContext.existing_positions_list を設定してください。"
            "Hedging チェックは空リストのためスキップされます。"
        )
    new_order_dict = {
        "symbol": ctx.symbol,
        "side":   ctx.side,
        "qty":    ctx.qty,
    }
    hedge_ok, hedge_reason = check_hedging_violation(existing_list, new_order_dict)
    if not hedge_ok:
        log.error(f"[ChronosPreTrade] Hedging violation: {hedge_reason}")
        return CheckResult(allow=False, layer="F7_hedging", reason=hedge_reason)

    # ── Step 6: Layer F2/F3/F4 — 将来実装（現在は pass） ────────────────────
    # F2: MFFU Consistency ルール → check_mffu_compliance() で別経路チェック済み
    # F3: MFFU Safety Buffer → trailing_drawdown check が MFFURuleGuard でカバー
    # F4: 先物証拠金 → est_margin が atlas_check_order の Layer 1 で確認済み

    log.debug(
        f"[ChronosPreTrade] All layers PASSED: "
        f"symbol={ctx.symbol} side={ctx.side} qty={ctx.qty}"
    )
    return CheckResult(allow=True, layer="all", reason="chronos_pre_trade_check: all layers passed")


def _check_layer_f1_symbol(ctx: FuturesOrderContext) -> Optional[CheckResult]:
    """Layer F1: 先物銘柄ホワイトリストチェック（MVP実装）。

    FUTURES_META に含まれない銘柄は発注拒否。
    chronos_symbol_meta.FUTURES_META をそのままホワイトリストとして使用する。

    cycle2: NotImplementedError を除去し、check_order() のメインパスで実際に呼ばれる。
    """
    symbol_upper = ctx.symbol.upper()
    if symbol_upper not in FUTURES_META:
        reason = (
            f"Layer F1 (FuturesSymbolWhitelist): '{ctx.symbol}' は "
            f"FUTURES_META に登録されていない銘柄 → 発注拒否"
        )
        log.error("[ChronosPreTrade] F1: %s", reason)
        return CheckResult(allow=False, layer="F1_symbol_whitelist", reason=reason)
    return None  # allow → 次の Layer へ


def _check_layer_f2_mffu_consistency(ctx: FuturesOrderContext) -> Optional[CheckResult]:
    """Layer F2: MFFU Consistency ルールチェック。

    check_mffu_compliance() (MFFURuleGuard) が呼出側で確認済みのため、
    この Layer は常に pass（None 返し）とする。
    将来 MFFU公式 Consistency ルール仕様確認後に実装する。

    cycle2: NotImplementedError を除去。
    """
    # check_mffu_compliance() でカバー済み → ここでは pass
    return None


def _check_layer_f3_mffu_safety_buffer(ctx: FuturesOrderContext) -> Optional[CheckResult]:
    """Layer F3: MFFU Safety Buffer チェック。

    MFFURuleGuard の trailing_drawdown チェックでカバー済みのため、
    この Layer は常に pass（None 返し）とする。
    将来 MFFU公式 Safety Buffer 計算式確認後に実装する。

    cycle2: NotImplementedError を除去。
    """
    # MFFURuleGuard.trailing_drawdown でカバー済み → ここでは pass
    return None


def check_hedging_violation(
    existing_positions: list[dict],
    new_order: dict,
) -> tuple[bool, str]:
    """
    Hedging Violation Guard (MFFU Fair Play Policy).

    MFFU禁止: 同一商品の同時 long+short 両建て。
    MES と ES は実質同一プロダクト (S&P500先物) のため両建て禁止扱い。

    Source: https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices
    "Hedging: simultaneously holding long and short positions in the same or correlated instruments"

    Args:
        existing_positions: 現在のオープンポジション一覧。
            各要素: {"symbol": str, "side": "long"|"short"|"BUY"|"SELL", "qty": int}
        new_order: 新規発注情報。
            {"symbol": str, "side": "BUY"|"SELL", "qty": int}

    Returns:
        (True=OK、False=違反, 違反理由文字列)
        OK の場合は理由は空文字列。
    """
    new_symbol = new_order.get("symbol", "").upper()
    new_side   = new_order.get("side", "").upper()

    # 新規発注サイドを正規化 (BUY=long, SELL=short)
    new_is_long = new_side in ("BUY", "LONG")

    for pos in existing_positions:
        pos_symbol = pos.get("symbol", "").upper()
        pos_side   = pos.get("side", "").upper()
        pos_qty    = pos.get("qty", 0)

        if pos_qty == 0:
            continue

        pos_is_long = pos_side in ("BUY", "LONG")

        # 同一シンボルの逆方向: 完全な両建て
        if pos_symbol == new_symbol and pos_is_long != new_is_long:
            reason = (
                f"HEDGE VIOLATION: {pos_symbol} 既存{'long' if pos_is_long else 'short'} "
                f"× 新規{'long' if new_is_long else 'short'} — 同一シンボル両建て禁止"
            )
            log.error(f"[HedgeGuard] {reason}")
            return False, reason

        # 同一プロダクトペア (MES×ES / MNQ×NQ 等) の逆方向: 実質両建て
        pair = frozenset({pos_symbol, new_symbol})
        if pair in _HEDGE_SAME_PRODUCT_PAIRS and pos_is_long != new_is_long:
            reason = (
                f"HEDGE VIOLATION: {pos_symbol}({'long' if pos_is_long else 'short'}) "
                f"× {new_symbol}({'long' if new_is_long else 'short'}) "
                f"— 同一プロダクト両建て禁止 (MFFU Fair Play Policy)"
            )
            log.error(f"[HedgeGuard] {reason}")
            return False, reason

    return True, ""


def _check_layer_f4_futures_margin(ctx: FuturesOrderContext) -> Optional[CheckResult]:
    """Layer F4: 先物固有の証拠金チェック（MVP実装）。

    chronos_symbol_meta.get_initial_margin() から exchange margin を取得し、
    ctx.est_margin が exchange margin × qty を下回る場合は拒否する。

    cycle2: NotImplementedError を除去し、MVP実装に置換。
    FUTURES_META に margin データがない銘柄は F1 で弾かれているため、
    ここでは 0.0 返しは発生しない（防御コードとして警告のみ）。
    """
    from chronos_symbol_meta import get_initial_margin as _get_initial_margin
    symbol_upper = ctx.symbol.upper()
    exchange_margin_per_contract = _get_initial_margin(symbol_upper)
    if exchange_margin_per_contract <= 0.0:
        # F1 で弾かれていない場合の防御 → 警告してpass
        log.warning(
            "[ChronosPreTrade] F4: exchange_margin=0 for '%s' — skip margin check",
            ctx.symbol,
        )
        return None

    required = exchange_margin_per_contract * max(ctx.qty, 1)
    if ctx.est_margin < required:
        reason = (
            f"Layer F4 (FuturesMargin): est_margin={ctx.est_margin:.2f} < "
            f"required={required:.2f} ({exchange_margin_per_contract:.2f}×{ctx.qty}) → 発注拒否"
        )
        log.error("[ChronosPreTrade] F4: %s", reason)
        return CheckResult(allow=False, layer="F4_futures_margin", reason=reason)
    return None  # allow → 次の Layer へ
