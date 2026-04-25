"""atlas_v3/bots/engines/ratio_spread.py — Call Ratio Spread 戦術エンジン（Type A: EnterExit）

設計概要:
    Call Ratio Spread = long 1 ATM call + short 2 OTM call (1:2 ratio)
    → クレジット収集（net premium 受取）しつつ limited upside exposure を許容する。
    short call 2 枚で premium を最大化、long call 1 枚でダウンサイドを定義。

エントリー条件:
    - IVR 40-70（中程度の IV 環境・premium 売りが有利だが急変動リスクが低い）
    - VIX 15-25（中ボラ前提）
    - entry_window: 10:00-12:00 ET（オープニングノイズ収束後・流動性十分）

leg 構成（発注順序）:
    [0] long  1 ATM call  (buy  / strike = atm_strike)
    [1] short 1 OTM call  (sell / strike = atm_strike + otm_offset_pts)  — qty=2 の 1 枚目
    [2] short 1 OTM call  (sell / strike = atm_strike + otm_offset_pts)  — qty=2 の 2 枚目

    ※ TradeEngine の place_ratio_spread は 3 leg リストを受け取り順次発注する。
    ※ OTM short を 2 件に分割発注するのは個別 rollback 粒度確保のため。

エグジット条件（優先順位順）:
    1. Kill Switch ARMED       → 即時強制クローズ
    2. profit target 40%       → net_credit の 40% 利確
    3. stop loss 1.5x credit   → net_credit の 1.5 倍損失で撤退
    4. 15:40 ET force close    → 残存リスク除去

TradeEngine wrapper:
    self.eng.place_ratio_spread(symbol, legs, quantity, idempotency_key) を呼び出す。
    TradeEngineProtocol で型を定義し、テスト用 NoOpTradeEngine を同梱する。

rollback:
    short 2 枚目の発注失敗時は 1 枚目を cancel して long も cancel する。
    rollback 結果は RatioSpreadEntryDecision.rollback_triggered に記録される。

禁則:
    - spy_bot.py / chronos_bot.py への import / 書換禁止
    - asyncio event loop 内からの直接呼び出し禁止（sync 専用）
    - IVR / VIX フィルタのハードコード禁止（config DTO 経由）
    - CC <= 20 規律
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Callable, Literal, Optional
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    pass

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

_ET = ZoneInfo("America/New_York")

#: エントリー可能ウィンドウ（ET）
_ENTRY_WINDOW_START: time = time(10, 0)
_ENTRY_WINDOW_END: time = time(12, 0)

#: 強制クローズ時刻（ET）
_FORCE_CLOSE_TIME: time = time(15, 40)

#: IVR 有効範囲
_IVR_SCALE_MIN: float = 0.0
_IVR_SCALE_MAX: float = 100.0

#: ratio 構成識別子
LegLabel = Literal["long_atm_call", "short_otm_call_1", "short_otm_call_2"]

#: 戦術識別子プレフィックス
RATIO_SPREAD_PREFIX: str = "ratio_spread"


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class RatioSpreadConfig:
    """Call Ratio Spread エンジン設定。

    Attributes:
        ivr_min:              エントリー最低 IVR（デフォルト 40）
        ivr_max:              エントリー最高 IVR（デフォルト 70）
        vix_min:              エントリー最低 VIX（デフォルト 15）
        vix_max:              エントリー最高 VIX（デフォルト 25）
        otm_offset_pts:       ATM から OTM strike までの幅（ポイント）
        profit_target_pct:    利確目標（net_credit 比 0.40 = 40%）
        stop_loss_credit_x:   損切り倍率（net_credit × 1.5）
        quantity:             long ATM call の枚数（short は 2x）
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        paper_mode:           True = paper 発注（broker 送信スキップ）
        force_close_hour_et:  強制クローズ時（ET・24h）
        force_close_minute_et: 強制クローズ分（ET）
    """
    ivr_min: float = 40.0
    ivr_max: float = 70.0
    vix_min: float = 15.0
    vix_max: float = 25.0
    otm_offset_pts: float = 5.0
    profit_target_pct: float = 0.40
    stop_loss_credit_x: float = 1.5
    quantity: int = 1
    slippage_tolerance_bps: int = 10
    paper_mode: bool = True
    force_close_hour_et: int = 15
    force_close_minute_et: int = 40
    earnings_proximity_days: Optional[int] = 5

    def __post_init__(self) -> None:
        """設定値バリデーション。

        Raises:
            ValueError: ivr_min >= ivr_max の場合
            ValueError: vix_min >= vix_max の場合
            ValueError: otm_offset_pts <= 0 の場合
            ValueError: profit_target_pct が (0.0, 1.0) 範囲外の場合
            ValueError: quantity <= 0 の場合
        """
        if self.ivr_min >= self.ivr_max:
            raise ValueError(
                f"ivr_min={self.ivr_min!r} >= ivr_max={self.ivr_max!r}"
            )
        if self.vix_min >= self.vix_max:
            raise ValueError(
                f"vix_min={self.vix_min!r} >= vix_max={self.vix_max!r}"
            )
        if self.otm_offset_pts <= 0:
            raise ValueError(
                f"otm_offset_pts={self.otm_offset_pts!r} は正の値でなければなりません。"
            )
        if not (0.0 < self.profit_target_pct < 1.0):
            raise ValueError(
                f"profit_target_pct={self.profit_target_pct!r} は (0.0, 1.0) 範囲外です。"
            )
        if self.quantity <= 0:
            raise ValueError(
                f"quantity={self.quantity!r} は正の整数でなければなりません。"
            )


# ---------------------------------------------------------------------------
# Leg DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RatioSpreadLeg:
    """Call Ratio Spread の 1 leg。

    Attributes:
        label:    leg 識別子（long_atm_call / short_otm_call_1 / short_otm_call_2）
        side:     "buy" | "sell"
        strike:   ストライク価格
        quantity: 発注枚数
    """
    label: LegLabel
    side: Literal["buy", "sell"]
    strike: float
    quantity: int = 1


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RatioSpreadEntryDecision:
    """Call Ratio Spread エントリー決定 DTO。

    Attributes:
        should_enter:      True = エントリー実行
        symbol:            対象銘柄
        legs:              3 legs（long_atm / short_otm_1 / short_otm_2）
        atm_strike:        ATM ストライク
        net_credit:        net 受取クレジット（long コスト差引後）
        quantity:          long 枚数（short は 2×quantity）
        idempotency_key:   二重発注防止キー
        reason:            エントリー判定理由
        rollback_triggered: short 発注失敗時に rollback が発動したか
    """
    should_enter: bool
    symbol: str
    legs: tuple[RatioSpreadLeg, ...] = field(default_factory=tuple)
    atm_strike: float = 0.0
    net_credit: float = 0.0
    quantity: int = 1
    idempotency_key: str = ""
    reason: str = ""
    rollback_triggered: bool = False


@dataclass(frozen=True)
class RatioSpreadExitDecision:
    """Call Ratio Spread エグジット決定 DTO。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "profit_target", "stop_loss", "force_close", "kill_switch", "none"
    ] = "none"


# ---------------------------------------------------------------------------
# ポジション stub（Phase 2 で common_v3/position に差し替え）
# ---------------------------------------------------------------------------

@dataclass
class RatioSpreadPosition:
    """Call Ratio Spread 保有ポジション。

    Attributes:
        symbol:         対象銘柄
        quantity:       long 枚数（short は 2×quantity）
        atm_strike:     エントリー時 ATM ストライク
        net_credit:     受取 net クレジット（long コスト差引後）
        unrealized_pnl: 未実現損益（正値 = 利益）
        entry_time:     エントリー時刻（UTC）
        tactic_name:    戦術名
    """
    symbol: str
    quantity: int
    atm_strike: float
    net_credit: float
    unrealized_pnl: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tactic_name: str = RATIO_SPREAD_PREFIX


# ---------------------------------------------------------------------------
# TradeEngine Protocol
# ---------------------------------------------------------------------------

class TradeEngineProtocol:
    """TradeEngine.place_ratio_spread() wrapper の呼出インターフェース（Protocol）。

    Phase 2 で futu SDK 経由の具体実装に差し替える。
    テスト用は NoOpTradeEngine を使用する。
    """

    def place_ratio_spread(
        self,
        symbol: str,
        legs: tuple[RatioSpreadLeg, ...],
        quantity: int,
        idempotency_key: str = "",
    ) -> str:
        """3 leg を順に発注し、注文 ID を返す。

        Args:
            symbol:           対象銘柄コード
            legs:             RatioSpreadLeg タプル（long_atm / short_otm_1 / short_otm_2）
            quantity:         long 枚数（short は各 1 枚・計 2 枚）
            idempotency_key:  冪等性キー（重複発注防止）

        Returns:
            注文 ID 文字列（broker 依存・テスト時は "DRY_ORDER_<ts>"）
        """
        raise NotImplementedError  # pragma: no cover

    def cancel_order(self, order_id: str) -> bool:
        """発注済み注文をキャンセルする。

        Args:
            order_id: cancel 対象の注文 ID

        Returns:
            True = cancel 成功 / False = cancel 失敗
        """
        raise NotImplementedError  # pragma: no cover


class NoOpTradeEngine(TradeEngineProtocol):
    """テスト用 TradeEngine stub（broker 接続なし）。

    place_ratio_spread は発注をシミュレートして "DRY_ORDER_<ts>" を返す。
    cancel_order は常に True を返す。
    """

    def __init__(self, *, fail_on_short_leg: int | None = None) -> None:
        """Args:
            fail_on_short_leg: None=全成功 / 1=short_otm_call_1 で失敗 / 2=short_otm_call_2 で失敗
        """
        self._fail_on_short_leg = fail_on_short_leg
        self._placed_orders: list[str] = []

    def place_ratio_spread(
        self,
        symbol: str,
        legs: tuple[RatioSpreadLeg, ...],
        quantity: int,
        idempotency_key: str = "",
    ) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        order_id = f"DRY_ORDER_{ts}"
        self._placed_orders.append(order_id)
        log.info(
            "[NoOpTradeEngine.place_ratio_spread] DRY symbol=%s legs=%d qty=%d key=%s order_id=%s",
            symbol,
            len(legs),
            quantity,
            idempotency_key,
            order_id,
        )
        return order_id

    def place_single_leg(
        self,
        symbol: str,
        leg: RatioSpreadLeg,
        idempotency_key: str = "",
    ) -> str:
        """1 leg を個別発注する（rollback 粒度確保用）。

        Args:
            symbol:          対象銘柄コード
            leg:             発注 leg
            idempotency_key: 冪等性キー

        Returns:
            注文 ID 文字列

        Raises:
            RuntimeError: fail_on_short_leg に一致する short leg の発注時
        """
        if (
            self._fail_on_short_leg == 1
            and leg.label == "short_otm_call_1"
        ):
            raise RuntimeError(
                f"[NoOpTradeEngine] SIMULATED FAILURE: {leg.label}"
            )
        if (
            self._fail_on_short_leg == 2
            and leg.label == "short_otm_call_2"
        ):
            raise RuntimeError(
                f"[NoOpTradeEngine] SIMULATED FAILURE: {leg.label}"
            )
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        order_id = f"DRY_SINGLE_{leg.label}_{ts}"
        self._placed_orders.append(order_id)
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self._placed_orders:
            self._placed_orders.remove(order_id)
        return True


# ---------------------------------------------------------------------------
# RatioSpreadEngine — Type A: EnterExit
# ---------------------------------------------------------------------------

class RatioSpreadEngine(TacticBase):
    """Call Ratio Spread 戦術エンジン（Type A: enter_exit）。

    long 1 ATM call + short 2 OTM call の 1:2 比率構成。
    IVR 40-70 / VIX 15-25 の環境フィルタ通過時のみ
    entry_window（ET 10:00-12:00）でエントリーを試みる。

    short 2 枚目発注失敗時は rollback（long + short 1 枚目をキャンセル）を実行する。

    エグジットは kill_switch > profit_target > stop_loss > force_close の順で判定。

    Args:
        trade_engine: TradeEngineProtocol 実装（None のとき NoOpTradeEngine を使用）
        config:       RatioSpreadConfig（None のときデフォルト値）
        clock_fn:     テスト用時刻注入関数（省略時は datetime.now(_ET)）
    """

    def __init__(
        self,
        trade_engine: TradeEngineProtocol | None = None,
        config: RatioSpreadConfig | None = None,
        clock_fn: "None | (() -> datetime)" = None,
        earnings_date_fn: "Optional[Callable[[str], Optional[object]]]" = None,
    ) -> None:
        self._eng = trade_engine or NoOpTradeEngine()
        self._cfg = config or RatioSpreadConfig()
        self._clock_fn = clock_fn
        self._earnings_date_fn = earnings_date_fn  # 決算日取得 DI（テスト用）

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return RATIO_SPREAD_PREFIX

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _now_et(self) -> datetime:
        """現在時刻（ET）を返す（テスト時は clock_fn で差し替え可）。"""
        if self._clock_fn is not None:
            return self._clock_fn()
        return datetime.now(_ET)

    def _in_entry_window(self) -> bool:
        """エントリー窓（10:00-12:00 ET）内かどうかを返す。"""
        t = self._now_et().time()
        return _ENTRY_WINDOW_START <= t < _ENTRY_WINDOW_END

    def _past_force_close(self) -> bool:
        """強制クローズ時刻（15:40 ET）を過ぎているかどうかを返す。"""
        t = self._now_et().time()
        return t >= _FORCE_CLOSE_TIME

    # ------------------------------------------------------------------
    # TacticBase ABC 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック順:
        1. env None ガード
        2. Kill Switch ARMED → False
        3. VIX < vix_min または VIX >= vix_max → False

        Returns:
            True  — 戦術発動可能
            False — 発動不可（理由は log に必ず出力）
        """
        if env is None:
            log.warning("[RatioSpreadEngine.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[RatioSpreadEngine.preflight] Kill Switch ARMED: ratio_spread を無効化"
            )
            return False

        if not (self._cfg.vix_min <= env.vix < self._cfg.vix_max):
            log.info(
                "[RatioSpreadEngine.preflight] VIX=%.2f out of range [%.1f, %.1f): スキップ",
                env.vix,
                self._cfg.vix_min,
                self._cfg.vix_max,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # leg 構築
    # ------------------------------------------------------------------

    def _build_legs(
        self,
        atm_strike: float,
    ) -> tuple[RatioSpreadLeg, ...]:
        """1:2 ratio の 3 leg を構築して返す。

        発注順序:
            [0] long_atm_call  — buy  / strike = atm_strike          / qty = quantity
            [1] short_otm_call_1 — sell / strike = atm + otm_offset  / qty = quantity
            [2] short_otm_call_2 — sell / strike = atm + otm_offset  / qty = quantity

        Args:
            atm_strike: ATM ストライク価格

        Returns:
            長さ 3 の RatioSpreadLeg タプル
        """
        otm_strike = atm_strike + self._cfg.otm_offset_pts
        return (
            RatioSpreadLeg(
                label="long_atm_call",
                side="buy",
                strike=atm_strike,
                quantity=self._cfg.quantity,
            ),
            RatioSpreadLeg(
                label="short_otm_call_1",
                side="sell",
                strike=otm_strike,
                quantity=self._cfg.quantity,
            ),
            RatioSpreadLeg(
                label="short_otm_call_2",
                side="sell",
                strike=otm_strike,
                quantity=self._cfg.quantity,
            ),
        )

    # ------------------------------------------------------------------
    # should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol: str,
        atm_strike: float = 0.0,
        net_credit: float = 0.0,
    ) -> RatioSpreadEntryDecision:
        """エントリー判定。

        判定順:
        1. Kill Switch ARMED → should_enter=False
        2. entry window 外 → should_enter=False
        3. IVR 範囲外（< ivr_min または > ivr_max）→ should_enter=False
        4. VIX 範囲外 → should_enter=False（preflight 後の二重確認）
        5. atm_strike <= 0 → should_enter=False
        6. net_credit <= 0 → should_enter=False
        7. 全条件通過 → 3 leg 構築・idempotency_key 生成・should_enter=True

        Args:
            env:        現在の MarketEnvironment
            symbol:     対象銘柄コード
            atm_strike: ATM ストライク価格
            net_credit: net 受取クレジット額（long コスト差引後・正値）

        Returns:
            RatioSpreadEntryDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[RatioSpreadEngine.should_enter] Kill Switch ARMED: スキップ (symbol=%s)",
                symbol,
            )
            return RatioSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="kill_switch_armed",
            )

        # 決算近接チェック（earnings_proximity_days が設定されている場合のみ）
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
                    "[RatioSpreadEngine.should_enter] 決算近接ブロック: %s",
                    ep_reason,
                )
                return RatioSpreadEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    reason=ep_reason,
                )

        if not self._in_entry_window():
            log.debug(
                "[RatioSpreadEngine.should_enter] entry window 外: %s ET (symbol=%s)",
                self._now_et().strftime("%H:%M"),
                symbol,
            )
            return RatioSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=(
                    f"entry_window_closed: "
                    f"now={self._now_et().strftime('%H:%M')} ET, "
                    f"window={_ENTRY_WINDOW_START}-{_ENTRY_WINDOW_END}"
                ),
            )

        ivr = env.ivr_by_symbol.get(symbol, 0.0)
        # 動的 IVR 閾値 (規律 feedback_no_fixed_params 準拠)
        from atlas_v3.bots.engines.dynamic_params import get_dynamic_ivr_threshold
        ivr_min_dyn = get_dynamic_ivr_threshold(env.vix, self._cfg.ivr_min)
        if not (ivr_min_dyn <= ivr <= self._cfg.ivr_max):
            log.info(
                "[RatioSpreadEngine.should_enter] IVR=%.1f out of range "
                "[%.1f(dyn,base=%.1f,VIX=%.2f), %.1f]: スキップ (symbol=%s)",
                ivr, ivr_min_dyn, self._cfg.ivr_min, env.vix,
                self._cfg.ivr_max, symbol,
            )
            return RatioSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=(
                    f"IVR={ivr:.1f} not in "
                    f"[{ivr_min_dyn:.1f}(dyn), {self._cfg.ivr_max:.1f}]"
                ),
            )

        if not (self._cfg.vix_min <= env.vix < self._cfg.vix_max):
            log.info(
                "[RatioSpreadEngine.should_enter] VIX=%.2f out of range "
                "[%.1f, %.1f): スキップ (symbol=%s)",
                env.vix,
                self._cfg.vix_min,
                self._cfg.vix_max,
                symbol,
            )
            return RatioSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=(
                    f"VIX={env.vix:.2f} not in "
                    f"[{self._cfg.vix_min:.1f}, {self._cfg.vix_max:.1f})"
                ),
            )

        if atm_strike <= 0.0:
            return RatioSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="atm_strike not provided",
            )

        if net_credit <= 0.0:
            return RatioSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="net_credit not provided",
            )

        legs = self._build_legs(atm_strike)

        now_utc = datetime.now(timezone.utc)
        bucket_min = (now_utc.minute // 5) * 5
        trigger_time = now_utc.replace(minute=bucket_min, second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[RatioSpreadEngine.should_enter] エントリー OK: symbol=%s atm=%.2f "
            "IVR=%.1f VIX=%.2f net_credit=%.4f key=%s",
            symbol,
            atm_strike,
            ivr,
            env.vix,
            net_credit,
            idem_key,
        )
        return RatioSpreadEntryDecision(
            should_enter=True,
            symbol=symbol,
            legs=legs,
            atm_strike=atm_strike,
            net_credit=net_credit,
            quantity=self._cfg.quantity,
            idempotency_key=idem_key,
            reason=(
                f"IVR={ivr:.1f} in [{self._cfg.ivr_min:.1f},{self._cfg.ivr_max:.1f}] / "
                f"VIX={env.vix:.2f} in [{self._cfg.vix_min:.1f},{self._cfg.vix_max:.1f}) / "
                f"window=ET10:00-12:00"
            ),
        )

    # ------------------------------------------------------------------
    # place_order（3 leg 発注 + short 失敗時 rollback）
    # ------------------------------------------------------------------

    def place_order(
        self,
        decision: RatioSpreadEntryDecision,
        capital_usd: float = 0.0,
    ) -> tuple[str, RatioSpreadEntryDecision]:
        """エントリー発注。TradeEngine.place_single_leg() を leg 順に呼び出す。

        short_otm_call_2 発注失敗時:
            1. short_otm_call_1 の cancel を試みる
            2. long_atm_call の cancel を試みる
            3. rollback_triggered=True の RatioSpreadEntryDecision を返す

        Args:
            decision:    should_enter=True の RatioSpreadEntryDecision
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            (order_id, decision) タプル。
            rollback 時は order_id="" / decision.rollback_triggered=True。

        Raises:
            ValueError:      decision.should_enter=False の場合
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        if not decision.should_enter:
            raise ValueError(
                f"[RatioSpreadEngine.place_order] should_enter=False: {decision}"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        # 2026-04-25: PreTradeGate L2 whitelist は "US.XXX" 形式。decision.symbol が
        # 短縮形の場合は prefix を補う。L3 margin は capital_usd + est_margin 必須。
        sym = decision.symbol if decision.symbol.startswith("US.") else f"US.{decision.symbol}"
        # ratio spread の est_margin = 全 leg の数量合計 (proxy)。capital_usd は引数 capital_usd。
        est_margin = sum(abs(leg.quantity) for leg in decision.legs) * 100  # 1 contract = 100 株
        _gr = _gate(_Ctx(
            symbol=sym, qty=decision.quantity, side="SELL", is_long=False,
            est_margin=est_margin, capital_usd=capital_usd,
        ))
        if not _gr.allowed:
            raise ValueError(f"[RatioSpreadEngine.place_order] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=self._cfg.paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        placed: list[tuple[LegLabel, str]] = []

        try:
            for leg in decision.legs:
                leg_key = f"{decision.idempotency_key}_{leg.label}"
                order_id = self._eng.place_single_leg(
                    symbol=decision.symbol,
                    leg=leg,
                    idempotency_key=leg_key,
                )
                placed.append((leg.label, order_id))
                log.info(
                    "[RatioSpreadEngine.place_order] leg placed: "
                    "symbol=%s leg=%s order_id=%s",
                    decision.symbol,
                    leg.label,
                    order_id,
                )
        except RuntimeError as exc:
            log.error(
                "[RatioSpreadEngine.place_order] short leg 発注失敗・rollback 開始: "
                "symbol=%s error=%s",
                decision.symbol,
                exc,
            )
            self._rollback(decision.symbol, placed)
            rolled = RatioSpreadEntryDecision(
                should_enter=False,
                symbol=decision.symbol,
                legs=decision.legs,
                atm_strike=decision.atm_strike,
                net_credit=decision.net_credit,
                quantity=decision.quantity,
                idempotency_key=decision.idempotency_key,
                reason=f"rollback: {exc}",
                rollback_triggered=True,
            )
            return "", rolled

        final_order_id = placed[-1][1] if placed else ""
        log.info(
            "[RatioSpreadEngine.place_order] 全 leg 発注完了: symbol=%s legs=%d",
            decision.symbol,
            len(placed),
        )
        return final_order_id, decision

    def _rollback(
        self,
        symbol: str,
        placed: list[tuple[LegLabel, str]],
    ) -> None:
        """発注済み leg を逆順でキャンセルする。

        Args:
            symbol: 対象銘柄コード
            placed: [(leg_label, order_id), ...] の発注済みリスト
        """
        for leg_label, order_id in reversed(placed):
            ok = self._eng.cancel_order(order_id)
            log.warning(
                "[RatioSpreadEngine._rollback] cancel: symbol=%s leg=%s "
                "order_id=%s ok=%s",
                symbol,
                leg_label,
                order_id,
                ok,
            )

    # ------------------------------------------------------------------
    # should_exit
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: RatioSpreadPosition,
        env: MarketEnvironment,
    ) -> RatioSpreadExitDecision:
        """エグジット判定。

        判定順（優先度高い順）:
        1. Kill Switch ARMED → kill_switch 強制クローズ
        2. profit target 40% → unrealized_pnl >= net_credit * 0.40
        3. stop loss 1.5x   → unrealized_pnl <= -(net_credit * 1.5)
        4. 15:40 ET force close

        Args:
            position: RatioSpreadPosition（net_credit 設定済みであること）
            env:      現在の MarketEnvironment

        Returns:
            RatioSpreadExitDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[RatioSpreadEngine.should_exit] Kill Switch ARMED: "
                "強制クローズ (symbol=%s)",
                position.symbol,
            )
            return RatioSpreadExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="kill_switch",
            )

        if position.net_credit <= 0:
            log.warning(
                "[RatioSpreadEngine.should_exit] net_credit=0: "
                "exit 判定不能 (symbol=%s)",
                position.symbol,
            )
            return RatioSpreadExitDecision(
                should_exit=False,
                reason="net_credit_not_set",
            )

        profit_threshold = position.net_credit * self._cfg.profit_target_pct
        loss_threshold = -(position.net_credit * self._cfg.stop_loss_credit_x)

        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[RatioSpreadEngine.should_exit] 利確 40%%: "
                "pnl=%.4f >= target=%.4f (symbol=%s)",
                position.unrealized_pnl,
                profit_threshold,
                position.symbol,
            )
            return RatioSpreadExitDecision(
                should_exit=True,
                reason=(
                    f"profit_target_40pct: "
                    f"pnl={position.unrealized_pnl:.4f} >= {profit_threshold:.4f}"
                ),
                exit_type="profit_target",
            )

        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[RatioSpreadEngine.should_exit] 損切り 1.5x: "
                "pnl=%.4f <= stop=%.4f (symbol=%s)",
                position.unrealized_pnl,
                loss_threshold,
                position.symbol,
            )
            return RatioSpreadExitDecision(
                should_exit=True,
                reason=(
                    f"stop_loss_1.5x: "
                    f"pnl={position.unrealized_pnl:.4f} <= {loss_threshold:.4f}"
                ),
                exit_type="stop_loss",
            )

        if self._past_force_close():
            log.info(
                "[RatioSpreadEngine.should_exit] force close 15:40 ET (symbol=%s)",
                position.symbol,
            )
            return RatioSpreadExitDecision(
                should_exit=True,
                reason="force_close_15:40_ET",
                exit_type="force_close",
            )

        return RatioSpreadExitDecision(
            should_exit=False,
            reason="holding",
            exit_type="none",
        )
