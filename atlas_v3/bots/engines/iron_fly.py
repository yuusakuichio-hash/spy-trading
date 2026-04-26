"""atlas_v3/bots/engines/iron_fly.py — Iron Fly 戦術エンジン（Type A: EnterExit）

設計概要:
    Iron Fly = ATM short call + ATM short put (同 strike) +
               OTM long call (ATM + wing_width) +
               OTM long put  (ATM - wing_width)
    → 4 leg 構成・受取クレジット最大化・wing 幅は 5-10pt 設定

エントリー条件:
    - IVR > 70（高 IV 環境でプレミアム売りが有利）
    - VIX < 25（低ボラ前提: 急変動リスクが低い range 相場）
    - entry_window: 10:30-11:30 ET（オープニングノイズ収束後・流動性十分）

エグジット条件（優先順位順）:
    1. Kill Switch ARMED       → 即時強制クローズ
    2. profit target 25%       → 受取クレジットの 25% 利確
    3. stop loss 1.5x credit   → 受取クレジットの 1.5 倍損失で撤退
    4. 15:40 ET force close    → 0DTE delta exposure 除去（狭 range で早め設定）

TradeEngine wrapper:
    self.eng.place_iron_fly(legs, quantity) を呼び出す。
    TradeEngineProtocol で型を定義し、futu SDK 依存を排除した
    stub 実装 (NoOpTradeEngine) をテスト用に同梱する。

禁則:
    - spy_bot.py / chronos_bot.py への import / 書換 禁止
    - asyncio event loop 内からの直接呼び出し禁止（sync 専用）
    - IVR / VIX フィルタのハードコード禁止（config DTO 経由）
    - CC <= 20 規律
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Literal, Optional, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.bots.engines.earnings_calendar_check import is_near_earnings
from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

ET = ZoneInfo("America/New_York")

#: entry window (ET): 10:30-11:30
ENTRY_WINDOW_START_H: int = 10
ENTRY_WINDOW_START_M: int = 30
ENTRY_WINDOW_END_H: int = 11
ENTRY_WINDOW_END_M: int = 30

#: force close 時刻 (ET): 15:40
FORCE_CLOSE_HOUR_ET: int = 15
FORCE_CLOSE_MINUTE_ET: int = 40


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class IronFlyConfig:
    """Iron Fly エンジン設定。

    Attributes:
        ivr_min:              エントリー最低 IVR（デフォルト 70）
        vix_max:              エントリー最大 VIX（デフォルト 25）
        wing_width_pts:       ATM から wing までの幅（ポイント数・5-10pt）
        profit_target_pct:    利確目標（クレジット比 0.25 = 25%）
        stop_loss_credit_x:   損切り倍率（受取クレジット × 1.5）
        quantity:             デフォルト発注枚数
        slippage_tolerance_bps: スリッページ許容幅 (basis points)
        force_close_hour_et:  強制クローズ時 (ET・24h)
        force_close_minute_et: 強制クローズ分 (ET)
    """
    ivr_min: float = 70.0
    vix_max: float = 25.0
    wing_width_pts: float = 5.0
    profit_target_pct: float = 0.25
    stop_loss_credit_x: float = 1.5
    quantity: int = 1
    slippage_tolerance_bps: int = 10
    force_close_hour_et: int = FORCE_CLOSE_HOUR_ET
    force_close_minute_et: int = FORCE_CLOSE_MINUTE_ET
    earnings_proximity_days: Optional[int] = 5


# ---------------------------------------------------------------------------
# IronFlyLeg — 個別 leg 表現
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IronFlyLeg:
    """Iron Fly の 1 leg 表現。

    Attributes:
        strike:   権利行使価格
        option_type: "call" | "put"
        side:     "buy" | "sell"
        quantity: 枚数
    """
    strike: float
    option_type: Literal["call", "put"]
    side: Literal["buy", "sell"]
    quantity: int


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IronFlyEntryDecision:
    """Iron Fly エントリー決定。

    legs は発注順序（指示仕様準拠）:
        [0] ATM short call  (sell)
        [1] ATM short put   (sell)
        [2] OTM long call   (buy)
        [3] OTM long put    (buy)
    """
    should_enter: bool
    symbol: str
    legs: tuple[IronFlyLeg, ...] = ()
    atm_strike: float = 0.0
    max_credit: float = 0.0
    quantity: int = 1
    reason: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class IronFlyExitDecision:
    """Iron Fly エグジット決定。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "profit_target", "stop_loss", "force_close", "kill_switch", "none"
    ] = "none"


# ---------------------------------------------------------------------------
# Position stub
# ---------------------------------------------------------------------------

@dataclass
class IronFlyPosition:
    """Iron Fly ポジション（Phase 2 で common_v3/position に差し替え）。

    max_credit は受取クレジット総額（ドル換算値）。
    unrealized_pnl は正値 = 利益（クレジット縮小分）、負値 = 損失。
    """
    symbol: str
    quantity: int
    atm_strike: float
    max_credit: float
    legs: tuple[IronFlyLeg, ...] = field(default_factory=tuple)
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: float = 0.0
    tactic_name: str = "iron_fly"


# ---------------------------------------------------------------------------
# TradeEngine Protocol（place_iron_fly wrapper 呼出インターフェース）
# ---------------------------------------------------------------------------

@runtime_checkable
class TradeEngineProtocol(Protocol):
    """TradeEngine.place_iron_fly() wrapper の呼出インターフェース。

    Phase 2 で futu SDK 経由の具体実装に差し替える。
    テスト用は NoOpTradeEngine を使用する。
    """

    def place_iron_fly(
        self,
        symbol: str,
        legs: tuple[IronFlyLeg, ...],
        quantity: int,
        idempotency_key: str = "",
    ) -> str:
        """4 leg を順に発注し、注文 ID を返す。

        Args:
            symbol:           対象銘柄コード
            legs:             IronFlyLeg タプル（ATM short call / ATM short put /
                              OTM long call / OTM long put の順序を保証）
            quantity:         発注枚数
            idempotency_key:  冪等性キー（重複発注防止）

        Returns:
            注文 ID 文字列（broker 依存・テスト時は "DRY_ORDER_<ts>"）
        """
        ...


class NoOpTradeEngine:
    """テスト用 TradeEngine stub（broker 接続なし）。

    place_iron_fly は発注をシミュレートして "DRY_ORDER_<ts>" を返す。
    """

    def place_iron_fly(
        self,
        symbol: str,
        legs: tuple[IronFlyLeg, ...],
        quantity: int,
        idempotency_key: str = "",
    ) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        order_id = f"DRY_ORDER_{ts}"
        log.info(
            "[NoOpTradeEngine.place_iron_fly] DRY symbol=%s legs=%d qty=%d key=%s order_id=%s",
            symbol,
            len(legs),
            quantity,
            idempotency_key,
            order_id,
        )
        return order_id


# ---------------------------------------------------------------------------
# IronFlyEngine — Type A: EnterExit
# ---------------------------------------------------------------------------

class IronFlyEngine(TacticBase):
    """Iron Fly 戦術エンジン（Type A: enter_exit）。

    設計:
        ATM strike を基準に ±wing_width_pts の 4 leg を構成。
        IVR > ivr_min かつ VIX < vix_max の環境フィルタを通過した場合のみ
        entry_window（ET 10:30-11:30）でエントリーを試みる。

        エグジットは kill_switch > profit_target > stop_loss > force_close の順で判定。

    Args:
        trade_engine: TradeEngineProtocol 実装（None のとき NoOpTradeEngine を使用）
        config:       IronFlyConfig（None のときデフォルト値）
    """

    def __init__(
        self,
        trade_engine: TradeEngineProtocol | None = None,
        config: IronFlyConfig | None = None,
        earnings_date_fn: "Optional[Callable[[str], Optional[object]]]" = None,
    ) -> None:
        self._eng: TradeEngineProtocol = trade_engine or NoOpTradeEngine()
        self._cfg = config or IronFlyConfig()
        self._earnings_date_fn = earnings_date_fn  # 決算日取得 DI（テスト用）

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "iron_fly"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック順:
        1. env None ガード
        2. Kill Switch ARMED → False
        3. VIX >= vix_max → False（高ボラ・range 相場前提崩壊）
        4. IVR（symbol="") < ivr_min → False（プレミアム不十分）

        Returns:
            True  — 戦術発動可能
            False — 発動不可（理由は log に必ず出力）
        """
        if env is None:
            log.warning("[IronFlyEngine.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning("[IronFlyEngine.preflight] Kill Switch ARMED: iron_fly を無効化")
            return False

        if env.vix >= self._cfg.vix_max:
            log.info(
                "[IronFlyEngine.preflight] VIX=%.2f >= vix_max=%.2f: range 相場前提崩壊・スキップ",
                env.vix,
                self._cfg.vix_max,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # entry window チェック（ET 10:30-11:30）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_in_entry_window(now_et: datetime | None = None) -> bool:
        """現在時刻が entry window（ET 10:30-11:30）内か判定する。

        Args:
            now_et: ET タイムゾーン付き datetime（None のとき現在時刻を使用）

        Returns:
            True — entry window 内
        """
        t = now_et or datetime.now(ET)
        window_start = t.replace(
            hour=ENTRY_WINDOW_START_H,
            minute=ENTRY_WINDOW_START_M,
            second=0,
            microsecond=0,
        )
        window_end = t.replace(
            hour=ENTRY_WINDOW_END_H,
            minute=ENTRY_WINDOW_END_M,
            second=0,
            microsecond=0,
        )
        return window_start <= t <= window_end

    # ------------------------------------------------------------------
    # 4 leg 構築
    # ------------------------------------------------------------------

    @staticmethod
    def _build_legs(
        atm_strike: float,
        wing_width_pts: float,
        quantity: int,
    ) -> tuple[IronFlyLeg, ...]:
        """Iron Fly 4 leg を仕様通りの順序で生成する。

        発注順序（指示仕様準拠）:
            [0] ATM short call  — sell / strike = atm
            [1] ATM short put   — sell / strike = atm
            [2] OTM long call   — buy  / strike = atm + wing_width_pts
            [3] OTM long put    — buy  / strike = atm - wing_width_pts

        Args:
            atm_strike:     ATM の権利行使価格
            wing_width_pts: ATM から wing までの幅（ポイント数）
            quantity:       枚数

        Returns:
            長さ 4 の IronFlyLeg タプル
        """
        return (
            IronFlyLeg(strike=atm_strike,                   option_type="call", side="sell", quantity=quantity),
            IronFlyLeg(strike=atm_strike,                   option_type="put",  side="sell", quantity=quantity),
            IronFlyLeg(strike=atm_strike + wing_width_pts,  option_type="call", side="buy",  quantity=quantity),
            IronFlyLeg(strike=atm_strike - wing_width_pts,  option_type="put",  side="buy",  quantity=quantity),
        )

    # ------------------------------------------------------------------
    # should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol: str,
        atm_strike: float = 0.0,
        max_credit: float = 0.0,
        now_et: datetime | None = None,
    ) -> IronFlyEntryDecision:
        """エントリー判定。

        判定順:
        1. entry_window 外 → should_enter=False
        2. IVR < ivr_min → should_enter=False
        3. VIX >= vix_max → should_enter=False（preflight 通過後の二重確認）
        4. 全条件通過 → 4 leg 構築・idempotency_key 生成・should_enter=True

        Args:
            env:        現在の MarketEnvironment
            symbol:     対象銘柄コード
            atm_strike: ATM の権利行使価格（0.0 の場合は entry 不可）
            max_credit: 見込みクレジット額（$・0.0 の場合は entry 不可）
            now_et:     ET 時刻（None のとき現在時刻。テスト用に注入可能）

        Returns:
            IronFlyEntryDecision
        """
        # 0. 決算近接チェック（earnings_proximity_days が設定されている場合のみ）
        # safe_default は earnings_date_fn 注入時のみ True（API キー未設定時のブロック防止）
        if self._cfg.earnings_proximity_days:
            _safe = self._earnings_date_fn is not None
            blocked, ep_reason = is_near_earnings(
                symbol=symbol,
                proximity_days=self._cfg.earnings_proximity_days,
                earnings_date_fn=self._earnings_date_fn,
                safe_default=_safe,
            )
            if blocked:
                log.info(
                    "[IronFlyEngine.should_enter] 決算近接ブロック: %s",
                    ep_reason,
                )
                return IronFlyEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    reason=ep_reason,
                )

        if not self._is_in_entry_window(now_et):
            return IronFlyEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="outside_entry_window (ET 10:30-11:30)",
            )

        ivr = env.ivr_by_symbol.get(symbol, 0.0)
        # 2026-04-25: 動的 IVR 閾値 (VIX に応じて緩和/厳格化・規律 feedback_no_fixed_params 遵守)
        from atlas_v3.bots.engines.dynamic_params import get_dynamic_ivr_threshold
        ivr_min_dynamic = get_dynamic_ivr_threshold(env.vix, self._cfg.ivr_min)
        if ivr <= ivr_min_dynamic:
            log.info(
                "[IronFlyEngine.should_enter] IVR=%.1f <= ivr_min_dynamic=%.1f (base=%.1f, VIX=%.2f): スキップ (symbol=%s)",
                ivr, ivr_min_dynamic, self._cfg.ivr_min, env.vix, symbol,
            )
            return IronFlyEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"IVR={ivr:.1f} <= ivr_min_dynamic={ivr_min_dynamic:.1f}",
            )

        if env.vix >= self._cfg.vix_max:
            log.info(
                "[IronFlyEngine.should_enter] VIX=%.2f >= vix_max=%.2f: スキップ (symbol=%s)",
                env.vix,
                self._cfg.vix_max,
                symbol,
            )
            return IronFlyEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"VIX={env.vix:.2f} >= vix_max={self._cfg.vix_max}",
            )

        if atm_strike <= 0.0:
            return IronFlyEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="atm_strike not provided",
            )

        if max_credit <= 0.0:
            return IronFlyEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="max_credit not provided",
            )

        legs = self._build_legs(
            atm_strike=atm_strike,
            wing_width_pts=self._cfg.wing_width_pts,
            quantity=self._cfg.quantity,
        )

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[IronFlyEngine.should_enter] エントリー OK: symbol=%s atm=%.2f "
            "IVR=%.1f VIX=%.2f wing=%.1fpt key=%s",
            symbol,
            atm_strike,
            ivr,
            env.vix,
            self._cfg.wing_width_pts,
            idem_key,
        )
        return IronFlyEntryDecision(
            should_enter=True,
            symbol=symbol,
            legs=legs,
            atm_strike=atm_strike,
            max_credit=max_credit,
            quantity=self._cfg.quantity,
            reason=(
                f"IVR={ivr:.1f}>{self._cfg.ivr_min}"
                f" / VIX={env.vix:.2f}<{self._cfg.vix_max}"
                f" / window=ET10:30-11:30"
            ),
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # place_order（TradeEngine.place_iron_fly wrapper 呼出）
    # ------------------------------------------------------------------

    def place_order(
        self,
        decision: IronFlyEntryDecision,
        paper_mode: bool = True,
        capital_usd: float = 0.0,
    ) -> str:
        """エントリー発注。TradeEngine.place_iron_fly() を呼び出す。

        Args:
            decision:    should_enter=True の IronFlyEntryDecision
            paper_mode:  True = paper 発注（PDT チェックスキップ）
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            order_id 文字列

        Raises:
            ValueError:      decision.should_enter=False の場合
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        if not decision.should_enter:
            raise ValueError(
                f"[IronFlyEngine.place_order] should_enter=False: {decision}"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        # 2026-04-25: PreTradeGate L2 whitelist は "US.XXX" 形式・L3 margin は capital_usd+est_margin 必須。
        sym = decision.symbol if decision.symbol.startswith("US.") else f"US.{decision.symbol}"
        est_margin = sum(abs(leg.quantity) for leg in decision.legs) * 100
        _gr = _gate(_Ctx(
            symbol=sym, qty=decision.quantity, side="SELL", is_long=False,
            est_margin=est_margin, capital_usd=capital_usd,
        ))
        if not _gr.allowed:
            raise ValueError(f"[IronFlyEngine.place_order] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        order_id = self._eng.place_iron_fly(
            symbol=decision.symbol,
            legs=decision.legs,
            quantity=decision.quantity,
            idempotency_key=decision.idempotency_key,
        )
        log.info(
            "[IronFlyEngine.place_order] submitted: symbol=%s order_id=%s",
            decision.symbol,
            order_id,
        )
        return order_id

    # ------------------------------------------------------------------
    # should_exit
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: IronFlyPosition,
        env: MarketEnvironment,
        now_et: datetime | None = None,
    ) -> IronFlyExitDecision:
        """エグジット判定。

        判定順（優先度高い順）:
        1. Kill Switch ARMED    → kill_switch 強制クローズ
        2. profit target 25%   → unrealized_pnl >= max_credit * 0.25
        3. stop loss 1.5x      → unrealized_pnl <= -(max_credit * 1.5)
        4. force close 15:40 ET → 時刻到達で強制クローズ

        Args:
            position: IronFlyPosition（max_credit 設定済みであること）
            env:      現在の MarketEnvironment
            now_et:   ET 時刻（None のとき現在時刻。テスト用に注入可能）

        Returns:
            IronFlyExitDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[IronFlyEngine.should_exit] Kill Switch ARMED: 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return IronFlyExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="kill_switch",
            )

        if position.max_credit <= 0:
            log.warning(
                "[IronFlyEngine.should_exit] max_credit=0: exit 判定不能 (symbol=%s)",
                position.symbol,
            )
            return IronFlyExitDecision(should_exit=False, reason="max_credit_not_set")

        # 動的 profit_target / stop_loss (規律 feedback_no_fixed_params 準拠・VIX 帯で調整)
        from atlas_v3.bots.engines.dynamic_params import (
            get_dynamic_profit_target, get_dynamic_stop_loss,
        )
        pt_dyn = get_dynamic_profit_target(env.vix, self._cfg.profit_target_pct)
        sl_dyn = get_dynamic_stop_loss(env.vix, self._cfg.stop_loss_credit_x)
        profit_threshold = position.max_credit * pt_dyn
        loss_threshold = -(position.max_credit * sl_dyn)

        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[IronFlyEngine.should_exit] 利確 25%%: pnl=%.2f >= target=%.2f (symbol=%s)",
                position.unrealized_pnl,
                profit_threshold,
                position.symbol,
            )
            return IronFlyExitDecision(
                should_exit=True,
                reason=f"profit_target_25pct: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[IronFlyEngine.should_exit] 損切り 1.5x: pnl=%.2f <= stop=%.2f (symbol=%s)",
                position.unrealized_pnl,
                loss_threshold,
                position.symbol,
            )
            return IronFlyExitDecision(
                should_exit=True,
                reason=f"stop_loss_1.5x: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        t = now_et or datetime.now(ET)
        force_close_time = t.replace(
            hour=self._cfg.force_close_hour_et,
            minute=self._cfg.force_close_minute_et,
            second=0,
            microsecond=0,
        )
        if t >= force_close_time:
            log.info(
                "[IronFlyEngine.should_exit] force close 15:40 ET: (symbol=%s)",
                position.symbol,
            )
            return IronFlyExitDecision(
                should_exit=True,
                reason="force_close_15:40_ET",
                exit_type="force_close",
            )

        return IronFlyExitDecision(should_exit=False, reason="holding", exit_type="none")
