"""atlas_v3/strategies/earnings_iv_crush.py — Earnings IV Crush 戦術（Type C: StateCarrying）

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 L177-L191
     atlas_spec A3 #9: 決算日・IV低下狙い。月利 5.39% (BT根拠: memory/project_atlas_monthly_rate_v6.md)

戦術分類: Type C (state_carrying)
理由: 決算対象銘柄リスト（Finnhub取得）を state として保持・再起動耐性が必要。
     複数銘柄を同時評価して候補リストを返す。

StateCarryingTactic Protocol 実装:
    observe(env, market_data) → 決算カレンダー更新・state 保持
    should_enter(env, symbol_candidates) → list[EntryDecision]
    build_order(decision) → OrderRequest
    should_exit(position, env) → ExitDecision
    build_exit_order(position, decision) → OrderRequest
    persist_state(storage) → StorageBackend への永続化

必須要件:
- TacticBase ABC 継承
- slippage_tolerance_bps config 必須
- idempotency_key は OrderRequest に必ず設定
- kill_switch 連動
- CC ≤ 20 規律
"""
from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# StorageBackend Protocol（B5 StateCarryingTactic.persist_state 用）
# ---------------------------------------------------------------------------

@runtime_checkable
class StorageBackend(Protocol):
    """state 永続化バックエンド Protocol（Phase 2 で concrete 実装に差し替え）。"""

    def save(self, key: str, data: dict) -> None: ...
    def load(self, key: str) -> dict | None: ...


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EarningsIVCrushConfig:
    """Earnings IV Crush 設定。

    Attributes:
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        days_before_earnings:   決算日前のエントリー日数
        days_after_earnings:    決算後のエグジット猶予日数
        iv_crush_target_pct:    IV低下率ターゲット（%）
        vix_max:                エントリー最大 VIX
        ivr_min:                決算前最低 IVR（IV が高いほど crush 効果大）
        max_symbols_per_day:    1日最大参戦銘柄数
        profit_target_pct:      利確目標（エントリー価値比 %）
        stop_loss_pct:          損切り水準（エントリー価値比 %）
    """
    slippage_tolerance_bps: int = 15
    days_before_earnings: int = 1
    days_after_earnings: int = 1
    iv_crush_target_pct: float = 0.30     # 30% IV 低下を期待
    vix_max: float = 35.0
    ivr_min: float = 50.0                 # 高 IVR 銘柄のみ参戦
    max_symbols_per_day: int = 3
    profit_target_pct: float = 0.40       # 40% of entry value
    stop_loss_pct: float = 1.50           # 150% of entry value


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EarningsEntryDecision:
    """Earnings IV Crush エントリー決定。"""
    should_enter: bool
    symbol: str
    side: str = "sell"
    quantity: int = 1
    earnings_date: str = ""   # ISO date str
    reason: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class EarningsExitDecision:
    """Earnings IV Crush エグジット決定。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal["profit_target", "stop_loss", "force_close",
                        "post_earnings_close", "none"] = "none"


# ---------------------------------------------------------------------------
# Position stub
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """ポジション表現（Phase 2 MoomooClient との接続前の最小 stub）。"""
    symbol: str
    quantity: int
    entry_price: float
    current_price: float = 0.0
    tactic_name: str = ""
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: float = 0.0
    entry_value: float = 0.0             # エントリー時のオプション価値
    earnings_date: str = ""              # ISO date str


# ---------------------------------------------------------------------------
# EarningsIVCrushTactic — Type C: StateCarrying
# ---------------------------------------------------------------------------

class EarningsIVCrushTactic(TacticBase):
    """Earnings IV Crush 戦術（Type C: state_carrying）。

    StateCarryingTactic Protocol を実装し、TacticBase ABC を継承する。
    決算対象銘柄リストを state として保持し、再起動後も継続できる。

    Args:
        config:          EarningsIVCrushConfig
        earnings_symbols: 初期決算カレンダー（テスト注入用。本番は observe() で更新）
    """

    # state persistence key
    _STATE_KEY = "earnings_iv_crush_state"

    def __init__(
        self,
        config: EarningsIVCrushConfig | None = None,
        earnings_symbols: dict[str, str] | None = None,
    ) -> None:
        self._cfg = config or EarningsIVCrushConfig()
        # {symbol: earnings_date_iso_str}
        self._earnings_calendar: dict[str, str] = earnings_symbols or {}

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "state_carrying"

    @property
    def tactic_name(self) -> str:
        return "earnings_iv_crush"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック:
        1. Kill Switch ARMED → False
        2. VIX > vix_max → False（極端な恐怖環境では決算プレイ回避）
        3. env None → False

        Returns:
            True — 戦術発動可能
        """
        if env is None:
            log.warning("[EarningsIVCrushTactic.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[EarningsIVCrushTactic.preflight] Kill Switch ARMED: earnings_iv_crush 無効化"
            )
            return False

        if env.vix > self._cfg.vix_max:
            log.info(
                "[EarningsIVCrushTactic.preflight] VIX=%.1f > max=%.1f: 高恐怖環境でスキップ",
                env.vix, self._cfg.vix_max,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # StateCarryingTactic Protocol 実装
    # ------------------------------------------------------------------

    def observe(self, env: MarketEnvironment, market_data: Any) -> None:
        """決算カレンダーを更新する（state 更新。Phase 2 で Finnhub API 連携）。

        Phase 1: market_data に get_earnings_calendar(date) が実装済みの場合は使用。
                 なければ現在の _earnings_calendar をそのまま保持。

        Args:
            env:         現在の市場環境
            market_data: MarketDataClient（Phase 2 で Finnhub 連携）
        """
        if hasattr(market_data, "get_earnings_calendar"):
            today = date.today().isoformat()
            try:
                new_calendar: dict[str, str] = market_data.get_earnings_calendar(today)
                self._earnings_calendar.update(new_calendar)
                log.info(
                    "[EarningsIVCrushTactic.observe] 決算カレンダー更新: %d 銘柄",
                    len(self._earnings_calendar),
                )
            except Exception as exc:
                log.error(
                    "[EarningsIVCrushTactic.observe] カレンダー取得失敗: %s (既存 state 保持)",
                    exc,
                )
                raise
        else:
            log.debug(
                "[EarningsIVCrushTactic.observe] market_data.get_earnings_calendar 未実装: "
                "既存 state を保持 (%d 銘柄)",
                len(self._earnings_calendar),
            )

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol_candidates: list[str],
    ) -> list[EarningsEntryDecision]:
        """複数 symbol 同時評価・エントリー候補リストを返す。

        フィルタ基準:
        1. _earnings_calendar に登録済み
        2. IVR >= ivr_min
        3. max_symbols_per_day を超えない

        Args:
            env:               現在の市場環境
            symbol_candidates: SymbolSelector から渡された候補銘柄リスト

        Returns:
            エントリー可能な銘柄の決定リスト
        """
        decisions: list[EarningsEntryDecision] = []
        today = date.today().isoformat()

        for symbol in symbol_candidates:
            if len(decisions) >= self._cfg.max_symbols_per_day:
                log.info(
                    "[EarningsIVCrushTactic.should_enter] max_symbols=%d 到達: 追加評価スキップ",
                    self._cfg.max_symbols_per_day,
                )
                break

            earnings_date = self._earnings_calendar.get(symbol)
            if not earnings_date:
                continue

            ivr = env.ivr_by_symbol.get(symbol, 0.0)
            if ivr < self._cfg.ivr_min:
                log.debug(
                    "[EarningsIVCrushTactic.should_enter] %s: IVR=%.1f < min=%.1f: スキップ",
                    symbol, ivr, self._cfg.ivr_min,
                )
                decisions.append(EarningsEntryDecision(
                    should_enter=False,
                    symbol=symbol,
                    earnings_date=earnings_date,
                    reason=f"IVR={ivr:.1f}<{self._cfg.ivr_min}",
                ))
                continue

            trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            idem_key = make_job_key(
                strategy=self.tactic_name,
                symbol=symbol,
                trigger_time=trigger_time,
            )

            log.info(
                "[EarningsIVCrushTactic.should_enter] エントリー候補: %s "
                "IVR=%.1f earnings=%s key=%s",
                symbol, ivr, earnings_date, idem_key,
            )
            decisions.append(EarningsEntryDecision(
                should_enter=True,
                symbol=symbol,
                side="sell",
                quantity=1,
                earnings_date=earnings_date,
                reason=f"IVR={ivr:.1f}>={self._cfg.ivr_min} / earnings={earnings_date}",
                idempotency_key=idem_key,
            ))

        return decisions

    def build_order(self, decision: EarningsEntryDecision) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest

        if not decision.should_enter:
            raise ValueError(
                f"[EarningsIVCrushTactic.build_order] should_enter=False: {decision}"
            )

        return OrderRequest(
            symbol=decision.symbol,
            side="sell",
            quantity=decision.quantity,
            order_type="limit",
            tactic_name=self.tactic_name,
            idempotency_key=decision.idempotency_key,
        )

    def should_exit(
        self, position: Position, env: MarketEnvironment
    ) -> EarningsExitDecision:
        """エグジット判定。

        判定順:
        1. Kill Switch ARMED → force_close
        2. 決算後 days_after_earnings 経過 → post_earnings_close
        3. profit_target 到達 → 利確
        4. stop_loss 超過 → 損切り

        Args:
            position: 現在ポジション（entry_value・earnings_date 設定済みであること）
            env:      現在の市場環境
        """
        if kill_switch_is_active():
            log.warning(
                "[EarningsIVCrushTactic.should_exit] Kill Switch ARMED: 強制クローズ (%s)",
                position.symbol,
            )
            return EarningsExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        # 決算後クローズ判定
        if position.earnings_date:
            today = date.today().isoformat()
            if today > position.earnings_date:
                log.info(
                    "[EarningsIVCrushTactic.should_exit] 決算通過後クローズ: %s (earnings=%s)",
                    position.symbol, position.earnings_date,
                )
                return EarningsExitDecision(
                    should_exit=True,
                    reason=f"post_earnings: earnings={position.earnings_date}",
                    exit_type="post_earnings_close",
                )

        if position.entry_value <= 0:
            log.warning(
                "[EarningsIVCrushTactic.should_exit] entry_value=0: exit 判定不能 (%s)",
                position.symbol,
            )
            return EarningsExitDecision(should_exit=False, reason="entry_value_not_set")

        profit_threshold = position.entry_value * self._cfg.profit_target_pct
        loss_threshold = -position.entry_value * self._cfg.stop_loss_pct

        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[EarningsIVCrushTactic.should_exit] 利確: pnl=%.2f >= target=%.2f (%s)",
                position.unrealized_pnl, profit_threshold, position.symbol,
            )
            return EarningsExitDecision(
                should_exit=True,
                reason=f"profit_target: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[EarningsIVCrushTactic.should_exit] 損切り: pnl=%.2f <= stop=%.2f (%s)",
                position.unrealized_pnl, loss_threshold, position.symbol,
            )
            return EarningsExitDecision(
                should_exit=True,
                reason=f"stop_loss: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        return EarningsExitDecision(should_exit=False, reason="holding", exit_type="none")

    def build_exit_order(
        self, position: Position, decision: EarningsExitDecision
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest

        if not decision.should_exit:
            raise ValueError(
                f"[EarningsIVCrushTactic.build_exit_order] should_exit=False"
            )

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit",
            symbol=position.symbol,
            trigger_time=trigger_time,
        )

        return OrderRequest(
            symbol=position.symbol,
            side="buy",
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    def persist_state(self, storage: StorageBackend) -> None:
        """決算カレンダー state を StorageBackend に永続化する（再起動耐性）。

        Args:
            storage: StorageBackend Protocol 実装（Phase 2 で S3/Redis 等に差し替え）
        """
        state_data = {
            "earnings_calendar": self._earnings_calendar,
            "persisted_at": datetime.now(timezone.utc).isoformat(),
        }
        storage.save(self._STATE_KEY, state_data)
        log.debug(
            "[EarningsIVCrushTactic.persist_state] state 永続化完了: %d 銘柄",
            len(self._earnings_calendar),
        )

    def restore_state(self, storage: StorageBackend) -> None:
        """StorageBackend から state を復元する（再起動後の継続）。

        Args:
            storage: StorageBackend Protocol 実装
        """
        data = storage.load(self._STATE_KEY)
        if data is None:
            log.info("[EarningsIVCrushTactic.restore_state] 保存 state なし: 初期状態を使用")
            return

        restored = data.get("earnings_calendar", {})
        self._earnings_calendar.update(restored)
        log.info(
            "[EarningsIVCrushTactic.restore_state] state 復元: %d 銘柄 (保存: %s)",
            len(self._earnings_calendar),
            data.get("persisted_at", "unknown"),
        )
