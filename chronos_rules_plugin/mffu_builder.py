#!/usr/bin/env python3
"""
chronos_rules_plugin/mffu_builder.py — MFFU Builder Plan ルールプラグイン (Sora Lab / Chronos)

MFFU Builder $50K ルール（2026-04-19 公式直接確認済み）:
  Source: https://myfundedfutures.com/
          https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices

  特徴（他プランとの差分）:
    - Payout Cap: $10,000（他プランの $5,000 より高い）— Builder最大の差分
    - Consistency Rule: 50%（Flexと同じ）
    - Trailing DD: EOD trailing
    - Daily Loss Limit: $1,000（HIGH-10 実装済み）
    - Max Contracts: 5 mini / 50 micro（Evaluation）
    - Overnight Hold: 禁止（HIGH-11 実装済み）
    - 設計思想: ORB/Level/Range Break 等 intraday 完結戦術に対応（2026-04-20更新）

  confirmed: Payout Cap $10,000 / Consistency 50% / EOD trailing / profit_target=$3,000

  NOTE: pro_rush 戦術は未実装のため廃止（2026-04-20 CRITICAL修正）。
  代替: orb_breakout / level_trading / range_break_long / range_break_short
  選定根拠: data/chronos_builder_strategy_definition_20260420.md
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional, Tuple

from chronos_rules_plugin import PropFirmRules, OrderContext, register_plugin

log = logging.getLogger(__name__)

# ── プラン固定値 ────────────────────────────────────────────────────────────────
_ACCOUNT_SIZE_USD = 50_000.0
_EVAL_PROFIT_TARGET_USD = 3_000.0
_EVAL_MAX_LOSS_USD = 2_000.0        # EOD trailing
_EVAL_CONSISTENCY_PCT = 0.50        # 50%（Flexと同じ）
_EVAL_MAX_MINI = 5
_EVAL_MAX_MICRO = 50

_SIM_MAX_LOSS_USD = 2_000.0
_SIM_MAX_LOSS_AFTER_PAYOUT_USD = 100.0

_SIM_CONTRACT_TABLE = {0: 2, 1_500: 3, 2_000: 5}

_PAYOUT_MIN_WINNING_DAYS = 5
_PAYOUT_MIN_DAILY_PROFIT_USD = 150.0
_PAYOUT_MIN_NET_PROFIT_USD = 500.0
_PAYOUT_MIN_WITHDRAWAL_USD = 250.0
_PAYOUT_MAX_WITHDRAWAL_PCT = 0.50
_PAYOUT_MAX_WITHDRAWAL_CAP_USD = 10_000.0  # Builder固有: $10,000（他プランの2倍）
_PAYOUT_PROFIT_SPLIT_PCT = 0.80

# HIGH-10: MFFU Builder 公式 Daily Loss Limit $1,000
# Source: MFFU Builder plan 公式ページ確認済み (2026-04-19)
# "All positions must be closed before the end of the trading session"
# "Daily loss limit: $1,000"
_BUILDER_DAILY_LOSS_LIMIT_USD = 1_000.0

# HIGH-11: Builder overnight強制クローズ時刻（ET）
# MFFU Builder公式「All positions must be closed before the end of the trading session」
_BUILDER_FORCE_CLOSE_HOUR_ET = 15   # 15:55 ET
_BUILDER_FORCE_CLOSE_MINUTE_ET = 55


class MFFUBuilderRules(PropFirmRules):
    """MFFU Builder Plan ルールプラグイン。

    他プランとの主な差分:
      - Payout Cap: $10,000（intraday集中戦術との相性が良い理由）
      - Consistency: 50%（Flexと同じ）
      - EOD trailing（Flexと同じ）
      - Daily Loss Limit: $1,000（HIGH-10）
      - Overnight 禁止: 15:55 ET 強制クローズ（HIGH-11）
      - 対応戦術: orb_breakout / level_trading / range_break_long / range_break_short
        ※ pro_rush は廃止（未実装・CRITICAL修正 2026-04-20）
    """

    def get_max_loss_usd(self, phase: str) -> float:
        """フェーズ別MLL。Builder は EOD trailing。"""
        if phase == "sim_funded_after_payout":
            return _SIM_MAX_LOSS_AFTER_PAYOUT_USD
        if phase == "sim_funded":
            return _SIM_MAX_LOSS_USD
        return _EVAL_MAX_LOSS_USD

    def get_daily_loss_limit_usd(self) -> Optional[float]:
        """日次損失制限 $1,000。

        HIGH-10: MFFU Builder 公式 Daily Loss Limit $1,000 を実装。
        Flex/Rapid は None（制限なし）だが、Builder は制限あり。
        Source: MFFU Builder plan 公式ページ (confirmed 2026-04-19)
        """
        return _BUILDER_DAILY_LOSS_LIMIT_USD

    def should_force_close_now(self, now_et: datetime.datetime) -> bool:
        """HIGH-11: Builder overnight強制クローズ判定。

        15:55 ET 以降はポジションを強制クローズする。
        MFFU Builder公式「All positions must be closed before the end of the trading session」
        Source: MFFU Builder plan 公式ページ (confirmed 2026-04-19)

        Args:
            now_et: 現在時刻（ET timezone-aware）

        Returns:
            True = 強制クローズが必要な時刻
        """
        if now_et.hour > _BUILDER_FORCE_CLOSE_HOUR_ET:
            return True
        if (now_et.hour == _BUILDER_FORCE_CLOSE_HOUR_ET
                and now_et.minute >= _BUILDER_FORCE_CLOSE_MINUTE_ET):
            return True
        return False

    def get_consistency_pct(self, phase: str) -> Optional[float]:
        """Consistencyルール（Evaluationのみ50%制約）。

        Source: MFFU公式（confirmed 2026-04-19）
        Builder: 50%（Flexと同じ・Coreの40%より緩い）
        """
        if phase == "evaluation":
            return _EVAL_CONSISTENCY_PCT
        return None

    def get_max_contracts(self, symbol: str, phase: str) -> int:
        """最大コントラクト数。"""
        is_micro = symbol.startswith("M")
        if phase == "evaluation":
            return _EVAL_MAX_MICRO if is_micro else _EVAL_MAX_MINI
        return _SIM_CONTRACT_TABLE[max(_SIM_CONTRACT_TABLE.keys())] * (10 if is_micro else 1)

    def get_max_contracts_for_balance(self, symbol: str, net_profit_usd: float) -> int:
        """Sim-Funded残高連動コントラクト上限。"""
        is_micro = symbol.startswith("M")
        net = max(0.0, net_profit_usd)
        result = min(_SIM_CONTRACT_TABLE.values())
        for floor in sorted(_SIM_CONTRACT_TABLE.keys()):
            if net >= floor:
                result = _SIM_CONTRACT_TABLE[floor]
        return result * (10 if is_micro else 1)

    def get_profit_target_usd(self) -> float:
        """Evaluation通過目標額。"""
        return _EVAL_PROFIT_TARGET_USD

    def get_payout_rules(self) -> dict:
        """Payout条件辞書。Builder固有: Capが$10,000。

        Source: MFFU公式（confirmed 2026-04-19）
        """
        return {
            "min_winning_days": _PAYOUT_MIN_WINNING_DAYS,
            "min_daily_profit_usd": _PAYOUT_MIN_DAILY_PROFIT_USD,
            "min_net_profit_usd": _PAYOUT_MIN_NET_PROFIT_USD,
            "min_withdrawal_usd": _PAYOUT_MIN_WITHDRAWAL_USD,
            "max_withdrawal_pct": _PAYOUT_MAX_WITHDRAWAL_PCT,
            "max_withdrawal_cap_usd": _PAYOUT_MAX_WITHDRAWAL_CAP_USD,  # $10,000
            "profit_split_pct": _PAYOUT_PROFIT_SPLIT_PCT,
        }

    def check_news_window(self, now_et: datetime.datetime) -> bool:
        """ニュース禁止窓（Bot側のNewsTradingFilterで詳細判断）。"""
        return False

    def check_compliance(self, order: OrderContext) -> Tuple[bool, str]:
        """MFFU Builder ルール全チェック。

        intraday 戦術向け（orb_breakout / level_trading / range_break 等）:
        Consistency チェックは積極的に監視。overnight は should_force_close_now() で防護。

        チェック順序:
          1. EOD Trailing Drawdown（MLL）
          2. Daily Loss Limit $1,000（HIGH-10）
          3. Consistency 50%（Evaluation）
          4. コントラクト数上限
        """
        # 1. EOD Trailing Drawdown
        max_loss = self.get_max_loss_usd(order.phase)
        ok, reason = self._check_trailing_drawdown(
            current_balance=order.current_balance_usd,
            peak_balance=order.peak_balance_usd,
            max_loss_usd=max_loss,
            phase=order.phase,
            account_size_usd=_ACCOUNT_SIZE_USD,
        )
        if not ok:
            return False, f"[Builder] {reason}"

        # 2. Daily Loss Limit $1,000（HIGH-10）
        daily_limit = self.get_daily_loss_limit_usd()
        if daily_limit is not None and order.daily_pnl_usd <= -daily_limit:
            return False, (
                f"[Builder] Daily Loss Limit違反: "
                f"当日PnL ${order.daily_pnl_usd:.2f} <= "
                f"-${daily_limit:.0f} — soft停止"
            )

        # 3. Consistency 50% チェック
        consistency_pct = self.get_consistency_pct(order.phase)
        ok, reason = self._check_consistency(order.daily_pnl_history, consistency_pct)
        if not ok:
            return False, f"[Builder] {reason}"

        # 4. コントラクト数チェック
        if order.phase in ("sim_funded", "sim_funded_after_payout"):
            max_qty = self.get_max_contracts_for_balance(
                order.symbol, order.current_balance_usd
            )
        else:
            max_qty = self.get_max_contracts(order.symbol, order.phase)

        if order.qty > max_qty:
            return False, (
                f"[Builder] コントラクト数超過: {order.qty} > 上限 {max_qty} "
                f"(symbol={order.symbol}, phase={order.phase})"
            )

        return True, ""


# ── プラグイン登録 ────────────────────────────────────────────────────────────────
register_plugin("mffu_builder", MFFUBuilderRules)
