"""atlas_v3/bots/engines/vix_tail_hedge.py — VIX Tail Hedge 戦術エンジン

設計概要:
    ブラックスワン保険として VIX call (deep OTM) を long 保有する。
    ポートフォリオ全体の損失を限定する役割を担い、ノーマル相場では
    プレミアム支出は portfolio 残高の 0.5-2% 以内に収める。

    対象オプション:
        VIX call long / deep OTM / delta 0.10-0.15 / 30-60 DTE

    エントリーロジック（monthly roll 方式）:
        - VIX < vix_entry_max (default 20) のとき安く仕込む（IVR low filter）
        - 月次ロール日（月の第1営業日）に既存ポジションをロールオーバー
        - portfolio_value に対して premium_cap_pct (0.5-2%) を超える発注は拒否

    エグジットロジック:
        1. Kill Switch ARMED        → 強制クローズ
        2. VIX > vix_profit_exit (default 30) に到達 AND pnl が
           entry premium の profit_multiplier_min (5x) 以上 → spike_profit
        3. 満期到達（DTE == 0）       → expiry_close
        4. 通常 holding              → なし

    禁則:
        - spy_bot.py / chronos_bot.py / common/* への書換禁止
        - IVR / VIX しきい値のハードコード禁止（config DTO 経由）
        - CC <= 20 規律
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")

#: VIX call delta 範囲（deep OTM: 保険として安価に仕込む）
VIX_CALL_DELTA_MIN: float = 0.10
VIX_CALL_DELTA_MAX: float = 0.15

#: DTE 範囲（30-60 DTE でロールオーバー）
VIX_CALL_DTE_MIN: int = 30
VIX_CALL_DTE_MAX: int = 60

#: エントリー条件: VIX がこの値未満の時のみ仕込む（安い時に買う）
VIX_ENTRY_MAX: float = 20.0

#: スパイク時利確条件: VIX がこの値を超えたら spike 利確評価
VIX_PROFIT_EXIT: float = 30.0

#: profit_multiplier: entry premium のこの倍数以上で spike 利確
PROFIT_MULTIPLIER_MIN: float = 5.0

#: portfolio に対するプレミアム上限（比率: 0.005 = 0.5%）
PREMIUM_CAP_PCT_MIN: float = 0.005  # 0.5%
PREMIUM_CAP_PCT_MAX: float = 0.020  # 2.0%

#: デフォルトの premium cap (1.0%)
PREMIUM_CAP_PCT_DEFAULT: float = 0.010


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class VixTailHedgeConfig:
    """VIX Tail Hedge エンジン設定。

    Attributes:
        delta_min:            VIX call の delta 下限（deep OTM）
        delta_max:            VIX call の delta 上限（deep OTM）
        dte_min:              保有 DTE 下限（ロール下限）
        dte_max:              保有 DTE 上限（ロール上限）
        vix_entry_max:        エントリー許可 VIX 上限（この値未満で仕込む）
        vix_profit_exit:      スパイク利確評価を開始する VIX 水準
        profit_multiplier_min: entry premium の何倍で spike 利確するか
        premium_cap_pct:      portfolio_value に対するプレミアム上限比率（0.005-0.02）
        quantity:             デフォルト発注枚数
        roll_day_of_month:    月次ロール日（何日に roll するか。1=第1日）
    """
    delta_min: float = VIX_CALL_DELTA_MIN
    delta_max: float = VIX_CALL_DELTA_MAX
    dte_min: int = VIX_CALL_DTE_MIN
    dte_max: int = VIX_CALL_DTE_MAX
    vix_entry_max: float = VIX_ENTRY_MAX
    vix_profit_exit: float = VIX_PROFIT_EXIT
    profit_multiplier_min: float = PROFIT_MULTIPLIER_MIN
    premium_cap_pct: float = PREMIUM_CAP_PCT_DEFAULT
    quantity: int = 1
    roll_day_of_month: int = 1

    def __post_init__(self) -> None:
        """設定値バリデーション。

        Raises:
            ValueError: delta / DTE / VIX / premium_cap 範囲が矛盾する場合
        """
        if not (0.0 < self.delta_min < self.delta_max < 1.0):
            raise ValueError(
                f"delta_min={self.delta_min} / delta_max={self.delta_max}: "
                "0 < min < max < 1 を満たしていません"
            )
        if not (0 < self.dte_min <= self.dte_max):
            raise ValueError(
                f"dte_min={self.dte_min} / dte_max={self.dte_max}: "
                "0 < min <= max を満たしていません"
            )
        if not (0.0 < self.vix_entry_max < self.vix_profit_exit):
            raise ValueError(
                f"vix_entry_max={self.vix_entry_max} / vix_profit_exit={self.vix_profit_exit}: "
                "0 < vix_entry_max < vix_profit_exit を満たしていません"
            )
        if self.profit_multiplier_min <= 1.0:
            raise ValueError(
                f"profit_multiplier_min={self.profit_multiplier_min}: > 1.0 が必要です"
            )
        if not (PREMIUM_CAP_PCT_MIN <= self.premium_cap_pct <= PREMIUM_CAP_PCT_MAX):
            raise ValueError(
                f"premium_cap_pct={self.premium_cap_pct}: "
                f"[{PREMIUM_CAP_PCT_MIN}, {PREMIUM_CAP_PCT_MAX}] の範囲内でなければなりません"
            )
        if not (1 <= self.roll_day_of_month <= 28):
            raise ValueError(
                f"roll_day_of_month={self.roll_day_of_month}: 1-28 の範囲内でなければなりません"
            )


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VixTailHedgeEntryDecision:
    """VIX Tail Hedge エントリー決定。

    Attributes:
        should_enter:       エントリー可否
        symbol:             対象銘柄（"VIX" 固定想定）
        delta_target:       delta 目標値（deep OTM call）
        dte_target:         DTE 目標値
        quantity:           発注枚数
        estimated_premium:  見積もりプレミアム（発注前検証用）
        reason:             判定理由
        idempotency_key:    冪等性キー
    """
    should_enter: bool
    symbol: str
    delta_target: float = 0.0
    dte_target: int = 0
    quantity: int = 1
    estimated_premium: float = 0.0
    reason: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class VixTailHedgeExitDecision:
    """VIX Tail Hedge エグジット決定。

    Attributes:
        should_exit: エグジット可否
        reason:      判定理由
        exit_type:   エグジット種別
    """
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "spike_profit",
        "expiry_close",
        "monthly_roll",
        "force_close",
        "none",
    ] = "none"


# ---------------------------------------------------------------------------
# Position DTO
# ---------------------------------------------------------------------------

@dataclass
class VixTailHedgePosition:
    """VIX Tail Hedge ポジション表現。

    Attributes:
        symbol:             銘柄（"VIX" 等）
        quantity:           枚数
        entry_premium:      エントリー時のプレミアム支払い総額（per unit）
        current_value:      現在のオプション価値（per unit）
        expiry:             満期日
        entry_time:         エントリー時刻 (UTC)
        unrealized_pnl:     含み損益 (current_value - entry_premium) * quantity * 100
        tactic_name:        戦術名
    """
    symbol: str
    quantity: int
    entry_premium: float
    current_value: float = 0.0
    expiry: date | None = None
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: float = 0.0
    tactic_name: str = "vix_tail_hedge"


# ---------------------------------------------------------------------------
# VixTailHedgeEngine — Type A: EnterExit（TacticBase 継承）
# ---------------------------------------------------------------------------

class VixTailHedgeEngine(TacticBase):
    """VIX Tail Hedge 戦術エンジン（Type A: enter_exit）。

    VIX が低位（< vix_entry_max）のときに deep OTM VIX call を long し、
    ブラックスワン発生時（VIX spike）に portfolio 全体を守る保険として機能する。

    エントリー条件:
    - VIX < vix_entry_max (20): 安い時に仕込む（IVR low filter）
    - estimated_premium <= portfolio_value * premium_cap_pct: コスト上限守護
    - 月次ロール日（roll_day_of_month）に発動

    エグジット:
    - Kill Switch ARMED    → force_close（最優先）
    - VIX > vix_profit_exit (30) AND pnl >= entry_premium * profit_multiplier_min → spike_profit
    - DTE == 0（満期到達）→ expiry_close
    - 月次ロール日到達    → monthly_roll（existing position 入替え）

    Args:
        config: VixTailHedgeConfig（None のときデフォルト設定を使用）
    """

    def __init__(self, config: VixTailHedgeConfig | None = None) -> None:
        self._cfg = config or VixTailHedgeConfig()

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "vix_tail_hedge"

    # ------------------------------------------------------------------
    # TacticBase 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック項目（優先順）:
        1. env None → False（型安全ガード）
        2. Kill Switch ARMED → False（EICAS Advisory）

        VIX 水準は preflight では弾かない。
        should_enter / should_exit のフィルタで制御するため。

        Returns:
            True  — 戦術発動可能
            False — 発動不可（理由は log に必ず出力）
        """
        if env is None:
            log.warning("[VixTailHedgeEngine.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[VixTailHedgeEngine.preflight] Kill Switch ARMED: vix_tail_hedge を無効化"
            )
            return False

        return True

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol: str,
        portfolio_value: float = 0.0,
        estimated_premium: float = 0.0,
        today: date | None = None,
    ) -> VixTailHedgeEntryDecision:
        """エントリー判定（monthly roll / IVR low filter）。

        判定順:
        0. Kill Switch ARMED → should_enter=False（発注完全遮断）
        1. estimated_premium が NaN/inf → ValueError
        2. VIX >= vix_entry_max → should_enter=False（安い時のみ）
        3. portfolio_value > 0 AND premium > portfolio_value * cap_pct → False（コスト上限）
        4. 月次ロール日でない → should_enter=False（monthly roll 専用）
        5. 全条件 pass → delta 中央値 / dte 中央値でエントリー決定

        Args:
            env:               現在の市場環境スナップショット
            symbol:            対象銘柄（通常 "VIX"）
            portfolio_value:   ポートフォリオ残高（0.0 = チェックスキップ）
            estimated_premium: 見積もりプレミアム（1 枚当たり・0.0 = チェックスキップ）
            today:             判定基準日（None のとき ET 当日。テスト用注入可能）

        Returns:
            VixTailHedgeEntryDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[VixTailHedgeEngine.should_enter] Kill Switch ARMED: "
                "エントリー判定をスキップ (symbol=%s)",
                symbol,
            )
            return VixTailHedgeEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="kill_switch_armed",
            )

        # estimated_premium 数値検証
        if not math.isfinite(estimated_premium):
            raise ValueError(
                f"estimated_premium={estimated_premium!r} は NaN または inf です。"
                "有限の正の数値を渡してください。"
            )

        # VIX エントリーフィルタ: VIX < vix_entry_max のときのみ仕込む
        if env.vix >= self._cfg.vix_entry_max:
            log.debug(
                "[VixTailHedgeEngine.should_enter] VIX=%.2f >= vix_entry_max=%.2f: "
                "エントリー見送り（高値では仕込まない）(symbol=%s)",
                env.vix, self._cfg.vix_entry_max, symbol,
            )
            return VixTailHedgeEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"VIX={env.vix:.2f} >= vix_entry_max={self._cfg.vix_entry_max:.2f}: 仕込み条件外",
            )

        # premium コスト上限チェック（portfolio_value > 0 の場合のみ）
        if portfolio_value > 0.0 and estimated_premium > 0.0:
            cap = portfolio_value * self._cfg.premium_cap_pct
            total_premium = estimated_premium * self._cfg.quantity
            if total_premium > cap:
                log.warning(
                    "[VixTailHedgeEngine.should_enter] premium=%.2f > cap=%.2f (%.1f%% of portfolio): "
                    "コスト上限超過・エントリー拒否 (symbol=%s)",
                    total_premium, cap, self._cfg.premium_cap_pct * 100, symbol,
                )
                return VixTailHedgeEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    estimated_premium=estimated_premium,
                    reason=(
                        f"premium {total_premium:.2f} > cap {cap:.2f} "
                        f"({self._cfg.premium_cap_pct * 100:.1f}% of portfolio={portfolio_value:.0f})"
                    ),
                )

        # monthly roll 日チェック
        ref_date = today or datetime.now(_ET).date()
        if not self._is_roll_day(ref_date):
            log.debug(
                "[VixTailHedgeEngine.should_enter] ロール日ではない (day=%d, roll_day=%d): スキップ (symbol=%s)",
                ref_date.day, self._cfg.roll_day_of_month, symbol,
            )
            return VixTailHedgeEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"非ロール日 (today={ref_date}, roll_day={self._cfg.roll_day_of_month})",
            )

        # delta / dte 中央値を採用
        delta_target = (self._cfg.delta_min + self._cfg.delta_max) / 2.0
        dte_target = (self._cfg.dte_min + self._cfg.dte_max) // 2

        # idempotency key: 月単位バケット（同月内の二重発注を防ぐ）
        trigger_time = datetime(
            ref_date.year, ref_date.month, 1, 0, 0, 0, tzinfo=timezone.utc
        )
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[VixTailHedgeEngine.should_enter] エントリー OK: symbol=%s "
            "VIX=%.2f delta_target=%.3f dte_target=%d qty=%d key=%s",
            symbol, env.vix, delta_target, dte_target, self._cfg.quantity, idem_key,
        )
        return VixTailHedgeEntryDecision(
            should_enter=True,
            symbol=symbol,
            delta_target=delta_target,
            dte_target=dte_target,
            quantity=self._cfg.quantity,
            estimated_premium=estimated_premium,
            reason=(
                f"VIX={env.vix:.2f}<{self._cfg.vix_entry_max:.2f} / "
                f"delta={delta_target:.3f} / dte={dte_target}"
            ),
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: build_order
    # ------------------------------------------------------------------

    def build_order(
        self,
        decision: VixTailHedgeEntryDecision,
        paper_mode: bool = True,
        capital_usd: float = 0.0,
    ) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。

        Args:
            decision:    should_enter=True の VixTailHedgeEntryDecision
            paper_mode:  True = paper 発注（PDT チェックスキップ）
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            OrderRequest — 発注 DTO

        Raises:
            ValueError:      decision.should_enter=False の場合
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_enter:
            raise ValueError(
                f"[VixTailHedgeEngine.build_order] "
                f"should_enter=False の decision が渡された: {decision}"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        _option_price = getattr(decision, "option_price", 0.0) or 0.0
        _gr = _gate(_Ctx(symbol=decision.symbol, qty=decision.quantity, side="BUY", is_long=True,
                         option_price=float(_option_price)))
        if not _gr.allowed:
            raise ValueError(f"[VixTailHedgeEngine.build_order] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        return OrderRequest(
            symbol=decision.symbol,
            side="buy",       # VIX call long = buy
            quantity=decision.quantity,
            order_type="limit",
            tactic_name=self.tactic_name,
            idempotency_key=decision.idempotency_key,
        )

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: should_exit
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: VixTailHedgePosition,
        env: MarketEnvironment,
        today: date | None = None,
    ) -> VixTailHedgeExitDecision:
        """エグジット判定。

        判定順（優先度降順）:
        1. Kill Switch ARMED → force_close（最優先）
        2. entry_premium 未設定（<= 0）→ 判定不能（holding 返却）
        3. VIX > vix_profit_exit AND unrealized_pnl >= entry_premium * profit_multiplier_min → spike_profit
        4. 満期到達（expiry <= today_et または expiry None 扱い）→ expiry_close
        5. 月次ロール日 → monthly_roll（ポジション入替え）

        Args:
            position: 現在ポジション（VixTailHedgePosition）
            env:      現在の市場環境
            today:    判定基準日（None のとき ET 当日。テスト用注入可能）

        Returns:
            VixTailHedgeExitDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[VixTailHedgeEngine.should_exit] Kill Switch ARMED: "
                "強制クローズ (symbol=%s)",
                position.symbol,
            )
            return VixTailHedgeExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        if position.entry_premium <= 0:
            log.warning(
                "[VixTailHedgeEngine.should_exit] entry_premium=%.4f: "
                "exit 判定不能 (symbol=%s)",
                position.entry_premium, position.symbol,
            )
            return VixTailHedgeExitDecision(
                should_exit=False,
                reason="entry_premium_not_set",
            )

        # VIX spike 利確: VIX > threshold AND pnl >= entry_premium * multiplier
        if self._is_spike_profit(position, env):
            log.info(
                "[VixTailHedgeEngine.should_exit] VIX spike 利確: "
                "VIX=%.2f > %.2f / pnl=%.2f >= %.1fx entry (symbol=%s)",
                env.vix, self._cfg.vix_profit_exit,
                position.unrealized_pnl,
                self._cfg.profit_multiplier_min,
                position.symbol,
            )
            return VixTailHedgeExitDecision(
                should_exit=True,
                reason=(
                    f"spike_profit: VIX={env.vix:.2f}>{self._cfg.vix_profit_exit:.2f} "
                    f"/ pnl={position.unrealized_pnl:.2f}>="
                    f"{position.entry_premium * self._cfg.profit_multiplier_min:.2f}"
                ),
                exit_type="spike_profit",
            )

        ref_date = today or datetime.now(_ET).date()

        # 満期到達チェック
        if self._is_expired(position, ref_date):
            log.info(
                "[VixTailHedgeEngine.should_exit] 満期到達: expiry=%s today=%s (symbol=%s)",
                position.expiry, ref_date, position.symbol,
            )
            return VixTailHedgeExitDecision(
                should_exit=True,
                reason=f"expiry_close: expiry={position.expiry} / today={ref_date}",
                exit_type="expiry_close",
            )

        # 月次ロール日チェック
        if self._is_roll_day(ref_date):
            log.info(
                "[VixTailHedgeEngine.should_exit] 月次ロール日到達: "
                "既存ポジションをクローズして入替え (symbol=%s day=%d)",
                position.symbol, ref_date.day,
            )
            return VixTailHedgeExitDecision(
                should_exit=True,
                reason=f"monthly_roll: roll_day={self._cfg.roll_day_of_month} / today={ref_date}",
                exit_type="monthly_roll",
            )

        return VixTailHedgeExitDecision(
            should_exit=False,
            reason="holding",
            exit_type="none",
        )

    def build_exit_order(
        self,
        position: VixTailHedgePosition,
        decision: VixTailHedgeExitDecision,
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。

        Args:
            position: 現在ポジション
            decision: should_exit=True の VixTailHedgeExitDecision

        Returns:
            OrderRequest

        Raises:
            ValueError: decision.should_exit=False の場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_exit:
            raise ValueError(
                "[VixTailHedgeEngine.build_exit_order] "
                "should_exit=False の decision が渡された"
            )

        _now_exit = datetime.now(timezone.utc)
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit_{decision.exit_type}",
            symbol=position.symbol,
            trigger_time=_now_exit,
        )

        return OrderRequest(
            symbol=position.symbol,
            side="sell",      # long の hand back = sell
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _is_roll_day(self, ref_date: date) -> bool:
        """指定日が月次ロール日かどうかを返す。

        Args:
            ref_date: 判定基準日

        Returns:
            True — roll_day_of_month に一致する日
        """
        return ref_date.day == self._cfg.roll_day_of_month

    def _is_expired(self, position: VixTailHedgePosition, ref_date: date) -> bool:
        """満期到達チェック。

        position.expiry が None の場合は expired 扱いとしない（DTE 情報なし）。
        expiry <= today の場合は expired。

        Args:
            position: ポジション
            ref_date: 基準日

        Returns:
            True — 満期到達または当日
        """
        if position.expiry is None:
            return False
        return ref_date >= position.expiry

    def _is_spike_profit(
        self,
        position: VixTailHedgePosition,
        env: MarketEnvironment,
    ) -> bool:
        """VIX spike 利確条件チェック。

        VIX > vix_profit_exit かつ
        unrealized_pnl >= entry_premium * profit_multiplier_min の両条件が
        必要。

        Args:
            position: ポジション
            env:      市場環境

        Returns:
            True — 利確条件成立
        """
        if env.vix <= self._cfg.vix_profit_exit:
            return False
        profit_threshold = position.entry_premium * self._cfg.profit_multiplier_min
        return position.unrealized_pnl >= profit_threshold
