"""atlas_v3/bots/engines/broken_wing_butterfly.py — Broken Wing Butterfly 戦術エンジン

設計概要:
    Broken Wing Butterfly (BWB) = 非対称 butterfly。
    標準 butterfly は上下均等 wing だが、BWB は片翼を意図的に短く設定することで
    片方向バイアスを持ちつつ net credit（プレミアム純受取）を確保する。

    コール側（上方バイアス版・デフォルト）構成:
        Leg[0] long call    strike = atm              (buy  1)
        Leg[1] short call × 2  strike = atm + short_wing  (sell 2)  ← short wing (-5pt)
        Leg[2] long call    strike = atm + long_wing   (buy  1)  ← long wing (-15pt)
        Leg[3] asymmetric offset: long call strike = atm - offset_pts (buy 1)
               ↑ broken = long_wing と short_wing が等距離でなく offset で非対称化

    net premium:
        credit = (short × 2) - (long × 1 lower) - (long × 1 upper)
        long_wing > short_wing で lower short を相殺しきれず asymmetric credit 残り

    IVR 条件: 50-80（中程度 IV 環境・spread 値が出やすいゾーン）
    entry window: 10:30-13:00 ET（モーニング ORB 収束後・ランチタイム前）
    exit:
        1. Kill Switch ARMED → 即時強制クローズ
        2. profit target 30% （受取クレジット比）
        3. max loss stop 50% （受取クレジット × 0.50 の損失到達）
        4. 15:45 ET force close

TradeEngine wrapper:
    self.eng.place_broken_wing_butterfly(legs, quantity) 呼び出し。
    Protocol + NoOpTradeEngine（テスト stub）を同梱。

禁則:
    - spy_bot.py / chronos_bot.py への import / 書換禁止
    - asyncio event loop 内からの直接呼び出し禁止（sync 専用）
    - IVR フィルタのハードコード禁止（config DTO 経由）
    - CC <= 20 規律
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    pass

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
_ENTRY_WINDOW_START: time = time(10, 30)
_ENTRY_WINDOW_END: time = time(13, 0)

#: 強制クローズ時刻（ET）
_FORCE_CLOSE_TIME: time = time(15, 45)

#: IVR フィルタ範囲（0-100 スケール）
_IVR_MIN_DEFAULT: float = 50.0
_IVR_MAX_DEFAULT: float = 80.0

#: 非対称 wing デフォルト（ポイント）
_SHORT_WING_PTS_DEFAULT: float = 5.0
_LONG_WING_PTS_DEFAULT: float = 15.0

#: 戦術識別子
BWB_TACTIC_NAME: str = "broken_wing_butterfly"


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class BrokenWingButterflyConfig:
    """Broken Wing Butterfly 戦術設定。

    Attributes:
        ivr_min:              エントリー最低 IVR（デフォルト 50）
        ivr_max:              エントリー最大 IVR（デフォルト 80）
        short_wing_pts:       ATM から short wing までの幅（-5pt デフォルト）
        long_wing_pts:        ATM から long wing までの幅（-15pt デフォルト）
        offset_pts:           asymmetric offset（long call 下方シフト幅・デフォルト 5pt）
        profit_target_pct:    利確目標（クレジット比 0.30 = 30%）
        max_loss_pct:         最大損失許容（クレジット比 0.50 = 50%）
        quantity:             デフォルト発注枚数
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        paper_mode:           True = paper 発注
    """
    ivr_min: float = _IVR_MIN_DEFAULT
    ivr_max: float = _IVR_MAX_DEFAULT
    short_wing_pts: float = _SHORT_WING_PTS_DEFAULT
    long_wing_pts: float = _LONG_WING_PTS_DEFAULT
    offset_pts: float = 5.0
    profit_target_pct: float = 0.30
    max_loss_pct: float = 0.50
    quantity: int = 1
    slippage_tolerance_bps: int = 10
    paper_mode: bool = True

    def __post_init__(self) -> None:
        """設定値バリデーション。

        Raises:
            ValueError: long_wing_pts <= short_wing_pts（非対称条件未成立）の場合
            ValueError: profit_target_pct / max_loss_pct が範囲外の場合
            ValueError: ivr_min >= ivr_max の場合
        """
        if self.long_wing_pts <= self.short_wing_pts:
            raise ValueError(
                f"long_wing_pts={self.long_wing_pts!r} は "
                f"short_wing_pts={self.short_wing_pts!r} より大きくなければなりません "
                "(non-symmetric broken wing 条件)。"
            )
        if not (0.0 < self.profit_target_pct < 1.0):
            raise ValueError(
                f"profit_target_pct={self.profit_target_pct!r} は (0.0, 1.0) 範囲外です。"
            )
        if not (0.0 < self.max_loss_pct < 1.0):
            raise ValueError(
                f"max_loss_pct={self.max_loss_pct!r} は (0.0, 1.0) 範囲外です。"
            )
        if self.ivr_min >= self.ivr_max:
            raise ValueError(
                f"ivr_min={self.ivr_min!r} >= ivr_max={self.ivr_max!r}"
            )


# ---------------------------------------------------------------------------
# Leg DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BWBLeg:
    """Broken Wing Butterfly の 1 leg。

    Attributes:
        label:       leg 識別子（"long_call_lower" 等）
        strike:      権利行使価格
        option_type: "call" | "put"
        side:        "buy" | "sell"
        quantity:    枚数
    """
    label: str
    strike: float
    option_type: Literal["call", "put"]
    side: Literal["buy", "sell"]
    quantity: int


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BWBEntryDecision:
    """Broken Wing Butterfly エントリー決定。

    legs 発注順序（仕様準拠）:
        [0] long call lower  — buy 1  / strike = atm
        [1] short call × 2  — sell 2 / strike = atm + short_wing_pts
        [2] long call upper  — buy 1  / strike = atm + long_wing_pts
        [3] asymmetric offset long — buy 1 / strike = atm - offset_pts
    """
    should_enter: bool
    symbol: str
    legs: tuple[BWBLeg, ...] = field(default_factory=tuple)
    atm_strike: float = 0.0
    net_credit: float = 0.0
    quantity: int = 1
    idempotency_key: str = ""
    reason: str = ""
    ivr: float = 0.0


@dataclass(frozen=True)
class BWBExitDecision:
    """Broken Wing Butterfly エグジット決定。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "profit_target", "max_loss_stop", "force_close", "kill_switch", "none"
    ] = "none"


# ---------------------------------------------------------------------------
# Position stub
# ---------------------------------------------------------------------------

@dataclass
class BWBPosition:
    """BWB 保有ポジション（Phase 2 で common_v3/position に差し替え）。

    Attributes:
        symbol:         対象銘柄
        quantity:       保有枚数
        net_credit:     エントリー時受取クレジット合計（正値）
        unrealized_pnl: 未実現損益（正値 = 利益・負値 = 損失）
        entry_time:     エントリー時刻（UTC）
        tactic_name:    戦術名（ログ用）
    """
    symbol: str
    quantity: int
    net_credit: float
    unrealized_pnl: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tactic_name: str = BWB_TACTIC_NAME


# ---------------------------------------------------------------------------
# TradeEngine Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class TradeEngineProtocol(Protocol):
    """BWB 4-leg 発注インターフェース。Phase 2 で futu SDK 実装に差し替える。"""

    def place_broken_wing_butterfly(
        self,
        symbol: str,
        legs: tuple[BWBLeg, ...],
        quantity: int,
        idempotency_key: str = "",
    ) -> str:
        """4 leg を順に発注し、注文 ID を返す。

        Args:
            symbol:          対象銘柄コード
            legs:            BWBLeg タプル（4 legs・順序保証）
            quantity:        発注枚数
            idempotency_key: 冪等性キー

        Returns:
            注文 ID 文字列
        """
        ...


class NoOpTradeEngine:
    """テスト用 TradeEngine stub（broker 接続なし）。"""

    def place_broken_wing_butterfly(
        self,
        symbol: str,
        legs: tuple[BWBLeg, ...],
        quantity: int,
        idempotency_key: str = "",
    ) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        order_id = f"DRY_BWB_{ts}"
        log.info(
            "[NoOpTradeEngine.place_bwb] DRY symbol=%s legs=%d qty=%d key=%s order_id=%s",
            symbol,
            len(legs),
            quantity,
            idempotency_key,
            order_id,
        )
        return order_id


# ---------------------------------------------------------------------------
# BrokenWingButterflyEngine — Type A: EnterExit
# ---------------------------------------------------------------------------

class BrokenWingButterflyEngine(TacticBase):
    """Broken Wing Butterfly 戦術エンジン（Type A: enter_exit）。

    非対称 wing（short_wing_pts < long_wing_pts）で片方向バイアスと
    premium 受取を両立する。IVR 50-80 の中程度 IV 環境向け。

    Args:
        trade_engine: TradeEngineProtocol 実装（None のとき NoOpTradeEngine を使用）
        config:       BrokenWingButterflyConfig（None のときデフォルト値）
        clock_fn:     テスト用時刻注入関数（省略時は datetime.now(_ET)）
    """

    def __init__(
        self,
        trade_engine: TradeEngineProtocol | None = None,
        config: BrokenWingButterflyConfig | None = None,
        clock_fn: "None | (() -> datetime)" = None,
    ) -> None:
        self._eng: TradeEngineProtocol = trade_engine or NoOpTradeEngine()
        self._cfg = config or BrokenWingButterflyConfig()
        self._clock_fn = clock_fn

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return BWB_TACTIC_NAME

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _now_et(self) -> datetime:
        """現在時刻（ET）を返す（テスト時は clock_fn で差し替え可）。"""
        if self._clock_fn is not None:
            return self._clock_fn()
        return datetime.now(_ET)

    def _in_entry_window(self, now_et: datetime | None = None) -> bool:
        """エントリー窓（10:30-13:00 ET）内かどうかを返す。"""
        t = (now_et or self._now_et()).time()
        return _ENTRY_WINDOW_START <= t < _ENTRY_WINDOW_END

    def _past_force_close(self, now_et: datetime | None = None) -> bool:
        """強制クローズ時刻（15:45 ET）を過ぎているかどうかを返す。"""
        t = (now_et or self._now_et()).time()
        return t >= _FORCE_CLOSE_TIME

    # ------------------------------------------------------------------
    # 4 leg 構築
    # ------------------------------------------------------------------

    def _build_legs(
        self,
        atm_strike: float,
        quantity: int,
    ) -> tuple[BWBLeg, ...]:
        """Broken Wing Butterfly 4 leg を仕様通りの順序で生成する。

        非対称 wing 構成（コール側）:
            [0] long call lower  — buy  1 / atm
            [1] short call × 2  — sell 2 / atm + short_wing_pts
            [2] long call upper  — buy  1 / atm + long_wing_pts
            [3] asymmetric offset — buy 1 / atm - offset_pts

        short_wing (5pt) < long_wing (15pt) → broken（非対称）。
        offset leg が premium を回収し net credit を確保する。

        Args:
            atm_strike: ATM の権利行使価格
            quantity:   枚数（各 leg の base unit・short call のみ ×2）

        Returns:
            長さ 4 の BWBLeg タプル
        """
        short_strike = atm_strike + self._cfg.short_wing_pts
        long_upper_strike = atm_strike + self._cfg.long_wing_pts
        offset_strike = atm_strike - self._cfg.offset_pts

        return (
            BWBLeg(
                label="long_call_lower",
                strike=atm_strike,
                option_type="call",
                side="buy",
                quantity=quantity,
            ),
            BWBLeg(
                label="short_call_body",
                strike=short_strike,
                option_type="call",
                side="sell",
                quantity=quantity * 2,
            ),
            BWBLeg(
                label="long_call_upper",
                strike=long_upper_strike,
                option_type="call",
                side="buy",
                quantity=quantity,
            ),
            BWBLeg(
                label="asymmetric_offset",
                strike=offset_strike,
                option_type="call",
                side="buy",
                quantity=quantity,
            ),
        )

    # ------------------------------------------------------------------
    # TacticBase ABC 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック順:
        1. env None ガード
        2. Kill Switch ARMED → False

        Returns:
            True  — 戦術発動可能
            False — 発動不可（理由は log に必ず出力）
        """
        if env is None:
            log.warning("[BWBEngine.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[BWBEngine.preflight] Kill Switch ARMED: %s を無効化",
                BWB_TACTIC_NAME,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol: str,
        atm_strike: float = 0.0,
        net_credit: float = 0.0,
        now_et: datetime | None = None,
    ) -> BWBEntryDecision:
        """エントリー判定。

        判定順:
        1. Kill Switch ARMED → should_enter=False
        2. entry_window 外（10:30-13:00 ET）→ should_enter=False
        3. IVR < ivr_min → should_enter=False
        4. IVR > ivr_max → should_enter=False（IV 高すぎ・butterfly 不利）
        5. atm_strike <= 0 → should_enter=False
        6. net_credit <= 0 → should_enter=False（credit 受取れない構成は不可）
        7. 4 leg 構築・idempotency_key 生成 → should_enter=True

        Args:
            env:        現在の MarketEnvironment
            symbol:     対象銘柄コード
            atm_strike: ATM の権利行使価格（0.0 の場合は entry 不可）
            net_credit: 見込みネットクレジット額（0.0 以下の場合は entry 不可）
            now_et:     ET 時刻（None のとき現在時刻。テスト用に注入可能）

        Returns:
            BWBEntryDecision
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[BWBEngine.should_enter] Kill Switch ARMED: エントリー判定スキップ (symbol=%s)",
                symbol,
            )
            return BWBEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="kill_switch_armed",
            )

        # 2. entry_window チェック
        if not self._in_entry_window(now_et):
            log.debug(
                "[BWBEngine.should_enter] entry window 外: window=%s-%s ET symbol=%s",
                _ENTRY_WINDOW_START,
                _ENTRY_WINDOW_END,
                symbol,
            )
            return BWBEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=(
                    f"outside_entry_window: window={_ENTRY_WINDOW_START}-{_ENTRY_WINDOW_END} ET"
                ),
            )

        # 3. IVR 下限フィルタ
        ivr = env.ivr_by_symbol.get(symbol, 0.0)
        if ivr < self._cfg.ivr_min:
            log.info(
                "[BWBEngine.should_enter] IVR=%.1f < ivr_min=%.1f: スキップ (symbol=%s)",
                ivr,
                self._cfg.ivr_min,
                symbol,
            )
            return BWBEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"IVR={ivr:.1f} < ivr_min={self._cfg.ivr_min:.1f}",
                ivr=ivr,
            )

        # 4. IVR 上限フィルタ（高 IV 過ぎると butterfly spread コスト高）
        if ivr > self._cfg.ivr_max:
            log.info(
                "[BWBEngine.should_enter] IVR=%.1f > ivr_max=%.1f: スキップ (symbol=%s)",
                ivr,
                self._cfg.ivr_max,
                symbol,
            )
            return BWBEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"IVR={ivr:.1f} > ivr_max={self._cfg.ivr_max:.1f}",
                ivr=ivr,
            )

        # 5. atm_strike ガード
        if atm_strike <= 0.0:
            return BWBEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="atm_strike not provided",
                ivr=ivr,
            )

        # 6. net_credit ガード
        if net_credit <= 0.0:
            return BWBEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="net_credit not provided or zero",
                ivr=ivr,
            )

        # 7. 4 leg 構築
        legs = self._build_legs(atm_strike=atm_strike, quantity=self._cfg.quantity)

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[BWBEngine.should_enter] エントリー OK: symbol=%s atm=%.2f "
            "IVR=%.1f short_wing=%.1fpt long_wing=%.1fpt key=%s",
            symbol,
            atm_strike,
            ivr,
            self._cfg.short_wing_pts,
            self._cfg.long_wing_pts,
            idem_key,
        )
        return BWBEntryDecision(
            should_enter=True,
            symbol=symbol,
            legs=legs,
            atm_strike=atm_strike,
            net_credit=net_credit,
            quantity=self._cfg.quantity,
            idempotency_key=idem_key,
            reason=(
                f"IVR={ivr:.1f} in [{self._cfg.ivr_min:.1f},{self._cfg.ivr_max:.1f}]"
                f" / short_wing={self._cfg.short_wing_pts}pt"
                f" / long_wing={self._cfg.long_wing_pts}pt"
            ),
            ivr=ivr,
        )

    # ------------------------------------------------------------------
    # place_order
    # ------------------------------------------------------------------

    def place_order(
        self,
        decision: BWBEntryDecision,
        capital_usd: float = 0.0,
    ) -> str:
        """エントリー発注。TradeEngine.place_broken_wing_butterfly() を呼び出す。

        Args:
            decision:    should_enter=True の BWBEntryDecision
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            order_id 文字列

        Raises:
            ValueError:      decision.should_enter=False の場合
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        if not decision.should_enter:
            raise ValueError(
                f"[BWBEngine.place_order] should_enter=False: {decision}"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        # 2026-04-25: PreTradeGate L2 whitelist は "US.XXX" 形式・L3 margin は capital_usd+est_margin 必須。
        # est_margin = 全 leg 数量合計 × 100 (1 contract = 100 株 proxy)。本来は moomoo provider から実 margin 取得。
        sym = decision.symbol if decision.symbol.startswith("US.") else f"US.{decision.symbol}"
        est_margin = sum(abs(leg.quantity) for leg in decision.legs) * 100
        _gr = _gate(_Ctx(
            symbol=sym, qty=decision.quantity, side="SELL", is_long=False,
            est_margin=est_margin, capital_usd=capital_usd,
        ))
        if not _gr.allowed:
            raise ValueError(f"[BWBEngine.place_order] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=self._cfg.paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        order_id = self._eng.place_broken_wing_butterfly(
            symbol=decision.symbol,
            legs=decision.legs,
            quantity=decision.quantity,
            idempotency_key=decision.idempotency_key,
        )
        log.info(
            "[BWBEngine.place_order] submitted: symbol=%s order_id=%s",
            decision.symbol,
            order_id,
        )
        return order_id

    # ------------------------------------------------------------------
    # should_exit
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: BWBPosition,
        env: MarketEnvironment,
        now_et: datetime | None = None,
    ) -> BWBExitDecision:
        """エグジット判定。

        判定順（優先度高い順）:
        1. Kill Switch ARMED    → kill_switch 強制クローズ
        2. profit target 30%   → unrealized_pnl >= net_credit * 0.30
        3. max loss stop 50%   → unrealized_pnl <= -(net_credit * 0.50)
        4. force close 15:45 ET

        Args:
            position: BWBPosition（net_credit 設定済みであること）
            env:      現在の MarketEnvironment
            now_et:   ET 時刻（None のとき現在時刻。テスト用に注入可能）

        Returns:
            BWBExitDecision
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[BWBEngine.should_exit] Kill Switch ARMED: 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return BWBExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="kill_switch",
            )

        # net_credit 未設定ガード
        if position.net_credit <= 0:
            log.warning(
                "[BWBEngine.should_exit] net_credit=%.4f: exit 判定不能 (symbol=%s)",
                position.net_credit,
                position.symbol,
            )
            return BWBExitDecision(
                should_exit=False,
                reason="net_credit_not_set",
                exit_type="none",
            )

        profit_threshold = position.net_credit * self._cfg.profit_target_pct
        loss_threshold = -(position.net_credit * self._cfg.max_loss_pct)

        # 2. 利確 30%
        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[BWBEngine.should_exit] 利確 30%%: pnl=%.4f >= target=%.4f (symbol=%s)",
                position.unrealized_pnl,
                profit_threshold,
                position.symbol,
            )
            return BWBExitDecision(
                should_exit=True,
                reason=(
                    f"profit_target_30pct: pnl={position.unrealized_pnl:.4f} "
                    f">= {profit_threshold:.4f}"
                ),
                exit_type="profit_target",
            )

        # 3. 最大損失 50% stop
        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[BWBEngine.should_exit] max loss 50%%: pnl=%.4f <= stop=%.4f (symbol=%s)",
                position.unrealized_pnl,
                loss_threshold,
                position.symbol,
            )
            return BWBExitDecision(
                should_exit=True,
                reason=(
                    f"max_loss_stop_50pct: pnl={position.unrealized_pnl:.4f} "
                    f"<= {loss_threshold:.4f}"
                ),
                exit_type="max_loss_stop",
            )

        # 4. force close 15:45 ET
        if self._past_force_close(now_et):
            log.info(
                "[BWBEngine.should_exit] force close %s ET: (symbol=%s)",
                _FORCE_CLOSE_TIME,
                position.symbol,
            )
            return BWBExitDecision(
                should_exit=True,
                reason=f"force_close_{_FORCE_CLOSE_TIME}_ET",
                exit_type="force_close",
            )

        return BWBExitDecision(
            should_exit=False,
            reason="holding",
            exit_type="none",
        )
