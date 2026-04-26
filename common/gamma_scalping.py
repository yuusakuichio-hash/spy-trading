"""common/gamma_scalping.py — ガンマスキャルピング 手数料試算 + PnL シミュレーター

Atlas 施策7: 0DTE ATM Straddle + Delta Hedge (Gamma Scalping)

設計根拠:
  - ORATS研究 Entry 5: Buy Straddle 0.40%/trade (OOS 0.65%)
  - 0DTE ATM はガンマ最大 → デルタリセットでガンマPnL蓄積
  - VIX低環境（IVR < 25%）でCS売りとの非相関分散効果
  - 期待月利寄与 +0.5-1.5%

手数料モデル (moomoo):
  - オプション発注: $0.65/contract (往復 $1.30)
  - SPY株: $0/share
  - スリッページ: ATM 0DTE bid-ask $0.03〜$0.10/leg を保守的に $0.05 で試算

制約:
  - IVR > 25% → disable（BT根拠）
  - 手数料 > ガンマPnL → 戦術 skip（enable_gamma_scalping=False を返す）
  - moomoo rate limit 20 req/sec → ヘッジ発注は最低 30 秒間隔

ヘッジ頻度パターン:
  - Pattern A: 5分ごと
  - Pattern B: 15分ごと（デフォルト）
  - Pattern C: 30分ごと

Usage:
  from common.gamma_scalping import (
      GammaScalpFeeSimulator,
      GammaScalpPnLResult,
      should_enable_gamma_scalping,
  )
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── 手数料定数 ─────────────────────────────────────────────────────────────────
MOOMOO_OPTION_FEE_PER_CONTRACT: float = 0.65   # $0.65/contract
MOOMOO_SPY_STOCK_FEE_PER_SHARE: float = 0.00   # $0/share (SPY株は無料)
SLIPPAGE_PER_LEG: float = 0.05                 # ATM 0DTE bid-ask スリッページ推定

# ── IVR 無効化閾値 ─────────────────────────────────────────────────────────────
GAMMA_SCALP_IVR_DISABLE_THRESHOLD: float = 25.0   # IVR > 25% → disable

# ── ヘッジ帯域（|delta| がこの値を超えたらヘッジ実行）────────────────────────────
DELTA_BAND_VIX_LOW:  float = 0.25   # VIX < 15
DELTA_BAND_VIX_MID:  float = 0.20   # VIX 15-20
DELTA_BAND_VIX_HIGH: float = 0.15   # VIX 20-25
DELTA_BAND_VIX_EXTREME: float = 0.10  # VIX > 25

# ── ヘッジ頻度パターン（分）──────────────────────────────────────────────────────
HEDGE_INTERVAL_PATTERNS: dict[str, int] = {
    "5min": 5,
    "15min": 15,
    "30min": 30,
}

# ── moomoo rate limit 対応: 発注間隔最短 30 秒 ──────────────────────────────────
MIN_HEDGE_INTERVAL_SEC: int = 30


@dataclass
class StraddleCostParams:
    """ストラドル 1 ロットのコスト・Greeks パラメータ（実測または推定値）。"""
    spy_price: float          # SPY 原資産価格（例: 550.0）
    vix: float                # VIX 指数
    call_mid: float           # ATM Call mid 価格（ドル/オプション）
    put_mid: float            # ATM Put mid 価格（ドル/オプション）
    gamma: float              # ATM ガンマ（per-dollar per contract, 0DTE 典型値: 0.10-0.30）
    theta_per_day: float      # シータ（1 日当たり損失、負値）
    qty: int = 1              # ストラドル枚数

    @property
    def entry_cost(self) -> float:
        """ストラドル購入コスト（手数料込み）。"""
        straddle_mid = (self.call_mid + self.put_mid) * 100 * self.qty
        fee = MOOMOO_OPTION_FEE_PER_CONTRACT * 2 * self.qty  # CALL + PUT × qty
        slip = SLIPPAGE_PER_LEG * 2 * 100 * self.qty         # 2 legs × 100 shares
        return straddle_mid + fee + slip

    @property
    def theta_cost_day(self) -> float:
        """1 日のシータ損失（正の値で返す）。"""
        return abs(self.theta_per_day) * self.qty * 100


@dataclass
class HedgeRoundTrip:
    """1 回のヘッジ往復コスト試算。"""
    delta_at_trigger: float      # トリガー時の portfolio delta
    spy_price: float             # ヘッジ時の SPY 価格
    hedge_shares: int            # ヘッジ株数（= round(|delta| × 100 × qty)）

    @property
    def stock_fee(self) -> float:
        """SPY 株の手数料（moomoo は 0）。"""
        return MOOMOO_SPY_STOCK_FEE_PER_SHARE * self.hedge_shares

    @property
    def stock_slippage(self) -> float:
        """SPY 株スリッページ（$0.01/share 保守推定）。"""
        return 0.01 * self.hedge_shares

    @property
    def total_cost(self) -> float:
        return self.stock_fee + self.stock_slippage


@dataclass
class GammaScalpPnLResult:
    """ヘッジ頻度パターンごとの PnL 試算結果。"""
    pattern: str                  # "5min" / "15min" / "30min"
    hedge_count: int              # ヘッジ実施回数（trading 日 6.5h）
    gamma_pnl_gross: float        # ガンマ利益（手数料前）
    theta_cost: float             # シータ損失（1日）
    hedge_cost_total: float       # ヘッジ往復コスト合計
    entry_fee: float              # ストラドル買い手数料（往復）
    exit_fee: float               # ストラドル売り手数料（決済）
    net_pnl: float                # ネット PnL
    ev_pct: float                 # EV / entry_cost (%)
    enable_gamma_scalping: bool   # 手数料がガンマPnL を上回る場合 False

    def summary(self) -> str:
        flag = "ENABLE" if self.enable_gamma_scalping else "DISABLE (fees>gamma)"
        return (
            f"[GammaScalp/{self.pattern}] "
            f"hedges={self.hedge_count} "
            f"gamma_gross=${self.gamma_pnl_gross:.2f} "
            f"theta=${self.theta_cost:.2f} "
            f"hedge_cost=${self.hedge_cost_total:.2f} "
            f"entry_fee=${self.entry_fee:.2f} "
            f"exit_fee=${self.exit_fee:.2f} "
            f"net=${self.net_pnl:.2f} "
            f"ev={self.ev_pct:.2f}% "
            f"→ {flag}"
        )


class GammaScalpFeeSimulator:
    """ガンマスキャルピング 手数料試算 + PnL シミュレーター。

    Args:
        params: StraddleCostParams (エントリー時のオプション情報)
        trading_hours: 取引時間（時間）。0DTE は 6.5h
        spy_daily_move_avg: SPY 日中平均変動幅（ドル）。例: 2.0（VIX15〜20 相当）
        ivr: IVR (%) 現在値
    """

    TRADING_HOURS: float = 6.5   # ET 09:30〜16:00

    def __init__(
        self,
        params: StraddleCostParams,
        spy_daily_move_avg: float = 2.0,
        ivr: float = 15.0,
    ):
        self.params = params
        self.spy_daily_move_avg = spy_daily_move_avg
        self.ivr = ivr

    @property
    def delta_band(self) -> float:
        """VIX に連動した動的ヘッジ帯域。"""
        v = self.params.vix
        if v < 15:
            return DELTA_BAND_VIX_LOW
        if v < 20:
            return DELTA_BAND_VIX_MID
        if v < 25:
            return DELTA_BAND_VIX_HIGH
        return DELTA_BAND_VIX_EXTREME

    def _estimate_hedge_count(self, interval_min: int) -> int:
        """1 日のヘッジ回数を推定する（帯域トリガー vs 時間インターバルの最小）。

        時間インターバルが短いほどトリガーは多いが、帯域が広ければ
        実際のトリガー頻度は低くなる。保守的に min() を使う。
        """
        minutes_per_day = int(self.TRADING_HOURS * 60)
        interval_triggers = minutes_per_day // interval_min

        # 帯域方式: 平均変動ペースで帯域超過頻度を推定
        # SPY が BAND をまたぐ回数 = daily_move / BAND × 0.5（往復）
        band_triggers = int(self.spy_daily_move_avg / max(self.delta_band, 0.01) * 0.5)

        # moomoo rate limit: 最大 2 回/分（30 秒間隔）
        rate_limit_max = minutes_per_day * 2

        count = min(interval_triggers, band_triggers, rate_limit_max)
        return max(count, 0)

    def _gamma_pnl_per_hedge(self, delta_move: float) -> float:
        """1 回のヘッジで得られるガンマ利益。

        Gamma PnL = 0.5 × Gamma × (ΔS)^2 × 100 × qty
        delta_move = ヘッジ 1 回当たりの平均 SPY 変動幅（ドル）
        """
        return 0.5 * self.params.gamma * (delta_move ** 2) * 100 * self.params.qty

    def _hedge_cost_per_trip(self) -> float:
        """ヘッジ 1 往復のコスト（SPY 株手数料 + スリッページ）。"""
        hedge_shares = max(1, round(self.delta_band * 100 * self.params.qty))
        rtrip = HedgeRoundTrip(
            delta_at_trigger=self.delta_band,
            spy_price=self.params.spy_price,
            hedge_shares=hedge_shares,
        )
        return rtrip.total_cost * 2  # 往復（open + close）

    def simulate_pattern(self, pattern: str) -> GammaScalpPnLResult:
        """単一ヘッジ頻度パターンの PnL を試算する。

        Args:
            pattern: "5min" / "15min" / "30min"
        """
        interval_min = HEDGE_INTERVAL_PATTERNS[pattern]
        hedge_count = self._estimate_hedge_count(interval_min)

        # ヘッジ 1 回当たりの SPY 変動幅（日中平均 / ヘッジ回数の平方根）
        delta_move = (
            self.spy_daily_move_avg / math.sqrt(max(hedge_count, 1))
            if hedge_count > 0
            else 0.0
        )

        # ガンマ利益
        gamma_pnl_gross = self._gamma_pnl_per_hedge(delta_move) * hedge_count

        # シータ損失
        theta_cost = self.params.theta_cost_day

        # ヘッジ往復コスト
        hedge_cost_total = self._hedge_cost_per_trip() * hedge_count

        # ストラドル 購入手数料（エントリー時のみ: fee + slip）
        entry_fee = (
            MOOMOO_OPTION_FEE_PER_CONTRACT * 2 * self.params.qty
            + SLIPPAGE_PER_LEG * 2 * 100 * self.params.qty
        )
        # 決済手数料（エグジット: 同額）
        exit_fee = entry_fee

        net_pnl = gamma_pnl_gross - theta_cost - hedge_cost_total - entry_fee - exit_fee

        entry_cost_raw = (self.params.call_mid + self.params.put_mid) * 100 * self.params.qty
        ev_pct = (net_pnl / max(entry_cost_raw, 0.01)) * 100.0

        # 手数料 + シータ > ガンマ利益 → disable
        total_cost = theta_cost + hedge_cost_total + entry_fee + exit_fee
        enable = gamma_pnl_gross > total_cost

        result = GammaScalpPnLResult(
            pattern=pattern,
            hedge_count=hedge_count,
            gamma_pnl_gross=gamma_pnl_gross,
            theta_cost=theta_cost,
            hedge_cost_total=hedge_cost_total,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            net_pnl=net_pnl,
            ev_pct=ev_pct,
            enable_gamma_scalping=enable,
        )
        log.info(result.summary())
        return result

    def simulate_all_patterns(self) -> dict[str, GammaScalpPnLResult]:
        """全 3 パターン（5min / 15min / 30min）を一括試算する。"""
        return {p: self.simulate_pattern(p) for p in HEDGE_INTERVAL_PATTERNS}

    def best_pattern(self) -> Optional[GammaScalpPnLResult]:
        """最大 net_pnl かつ enable_gamma_scalping=True のパターンを返す。

        全パターン disable なら None を返す（戦術 skip 推奨）。
        """
        results = self.simulate_all_patterns()
        enabled = [r for r in results.values() if r.enable_gamma_scalping]
        if not enabled:
            log.warning(
                "[GammaScalp] 全パターンで手数料 > ガンマPnL → enable_gamma_scalping=False"
            )
            return None
        return max(enabled, key=lambda r: r.net_pnl)


def should_enable_gamma_scalping(
    ivr: float,
    vix: float,
    spy_price: float,
    call_mid: float,
    put_mid: float,
    gamma: float,
    theta_per_day: float,
    qty: int = 1,
    spy_daily_move_avg: float = 2.0,
) -> tuple[bool, str, Optional[GammaScalpPnLResult]]:
    """ガンマスキャルピング戦術を有効にすべきか判定する。

    Args:
        ivr: IVR (%) 現在値
        vix: VIX 指数
        spy_price: SPY 現在価格
        call_mid: ATM Call mid 価格
        put_mid: ATM Put mid 価格
        gamma: ATM ガンマ（0DTE 典型値: 0.10〜0.30）
        theta_per_day: シータ（負値）
        qty: ストラドル枚数
        spy_daily_move_avg: SPY 日中平均変動幅（ドル）

    Returns:
        (enabled: bool, reason: str, best_result: Optional[GammaScalpPnLResult])
    """
    # IVR 条件チェック（BT根拠: IVR > 25% はコスト高でEV負）
    if ivr > GAMMA_SCALP_IVR_DISABLE_THRESHOLD:
        reason = (
            f"IVR={ivr:.1f}% > {GAMMA_SCALP_IVR_DISABLE_THRESHOLD}% "
            f"→ ストラドルコスト高・EV負 (BT根拠)"
        )
        log.info(f"[GammaScalp] DISABLED: {reason}")
        return False, reason, None

    params = StraddleCostParams(
        spy_price=spy_price,
        vix=vix,
        call_mid=call_mid,
        put_mid=put_mid,
        gamma=gamma,
        theta_per_day=theta_per_day,
        qty=qty,
    )
    sim = GammaScalpFeeSimulator(
        params=params,
        spy_daily_move_avg=spy_daily_move_avg,
        ivr=ivr,
    )
    best = sim.best_pattern()

    if best is None:
        reason = "全ヘッジ頻度パターンで手数料 > ガンマPnL → enable_gamma_scalping=False"
        log.warning(f"[GammaScalp] DISABLED: {reason}")
        return False, reason, None

    reason = (
        f"最適パターン={best.pattern} "
        f"net_pnl=${best.net_pnl:.2f} "
        f"ev={best.ev_pct:.2f}% "
        f"hedges={best.hedge_count}"
    )
    log.info(f"[GammaScalp] ENABLED: {reason}")
    return True, reason, best


def estimate_monthly_contribution(
    daily_ev_pct: float,
    capital_usd: float,
    trading_days: int = 21,
) -> dict[str, float]:
    """月次月利寄与を試算する。

    Args:
        daily_ev_pct: 1 日の EV (% of entry_cost)
        capital_usd: 元本（USD）
        trading_days: 月次取引日数（デフォルト: 21）

    Returns:
        {
            "daily_ev_usd": float,         # 1 日 EV（USD）
            "monthly_ev_usd": float,       # 月次 EV（USD）
            "monthly_contribution_pct": float,  # 元本に対する月利寄与（%）
        }
    """
    daily_ev_usd = capital_usd * (daily_ev_pct / 100.0)
    monthly_ev_usd = daily_ev_usd * trading_days
    monthly_pct = (monthly_ev_usd / max(capital_usd, 1.0)) * 100.0
    return {
        "daily_ev_usd": daily_ev_usd,
        "monthly_ev_usd": monthly_ev_usd,
        "monthly_contribution_pct": monthly_pct,
    }
