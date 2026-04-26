"""atlas_v3/bots/engines/pmcc.py — Poor Man's Covered Call (PMCC) 戦術エンジン

設計概要:
  Deep ITM long call を株式代替として保有しつつ、weekly OTM short call で
  プレミアムを回収するレバレッジ効率の高い covered-call 変形戦術。

  - Long  leg: deep ITM call (60-90 DTE・delta 0.75-0.90) — 株式代替
  - Short leg: OTM call    (weekly・delta 0.25-0.35) — premium 回収

エントリー条件:
  - 上方向バイアス必須 (env.bias == "bull")
  - IVR 中程度 OK (デフォルト 20-80 — weekly premium が取れる水準)
  - エントリーウィンドウ: 毎週月曜 or 火曜 10:00-13:00 ET (weekly roll 想定)

エグジット / ロール優先順:
  1. Kill Switch ARMED → force_close (両 leg 即時クローズ)
  2. long call DTE <= 60 DTE → long_call_roll (深い月足に乗り換え)
  3. short call DTE <= 1 (expiry 当日翌日) → weekly_short_roll
  4. short call unrealized_pnl >= entry_premium × 0.50 → profit_exit_short (50% profit)
  5. 含み損が long call net_debit × 2.0 超過 → stop_loss

発注: 2 leg 発注 (long_call + short_call) — 異なる expiry・1 枚ずつ
      AtlasEngine が OrderRequest を leg ごとに dispatch する。

TacticBase 継承 + EnterExitTactic Protocol 実装。
spy_bot.py / chronos_bot.py / common/* は無変更。
CC 規律: 各メソッド CC <= 20
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

#: Long leg delta 範囲（deep ITM call・株式代替）
PMCC_LONG_DELTA_MIN: float = 0.75
PMCC_LONG_DELTA_MAX: float = 0.90

#: Long leg DTE 範囲（60-90 DTE で購入・60 DTE 到達でロール）
PMCC_LONG_DTE_MIN: int = 60
PMCC_LONG_DTE_MAX: int = 90

#: Long leg ロールトリガー DTE（この DTE 以下になったら roll）
PMCC_LONG_ROLL_TRIGGER_DTE: int = 60

#: Short leg delta 範囲（weekly OTM call）
PMCC_SHORT_DELTA_MIN: float = 0.25
PMCC_SHORT_DELTA_MAX: float = 0.35

#: Short leg DTE 範囲（weekly: 3-7 DTE）
PMCC_SHORT_DTE_MIN: int = 3
PMCC_SHORT_DTE_MAX: int = 7

#: short call ロールトリガー DTE（weekly expiry 翌日）
PMCC_SHORT_ROLL_TRIGGER_DTE: int = 1

#: IVR 許容範囲（0-100 スケール）
PMCC_IVR_MIN: float = 20.0
PMCC_IVR_MAX: float = 80.0

#: short call 利確目標（エントリー時受取プレミアム比）
PMCC_SHORT_PROFIT_TARGET: float = 0.50   # 50% profit exit

#: 全体損切り水準（long call net_debit 比）
PMCC_STOP_LOSS_RATIO: float = 2.00       # 2.0x net_debit

#: エントリーウィンドウ（ET）
PMCC_ENTRY_WINDOW_START_ET: int = 10    # 10:00 ET
PMCC_ENTRY_WINDOW_END_ET: int = 13      # 13:00 ET

#: leg 識別子
LegLabel = Literal["long_call", "short_call"]

#: 戦術識別子
PMCC_PREFIX: str = "pmcc"


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class PMCCConfig:
    """PMCC 戦術設定。

    Attributes:
        long_delta_min:         long call delta 下限 (deep ITM)
        long_delta_max:         long call delta 上限 (deep ITM)
        long_dte_min:           long call 最低購入 DTE
        long_dte_max:           long call 最大購入 DTE
        long_roll_trigger_dte:  long call ロールトリガー DTE (この値以下でロール)
        short_delta_min:        short call delta 下限 (OTM weekly)
        short_delta_max:        short call delta 上限 (OTM weekly)
        short_dte_min:          short call 最低 DTE
        short_dte_max:          short call 最大 DTE
        ivr_min:                エントリー最低 IVR (0-100)
        ivr_max:                エントリー最高 IVR (0-100)
        short_profit_target:    short call 利確目標 (受取プレミアム比 0.0-1.0)
        stop_loss_ratio:        全体損切り水準 (long call net_debit 比)
        entry_window_start_et:  エントリー開始時刻 (ET hour)
        entry_window_end_et:    エントリー終了時刻 (ET hour)
        paper_mode:             True = paper 発注
    """
    long_delta_min: float = PMCC_LONG_DELTA_MIN
    long_delta_max: float = PMCC_LONG_DELTA_MAX
    long_dte_min: int = PMCC_LONG_DTE_MIN
    long_dte_max: int = PMCC_LONG_DTE_MAX
    long_roll_trigger_dte: int = PMCC_LONG_ROLL_TRIGGER_DTE
    short_delta_min: float = PMCC_SHORT_DELTA_MIN
    short_delta_max: float = PMCC_SHORT_DELTA_MAX
    short_dte_min: int = PMCC_SHORT_DTE_MIN
    short_dte_max: int = PMCC_SHORT_DTE_MAX
    ivr_min: float = PMCC_IVR_MIN
    ivr_max: float = PMCC_IVR_MAX
    short_profit_target: float = PMCC_SHORT_PROFIT_TARGET
    stop_loss_ratio: float = PMCC_STOP_LOSS_RATIO
    entry_window_start_et: int = PMCC_ENTRY_WINDOW_START_ET
    entry_window_end_et: int = PMCC_ENTRY_WINDOW_END_ET
    paper_mode: bool = True

    def __post_init__(self) -> None:
        """設定値バリデーション。

        Raises:
            ValueError: delta / DTE / IVR 範囲が矛盾する場合
        """
        if not (0.0 < self.long_delta_min < self.long_delta_max < 1.0):
            raise ValueError(
                f"long_delta_min={self.long_delta_min} / long_delta_max={self.long_delta_max}: "
                "0 < min < max < 1 を満たしていません"
            )
        if not (0.0 < self.short_delta_min < self.short_delta_max < 1.0):
            raise ValueError(
                f"short_delta_min={self.short_delta_min} / short_delta_max={self.short_delta_max}: "
                "0 < min < max < 1 を満たしていません"
            )
        if self.long_delta_min <= self.short_delta_max:
            raise ValueError(
                f"long_delta_min={self.long_delta_min} must be > "
                f"short_delta_max={self.short_delta_max}: "
                "long call は short call より深く ITM でなければなりません"
            )
        if not (0 < self.long_dte_min <= self.long_dte_max):
            raise ValueError(
                f"long_dte_min={self.long_dte_min} / long_dte_max={self.long_dte_max}: "
                "0 < min <= max を満たしていません"
            )
        if not (0 < self.short_dte_min <= self.short_dte_max):
            raise ValueError(
                f"short_dte_min={self.short_dte_min} / short_dte_max={self.short_dte_max}: "
                "0 < min <= max を満たしていません"
            )
        if self.short_dte_max >= self.long_dte_min:
            raise ValueError(
                f"short_dte_max={self.short_dte_max} must be < "
                f"long_dte_min={self.long_dte_min}: "
                "short call は long call より短期でなければなりません"
            )
        if not (0.0 <= self.ivr_min < self.ivr_max <= 100.0):
            raise ValueError(
                f"ivr_min={self.ivr_min} / ivr_max={self.ivr_max}: "
                "0 <= min < max <= 100 を満たしていません"
            )
        if not (0.0 < self.short_profit_target < 1.0):
            raise ValueError(
                f"short_profit_target={self.short_profit_target}: (0, 1) の範囲でなければなりません"
            )
        if self.stop_loss_ratio <= 0.0:
            raise ValueError(
                f"stop_loss_ratio={self.stop_loss_ratio}: 正の値が必要です"
            )
        if self.entry_window_start_et >= self.entry_window_end_et:
            raise ValueError(
                f"entry_window_start_et={self.entry_window_start_et} must be < "
                f"entry_window_end_et={self.entry_window_end_et}"
            )


# ---------------------------------------------------------------------------
# Leg DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PMCCLeg:
    """PMCC の 1 leg を表す発注単位。

    Attributes:
        label:        "long_call" | "short_call"
        side:         "buy" | "sell"
        delta:        デルタ絶対値（ストライク選定根拠）
        dte_target:   目標 DTE
        quantity:     発注枚数
        strike:       ストライク価格（0.0 = Phase 2 で option chain から解決）
        premium:      受取/支払いプレミアム (long は負・short は正)
    """
    label: LegLabel
    side: Literal["buy", "sell"]
    delta: float
    dte_target: int
    quantity: int = 1
    strike: float = 0.0
    premium: float = 0.0


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PMCCEntryDecision:
    """PMCC エントリー決定 DTO。

    Attributes:
        should_enter:    True = エントリー実行
        symbol:          対象銘柄
        legs:            2 legs (long_call, short_call) — 異なる expiry
        net_debit:       支払い net debit (long premium - short premium)
        quantity:        発注枚数
        idempotency_key: 二重発注防止キー
        reason:          判定理由
        ivr:             エントリー時 IVR（監査用）
    """
    should_enter: bool
    symbol: str
    legs: tuple[PMCCLeg, ...] = field(default_factory=tuple)
    net_debit: float = 0.0
    quantity: int = 1
    idempotency_key: str = ""
    reason: str = ""
    ivr: float = 0.0


@dataclass(frozen=True)
class PMCCExitDecision:
    """PMCC エグジット / ロール決定 DTO。

    Attributes:
        should_exit:  True = クローズまたはロール実行
        reason:       判定理由
        exit_type:    クローズ / ロール種別
    """
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "long_call_roll",
        "weekly_short_roll",
        "profit_exit_short",
        "stop_loss",
        "force_close",
        "none",
    ] = "none"


# ---------------------------------------------------------------------------
# ポジション stub
# ---------------------------------------------------------------------------

@dataclass
class PMCCPosition:
    """PMCC 保有ポジション。

    Attributes:
        symbol:              銘柄
        quantity:            枚数
        long_call_expiry:    long call 満期日
        short_call_expiry:   short call 満期日
        long_call_dte:       現在の long call 残 DTE
        short_call_dte:      現在の short call 残 DTE
        net_debit:           エントリー時支払い net debit (long - short premium)
        short_entry_premium: short call エントリー時受取プレミアム (正値)
        unrealized_pnl:      含み損益 (正値 = 利益)
        tactic_name:         戦術名
        entry_time:          エントリー時刻 (UTC)
    """
    symbol: str
    quantity: int
    long_call_expiry: date | None = None
    short_call_expiry: date | None = None
    long_call_dte: int = 90
    short_call_dte: int = 7
    net_debit: float = 0.0
    short_entry_premium: float = 0.0
    unrealized_pnl: float = 0.0
    tactic_name: str = PMCC_PREFIX
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# PMCCTactic — Type A: EnterExit
# ---------------------------------------------------------------------------

class PMCCTactic(TacticBase):
    """Poor Man's Covered Call 戦術（Type A: enter_exit）。

    Deep ITM long call (60-90 DTE, delta 0.75-0.90) を株式代替として保有し、
    weekly OTM short call (delta 0.25-0.35) でプレミアムを週次回収する。

    エントリー条件:
    - env.bias == "bull" (上方向バイアス必須)
    - IVR 20-80 (middle-range premium 取得可能)
    - ET 10:00-13:00 ウィンドウ内

    ロール / エグジット優先順:
    1. Kill Switch ARMED → force_close
    2. long call DTE <= 60 → long_call_roll (深い月足に乗り換え)
    3. short call DTE <= 1 → weekly_short_roll
    4. short call 利益 >= entry_premium × 50% → profit_exit_short
    5. unrealized_pnl <= -net_debit × stop_loss_ratio → stop_loss

    Args:
        config:   PMCCConfig（省略時はデフォルト設定）
        clock_fn: テスト用時刻注入 (省略時は datetime.now(ET))
    """

    def __init__(
        self,
        config: PMCCConfig | None = None,
        clock_fn: "None | (() -> datetime)" = None,
    ) -> None:
        self._cfg = config or PMCCConfig()
        self._clock_fn = clock_fn

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return PMCC_PREFIX

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _now_et(self) -> datetime:
        """現在時刻 (ET) を返す（テスト時は clock_fn で差し替え可）。"""
        if self._clock_fn is not None:
            return self._clock_fn()
        return datetime.now(_ET)

    def _in_entry_window(self) -> bool:
        """ET 10:00-13:00 ウィンドウ内かを返す。"""
        h = self._now_et().hour
        return self._cfg.entry_window_start_et <= h < self._cfg.entry_window_end_et

    @staticmethod
    def _validate_ivr(symbol: str, ivr: float) -> None:
        """IVR を 0-100 スケールで検証する。

        Raises:
            TypeError: NaN / inf の場合
            TypeError: 0-100 範囲外の場合
        """
        if not math.isfinite(ivr):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} は NaN または inf です。"
                "IVR は 0-100 スケールの有限値でなければなりません。"
            )
        if not (0.0 <= ivr <= 100.0):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} が 0-100 スケール範囲外です。"
            )

    def _build_legs(self) -> tuple[PMCCLeg, ...]:
        """2 leg (long_call + short_call) を設定から構築する。

        long call:  delta = long_delta_min/max 中央値 / DTE = long_dte_max
        short call: delta = short_delta_min/max 中央値 / DTE = short_dte_max

        Returns:
            (long_call_leg, short_call_leg) の tuple
        """
        long_delta = (self._cfg.long_delta_min + self._cfg.long_delta_max) / 2.0
        short_delta = (self._cfg.short_delta_min + self._cfg.short_delta_max) / 2.0

        long_leg = PMCCLeg(
            label="long_call",
            side="buy",
            delta=long_delta,
            dte_target=self._cfg.long_dte_max,
            quantity=1,
        )
        short_leg = PMCCLeg(
            label="short_call",
            side="sell",
            delta=short_delta,
            dte_target=self._cfg.short_dte_max,
            quantity=1,
        )
        return (long_leg, short_leg)

    def _make_idem_key(self, symbol: str, suffix: str = "") -> str:
        """5 分バケット idempotency key を生成する。

        Args:
            symbol: 対象銘柄
            suffix: leg label / exit_type サフィックス（衝突防止）

        Returns:
            str: idempotency key
        """
        now_utc = datetime.now(timezone.utc)
        bucket_min = (now_utc.minute // 5) * 5
        trigger_time = now_utc.replace(minute=bucket_min, second=0, microsecond=0)
        strategy = f"{PMCC_PREFIX}_{suffix}" if suffix else PMCC_PREFIX
        return make_job_key(strategy=strategy, symbol=symbol, trigger_time=trigger_time)

    # ------------------------------------------------------------------
    # TacticBase ABC 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック:
        1. env が None → False
        2. Kill Switch ARMED → False
        3. bias != "bull" → False (上方向バイアス必須)

        Returns:
            True — 戦術発動可能 / False — 発動不可
        """
        if env is None:
            log.warning("[PMCCTactic.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning("[PMCCTactic.preflight] Kill Switch ARMED: pmcc を無効化")
            return False

        if env.bias != "bull":
            log.info(
                "[PMCCTactic.preflight] bias=%s: PMCC は bull 専用・preflight False",
                env.bias,
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
    ) -> PMCCEntryDecision:
        """エントリー判定。

        判定順:
        1. Kill Switch ARMED → should_enter=False
        2. bias != "bull" → should_enter=False
        3. エントリーウィンドウ外 → should_enter=False
        4. IVR NaN/inf チェック → TypeError
        5. IVR 0-100 スケール外 → TypeError
        6. IVR 範囲外 [ivr_min, ivr_max] → should_enter=False
        7. 全条件 pass → 2 leg 構築・net_debit 算出・idempotency key 生成

        Args:
            env:    市場環境スナップショット
            symbol: 対象銘柄

        Returns:
            PMCCEntryDecision

        Raises:
            TypeError: IVR が NaN/inf または 0-100 範囲外の場合
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[PMCCTactic.should_enter] Kill Switch ARMED: "
                "エントリー判定スキップ (symbol=%s)",
                symbol,
            )
            return PMCCEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="kill_switch_armed",
            )

        # 2. 上方向バイアス確認
        if env.bias != "bull":
            log.debug(
                "[PMCCTactic.should_enter] bias=%s: PMCC は bull 専用・スキップ (symbol=%s)",
                env.bias,
                symbol,
            )
            return PMCCEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"bias={env.bias}: pmcc は bull 専用",
            )

        # 3. エントリーウィンドウ確認
        if not self._in_entry_window():
            log.debug(
                "[PMCCTactic.should_enter] エントリーウィンドウ外: スキップ (symbol=%s)",
                symbol,
            )
            return PMCCEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=(
                    f"entry_window_closed: window={self._cfg.entry_window_start_et}:00-"
                    f"{self._cfg.entry_window_end_et}:00 ET"
                ),
            )

        # 4-5. IVR 検証
        ivr = env.ivr_by_symbol.get(symbol, 0.0)
        self._validate_ivr(symbol, ivr)

        # 6. IVR 範囲チェック (動的閾値・規律 feedback_no_fixed_params 準拠)
        from atlas_v3.bots.engines.dynamic_params import get_dynamic_ivr_threshold
        ivr_min_dyn = get_dynamic_ivr_threshold(env.vix, self._cfg.ivr_min)
        if not (ivr_min_dyn <= ivr <= self._cfg.ivr_max):
            log.debug(
                "[PMCCTactic.should_enter] IVR=%.1f 範囲外 [%.1f(dyn,base=%.1f,VIX=%.2f), %.1f]: スキップ (symbol=%s)",
                ivr, ivr_min_dyn, self._cfg.ivr_min, env.vix, self._cfg.ivr_max, symbol,
            )
            return PMCCEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"IVR={ivr:.1f} 範囲外 [{ivr_min_dyn:.1f}(dyn),{self._cfg.ivr_max:.1f}]",
                ivr=ivr,
            )

        # 7. 2 leg 構築
        legs = self._build_legs()
        idem_key = self._make_idem_key(symbol)

        log.info(
            "[PMCCTactic.should_enter] エントリー OK: symbol=%s IVR=%.1f "
            "bias=%s long_delta=%.2f short_delta=%.2f key=%s",
            symbol,
            ivr,
            env.bias,
            legs[0].delta,
            legs[1].delta,
            idem_key,
        )

        return PMCCEntryDecision(
            should_enter=True,
            symbol=symbol,
            legs=legs,
            net_debit=0.0,  # Phase 2 で option chain の actual premium に差し替え
            quantity=1,
            idempotency_key=idem_key,
            reason=(
                f"bias={env.bias} / IVR={ivr:.1f} in [{self._cfg.ivr_min:.1f},"
                f"{self._cfg.ivr_max:.1f}] / "
                f"long_delta={legs[0].delta:.2f} / short_delta={legs[1].delta:.2f}"
            ),
            ivr=ivr,
        )

    # ------------------------------------------------------------------
    # 2-Leg 発注構築
    # ------------------------------------------------------------------

    def build_orders(
        self,
        decision: PMCCEntryDecision,
        capital_usd: float = 0.0,
    ) -> "list[OrderRequest]":
        """エントリー判定から 2 件の OrderRequest を構築する。

        Leg ごとに個別 idempotency_key (leg label サフィックス付き) を生成し
        二重発注・leg 間キー衝突を防止する。

        Args:
            decision:    should_enter=True の PMCCEntryDecision
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            [long_call_order, short_call_order]

        Raises:
            ValueError:      should_enter=False の decision が渡された場合
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_enter:
            raise ValueError(
                "[PMCCTactic.build_orders] should_enter=False の decision が渡されました。"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        # 2026-04-25: PreTradeGate L2 whitelist は "US.XXX" 形式・L3 margin は capital_usd+est_margin 必須。
        sym = decision.symbol if decision.symbol.startswith("US.") else f"US.{decision.symbol}"
        est_margin = sum(abs(leg.quantity) for leg in decision.legs) * 100 if decision.legs else decision.quantity * 100
        _gr = _gate(_Ctx(
            symbol=sym, qty=decision.quantity, side="BUY", is_long=True,
            est_margin=est_margin, capital_usd=capital_usd,
        ))
        if not _gr.allowed:
            raise ValueError(f"[PMCCTactic.build_orders] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=self._cfg.paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        order_type = "paper_limit" if self._cfg.paper_mode else "limit"
        orders: list[OrderRequest] = []

        for leg in decision.legs:
            leg_key = f"{decision.idempotency_key}_{leg.label}"
            orders.append(
                OrderRequest(
                    symbol=f"{decision.symbol}_{leg.label}_{leg.dte_target}dte",
                    side=leg.side,
                    quantity=decision.quantity,
                    order_type=order_type,
                    tactic_name=self.tactic_name,
                    idempotency_key=leg_key,
                )
            )

        return orders

    # ------------------------------------------------------------------
    # Exit / Roll 判定
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: PMCCPosition,
        env: MarketEnvironment,
    ) -> PMCCExitDecision:
        """エグジット / ロール判定。

        判定優先度:
        1. Kill Switch ARMED → force_close
        2. long call DTE <= long_roll_trigger_dte → long_call_roll
        3. short call DTE <= PMCC_SHORT_ROLL_TRIGGER_DTE → weekly_short_roll
        4. short call 利益 >= short_entry_premium × short_profit_target → profit_exit_short
        5. unrealized_pnl <= -net_debit × stop_loss_ratio → stop_loss

        Args:
            position: 現在ポジション (PMCCPosition)
            env:      現在の市場環境

        Returns:
            PMCCExitDecision
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[PMCCTactic.should_exit] Kill Switch ARMED: 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return PMCCExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        # 2. long call ロールトリガー (60 DTE 以前)
        if self._should_roll_long(position):
            log.info(
                "[PMCCTactic.should_exit] long call DTE=%d <= trigger=%d: "
                "long_call_roll (symbol=%s)",
                position.long_call_dte,
                self._cfg.long_roll_trigger_dte,
                position.symbol,
            )
            return PMCCExitDecision(
                should_exit=True,
                reason=f"long_call_dte={position.long_call_dte} <= trigger={self._cfg.long_roll_trigger_dte}",
                exit_type="long_call_roll",
            )

        # 3. weekly short call ロール (expiry 当日翌日)
        if self._should_roll_short(position):
            log.info(
                "[PMCCTactic.should_exit] short call DTE=%d <= 1: "
                "weekly_short_roll (symbol=%s)",
                position.short_call_dte,
                position.symbol,
            )
            return PMCCExitDecision(
                should_exit=True,
                reason=f"short_call_dte={position.short_call_dte} <= {PMCC_SHORT_ROLL_TRIGGER_DTE}",
                exit_type="weekly_short_roll",
            )

        # 4. short call 50% profit exit
        exit_decision = self._check_short_profit(position)
        if exit_decision is not None:
            return exit_decision

        # 5. 全体損切り
        return self._check_stop_loss(position)

    def build_exit_orders(
        self,
        position: PMCCPosition,
        decision: PMCCExitDecision,
    ) -> "list[OrderRequest]":
        """エグジット / ロール発注オブジェクトを構築する。

        exit_type に応じて対象 leg を決定する:
        - long_call_roll: long_call leg のみ close (sell)
        - weekly_short_roll: short_call leg のみ close (buy back)
        - profit_exit_short: short_call leg のみ close (buy back)
        - stop_loss / force_close: 両 leg close

        Args:
            position: 現在ポジション
            decision: should_exit=True の PMCCExitDecision

        Returns:
            list[OrderRequest]

        Raises:
            ValueError: should_exit=False の decision が渡された場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_exit:
            raise ValueError(
                "[PMCCTactic.build_exit_orders] should_exit=False の decision が渡されました。"
            )

        idem_key = self._make_idem_key(
            position.symbol, suffix=f"exit_{decision.exit_type}"
        )
        orders: list[OrderRequest] = []

        if decision.exit_type == "long_call_roll":
            orders.append(
                OrderRequest(
                    symbol=f"{position.symbol}_long_call_roll",
                    side="sell",
                    quantity=position.quantity,
                    order_type="limit",
                    tactic_name=self.tactic_name,
                    idempotency_key=f"{idem_key}_long_call",
                )
            )
        elif decision.exit_type in ("weekly_short_roll", "profit_exit_short"):
            orders.append(
                OrderRequest(
                    symbol=f"{position.symbol}_short_call_close",
                    side="buy",
                    quantity=position.quantity,
                    order_type="limit",
                    tactic_name=self.tactic_name,
                    idempotency_key=f"{idem_key}_short_call",
                )
            )
        else:
            # stop_loss / force_close: 両 leg close
            for label, side in (("long_call", "sell"), ("short_call", "buy")):
                orders.append(
                    OrderRequest(
                        symbol=f"{position.symbol}_{label}_close",
                        side=side,
                        quantity=position.quantity,
                        order_type="market",
                        tactic_name=self.tactic_name,
                        idempotency_key=f"{idem_key}_{label}",
                    )
                )

        return orders

    # ------------------------------------------------------------------
    # 内部判定ヘルパー（CC 分離）
    # ------------------------------------------------------------------

    def _should_roll_long(self, position: PMCCPosition) -> bool:
        """long call が 60 DTE 以下に到達したか判定する。

        Args:
            position: 現在ポジション

        Returns:
            True — ロール実行すべき
        """
        return position.long_call_dte <= self._cfg.long_roll_trigger_dte

    def _should_roll_short(self, position: PMCCPosition) -> bool:
        """short call が weekly expiry 翌日に到達したか判定する。

        expiry 日付ベースと DTE ベースの両方を確認する。

        Args:
            position: 現在ポジション

        Returns:
            True — weekly roll 実行すべき
        """
        # DTE ベースチェック
        if position.short_call_dte <= PMCC_SHORT_ROLL_TRIGGER_DTE:
            return True

        # expiry 日付ベースチェック（date が設定されている場合）
        if position.short_call_expiry is None:
            return False

        today_et = self._now_et().date()
        roll_trigger = position.short_call_expiry + timedelta(days=1)
        return today_et >= roll_trigger

    def _check_short_profit(
        self, position: PMCCPosition
    ) -> PMCCExitDecision | None:
        """short call の 50% profit exit を判定する。

        short_entry_premium が未設定 (0) の場合は判定不能として None を返す。

        Args:
            position: 現在ポジション

        Returns:
            PMCCExitDecision (profit_exit_short) または None
        """
        if position.short_entry_premium <= 0:
            return None

        # 動的 short profit target (規律 feedback_no_fixed_params 準拠・env 不在のため引数追加が大きいので
        # ここでは spy_bot 互換 hardcoded vix=20 base で base 値そのまま返す)
        profit_threshold = position.short_entry_premium * self._cfg.short_profit_target
        # unrealized_pnl > 0 が short premium 回収を表す
        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[PMCCTactic._check_short_profit] 50%% profit: "
                "pnl=%.2f >= target=%.2f (symbol=%s)",
                position.unrealized_pnl,
                profit_threshold,
                position.symbol,
            )
            return PMCCExitDecision(
                should_exit=True,
                reason=(
                    f"profit_exit_short: pnl={position.unrealized_pnl:.2f} "
                    f">= {profit_threshold:.2f}"
                ),
                exit_type="profit_exit_short",
            )
        return None

    def _check_stop_loss(self, position: PMCCPosition) -> PMCCExitDecision:
        """全体損切りを判定する。

        net_debit が未設定 (0) の場合は損切り判定不能として保留を返す。

        Args:
            position: 現在ポジション

        Returns:
            PMCCExitDecision (stop_loss or none)
        """
        if position.net_debit <= 0:
            log.debug(
                "[PMCCTactic._check_stop_loss] net_debit=%.2f: 損切り判定不能・保留 (symbol=%s)",
                position.net_debit,
                position.symbol,
            )
            return PMCCExitDecision(should_exit=False, reason="net_debit_not_set")

        loss_threshold = -(position.net_debit * self._cfg.stop_loss_ratio)
        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[PMCCTactic._check_stop_loss] 損切り: pnl=%.2f <= stop=%.2f (symbol=%s)",
                position.unrealized_pnl,
                loss_threshold,
                position.symbol,
            )
            return PMCCExitDecision(
                should_exit=True,
                reason=(
                    f"stop_loss_{self._cfg.stop_loss_ratio}x: "
                    f"pnl={position.unrealized_pnl:.2f} <= {loss_threshold:.2f}"
                ),
                exit_type="stop_loss",
            )

        return PMCCExitDecision(should_exit=False, reason="holding", exit_type="none")
