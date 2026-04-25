"""atlas_v3/bots/engines/diagonal_spread.py — Diagonal Spread 戦術エンジン

設計概要:
  上方向バイアス + theta 差によるプレミアム回収を目的とした 2-legged オプション構造。
  - Short leg: 近月 OTM call (delta 0.20-0.30, 7-21 DTE)
  - Long  leg: 遠月 ITM/ATM call (30-60 DTE)

環境フィルタ:
  - IVR 30-70
  - VIX 15-25 (spread stable 条件)
  - term_structure contango: 遠月 IV > 近月 IV (term_ratio > 1.0)

エントリーウィンドウ: 10:00-13:00 ET

エグジット優先順:
  1. Kill Switch ARMED → force_close
  2. 近月 expiry 翌日到達  → roll (short leg を次限月にロール)
  3. 利確 30% of net debit → profit_target
  4. 損切り 1.5x net debit → stop_loss

TacticBase 継承 + EnterExitTactic Protocol 実装。
spy_bot.py / chronos_bot.py / common/* は無変更。
CC 規律: 各メソッド CC ≤ 20
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

#: Short leg delta 選定範囲（OTM call）
DIAG_DELTA_SHORT_MIN: float = 0.20
DIAG_DELTA_SHORT_MAX: float = 0.30

#: Long leg DTE 範囲（遠月 ITM/ATM call）
DIAG_LONG_DTE_MIN: int = 30
DIAG_LONG_DTE_MAX: int = 60

#: Short leg DTE 上限（近月）
DIAG_SHORT_DTE_MAX: int = 21

#: エントリーウィンドウ（ET）
ENTRY_WINDOW_START_ET: int = 10   # 10:00 ET
ENTRY_WINDOW_END_ET: int = 13     # 13:00 ET

#: IVR 許容範囲（0-100 スケール）
IVR_MIN: float = 30.0
IVR_MAX: float = 70.0

#: VIX 許容範囲
VIX_MIN: float = 15.0
VIX_MAX: float = 25.0

#: term structure contango 最低比率 (遠月IV / 近月IV > 1.0)
TERM_RATIO_MIN: float = 1.0

#: 利確目標（net debit 比）
PROFIT_TARGET_RATIO: float = 0.30   # 30% of net debit

#: 損切り水準（net debit 比）
STOP_LOSS_RATIO: float = 1.50       # 1.5x net debit

#: 東部時間 ZoneInfo
_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class DiagonalSpreadConfig:
    """Diagonal Spread 戦術設定。

    Attributes:
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        delta_short_min:        短期 leg の delta 下限 (OTM call)
        delta_short_max:        短期 leg の delta 上限 (OTM call)
        long_dte_min:           長期 leg の最低 DTE
        long_dte_max:           長期 leg の最大 DTE
        short_dte_max:          短期 leg の最大 DTE
        ivr_min:                エントリー最低 IVR (0-100)
        ivr_max:                エントリー最高 IVR (0-100)
        vix_min:                エントリー最低 VIX
        vix_max:                エントリー最高 VIX
        term_ratio_min:         contango 判定の最低 term_ratio (遠月IV/近月IV)
        profit_target_ratio:    利確目標 (net debit 比)
        stop_loss_ratio:        損切り水準 (net debit 比)
        entry_window_start_et:  エントリー開始時刻 (ET 時間)
        entry_window_end_et:    エントリー終了時刻 (ET 時間)
    """
    slippage_tolerance_bps: int = 10
    delta_short_min: float = DIAG_DELTA_SHORT_MIN
    delta_short_max: float = DIAG_DELTA_SHORT_MAX
    long_dte_min: int = DIAG_LONG_DTE_MIN
    long_dte_max: int = DIAG_LONG_DTE_MAX
    short_dte_max: int = DIAG_SHORT_DTE_MAX
    ivr_min: float = IVR_MIN
    ivr_max: float = IVR_MAX
    vix_min: float = VIX_MIN
    vix_max: float = VIX_MAX
    term_ratio_min: float = TERM_RATIO_MIN
    profit_target_ratio: float = PROFIT_TARGET_RATIO
    stop_loss_ratio: float = STOP_LOSS_RATIO
    entry_window_start_et: int = ENTRY_WINDOW_START_ET
    entry_window_end_et: int = ENTRY_WINDOW_END_ET

    def __post_init__(self) -> None:
        """設定値バリデーション。

        Raises:
            ValueError: delta / DTE / IVR / VIX 範囲が矛盾する場合
        """
        if not (0.0 < self.delta_short_min < self.delta_short_max < 1.0):
            raise ValueError(
                f"delta_short_min={self.delta_short_min} / delta_short_max={self.delta_short_max}: "
                "0 < min < max < 1 を満たしていません"
            )
        if not (0 < self.long_dte_min <= self.long_dte_max):
            raise ValueError(
                f"long_dte_min={self.long_dte_min} / long_dte_max={self.long_dte_max}: "
                "0 < min <= max を満たしていません"
            )
        if not (0 < self.short_dte_max < self.long_dte_min):
            raise ValueError(
                f"short_dte_max={self.short_dte_max} must be < long_dte_min={self.long_dte_min}: "
                "近月 < 遠月 の制約が必要です"
            )
        if not (0.0 <= self.ivr_min < self.ivr_max <= 100.0):
            raise ValueError(
                f"ivr_min={self.ivr_min} / ivr_max={self.ivr_max}: "
                "0 <= min < max <= 100 を満たしていません"
            )
        if not (0.0 < self.vix_min < self.vix_max):
            raise ValueError(
                f"vix_min={self.vix_min} / vix_max={self.vix_max}: "
                "0 < min < max を満たしていません"
            )
        if self.profit_target_ratio <= 0.0:
            raise ValueError(
                f"profit_target_ratio={self.profit_target_ratio}: 正の値が必要です"
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
# エントリー / エグジット決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiagonalSpreadEntryDecision:
    """Diagonal Spread エントリー決定。

    Attributes:
        should_enter:      エントリー可否
        symbol:            対象銘柄
        short_delta_target: short leg の delta 目標値
        short_dte_target:   short leg の DTE 目標値
        long_dte_target:    long leg の DTE 目標値
        quantity:          枚数 (1 単位 = 1 spread)
        reason:            判定理由
        idempotency_key:   冪等性キー
    """
    should_enter: bool
    symbol: str
    short_delta_target: float = 0.0
    short_dte_target: int = 0
    long_dte_target: int = 0
    quantity: int = 1
    reason: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class DiagonalSpreadExitDecision:
    """Diagonal Spread エグジット決定。

    Attributes:
        should_exit: エグジット可否
        reason:      判定理由
        exit_type:   エグジット種別
    """
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "roll_short_expiry",
        "profit_target",
        "stop_loss",
        "force_close",
        "none",
    ] = "none"


# ---------------------------------------------------------------------------
# Position stub（ic_sell.Position と同一インターフェース・相互運用可能）
# ---------------------------------------------------------------------------

@dataclass
class DiagonalPosition:
    """Diagonal Spread ポジション表現。

    Attributes:
        symbol:              銘柄
        quantity:            枚数
        entry_price:         エントリー価格 (net debit)
        current_price:       現在価格 (net value of spread)
        tactic_name:         戦術名
        entry_time:          エントリー時刻 (UTC)
        unrealized_pnl:      含み損益 (current_price - entry_price) * quantity * 100
        net_debit:           支払った net debit（利確/損切り基準）
        short_expiry:        short leg の満期日
        long_expiry:         long leg の満期日
    """
    symbol: str
    quantity: int
    entry_price: float
    current_price: float = 0.0
    tactic_name: str = "diagonal_spread"
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: float = 0.0
    net_debit: float = 0.0          # エントリー時の支払い debit
    short_expiry: date | None = None
    long_expiry: date | None = None

    # ic_sell.Position と相互運用するために max_credit を alias として提供する
    @property
    def max_credit(self) -> float:
        """net_debit の alias（ICSellTactic インターフェース互換）。"""
        return self.net_debit


# ---------------------------------------------------------------------------
# DiagonalSpreadTactic — Type A: EnterExit（TacticBase 継承）
# ---------------------------------------------------------------------------

class DiagonalSpreadTactic(TacticBase):
    """Diagonal Spread 戦術（Type A: enter_exit）。

    上方向バイアス環境で short 近月 OTM call + long 遠月 ATM/ITM call を
    同時発注し、theta 差とコール価値上昇でプレミアムを回収する。

    エントリー条件:
    - IVR 30-70: プレミアムが取れる適度な IV 水準
    - VIX 15-25: スプレッド拡大リスクが低い安定環境
    - term_ratio > 1.0: contango 構造（遠月 IV > 近月 IV）で long leg が割安
    - env.bias == "bull": 上方向バイアス確認
    - 10:00-13:00 ET ウィンドウ内

    エグジット:
    - short leg の満期翌日: roll（short を次限月にロール）
    - 含み益が net_debit の 30%: 利確
    - 含み損が net_debit の 150%: 損切り

    Args:
        config: DiagonalSpreadConfig（None のときデフォルト設定を使用）
    """

    def __init__(self, config: DiagonalSpreadConfig | None = None) -> None:
        self._cfg = config or DiagonalSpreadConfig()

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "diagonal_spread"

    # ------------------------------------------------------------------
    # TacticBase 必須: preflight
    # ------------------------------------------------------------------

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック項目（優先順）:
        1. env が None → False（型安全ガード）
        2. Kill Switch ARMED → False（EICAS Advisory）
        3. VIX が設定範囲外 → False
        4. term_ratio が contango 条件未満 → False

        Returns:
            True  — 戦術発動可能
            False — 発動不可（理由は log に必ず出力）
        """
        if env is None:
            log.warning("[DiagonalSpreadTactic.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[DiagonalSpreadTactic.preflight] Kill Switch ARMED: diagonal_spread を無効化"
            )
            return False

        if not (self._cfg.vix_min <= env.vix <= self._cfg.vix_max):
            log.info(
                "[DiagonalSpreadTactic.preflight] VIX=%.1f が範囲外 [%.1f, %.1f]: スキップ",
                env.vix, self._cfg.vix_min, self._cfg.vix_max,
            )
            return False

        if env.term_ratio < self._cfg.term_ratio_min:
            log.info(
                "[DiagonalSpreadTactic.preflight] term_ratio=%.3f < min=%.3f (backwardation): スキップ",
                env.term_ratio, self._cfg.term_ratio_min,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: should_enter
    # ------------------------------------------------------------------

    def should_enter(
        self, env: MarketEnvironment, symbol: str
    ) -> DiagonalSpreadEntryDecision | None:
        """エントリー判定。

        判定順:
        0. Kill Switch ARMED → None 返却（発注完全遮断）
        1. IVR NaN/inf チェック → TypeError
        2. IVR スケール検証（0-100 固定）→ TypeError
        3. IVR が設定範囲外 → should_enter=False
        4. bias != "bull" → should_enter=False（方向性バイアス確認）
        5. エントリーウィンドウ外（ET 10:00-13:00）→ should_enter=False
        6. term_ratio contango 再チェック（env.term_ratio > 1.0）
        7. 全条件 pass → short delta 中央値で決定・idempotency key 生成

        Args:
            env:    現在の市場環境スナップショット
            symbol: 対象銘柄

        Returns:
            DiagonalSpreadEntryDecision — エントリー判定結果
            None — Kill Switch ARMED の場合

        Raises:
            TypeError: IVR が NaN/inf または 0-100 スケール範囲外の場合
        """
        if kill_switch_is_active():
            log.warning(
                "[DiagonalSpreadTactic.should_enter] Kill Switch ARMED: "
                "エントリー判定をスキップ (symbol=%s)",
                symbol,
            )
            return None

        ivr = env.ivr_by_symbol.get(symbol, 0.0)

        # IVR 数値検証
        if not math.isfinite(ivr):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} は NaN または inf です。"
                "IVR は math.isfinite() を満たす有限値（0-100 スケール）でなければなりません。"
            )
        if not (0.0 <= ivr <= 100.0):
            raise TypeError(
                f"env.ivr_by_symbol[{symbol!r}]={ivr!r} が 0-100 スケール範囲外です。"
                "MarketEnvironment.ivr_by_symbol は 0-100 スケール固定。"
            )

        # IVR 範囲チェック（30-70: プレミアムが取れる適度な水準）
        if not (self._cfg.ivr_min <= ivr <= self._cfg.ivr_max):
            log.debug(
                "[DiagonalSpreadTactic.should_enter] IVR=%.1f が範囲外 [%.1f, %.1f]: スキップ "
                "(symbol=%s)",
                ivr, self._cfg.ivr_min, self._cfg.ivr_max, symbol,
            )
            return DiagonalSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"IVR={ivr:.1f} 範囲外 [{self._cfg.ivr_min:.1f},{self._cfg.ivr_max:.1f}]",
            )

        # 上方向バイアス確認（Diagonal Spread は bull 環境専用）
        if env.bias != "bull":
            log.debug(
                "[DiagonalSpreadTactic.should_enter] bias=%s: diagonal_spread は bull 専用・スキップ "
                "(symbol=%s)",
                env.bias, symbol,
            )
            return DiagonalSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"bias={env.bias}: diagonal_spread は bull 専用",
            )

        # エントリーウィンドウ確認（ET 10:00-13:00）
        if not self._is_in_entry_window():
            log.debug(
                "[DiagonalSpreadTactic.should_enter] エントリーウィンドウ外: スキップ (symbol=%s)",
                symbol,
            )
            return DiagonalSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"エントリーウィンドウ外 (ET {self._cfg.entry_window_start_et}:00-"
                       f"{self._cfg.entry_window_end_et}:00)",
            )

        # term structure contango 再確認
        if env.term_ratio < self._cfg.term_ratio_min:
            log.debug(
                "[DiagonalSpreadTactic.should_enter] term_ratio=%.3f < %.3f: contango 不足・スキップ "
                "(symbol=%s)",
                env.term_ratio, self._cfg.term_ratio_min, symbol,
            )
            return DiagonalSpreadEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason=f"term_ratio={env.term_ratio:.3f} < {self._cfg.term_ratio_min:.3f} (backwardation)",
            )

        # short delta: 設定範囲の中央値
        short_delta = (self._cfg.delta_short_min + self._cfg.delta_short_max) / 2.0
        # long DTE: 範囲の中央値
        long_dte = (self._cfg.long_dte_min + self._cfg.long_dte_max) // 2
        # short DTE: short_dte_max のちょうど半分（週次オプション想定）
        short_dte = max(1, self._cfg.short_dte_max // 2)

        # idempotency key: 5 分バケットに丸めて秒境界二重発注を防ぐ
        _now = datetime.now(timezone.utc)
        trigger_time = _now.replace(
            minute=(_now.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[DiagonalSpreadTactic.should_enter] エントリー OK: symbol=%s "
            "IVR=%.1f VIX=%.1f term_ratio=%.3f short_delta=%.2f "
            "short_dte=%d long_dte=%d key=%s",
            symbol, ivr, env.vix, env.term_ratio,
            short_delta, short_dte, long_dte, idem_key,
        )
        return DiagonalSpreadEntryDecision(
            should_enter=True,
            symbol=symbol,
            short_delta_target=short_delta,
            short_dte_target=short_dte,
            long_dte_target=long_dte,
            quantity=1,
            reason=(
                f"IVR={ivr:.1f} / VIX={env.vix:.1f} / bias={env.bias} "
                f"/ term_ratio={env.term_ratio:.3f} / short_delta={short_delta:.2f}"
            ),
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol: build_order
    # ------------------------------------------------------------------

    def build_order(
        self,
        decision: DiagonalSpreadEntryDecision,
        paper_mode: bool = True,
        capital_usd: float = 0.0,
    ) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。

        Args:
            decision:    should_enter=True の DiagonalSpreadEntryDecision
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
                f"[DiagonalSpreadTactic.build_order] "
                f"should_enter=False の decision が渡された: {decision}"
            )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        # 2026-04-25: PreTradeGate L2 whitelist は "US.XXX" 形式・L3 margin は capital_usd+est_margin 必須。
        sym = decision.symbol if decision.symbol.startswith("US.") else f"US.{decision.symbol}"
        est_margin = decision.quantity * 100  # proxy: 1 contract = 100 株
        _gr = _gate(_Ctx(
            symbol=sym, qty=decision.quantity, side="BUY", is_long=True,
            est_margin=est_margin, capital_usd=capital_usd,
        ))
        if not _gr.allowed:
            raise ValueError(f"[DiagonalSpreadTactic.build_order] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")

        return OrderRequest(
            symbol=decision.symbol,
            side="buy",         # net debit: long 遠月 - short 近月 → buy net
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
        position: DiagonalPosition,
        env: MarketEnvironment,
    ) -> DiagonalSpreadExitDecision:
        """エグジット判定。

        判定順（優先度降順）:
        1. Kill Switch ARMED → force_close
        2. net_debit 未設定（== 0）→ 判定不能（保留）
        3. short leg の満期翌日到達 → roll_short_expiry
        4. 含み益が net_debit * profit_target_ratio 超過 → profit_target
        5. 含み損が net_debit * stop_loss_ratio 超過 → stop_loss

        Args:
            position: 現在ポジション（DiagonalPosition）
            env:      現在の市場環境

        Returns:
            DiagonalSpreadExitDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[DiagonalSpreadTactic.should_exit] Kill Switch ARMED: "
                "強制クローズ (symbol=%s)",
                position.symbol,
            )
            return DiagonalSpreadExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        if position.net_debit <= 0:
            log.warning(
                "[DiagonalSpreadTactic.should_exit] net_debit=%.2f: exit 判定不能 (symbol=%s)",
                position.net_debit, position.symbol,
            )
            return DiagonalSpreadExitDecision(
                should_exit=False,
                reason="net_debit_not_set",
            )

        # short expiry 翌日到達チェック（roll トリガー）
        if self._should_roll_short(position):
            log.info(
                "[DiagonalSpreadTactic.should_exit] short_expiry 翌日到達: "
                "roll (symbol=%s short_expiry=%s)",
                position.symbol, position.short_expiry,
            )
            return DiagonalSpreadExitDecision(
                should_exit=True,
                reason=f"short_expiry 翌日到達: short_expiry={position.short_expiry}",
                exit_type="roll_short_expiry",
            )

        profit_threshold = position.net_debit * self._cfg.profit_target_ratio
        loss_threshold = -position.net_debit * self._cfg.stop_loss_ratio

        # 利確: net_debit の 30% 超
        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[DiagonalSpreadTactic.should_exit] 利確30%%: pnl=%.2f >= target=%.2f (symbol=%s)",
                position.unrealized_pnl, profit_threshold, position.symbol,
            )
            return DiagonalSpreadExitDecision(
                should_exit=True,
                reason=f"profit_target: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        # 損切り: net_debit の 1.5 倍超
        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[DiagonalSpreadTactic.should_exit] 損切り1.5x: pnl=%.2f <= stop=%.2f (symbol=%s)",
                position.unrealized_pnl, loss_threshold, position.symbol,
            )
            return DiagonalSpreadExitDecision(
                should_exit=True,
                reason=f"stop_loss_1.5x: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        return DiagonalSpreadExitDecision(
            should_exit=False,
            reason="holding",
            exit_type="none",
        )

    def build_exit_order(
        self,
        position: DiagonalPosition,
        decision: DiagonalSpreadExitDecision,
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。

        exit_type を idempotency key に含めることで、
        同一 5 分バケット内で profit_target と stop_loss が
        同一キーになるバグを回避する（statistical_premium_seller R3-C3 と同パターン）。

        Args:
            position: 現在ポジション
            decision: should_exit=True の DiagonalSpreadExitDecision

        Returns:
            OrderRequest

        Raises:
            ValueError: decision.should_exit=False の場合
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_exit:
            raise ValueError(
                "[DiagonalSpreadTactic.build_exit_order] "
                "should_exit=False の decision が渡された"
            )

        # exit idem key: exit_type を含めることで reason ごとに key を分離
        _now_exit = datetime.now(timezone.utc)
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit_{decision.exit_type}",
            symbol=position.symbol,
            trigger_time=_now_exit,
        )

        # roll の場合は leg 操作が複雑だが発注 DTO は sell（short leg close）を表現
        order_side = "sell" if decision.exit_type == "roll_short_expiry" else "sell"

        return OrderRequest(
            symbol=position.symbol,
            side=order_side,
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # 内部ユーティリティ
    # ------------------------------------------------------------------

    def _is_in_entry_window(self, now_et: datetime | None = None) -> bool:
        """現在時刻が ET エントリーウィンドウ内かどうかを返す。

        Args:
            now_et: テスト用オーバーライド（None のとき実時刻を使用）

        Returns:
            True — 10:00-13:00 ET の範囲内
            False — ウィンドウ外
        """
        if now_et is None:
            now_et = datetime.now(_ET)
        hour = now_et.hour
        return self._cfg.entry_window_start_et <= hour < self._cfg.entry_window_end_et

    def _should_roll_short(self, position: DiagonalPosition) -> bool:
        """short leg の満期翌日到達チェック。

        position.short_expiry が設定されており、
        ET 基準の today が short_expiry の翌日以降なら True。

        Args:
            position: 評価するポジション

        Returns:
            True — roll トリガー（short_expiry 翌日到達）
            False — まだロールすべきでない
        """
        if position.short_expiry is None:
            return False

        today_et = datetime.now(_ET).date()
        roll_trigger_date = position.short_expiry + timedelta(days=1)
        return today_et >= roll_trigger_date
