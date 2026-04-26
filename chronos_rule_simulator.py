#!/usr/bin/env python3
"""
chronos_rule_simulator.py — MyFundedFutures (MFFU) ルールシミュレーター

MFFU公式ルール定義・チェック関数・合格率シミュレーション。

対応口座サイズ: $25K / $50K / $100K / $150K / $300K

MFFUルール (2025年公式サイト確認済み, https://myfundedfutures.com):
  ─ Core / Evaluation フェーズ ─
    Profit Target: 口座サイズの定まった%
    EOD (End-of-Day) Drawdown: 取引日終了時の口座残高ベースで計算
      ※ Apexのような"Trailing"ではなく"EOD"基準が最大の違い
      ※ 日中含み損はDrawdownに影響しない（クローズ後の残高のみ）
    Intraday Drawdown: なし（Apex比で大幅に緩い）
    Consistency Rule: 1日の利益が全利益の40%以下（Apexの30%より緩い）
    Minimum Trading Days: 5日（Evalフェーズ）
    Minimum Contracts/Day: 1日1契約以上（1回以上取引）

  ─ Funded フェーズ ─
    EOD Drawdown: 同上（静的 = 初期残高から計算・以後変動しない）
    Profit Split: 80/20（トレーダー80%）
    Scaling Plan: 実績に応じてコントラクト数を増やせる

  ─ News Trading制限 ─
    FOMC / CPI / NFP イベント前後2分は取引禁止
    ※ Apexの5分より短い

注意事項:
  - EOD Drawdownは"当日クローズ後の確定残高"が基準
  - ポジションを全クローズした状態での残高 > (初期残高 - EOD DD上限) が必要
  - 日中含み損がいくら大きくなっても、クローズ前は違反にならない
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 口座ルール定義
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MFFUAccountRules:
    """MyFundedFutures の口座ルールセット。"""

    account_size:      int    # ドル (例: 50000)
    profit_target:     float  # 評価フェーズ達成目標 (ドル)
    eod_drawdown:      float  # EOD Drawdown上限 (ドル) — 確定残高ベース
    consistency_limit: float  # 1日利益の上限比率 (全利益比, 例: 0.40 = 40%)
    max_contracts:     int    # 最大契約数 (評価フェーズ)
    funded_contracts:  int    # Funded後の最大契約数
    min_trading_days:  int    # 最小取引日数 (Evalフェーズ)
    consistency_rule_applies: bool = True  # Consistency Rule適用有無
    # Intradayドローダウン制限はMFFUでは存在しない（None相当）


# MFFU公式ルール (2025年公式サイト確認済み)
# $50K Core: EOD DD $2,000 (4%), Profit Target $3,000 (6%), Consistency 40%
# ※ $50K Evaluation と $50K Core は同一ルールを使用
MFFU_ACCOUNT_RULES: dict[int, MFFUAccountRules] = {
    25_000: MFFUAccountRules(
        account_size      = 25_000,
        profit_target     = 1_500,    # $1,500 (6%)
        eod_drawdown      = 1_000,    # $1,000 (4%)
        consistency_limit = 0.40,     # 40%ルール
        max_contracts     = 2,
        funded_contracts  = 2,
        min_trading_days  = 5,
        consistency_rule_applies = False,  # $25Kはconsistencyルール適用外
    ),
    50_000: MFFUAccountRules(
        account_size      = 50_000,
        profit_target     = 3_000,    # $3,000 (6%)
        eod_drawdown      = 2_000,    # $2,000 (4%) ← Apex $2,500より小さい
        consistency_limit = 0.40,     # 40%ルール (Apex 30%より緩い)
        max_contracts     = 5,
        funded_contracts  = 5,
        min_trading_days  = 5,
        consistency_rule_applies = True,
    ),
    100_000: MFFUAccountRules(
        account_size      = 100_000,
        profit_target     = 6_000,    # $6,000 (6%)
        eod_drawdown      = 3_000,    # $3,000 (3%)
        consistency_limit = 0.40,     # 40%ルール
        max_contracts     = 10,
        funded_contracts  = 10,
        min_trading_days  = 5,
        consistency_rule_applies = True,
    ),
    150_000: MFFUAccountRules(
        account_size      = 150_000,
        profit_target     = 9_000,    # $9,000 (6%)
        eod_drawdown      = 4_500,    # $4,500 (3%)
        consistency_limit = 0.40,     # 40%ルール
        max_contracts     = 12,
        funded_contracts  = 12,
        min_trading_days  = 5,
        consistency_rule_applies = True,
    ),
    300_000: MFFUAccountRules(
        account_size      = 300_000,
        profit_target     = 20_000,   # $20,000 (6.7%)
        eod_drawdown      = 7_500,    # $7,500 (2.5%)
        consistency_limit = 0.40,     # 40%ルール
        max_contracts     = 20,
        funded_contracts  = 20,
        min_trading_days  = 5,
        consistency_rule_applies = True,
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# ルールチェック関数
# ─────────────────────────────────────────────────────────────────────────────

def check_eod_drawdown(
    rules:           MFFUAccountRules,
    initial_balance: float,
    eod_balance:     float,
) -> dict:
    """
    EOD (End-of-Day) Drawdown チェック。

    MFFUのEOD DDは「初期残高 - EOD確定残高 <= eod_drawdown上限」で判定。
    - 日中含み損は関係しない（クローズ後の確定残高のみ）
    - Apex Trailing DDと異なり、ハイウォーターマークは使わない（静的基準）
    - Funded後も同一の静的基準が継続する

    Args:
        rules:           口座ルール
        initial_balance: 口座開設時の残高（account_size）
        eod_balance:     当日EOD確定残高（全ポジションクローズ後）
    Returns:
        {
          "passed":        bool,
          "drawdown":      float,  — 初期残高からの下落額（正の値）
          "limit":         float,  — EOD DD上限（正の値）
          "threshold":     float,  — 違反境界値（initial - limit）
          "remaining":     float,  — あと何ドル損失できるか
          "margin_pct":    float,  — 残りマージン（上限比）
          "eod_balance":   float,
        }
    """
    drawdown  = initial_balance - eod_balance   # 正の値なら損失
    limit     = rules.eod_drawdown
    threshold = initial_balance - limit         # これを下回るとアウト

    passed    = eod_balance >= threshold
    remaining = eod_balance - threshold
    margin_pct = remaining / limit * 100 if limit > 0 else 0.0

    return {
        "passed":      passed,
        "drawdown":    max(0.0, drawdown),
        "limit":       limit,
        "threshold":   threshold,
        "remaining":   remaining,
        "margin_pct":  margin_pct,
        "eod_balance": eod_balance,
    }


def check_consistency_rule(
    rules:      MFFUAccountRules,
    daily_pnls: list[float],
    today_pnl:  float,
) -> dict:
    """
    Consistency Rule (40%ルール) チェック。
    1日の利益が全累積利益の40%を超えてはいけない。

    MFFUの40%ルールはApexの30%ルールより緩い。

    Args:
        rules:      口座ルール
        daily_pnls: 過去の日次P&Lリスト（Funded開始以降）
        today_pnl:  当日の確定P&L
    Returns:
        {
          "passed":           bool,
          "today_pnl":        float,
          "total_profit":     float,
          "max_allowed":      float,   — 今日許容される最大利益
          "limit_pct":        float,   — 40%
          "violation_amount": float,   — 超過額（0なら超過なし）
        }
    """
    if not rules.consistency_rule_applies:
        return {
            "passed":           True,
            "today_pnl":        today_pnl,
            "total_profit":     sum(p for p in daily_pnls if p > 0),
            "max_allowed":      float("inf"),
            "limit_pct":        rules.consistency_limit,
            "violation_amount": 0.0,
            "note":             "consistency rule not applicable for this account size",
        }

    all_pnls      = list(daily_pnls) + [today_pnl]
    total_profit  = sum(p for p in all_pnls if p > 0)
    limit_pct     = rules.consistency_limit
    max_allowed   = total_profit * limit_pct

    violation_amount = max(0.0, today_pnl - max_allowed) if today_pnl > 0 else 0.0
    passed = today_pnl <= max_allowed or today_pnl <= 0

    return {
        "passed":           passed,
        "today_pnl":        today_pnl,
        "total_profit":     total_profit,
        "max_allowed":      max_allowed,
        "limit_pct":        limit_pct,
        "violation_amount": violation_amount,
    }


def check_profit_target(
    rules:           MFFUAccountRules,
    initial_balance: float,
    current_balance: float,
) -> dict:
    """
    Profit Target チェック（評価フェーズ）。
    Returns:
        {"achieved": bool, "profit": float, "target": float, "remaining": float}
    """
    profit    = current_balance - initial_balance
    target    = rules.profit_target
    achieved  = profit >= target
    remaining = max(0.0, target - profit)

    return {
        "achieved":     achieved,
        "profit":       profit,
        "target":       target,
        "remaining":    remaining,
        "progress_pct": min(100.0, profit / target * 100) if target > 0 else 0.0,
    }


def check_min_trading_days(
    rules:        MFFUAccountRules,
    trading_days: int,
) -> dict:
    """
    最小取引日数チェック（Evalフェーズ）。
    Funded移行にはmin_trading_days以上の取引日が必要。
    Returns:
        {"met": bool, "trading_days": int, "required": int, "remaining": int}
    """
    met       = trading_days >= rules.min_trading_days
    remaining = max(0, rules.min_trading_days - trading_days)

    return {
        "met":          met,
        "trading_days": trading_days,
        "required":     rules.min_trading_days,
        "remaining":    remaining,
    }


def check_all_rules(
    account_size:   int,
    initial_balance: float,
    eod_balance:    float,
    daily_pnls:     list[float],
    today_pnl:      float,
    trading_days:   int = 0,
    include_consistency_in_violations: bool = False,
) -> dict:
    """
    全ルールを一括チェックする。

    Args:
        account_size:    口座サイズ ($50000 など)
        initial_balance: 口座開設時の残高
        eod_balance:     当日EOD確定残高（全ポジションクローズ後）
        daily_pnls:      過去の日次P&Lリスト
        today_pnl:       当日の確定P&L
        trading_days:    これまでの取引日数
        include_consistency_in_violations:
            Trueの場合、Consistency違反もviolationsに含める。

    Returns:
        {
          "account_size":   int,
          "overall_passed": bool,
          "eod_dd":         {...},
          "consistency":    {...},
          "profit_target":  {...},
          "min_days":       {...},
          "violations":     list[str],
        }
    """
    rules = MFFU_ACCOUNT_RULES.get(account_size)
    if not rules:
        raise ValueError(
            f"Unknown account size: {account_size}. "
            f"Valid: {list(MFFU_ACCOUNT_RULES.keys())}"
        )

    eod_dd_result    = check_eod_drawdown(rules, initial_balance, eod_balance)
    consistency_result = check_consistency_rule(rules, daily_pnls, today_pnl)
    profit_result    = check_profit_target(rules, initial_balance, eod_balance)
    min_days_result  = check_min_trading_days(rules, trading_days)

    violations = []
    if not eod_dd_result["passed"]:
        violations.append(
            f"EOD_DD_VIOLATED: drawdown ${eod_dd_result['drawdown']:.0f} "
            f"(limit ${rules.eod_drawdown}, threshold ${eod_dd_result['threshold']:.0f})"
        )
    if include_consistency_in_violations and not consistency_result["passed"]:
        violations.append(
            f"CONSISTENCY_VIOLATED: today_pnl ${today_pnl:.0f} > "
            f"max_allowed ${consistency_result['max_allowed']:.0f}"
        )

    return {
        "account_size":   account_size,
        "overall_passed": len(violations) == 0,
        "eod_dd":         eod_dd_result,
        "consistency":    consistency_result,
        "profit_target":  profit_result,
        "min_days":       min_days_result,
        "violations":     violations,
    }


# ─────────────────────────────────────────────────────────────────────────────
# スケーリングプラン
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MFFUScalingTier:
    """MFFU スケーリングプランの各段階。"""
    profit_threshold: float   # この利益を達成したら
    max_contracts:    int     # この枚数まで増やせる
    label:            str     # ラベル


def get_scaling_plan(account_size: int) -> list[MFFUScalingTier]:
    """
    MFFU Scaling Planを返す。
    Funded後、実績に応じてコントラクト数を段階的に増やせる。

    MFFUの公式Scale Planに準拠。
    $50K口座: 初期5枚 → 実績に応じて段階的増加
    """
    plans: dict[int, list[MFFUScalingTier]] = {
        50_000: [
            MFFUScalingTier(0,       5,  "Initial"),
            MFFUScalingTier(3_000,   6,  "Tier 1"),
            MFFUScalingTier(6_000,   8,  "Tier 2"),
            MFFUScalingTier(10_000, 10,  "Tier 3"),
            MFFUScalingTier(15_000, 12,  "Tier 4"),
        ],
        100_000: [
            MFFUScalingTier(0,       10, "Initial"),
            MFFUScalingTier(6_000,   12, "Tier 1"),
            MFFUScalingTier(12_000,  15, "Tier 2"),
            MFFUScalingTier(20_000,  18, "Tier 3"),
            MFFUScalingTier(30_000,  20, "Tier 4"),
        ],
    }
    rules = MFFU_ACCOUNT_RULES.get(account_size)
    default_contracts = rules.funded_contracts if rules else 1
    return plans.get(
        account_size,
        [MFFUScalingTier(0, default_contracts, "Default")]
    )


def get_allowed_contracts(account_size: int, total_profit: float) -> int:
    """現在の利益に応じた許容コントラクト数を返す。"""
    plan = get_scaling_plan(account_size)
    if not plan:
        rules = MFFU_ACCOUNT_RULES.get(account_size)
        return rules.max_contracts if rules else 1

    allowed = plan[0].max_contracts
    for tier in plan:
        if total_profit >= tier.profit_threshold:
            allowed = tier.max_contracts
    return allowed


# ─────────────────────────────────────────────────────────────────────────────
# ニュースイベントカレンダー
# ─────────────────────────────────────────────────────────────────────────────

# MFFU禁止イベント: FOMC / CPI / NFP 前後2分
# 実運用ではecon_calendarから取得する。このモジュールではスタティックな型定義のみ。
NEWS_EVENT_BLACKOUT_MINUTES = 2   # イベント前後2分（Apexの5分より短い）

MFFU_HIGH_IMPACT_EVENTS = {
    "FOMC",       # Federal Open Market Committee（金利発表）
    "CPI",        # Consumer Price Index（消費者物価指数）
    "NFP",        # Non-Farm Payrolls（非農業部門雇用者数）
    "FOMC_MINUTES",  # FOMC議事録
    "PPI",        # Producer Price Index（任意追加）
}


# ─────────────────────────────────────────────────────────────────────────────
# シミュレーション: 過去データで合格率を計算
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MFFUSimResult:
    """シミュレーション結果。"""
    account_size:       int
    total_days:         int
    passed_days:        int
    failed_days:        int
    pass_rate_pct:      float
    max_drawdown:       float
    total_profit:       float
    final_balance:      float
    eod_dd_violations:  int
    consistency_violations: int
    eval_passed:        bool   # 評価フェーズ通過（target達成 + min_days満足 + 違反なし）
    pass_rate_by_rule:  dict[str, float]


def simulate_mffu_evaluation(
    account_size: int,
    daily_pnls:   list[float],
    verbose:      bool = False,
) -> MFFUSimResult:
    """
    日次P&Lリストを入力として、MFFU評価フェーズを通過できるか
    シミュレーションを実行する。

    Apex版との主な違い:
      - Trailing DDではなくEOD DD（静的基準）
      - Intraday DD制限なし
      - Consistency 40%（Apex 30%より緩い）

    Args:
        account_size: 口座サイズ ($50000 など)
        daily_pnls:   日次P&Lのリスト（ドル）
        verbose:      詳細ログ出力
    Returns:
        MFFUSimResult
    """
    rules = MFFU_ACCOUNT_RULES.get(account_size)
    if not rules:
        raise ValueError(f"Unknown account size: {account_size}")

    initial_balance  = float(account_size)
    balance          = initial_balance
    cumulative_pnls: list[float] = []
    trading_days     = 0

    total_days             = len(daily_pnls)
    eod_dd_violations      = 0
    consistency_violations = 0
    eval_complete          = False
    failed_day             = None

    for i, today_pnl in enumerate(daily_pnls):
        eod_balance = balance + today_pnl  # 当日EOD残高（全クローズ後想定）

        if today_pnl != 0:
            trading_days += 1

        # ルールチェック
        result = check_all_rules(
            account_size     = account_size,
            initial_balance  = initial_balance,
            eod_balance      = eod_balance,
            daily_pnls       = cumulative_pnls,
            today_pnl        = today_pnl,
            trading_days     = trading_days,
            include_consistency_in_violations = False,  # Consistencyは即失格にしない
        )

        # EOD DD違反は即失格
        if not result["overall_passed"]:
            for v in result["violations"]:
                if "EOD_DD" in v:
                    eod_dd_violations += 1
            failed_day = i + 1
            if verbose:
                log.info(f"[MFFUSim] Day {i+1}: FAILED — {result['violations']}")
            break

        # Consistency違反カウント（情報収集のみ）
        if not result["consistency"]["passed"]:
            consistency_violations += 1
            if verbose:
                log.info(f"[MFFUSim] Day {i+1}: Consistency warn "
                         f"today_pnl={today_pnl:.0f} "
                         f"max_allowed={result['consistency']['max_allowed']:.0f}")

        # 残高更新
        balance = eod_balance
        cumulative_pnls.append(today_pnl)

        if verbose:
            log.info(f"[MFFUSim] Day {i+1}: P&L={today_pnl:+.0f} "
                     f"balance={balance:.0f} "
                     f"drawdown={initial_balance - balance:.0f}")

        # Profit Target達成チェック
        pt = result["profit_target"]
        if pt["achieved"] and not eval_complete:
            eval_complete = True
            if verbose:
                log.info(f"[MFFUSim] Day {i+1}: PROFIT TARGET ACHIEVED! "
                         f"profit={pt['profit']:.0f}")

    passed_days = len(cumulative_pnls)

    # 評価フェーズ通過判定: 違反なし + Profit Target達成 + min_days満足
    eval_passed = (
        failed_day is None
        and eval_complete
        and trading_days >= rules.min_trading_days
    )

    pass_rate_pct = passed_days / total_days * 100 if total_days > 0 else 0.0

    # 最大ドローダウン計算（残高ベース・静的基準）
    max_dd = 0.0
    bal    = initial_balance
    for pnl in cumulative_pnls:
        bal   += pnl
        dd     = initial_balance - bal
        max_dd = max(max_dd, dd)

    total_profit = balance - initial_balance

    pass_rate_by_rule = {
        "eod_dd":      (passed_days - eod_dd_violations) / total_days * 100 if total_days > 0 else 100.0,
        "consistency": (passed_days - consistency_violations) / total_days * 100 if total_days > 0 else 100.0,
    }

    return MFFUSimResult(
        account_size           = account_size,
        total_days             = total_days,
        passed_days            = passed_days,
        failed_days            = 1 if failed_day else 0,
        pass_rate_pct          = pass_rate_pct,
        max_drawdown           = max_dd,
        total_profit           = total_profit,
        final_balance          = balance,
        eod_dd_violations      = eod_dd_violations,
        consistency_violations = consistency_violations,
        eval_passed            = eval_passed,
        pass_rate_by_rule      = pass_rate_by_rule,
    )


# ─────────────────────────────────────────────────────────────────────────────
# サンプル出力
# ─────────────────────────────────────────────────────────────────────────────

def print_account_rules(account_size: int) -> None:
    """口座ルールを表示する。"""
    rules = MFFU_ACCOUNT_RULES.get(account_size)
    if not rules:
        print(f"Unknown account size: {account_size}")
        return

    print(f"\n=== MFFU ${account_size:,} Account Rules ===")
    print(f"  Profit Target:      ${rules.profit_target:,.0f}")
    print(f"  EOD Drawdown:       ${rules.eod_drawdown:,.0f}  [NOTE: EOD-based, not trailing]")
    print(f"  Intraday DD:        None  [MFFU advantage: no intraday limit]")
    print(f"  Consistency Rule:   {rules.consistency_limit * 100:.0f}% "
          f"(applies: {rules.consistency_rule_applies})")
    print(f"  Min Trading Days:   {rules.min_trading_days}")
    print(f"  Max Contracts:      {rules.max_contracts}")
    print(f"  Funded Contracts:   {rules.funded_contracts}")
    print(f"  News Blackout:      ±{NEWS_EVENT_BLACKOUT_MINUTES} min (FOMC/CPI/NFP)")


if __name__ == "__main__":
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # ── 全口座ルール表示 ──
    for size in [25_000, 50_000, 100_000, 150_000, 300_000]:
        print_account_rules(size)

    # ── $50K口座でサンプルシミュレーション ──
    print("\n\n=== Sample Simulation: $50K MFFU Account ===")
    print("Scenario: MES 3枚, 日次目標$200, 勝率60%")

    import random
    random.seed(42)

    sample_pnls = []
    for _ in range(20):
        if random.random() < 0.60:
            pnl = random.uniform(100, 400)
        else:
            pnl = -random.uniform(50, 250)
        sample_pnls.append(round(pnl, 2))

    print(f"\n日次P&L (20日): {[f'{p:+.0f}' for p in sample_pnls]}")
    print(f"合計: ${sum(sample_pnls):+.0f}")

    result = simulate_mffu_evaluation(50_000, sample_pnls, verbose=True)

    print(f"\n── Simulation Result ──")
    print(f"  Total Days:         {result.total_days}")
    print(f"  Passed Days:        {result.passed_days}")
    print(f"  EOD DD Violations:  {result.eod_dd_violations}")
    print(f"  Consistency Warns:  {result.consistency_violations}")
    print(f"  Max Drawdown:       ${result.max_drawdown:,.0f}")
    print(f"  Total Profit:       ${result.total_profit:+,.0f}")
    print(f"  Final Balance:      ${result.final_balance:,.0f}")
    print(f"  Eval Passed:        {result.eval_passed}")

    # ── 単発ルールチェックのデモ ──
    print("\n\n=== Single Day Rule Check Example ===")
    rules = MFFU_ACCOUNT_RULES[50_000]

    # EOD DD Check（MFFUはEODベース・静的基準）
    eod = check_eod_drawdown(
        rules           = rules,
        initial_balance = 50_000,
        eod_balance     = 48_500,   # EOD残高$48,500 = $1,500損失
    )
    print(f"\nEOD Drawdown Check ($50K):")
    print(f"  initial_balance: ${50_000:,.0f}")
    print(f"  eod_balance:     ${eod['eod_balance']:,.0f}")
    print(f"  drawdown:        ${eod['drawdown']:,.0f}")
    print(f"  limit:           ${eod['limit']:,.0f}")
    print(f"  threshold:       ${eod['threshold']:,.0f}")
    print(f"  remaining:       ${eod['remaining']:,.0f}")
    print(f"  passed:          {eod['passed']}")

    # Consistency Rule Check (40%ルール)
    cr = check_consistency_rule(
        rules      = rules,
        daily_pnls = [200, 300, 150, -100, 250, 180],
        today_pnl  = 400,
    )
    total_profit = sum(p for p in [200, 300, 150, -100, 250, 180, 400] if p > 0)
    print(f"\nConsistency Rule Check ($50K, 40% rule):")
    print(f"  today_pnl:     ${cr['today_pnl']:+,.0f}")
    print(f"  total_profit:  ${cr['total_profit']:,.0f}")
    print(f"  max_allowed:   ${cr['max_allowed']:,.0f} (40% of ${cr['total_profit']:,.0f})")
    print(f"  passed:        {cr['passed']}")
    if not cr["passed"]:
        print(f"  violation:     ${cr['violation_amount']:,.0f} over limit")
