#!/usr/bin/env python3
"""
chronos_rules_plugin/tradeify_lightning.py — Tradeify Lightning Instant Funded ルールプラグイン
                                              (Sora Lab / Chronos)

Tradeify Lightning 仕様（2025-09-12 以降の改定仕様・2026-04-20 公式直接確認済み）:
  Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
          https://tradeify.co/funded-trader-agreement
  調査: data/tradeify_full_spec_20260420.md

  Lightning Instant Funding（評価スキップ・即 Funded）:
    account_size: 25K / 50K / 100K / 150K
    fee: $295 買い切り（Select planとの混同に注意: activation feeは Lightning には不要）
    activation_fee: $0（Lightning は不要。Select plan の $1,500/$4,000 とは別物）
    Drawdown: EOD（日中 intra-day 制限なし）
    profit_split_pct: 常時 90%（threshold ロジックなし・Select plan の 100%→90% 段階制とは異なる）
    daily_loss_limit_usd: $1,250/日
    payout_cycle_days: 5（勝利日）
    consistency_rule: 20%/25%/30% scaling
      $0–$3K 利益帯: 1日の利益は累計利益の 20% 以下
      $3K–$10K 利益帯: 1日の利益は累計利益の 25% 以下
      $10K 超利益帯: 1日の利益は累計利益の 30% 以下
    max_contracts: 4 mini / 40 micro（2025-09-12 以降）
    news_trading: 制限なし（公式明記: "Free rein, but beware of volatility"）
    overnight_hold: 可（weekend も 可・明示なし）
    concurrent_max_accounts: 5（Simulated Funded）/ 総合 $1M まで
    japan_allowed: True（地域制限なし・確認済み）
    automation_policy: 自己所有戦略のみ OK・HFT 禁止・commercial 禁止・Live 動画検証要
    broker: Tradovate / NinjaTrader / WealthCharts / tradesea

  戦術割当（1戦略1firm制約・§7 戦術分離）:
    Tradeify 専用: VWAP Reclaim / Liquidity Sweep
    MFFU 側:       ORB / VIX-MR / Session Break / Gap Fill（Tradeify と重複禁止）

  参考: data/tradeify_full_spec_20260420.md / data/research_prop_firms_automation_20260419.md §2-A, §2-B, §3-B
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional, Tuple

import pytz

from chronos_rules_plugin import PropFirmRules, OrderContext, register_plugin

log = logging.getLogger(__name__)

# ── 戦術識別定数 ──────────────────────────────────────────────────────────────
# Tradeify 専用戦術セット（MFFU 戦術との分離に使用）
TRADEIFY_ALLOWED_TACTICS = frozenset({
    "vwap_reclaim",
    "liquidity_sweep",
})

# MFFU 専用戦術（Tradeify では使用禁止）
MFFU_EXCLUSIVE_TACTICS = frozenset({
    "orb",
    "orb_buy",
    "vix_mr",
    "session_break",
    "gap_fill",
    "overnight_gap",
    # pro_rush は Builder 戦術置換により orb_breakout 等に分割済み（2026-04-20）
})

# ── プラン固定値（Tradeify 公式 2025-09-12 以降改定仕様・2026-04-20 確認済み）──
# Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
#         https://tradeify.co/funded-trader-agreement

# アカウントサイズ別月額（USD）
# 注意: Lightning は買い切り $295。旧値 $67/$111/$181/$251 は Select plan の月額。
_LIGHTNING_FEE_BUYONCE_USD = 295.0  # 買い切り（月額ではない）

# 後方互換 alias（旧コードからの参照用・実際は買い切りのため月額換算は不正確）
_MONTHLY_FEE_BY_SIZE = {
    25_000:  295.0,  # 買い切り $295（旧: $67/月は Select plan）
    50_000:  295.0,  # 買い切り $295（旧: $111/月は Select plan）
    100_000: 295.0,  # 買い切り $295（旧: $181/月は Select plan）
    150_000: 295.0,  # 買い切り $295（旧: $251/月は Select plan）
}

# Activation Fee: Lightning は $0（Select plan の $1,500/$4,000 と混同しないこと）
# Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
_ACTIVATION_FEE_USD = 0.0

# Profit Split: 常時 90%（threshold ロジックなし）
# Select plan の「初回 $15K は 100%→以降 90%」段階制は Lightning には適用されない
# Source: https://tradeify.co/funded-trader-agreement
_PROFIT_SPLIT_PCT = 0.90  # 常時 90%

# Payout サイクル（勝利日 5 日）
_PAYOUT_CYCLE_WINNING_DAYS = 5

# 最大同時口座数（Simulated Funded）
_CONCURRENT_MAX_ACCOUNTS = 5

# EOD Drawdown: Max Loss Limit（USD）— 口座サイズ別
_MAX_LOSS_BY_SIZE = {
    25_000:  1_500.0,
    50_000:  2_000.0,
    100_000: 3_500.0,
    150_000: 5_000.0,
}
_DEFAULT_ACCOUNT_SIZE_USD = 50_000.0
_DEFAULT_MAX_LOSS_USD     = _MAX_LOSS_BY_SIZE[_DEFAULT_ACCOUNT_SIZE_USD]

# Daily Loss Limit（2025-09-12 以降追加）
# Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
_DAILY_LOSS_LIMIT_USD = 1_250.0

# Max Contracts（2025-09-12 以降改定）
# 旧: 10 mini / 100 micro → 新: 4 mini / 40 micro
# Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
_MAX_MINI_CONTRACTS   = 4   # 2025-09-12 以降（旧: 10）
_MAX_MICRO_CONTRACTS  = 40  # micro 換算（mini × 10、旧: 100）

# Consistency Rule: 累計利益帯別スケーリング（2025-09-12 以降追加）
# Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
# $0–$3K: 1日の利益は累計の 20% 以下
# $3K–$10K: 1日の利益は累計の 25% 以下
# $10K 超: 1日の利益は累計の 30% 以下
_CONSISTENCY_TIERS = [
    (0.0,       3_000.0,  0.20),
    (3_000.0,  10_000.0,  0.25),
    (10_000.0, float("inf"), 0.30),
]


class TradeifyLightningRules(PropFirmRules):
    """Tradeify Lightning Instant Funded ルールプラグイン。

    2025-09-12 以降改定仕様:
      Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
              https://tradeify.co/funded-trader-agreement

      - Profit Split: 常時 90%（Select plan の 100%→90% 段階制とは異なる）
      - Activation Fee: $0（Select plan の $1,500/$4,000 は Lightning には不要）
      - Consistency Rule: 20%/25%/30% scaling（$0-$3K / $3K-$10K / $10K超）
      - Max Contracts: 4 mini / 40 micro（旧: 10 mini / 100 micro）
      - Daily Loss Limit: $1,250/日
      - Fee: $295 買い切り（旧: $111/月は Select plan の値）
      - 評価スキップ（Instant Funded）
      - 戦術: VWAP Reclaim / Liquidity Sweep 専用（MFFU 戦術との分離）

    Args:
        account_size_usd: 口座サイズ（25K/50K/100K/150K）。デフォルト 50K。
    """

    def __init__(
        self,
        account_size_usd: float = _DEFAULT_ACCOUNT_SIZE_USD,
        path: str = "daily",  # 後方互換パラメータ（Lightning では使用しない）
    ) -> None:
        if account_size_usd not in _MONTHLY_FEE_BY_SIZE:
            raise ValueError(
                f"account_size_usd={account_size_usd} は Tradeify Lightning 対象外。"
                f"使用可能: {sorted(_MONTHLY_FEE_BY_SIZE.keys())}"
            )

        self._account_size_usd = account_size_usd
        self._path = path  # 後方互換のみ（Lightning では activation fee なし）
        self._max_loss_usd = _MAX_LOSS_BY_SIZE.get(account_size_usd, _DEFAULT_MAX_LOSS_USD)
        self._fee_buyonce_usd = _LIGHTNING_FEE_BUYONCE_USD
        self._activation_fee_usd = _ACTIVATION_FEE_USD  # $0

        log.info(
            "[TradeifyLightningRules] init: size=$%s "
            "max_loss=$%s fee_buyonce=$%s activation_fee=$%s (2025-09-12 spec)",
            account_size_usd, self._max_loss_usd,
            self._fee_buyonce_usd, self._activation_fee_usd,
        )

    # ── 基本パラメータ取得 ────────────────────────────────────────────────────

    def get_max_loss_usd(self, phase: str) -> float:
        """EOD Trailing Drawdown の Max Loss Limit（USD）。

        Tradeify Lightning は全フェーズで同一 MLL（日中制限なし）。
        phase: "funded" / "funded_post_activation"
        """
        return self._max_loss_usd

    def get_daily_loss_limit_usd(self) -> Optional[float]:
        """日次損失制限: $1,250/日（2025-09-12 以降追加）。

        Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
        """
        return _DAILY_LOSS_LIMIT_USD

    def get_consistency_pct(self, phase: str) -> Optional[float]:
        """Consistency ルール（固定値版・後方互換）。

        Lightning は累計利益帯別スケーリング制（20%/25%/30%）のため、
        単一の比率を返す固定値インターフェースでは表現できない。
        累計利益を考慮する場合は get_consistency_pct_for_profit() を使うこと。

        後方互換のため最低 tier の 20% を返す（保守的）。
        """
        return 0.20  # 最低 tier（$0-$3K 帯）

    def get_consistency_pct_for_profit(self, cumulative_profit_usd: float) -> float:
        """累計利益額に応じた Consistency ルール上限を返す（2025-09-12 以降）。

        Lightning Consistency Rule（累計利益帯別スケーリング）:
          $0–$3K: 1日の利益は累計の 20% 以下
          $3K–$10K: 1日の利益は累計の 25% 以下
          $10K 超: 1日の利益は累計の 30% 以下

        Args:
            cumulative_profit_usd: 口座開設以来の累計利益（USD）

        Returns:
            当該利益帯の Consistency 上限（0.0〜1.0）
        """
        for low, high, pct in _CONSISTENCY_TIERS:
            if low <= cumulative_profit_usd < high:
                return pct
        # fallback: 最高 tier
        return _CONSISTENCY_TIERS[-1][2]

    def get_max_contracts(self, symbol: str, phase: str) -> int:
        """最大コントラクト数（mini 基準）。"""
        is_micro = symbol.startswith("M") and symbol not in ("MNQ", "MES_INDEX")
        # MES, MNQ 等の micro contract は M で始まる
        if symbol in ("MES", "MNQ", "MYM", "M2K"):
            return _MAX_MICRO_CONTRACTS
        return _MAX_MINI_CONTRACTS

    def get_max_contracts_for_balance(self, symbol: str, net_profit_usd: float) -> int:
        """Tradeify は残高連動上限なし（固定上限を返す）。"""
        return self.get_max_contracts(symbol, phase="funded")

    def get_profit_target_usd(self) -> float:
        """Tradeify Lightning は評価スキップのため profit target なし（0.0 を返す）。"""
        return 0.0

    def get_activation_fee_usd(self) -> float:
        """Activation fee: Lightning は $0。

        Select plan の $1,500（Daily）/ $4,000（Flex）は Lightning には不要。
        Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
        """
        return _ACTIVATION_FEE_USD  # $0

    def get_fee_buyonce_usd(self) -> float:
        """Lightning 買い切り fee: $295。

        旧値 $111/月 は Select plan の 50K 月額。Lightning は買い切り。
        Source: https://help.tradeify.co/en/articles/10495938-lightning-funded-accounts
        """
        return _LIGHTNING_FEE_BUYONCE_USD

    def get_monthly_fee_usd(self) -> float:
        """後方互換: 月額費用として買い切り fee を返す（実態は buy-once）。

        WARNING: Lightning は買い切り $295。月割りでの計算が必要な場合は
        get_fee_buyonce_usd() を使い、期間で割って扱うこと。
        """
        return _LIGHTNING_FEE_BUYONCE_USD  # $295 buy-once

    def get_profit_split_pct(self, cumulative_profit_usd: float = 0.0) -> float:
        """Profit Split: 常時 90%（threshold ロジックなし）。

        Select plan の「初回 $15K は 100%→以降 90%」段階制は Lightning には適用されない。
        Source: https://tradeify.co/funded-trader-agreement
        """
        return _PROFIT_SPLIT_PCT  # 常時 90%

    def get_payout_rules(self) -> dict:
        """Payout 条件辞書（2025-09-12 以降改定仕様）。

        Returns:
            payout_cycle_winning_days: 5 勝利日
            profit_split_pct: 0.90（常時 90%）
            activation_fee_usd: $0（Lightning は不要）
            fee_buyonce_usd: $295
            daily_loss_limit_usd: $1,250
            max_mini_contracts: 4
            max_micro_contracts: 40
            concurrent_max_accounts: 5
        """
        return {
            "payout_cycle_winning_days": _PAYOUT_CYCLE_WINNING_DAYS,
            "profit_split_pct": _PROFIT_SPLIT_PCT,
            "activation_fee_usd": _ACTIVATION_FEE_USD,
            "activation_fee_path": None,  # Lightning には path 概念なし
            "fee_buyonce_usd": _LIGHTNING_FEE_BUYONCE_USD,
            "daily_loss_limit_usd": _DAILY_LOSS_LIMIT_USD,
            "max_mini_contracts": _MAX_MINI_CONTRACTS,
            "max_micro_contracts": _MAX_MICRO_CONTRACTS,
            "concurrent_max_accounts": _CONCURRENT_MAX_ACCOUNTS,
            "consistency_tiers": _CONSISTENCY_TIERS,
            "news_trading_restriction": None,
        }

    def get_consistency_pct_by_payout(self, payout_count: int) -> Optional[float]:
        """後方互換: ペイアウト回数別 Consistency ルール。

        Lightning は累計利益帯別スケーリング制のため payout_count では表現できない。
        保守的に最低 tier 20% を返す。累計利益考慮には get_consistency_pct_for_profit() を使うこと。
        """
        return 0.20  # 最低 tier（保守的）

    def check_news_window(self, now_et: datetime.datetime, phase: str = "funded") -> bool:
        """ニュース禁止窓チェック。

        Tradeify Lightning は news trading 制限なし（全フェーズ）。
        公式: "We do not have any rules against or guidelines around trading news events.
               Free rein, but beware of volatility"

        Returns:
            False: 常に発注可（禁止窓なし）
        """
        return False  # 制限なし

    # ── 戦術分離チェック ──────────────────────────────────────────────────────

    def check_tactic_allowed(self, tactic_name: str) -> Tuple[bool, str]:
        """Tradeify で許可される戦術かチェック。

        1戦略1firm制約: VWAP Reclaim / Liquidity Sweep 専用。
        MFFU 専用戦術（ORB/VIX-MR等）は使用禁止。

        Args:
            tactic_name: 戦術名（例: "vwap_reclaim", "orb_buy"）

        Returns:
            (True, "") = 許可
            (False, "理由") = 禁止
        """
        tactic_lower = tactic_name.lower()

        if tactic_lower in MFFU_EXCLUSIVE_TACTICS:
            return False, (
                f"[Tradeify] 戦術分離違反: '{tactic_name}' は MFFU 専用戦術。"
                f"Tradeify では {sorted(TRADEIFY_ALLOWED_TACTICS)} を使用してください。"
            )

        if tactic_lower not in TRADEIFY_ALLOWED_TACTICS:
            return False, (
                f"[Tradeify] 未許可戦術: '{tactic_name}' は Tradeify Lightning の許可リストにありません。"
                f"許可戦術: {sorted(TRADEIFY_ALLOWED_TACTICS)}"
            )

        return True, ""

    def check_automation_policy(self, strategy_type: str) -> Tuple[bool, str]:
        """自動化ポリシー準拠チェック。

        Tradeify 自動化ポリシー:
          - 自己所有戦略のみ: OK
          - HFT（1秒未満の反復売買）: 禁止
          - commercial（第三者販売）戦略: 禁止
          - Live 動画検証（初回 payout 時）: 要

        Args:
            strategy_type: "proprietary"（自己所有）/ "hft" / "commercial"

        Returns:
            (True, "") = 準拠
            (False, "理由") = 違反
        """
        if strategy_type == "hft":
            return False, "[Tradeify] HFT は自動化ポリシー違反（1秒未満の反復売買禁止）。"
        if strategy_type == "commercial":
            return False, "[Tradeify] commercial 戦略は自動化ポリシー違反（第三者販売禁止）。"
        return True, ""

    def check_compliance(self, order: OrderContext) -> Tuple[bool, str]:
        """Tradeify Lightning ルール全チェック（2025-09-12 以降改定仕様）。

        チェック順序:
          1. EOD Trailing Drawdown（Max Loss Limit）
          2. Daily Loss Limit $1,250（2025-09-12 以降追加）
          3. コントラクト数上限（4 mini / 40 micro）
          4. 戦術分離（MFFU 戦術使用禁止）
          5. automation policy（HFT/commercial 禁止）

        Note:
          - Consistency チェック: 累計利益帯別（order に cumulative_profit_usd があれば実施）
          - News Trading チェック: なし（制限なし）
        """
        # 1. EOD Trailing Drawdown
        # Tradeify Lightning は Instant Funded（Initial Balance $0 スタート）
        # → MFFU Sim-Funded と同様に floor = -MLL（評価フロアなし）
        max_loss = self.get_max_loss_usd(order.phase)
        ok, reason = self._check_trailing_drawdown(
            current_balance=order.current_balance_usd,
            peak_balance=order.peak_balance_usd,
            max_loss_usd=max_loss,
            phase="sim_funded",  # Funded = initial balance $0 フロア計算
            account_size_usd=self._account_size_usd,
        )
        if not ok:
            return False, f"[Tradeify] {reason}"

        # 2. Daily Loss Limit $1,250（2025-09-12 以降追加）
        daily_pnl = getattr(order, "daily_pnl_usd", None)
        if daily_pnl is not None and daily_pnl <= -_DAILY_LOSS_LIMIT_USD:
            return False, (
                f"[Tradeify] Daily Loss Limit 違反: "
                f"当日PnL ${daily_pnl:.2f} <= -${_DAILY_LOSS_LIMIT_USD:.0f} — 発注停止"
            )

        # 3. コントラクト数チェック（4 mini / 40 micro、2025-09-12 以降）
        max_qty = self.get_max_contracts(order.symbol, order.phase)
        if order.qty > max_qty:
            return False, (
                f"[Tradeify] コントラクト数超過: {order.qty} > 上限 {max_qty} "
                f"(symbol={order.symbol}, phase={order.phase})"
            )

        # 4. 戦術分離チェック（order に tactic_name があれば）
        tactic = getattr(order, "tactic_name", None)
        if tactic is not None:
            ok, reason = self.check_tactic_allowed(tactic)
            if not ok:
                return False, reason

        # 5. automation policy チェック（order に strategy_type があれば）
        strategy_type = getattr(order, "strategy_type", "proprietary")
        ok, reason = self.check_automation_policy(strategy_type)
        if not ok:
            return False, reason

        return True, ""


# ── プラグイン登録 ────────────────────────────────────────────────────────────
register_plugin("tradeify_lightning", TradeifyLightningRules)
