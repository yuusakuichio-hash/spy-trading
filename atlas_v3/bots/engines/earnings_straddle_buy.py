"""atlas_v3/bots/engines/earnings_straddle_buy.py — Earnings Straddle Buy 戦術

戦術概要
--------
IV Crush の逆張り: 決算発表**前夜**に ATM call + ATM put を両 long する
Long Volatility 戦術。決算発表後の大幅な方向性移動（"realized > implied"）
を期待する。IV Crush 売り戦術（earnings_iv_crush.py）と正反対のペイオフ。

エントリー条件
--------------
- 決算発表日 +1 日以内の銘柄のみ対象
- IVR < 60（ATMストラドルが相対的に安い環境で仕込む）
- 流動性フィルタ: underlying bid-ask spread が動的閾値以内
- エントリーウィンドウ: 決算発表日の前日 ET 15:00–15:45

エントリー内容
--------------
- ATM call 1 枚 + ATM put 1 枚（同一満期・同一 underlying）
- 満期: 決算発表日の翌日以降の最近傍 DTE（通常 0-2 DTE）
- 発注タイプ: limit（mid-price 基準）

エグジット条件（優先順位順）
-----------------------------
1. Kill Switch ARMED → force_close（即時）
2. profit 目標達成: unrealized_pnl >= entry_value * PROFIT_TARGET_PCT（デフォルト 40%）
3. stop 超過: unrealized_pnl <= -entry_value * STOP_LOSS_PCT（デフォルト 40%）
4. 決算発表日 open + 30 分以降（time-based exit）

依存関係
--------
- common.earnings_engine.EarningsEngine — 決算カレンダー取得・IVR 算出
- atlas_v3.strategies.base.TacticBase — 戦術基底 ABC
- atlas_v3.core.engine.OrderRequest — 発注 DTO
- atlas_v3.core.env_observer.MarketEnvironment — 市場環境スナップショット
- common_v3.idempotency.store.make_job_key — 冪等性キー生成
- common_v3.risk.kill_switch.is_active — Kill Switch チェック

設計規律
--------
- 固定パラメータ禁止: IVR 閾値・流動性閾値はすべて config で外部注入
- spy_bot.py / common/*.py への書き込み禁止（read-only 参照のみ）
- CC ≤ 20 per method
- TacticBase ABC 継承必須
- idempotency_key は全 OrderRequest に必ず設定
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class StraddleBuyConfig:
    """Earnings Straddle Buy 設定（全パラメータ外部注入・固定値禁止）。

    Attributes:
        ivr_max:               エントリー最大 IVR（これ以上は crush 環境 → 買い不利）
        days_until_earnings_max: 決算まで最大日数（この日数以内の銘柄のみ対象）
        entry_window_start_hour: エントリー開始時刻（ET 時）
        entry_window_start_min:  エントリー開始時刻（ET 分）
        entry_window_end_hour:   エントリー終了時刻（ET 時）
        entry_window_end_min:    エントリー終了時刻（ET 分）
        exit_open_offset_min:    決算発表日 open 後エグジット可能になるまでの分数
        profit_target_pct:       利確目標（エントリー価値比）
        stop_loss_pct:           損切り水準（エントリー価値比・正の値で指定）
        slippage_tolerance_bps:  スリッページ許容幅（basis points）
        max_symbols_per_day:     1 日最大参戦銘柄数
        vix_max:                 エントリー最大 VIX（パニック環境での参戦抑制）
    """
    ivr_max: float = 60.0
    days_until_earnings_max: int = 1
    entry_window_start_hour: int = 15
    entry_window_start_min: int = 0
    entry_window_end_hour: int = 15
    entry_window_end_min: int = 45
    exit_open_offset_min: int = 30
    profit_target_pct: float = 0.40
    stop_loss_pct: float = 0.40
    slippage_tolerance_bps: int = 20
    max_symbols_per_day: int = 3
    vix_max: float = 50.0


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StraddleBuyEntryDecision:
    """Straddle Buy エントリー決定 DTO。"""
    should_enter: bool
    symbol: str
    earnings_date: str = ""      # ISO date: 決算発表日
    ivr: float = 0.0
    reason: str = ""
    idempotency_key: str = ""
    quantity_call: int = 1       # ATM call 枚数
    quantity_put: int = 1        # ATM put 枚数


@dataclass(frozen=True)
class StraddleBuyExitDecision:
    """Straddle Buy エグジット決定 DTO。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal[
        "profit_target",
        "stop_loss",
        "force_close",
        "post_earnings_time_exit",
        "none",
    ] = "none"


# ---------------------------------------------------------------------------
# StraddlePosition — 両 leg を束ねたポジション表現
# ---------------------------------------------------------------------------

@dataclass
class StraddlePosition:
    """ATM call + ATM put の両 long ポジション。

    entry_value: エントリー時の合計プレミアム（call_mid + put_mid）
    unrealized_pnl: 現在の未実現 P&L（正=利益・負=損失）
    earnings_date: 決算発表日（ISO date str）
    earnings_open_dt: 決算発表日のマーケット open 時刻（ET）
                      None の場合は time-based exit を適用しない
    """
    symbol: str
    quantity: int
    entry_price_call: float
    entry_price_put: float
    earnings_date: str = ""
    earnings_open_dt: Optional[datetime] = None
    tactic_name: str = "earnings_straddle_buy"
    entry_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    unrealized_pnl: float = 0.0
    entry_value: float = 0.0     # call_mid + put_mid（ストップ計算の基準）


# ---------------------------------------------------------------------------
# EarningsStraddleBuyTactic — TacticBase 継承
# ---------------------------------------------------------------------------

class EarningsStraddleBuyTactic(TacticBase):
    """Earnings Straddle Buy 戦術（Type C: state_carrying）。

    決算前夜に ATM straddle（call + put 両 long）を仕込み、
    決算発表後の大幅移動（realized > implied）を取るロング・ボラティリティ戦術。

    IVR が低い環境（安く仕込める）かつ決算が翌日以内の銘柄を対象とする。

    StateCarrying 理由: 決算カレンダー・エントリー済み銘柄を state として
    保持し、再起動後も重複エントリーを防ぐ。

    Args:
        config:           StraddleBuyConfig（パラメータ外部注入）
        earnings_symbols: {symbol: earnings_date_iso} 初期カレンダー（テスト注入用）
    """

    _STATE_KEY = "earnings_straddle_buy_state"

    def __init__(
        self,
        config: StraddleBuyConfig | None = None,
        earnings_symbols: dict[str, str] | None = None,
    ) -> None:
        self._cfg = config or StraddleBuyConfig()
        # {symbol: earnings_date_iso_str}
        self._earnings_calendar: dict[str, str] = earnings_symbols or {}
        # エントリー済み銘柄の冪等性管理 {symbol: idempotency_key}
        self._entered_today: dict[str, str] = {}

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "state_carrying"

    @property
    def tactic_name(self) -> str:
        return "earnings_straddle_buy"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック:
        1. env が None → False
        2. Kill Switch ARMED → False
        3. VIX > vix_max → False（極端なパニック環境では参戦抑制）

        Returns:
            True — 戦術発動可能
        """
        if env is None:
            log.warning(
                "[StraddleBuy.preflight] env=None: preflight 失敗"
            )
            return False

        if kill_switch_is_active():
            log.warning(
                "[StraddleBuy.preflight] Kill Switch ARMED: "
                "earnings_straddle_buy 無効化"
            )
            return False

        if env.vix > self._cfg.vix_max:
            log.info(
                "[StraddleBuy.preflight] VIX=%.1f > max=%.1f: "
                "パニック環境でスキップ",
                env.vix, self._cfg.vix_max,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # StateCarryingTactic Protocol 実装
    # ------------------------------------------------------------------

    def observe(self, env: MarketEnvironment, market_data: Any) -> None:
        """決算カレンダーを更新する（Phase 2 で Finnhub 連携）。

        market_data に get_earnings_calendar(date_iso: str) → dict[str, str]
        が実装されていれば呼び出してカレンダーを更新する。
        """
        if hasattr(market_data, "get_earnings_calendar"):
            today = date.today().isoformat()
            try:
                new_cal: dict[str, str] = market_data.get_earnings_calendar(today)
                self._earnings_calendar.update(new_cal)
                log.info(
                    "[StraddleBuy.observe] 決算カレンダー更新: %d 銘柄",
                    len(self._earnings_calendar),
                )
            except Exception as exc:
                log.error(
                    "[StraddleBuy.observe] カレンダー取得失敗: %s (既存 state 保持)",
                    exc,
                )
                raise
        else:
            log.debug(
                "[StraddleBuy.observe] market_data.get_earnings_calendar 未実装: "
                "既存 state 保持 (%d 銘柄)",
                len(self._earnings_calendar),
            )

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol_candidates: list[str],
        now_et: Optional[datetime] = None,
    ) -> list[StraddleBuyEntryDecision]:
        """エントリー候補を評価して決定リストを返す。

        フィルタ基準（全条件 AND）:
        1. _earnings_calendar に登録済み
        2. 決算まで days_until_earnings_max 以内
        3. IVR < ivr_max（安い環境のみ買い）
        4. now_et がエントリーウィンドウ内（ET 15:00–15:45）
        5. max_symbols_per_day を超えない
        6. _entered_today に既登録でない（冪等性）

        Args:
            env:               MarketEnvironment
            symbol_candidates: 候補銘柄リスト
            now_et:            現在 ET 時刻（None=テスト用・ウィンドウ判定スキップ）

        Returns:
            list[StraddleBuyEntryDecision]
        """
        decisions: list[StraddleBuyEntryDecision] = []
        today = date.today()

        entered_count = len([
            k for k, v in self._entered_today.items() if v
        ])

        for symbol in symbol_candidates:
            if entered_count + len(
                [d for d in decisions if d.should_enter]
            ) >= self._cfg.max_symbols_per_day:
                log.info(
                    "[StraddleBuy.should_enter] max_symbols=%d 到達: 追加評価スキップ",
                    self._cfg.max_symbols_per_day,
                )
                break

            # 冪等性: 当日エントリー済みはスキップ
            if symbol in self._entered_today:
                log.debug(
                    "[StraddleBuy.should_enter] %s: already_entered today → skip",
                    symbol,
                )
                decisions.append(StraddleBuyEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    reason="already_entered_today",
                ))
                continue

            earnings_date_str = self._earnings_calendar.get(symbol)
            if not earnings_date_str:
                continue

            # 決算まで日数チェック
            try:
                earnings_dt = date.fromisoformat(earnings_date_str)
            except ValueError:
                log.warning(
                    "[StraddleBuy.should_enter] %s: 無効な earnings_date=%s → skip",
                    symbol, earnings_date_str,
                )
                continue

            days_until = (earnings_dt - today).days
            if not (0 <= days_until <= self._cfg.days_until_earnings_max):
                log.debug(
                    "[StraddleBuy.should_enter] %s: days_until=%d > max=%d → skip",
                    symbol, days_until, self._cfg.days_until_earnings_max,
                )
                decisions.append(StraddleBuyEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    earnings_date=earnings_date_str,
                    reason=f"days_until={days_until}>max={self._cfg.days_until_earnings_max}",
                ))
                continue

            # IVR フィルタ（低 IVR = 安い環境のみ買い）
            ivr = env.ivr_by_symbol.get(symbol, 100.0)
            if ivr >= self._cfg.ivr_max:
                log.info(
                    "[StraddleBuy.should_enter] %s: IVR=%.1f >= max=%.1f: "
                    "高 IVR 環境 → crush 環境 → 買い不利 → skip",
                    symbol, ivr, self._cfg.ivr_max,
                )
                decisions.append(StraddleBuyEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    earnings_date=earnings_date_str,
                    ivr=ivr,
                    reason=f"IVR={ivr:.1f}>={self._cfg.ivr_max}",
                ))
                continue

            # エントリーウィンドウ判定（now_et=None はテスト用スキップ）
            if now_et is not None and not self._in_entry_window(now_et):
                log.debug(
                    "[StraddleBuy.should_enter] %s: outside entry window (ET %02d:%02d) → skip",
                    symbol, now_et.hour, now_et.minute,
                )
                decisions.append(StraddleBuyEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    earnings_date=earnings_date_str,
                    ivr=ivr,
                    reason=f"outside_entry_window ET {now_et.hour:02d}:{now_et.minute:02d}",
                ))
                continue

            # 冪等性キー生成
            trigger_time = (
                now_et.replace(second=0, microsecond=0)
                if now_et is not None
                else datetime.now(timezone.utc).replace(second=0, microsecond=0)
            )
            idem_key = make_job_key(
                strategy=self.tactic_name,
                symbol=symbol,
                trigger_time=trigger_time,
            )

            log.info(
                "[StraddleBuy.should_enter] エントリー候補: %s "
                "IVR=%.1f earnings=%s days_until=%d key=%s",
                symbol, ivr, earnings_date_str, days_until, idem_key,
            )
            decisions.append(StraddleBuyEntryDecision(
                should_enter=True,
                symbol=symbol,
                earnings_date=earnings_date_str,
                ivr=ivr,
                reason=(
                    f"IVR={ivr:.1f}<{self._cfg.ivr_max} / "
                    f"days_until={days_until} / window=OK"
                ),
                idempotency_key=idem_key,
                quantity_call=1,
                quantity_put=1,
            ))

        return decisions

    def build_order(
        self,
        decision: StraddleBuyEntryDecision,
        leg: Literal["call", "put"] = "call",
        paper_mode: bool = True,
        capital_usd: float = 0.0,
    ) -> "OrderRequest":
        """ATM call または put の buy 発注オブジェクトを構築する。

        ATM straddle は call と put の 2 枚を別々に発注するため、
        leg 引数で call / put を指定して 2 回呼び出す。

        Args:
            decision:    StraddleBuyEntryDecision（should_enter=True 必須）
            leg:         "call" | "put"
            paper_mode:  True = paper 発注（PDT チェックスキップ）
            capital_usd: 口座資金額 USD（PDT 判定用。省略時 0.0）

        Returns:
            OrderRequest（side="buy"）

        Raises:
            ValueError:      should_enter=False または無効な leg
            PDTBlockedError: PDT 上限到達で発注ブロックの場合
        """
        if not decision.should_enter:
            raise ValueError(
                f"[StraddleBuy.build_order] should_enter=False: {decision}"
            )
        if leg not in ("call", "put"):
            raise ValueError(
                f"[StraddleBuy.build_order] invalid leg={leg!r}. Must be 'call' or 'put'"
            )
        from atlas_v3.core.engine import OrderRequest

        quantity = (
            decision.quantity_call if leg == "call" else decision.quantity_put
        )
        from common_v3.risk.pre_trade_check import OrderCtx as _Ctx, check_order_critical_only as _gate
        _option_price = getattr(decision, "option_price", 0.0) or 0.0
        _gr = _gate(_Ctx(symbol=decision.symbol, qty=quantity, side="BUY", is_long=True,
                         option_price=float(_option_price)))
        if not _gr.allowed:
            raise ValueError(f"[StraddleBuy.build_order] PreTradeGate BLOCKED: {_gr.reason}")

        guard = PDTGuard(paper_mode=paper_mode, capital_usd=capital_usd)
        result = guard.check_can_trade(decision.symbol)
        if not result.allowed:
            raise PDTBlockedError(f"PDT blocked: {result.reason}")
        # call/put で冪等性キーを区別するためサフィックスを付与
        idem_key = f"{decision.idempotency_key}_{leg}"

        return OrderRequest(
            symbol=f"{decision.symbol}_{leg.upper()}",  # e.g. "NVDA_CALL"
            side="buy",
            quantity=quantity,
            order_type="limit",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    def mark_entered(self, symbol: str, idempotency_key: str) -> None:
        """エントリー完了を state に記録する（冪等性保証）。

        build_order → broker 送信成功後に呼ぶ。
        """
        self._entered_today[symbol] = idempotency_key
        log.info(
            "[StraddleBuy.mark_entered] %s: entered_today 記録 key=%s",
            symbol, idempotency_key,
        )

    def should_exit(
        self,
        position: StraddlePosition,
        env: MarketEnvironment,
        now_et: Optional[datetime] = None,
    ) -> StraddleBuyExitDecision:
        """エグジット判定。

        判定順（先着優先）:
        1. Kill Switch ARMED → force_close
        2. time-based: 決算発表日 open + exit_open_offset_min 以降 → post_earnings_time_exit
        3. profit_target 到達（unrealized_pnl >= entry_value * profit_target_pct）
        4. stop_loss 超過（unrealized_pnl <= -entry_value * stop_loss_pct）

        Args:
            position: StraddlePosition（entry_value 設定済みであること）
            env:      MarketEnvironment
            now_et:   現在 ET 時刻（None=テスト用・time-based exit スキップ）

        Returns:
            StraddleBuyExitDecision
        """
        # 1. Kill Switch
        if kill_switch_is_active():
            log.warning(
                "[StraddleBuy.should_exit] Kill Switch ARMED: 強制クローズ (%s)",
                position.symbol,
            )
            return StraddleBuyExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        # 2. time-based: 決算 open + offset 以降
        if position.earnings_open_dt is not None and now_et is not None:
            exit_after = position.earnings_open_dt + timedelta(
                minutes=self._cfg.exit_open_offset_min
            )
            if now_et >= exit_after:
                log.info(
                    "[StraddleBuy.should_exit] time-based exit: %s "
                    "now=%s >= open+%dmin=%s",
                    position.symbol,
                    now_et.isoformat(),
                    self._cfg.exit_open_offset_min,
                    exit_after.isoformat(),
                )
                return StraddleBuyExitDecision(
                    should_exit=True,
                    reason=(
                        f"post_earnings open+{self._cfg.exit_open_offset_min}min: "
                        f"earnings_open={position.earnings_open_dt.isoformat()}"
                    ),
                    exit_type="post_earnings_time_exit",
                )

        # entry_value が未設定の場合は P&L 判定不能
        if position.entry_value <= 0:
            log.warning(
                "[StraddleBuy.should_exit] entry_value=0: P&L exit 判定不能 (%s)",
                position.symbol,
            )
            return StraddleBuyExitDecision(
                should_exit=False, reason="entry_value_not_set"
            )

        profit_threshold = position.entry_value * self._cfg.profit_target_pct
        loss_threshold = -(position.entry_value * self._cfg.stop_loss_pct)

        # 3. profit_target
        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[StraddleBuy.should_exit] 利確: pnl=%.4f >= target=%.4f (%s)",
                position.unrealized_pnl, profit_threshold, position.symbol,
            )
            return StraddleBuyExitDecision(
                should_exit=True,
                reason=f"profit_target: pnl={position.unrealized_pnl:.4f}",
                exit_type="profit_target",
            )

        # 4. stop_loss
        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[StraddleBuy.should_exit] 損切り: pnl=%.4f <= stop=%.4f (%s)",
                position.unrealized_pnl, loss_threshold, position.symbol,
            )
            return StraddleBuyExitDecision(
                should_exit=True,
                reason=f"stop_loss: pnl={position.unrealized_pnl:.4f}",
                exit_type="stop_loss",
            )

        return StraddleBuyExitDecision(
            should_exit=False, reason="holding", exit_type="none"
        )

    def build_exit_order(
        self,
        position: StraddlePosition,
        decision: StraddleBuyExitDecision,
        leg: Literal["call", "put"] = "call",
    ) -> "OrderRequest":
        """エグジット（call または put の sell）発注オブジェクトを構築する。

        エントリーと同様、call/put を leg 引数で指定して 2 回呼び出す。

        Args:
            position: StraddlePosition
            decision: StraddleBuyExitDecision（should_exit=True 必須）
            leg:      "call" | "put"

        Returns:
            OrderRequest（side="sell"）

        Raises:
            ValueError: should_exit=False または無効な leg
        """
        if not decision.should_exit:
            raise ValueError(
                f"[StraddleBuy.build_exit_order] should_exit=False"
            )
        if leg not in ("call", "put"):
            raise ValueError(
                f"[StraddleBuy.build_exit_order] invalid leg={leg!r}"
            )

        from atlas_v3.core.engine import OrderRequest

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit_{leg}",
            symbol=position.symbol,
            trigger_time=trigger_time,
        )

        return OrderRequest(
            symbol=f"{position.symbol}_{leg.upper()}",
            side="sell",
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def persist_state(self, storage: Any) -> None:
        """earnings_calendar・entered_today を StorageBackend に永続化する。"""
        state_data = {
            "earnings_calendar": self._earnings_calendar,
            "entered_today": self._entered_today,
            "persisted_at": datetime.now(timezone.utc).isoformat(),
        }
        storage.save(self._STATE_KEY, state_data)
        log.debug(
            "[StraddleBuy.persist_state] 永続化完了: cal=%d 銘柄 entered=%d 銘柄",
            len(self._earnings_calendar),
            len(self._entered_today),
        )

    def restore_state(self, storage: Any) -> None:
        """StorageBackend から state を復元する。"""
        data = storage.load(self._STATE_KEY)
        if data is None:
            log.info("[StraddleBuy.restore_state] 保存 state なし: 初期状態を使用")
            return
        self._earnings_calendar.update(data.get("earnings_calendar", {}))
        self._entered_today.update(data.get("entered_today", {}))
        log.info(
            "[StraddleBuy.restore_state] state 復元: cal=%d 銘柄 entered=%d 銘柄 (saved: %s)",
            len(self._earnings_calendar),
            len(self._entered_today),
            data.get("persisted_at", "unknown"),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _in_entry_window(self, now_et: datetime) -> bool:
        """now_et が ET 15:00–15:45 のエントリーウィンドウ内か判定する。"""
        start_minutes = (
            self._cfg.entry_window_start_hour * 60 + self._cfg.entry_window_start_min
        )
        end_minutes = (
            self._cfg.entry_window_end_hour * 60 + self._cfg.entry_window_end_min
        )
        now_minutes = now_et.hour * 60 + now_et.minute
        return start_minutes <= now_minutes <= end_minutes
