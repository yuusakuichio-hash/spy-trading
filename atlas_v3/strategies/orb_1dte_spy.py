"""atlas_v3/strategies/orb_1dte_spy.py — ORB 1DTE 戦術（Type C: StateCarrying）

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 L177-L191
     atlas_spec A3 #7: 方向性ある朝・ORB手法。月利 2.71% (BT根拠: memory/project_atlas_monthly_rate_v6.md)

戦術分類: Type C (state_carrying)
理由: ORB レンジ（09:30-09:45 ET の High/Low）を state として保持・再起動耐性が必要。

StateCarryingTactic Protocol 実装:
    observe(env, market_data) → ORB レンジ観測・state 保持
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

注意: 本ファイルは SPY 特化設計だが symbol 固定化禁止規律に従い
      symbol_candidates をパラメータで受け取る（QQQ/IWM も将来対応可能）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# StorageBackend Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class StorageBackend(Protocol):
    """state 永続化バックエンド Protocol。"""
    def save(self, key: str, data: dict) -> None: ...
    def load(self, key: str) -> dict | None: ...


# ---------------------------------------------------------------------------
# ORB Range state
# ---------------------------------------------------------------------------

@dataclass
class ORBRange:
    """Opening Range Breakout のレンジ情報（09:30-09:45 ET）。

    Attributes:
        high:        ORB high (確定後)
        low:         ORB low (確定後)
        is_confirmed: True = ORB 観測完了
        observed_at:  観測タイムスタンプ（UTC）
        symbol:      観測対象銘柄
    """
    high: float = 0.0
    low: float = 0.0
    is_confirmed: bool = False
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    symbol: str = ""


# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ORB1DTEConfig:
    """ORB 1DTE 設定。

    Attributes:
        slippage_tolerance_bps:  スリッページ許容幅（basis points）
        breakout_buffer_pct:     ブレイクアウト確認バッファ（ORBレンジ比 %）
        vix_max:                 エントリー最大 VIX
        profit_target_pct:       利確目標（エントリー価値比 %）
        stop_loss_pct:           損切り水準（エントリー価値比 %）
        orb_window_minutes:      ORB観測窓（分）
        dte_target:              1DTE 専用（固定1）
    """
    slippage_tolerance_bps: int = 20
    breakout_buffer_pct: float = 0.001   # 0.1% buffer above/below ORB
    vix_max: float = 30.0
    profit_target_pct: float = 1.00      # 100% gain (double)
    stop_loss_pct: float = 0.50          # 50% loss
    orb_window_minutes: int = 15         # 09:30-09:45 ET
    dte_target: int = 1                  # 1 DTE 固定


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ORBEntryDecision:
    """ORB エントリー決定。"""
    should_enter: bool
    symbol: str
    side: str = "buy"
    quantity: int = 1
    direction: Literal["call", "put", "none"] = "none"
    orb_high: float = 0.0
    orb_low: float = 0.0
    current_price: float = 0.0
    reason: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class ORBExitDecision:
    """ORB エグジット決定。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal["profit_target", "stop_loss", "force_close",
                        "eod_close", "none"] = "none"


# ---------------------------------------------------------------------------
# Position stub
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """ポジション表現（最小 stub）。"""
    symbol: str
    quantity: int
    entry_price: float
    current_price: float = 0.0
    tactic_name: str = ""
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: float = 0.0
    entry_value: float = 0.0
    direction: str = "none"   # "call" or "put"


# ---------------------------------------------------------------------------
# ORB1DTESPYTactic — Type C: StateCarrying
# ---------------------------------------------------------------------------

class ORB1DTESPYTactic(TacticBase):
    """ORB 1DTE 戦術（Type C: state_carrying）。

    09:30-09:45 ET の ORB レンジを観測し、ブレイクアウト方向に
    当日満期（1DTE）オプションのエントリーを行う。

    SPY 対象として設計されているが symbol_candidates を受け取るため
    銘柄固定化禁止規律に準拠している。

    Args:
        config:  ORB1DTEConfig（slippage_tolerance_bps 必須）
    """

    _STATE_KEY = "orb_1dte_state"

    def __init__(self, config: ORB1DTEConfig | None = None) -> None:
        self._cfg = config or ORB1DTEConfig()
        # symbol → ORBRange の辞書（複数銘柄対応）
        self._orb_ranges: dict[str, ORBRange] = {}

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "state_carrying"

    @property
    def tactic_name(self) -> str:
        return "orb_1dte_spy"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック:
        1. Kill Switch ARMED → False
        2. VIX > vix_max → False（高恐怖環境は 1DTE ORB 非適合）
        3. bias が neutral → False（ORBは方向性必須）
        4. env None → False

        Returns:
            True — 戦術発動可能
        """
        if env is None:
            log.warning("[ORB1DTESPYTactic.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[ORB1DTESPYTactic.preflight] Kill Switch ARMED: orb_1dte_spy 無効化"
            )
            return False

        if env.vix > self._cfg.vix_max:
            log.info(
                "[ORB1DTESPYTactic.preflight] VIX=%.1f > max=%.1f: 高恐怖環境でスキップ",
                env.vix, self._cfg.vix_max,
            )
            return False

        if env.bias == "neutral":
            log.info(
                "[ORB1DTESPYTactic.preflight] bias=neutral: ORB は方向性必須・スキップ"
            )
            return False

        return True

    # ------------------------------------------------------------------
    # StateCarryingTactic Protocol 実装
    # ------------------------------------------------------------------

    def observe(self, env: MarketEnvironment, market_data: Any) -> None:
        """ORB レンジを観測・更新する（09:30-09:45 ET 相当）。

        market_data に get_orb_range(symbol, window_minutes) が実装済みの場合は使用。
        なければ既存 state を保持。

        Phase 2 で moomoo/futu 分足データとの連携を追加。

        Args:
            env:         現在の市場環境
            market_data: MarketDataClient（Phase 2 で futu 分足データ連携）
        """
        if not hasattr(market_data, "get_orb_range"):
            log.debug(
                "[ORB1DTESPYTactic.observe] market_data.get_orb_range 未実装: "
                "既存 ORB state を保持 (%d 銘柄)",
                len(self._orb_ranges),
            )
            return

        # market_data 提供銘柄リストを取得（なければ既存 key を使用）
        symbols = getattr(market_data, "tracked_symbols", list(self._orb_ranges.keys()))

        for symbol in symbols:
            try:
                orb_data: dict = market_data.get_orb_range(
                    symbol, window_minutes=self._cfg.orb_window_minutes
                )
                orb = ORBRange(
                    high=orb_data["high"],
                    low=orb_data["low"],
                    is_confirmed=orb_data.get("is_confirmed", False),
                    observed_at=datetime.now(timezone.utc),
                    symbol=symbol,
                )
                self._orb_ranges[symbol] = orb
                log.info(
                    "[ORB1DTESPYTactic.observe] ORB 更新: %s H=%.2f L=%.2f confirmed=%s",
                    symbol, orb.high, orb.low, orb.is_confirmed,
                )
            except Exception as exc:
                log.error(
                    "[ORB1DTESPYTactic.observe] ORB 取得失敗: %s %s (既存 state 保持)",
                    symbol, exc,
                )
                raise

    def should_enter(
        self,
        env: MarketEnvironment,
        symbol_candidates: list[str],
    ) -> list[ORBEntryDecision]:
        """複数 symbol 評価・ORB ブレイクアウト判定。

        ORBRange が確定済みかつ現在価格がブレイクアウトしている場合にエントリー。
        現在価格は env.ivr_by_symbol から proxy で取得（Phase 2 で market_data から取得）。

        Args:
            env:               現在の市場環境
            symbol_candidates: SymbolSelector から渡された候補銘柄リスト

        Returns:
            エントリー可能な銘柄の決定リスト
        """
        decisions: list[ORBEntryDecision] = []

        for symbol in symbol_candidates:
            orb = self._orb_ranges.get(symbol)
            decision = self._evaluate_symbol(env, symbol, orb)
            decisions.append(decision)

        return decisions

    def _evaluate_symbol(
        self,
        env: MarketEnvironment,
        symbol: str,
        orb: ORBRange | None,
    ) -> ORBEntryDecision:
        """単一銘柄の ORB エントリー評価。CC ≤ 20 のため抽出。"""
        if orb is None or not orb.is_confirmed:
            return ORBEntryDecision(
                should_enter=False,
                symbol=symbol,
                reason="ORB 未確定・観測待ち",
            )

        # 現在価格の proxy（Phase 2 で market_data から取得）
        # IVR を normalized として代替（本実装では placeholder）
        current_price = orb.high  # Phase 2 で market_data.get_quote() に差し替え

        buffer = orb.high * self._cfg.breakout_buffer_pct

        if current_price > orb.high + buffer:
            direction: Literal["call", "put", "none"] = "call"
        elif current_price < orb.low - buffer:
            direction = "put"
        else:
            log.debug(
                "[ORB1DTESPYTactic._evaluate_symbol] %s: ブレイクアウトなし "
                "(price=%.2f ORB=%.2f-%.2f)",
                symbol, current_price, orb.low, orb.high,
            )
            return ORBEntryDecision(
                should_enter=False,
                symbol=symbol,
                orb_high=orb.high,
                orb_low=orb.low,
                current_price=current_price,
                reason=f"ブレイクアウトなし: price={current_price:.2f}",
            )

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[ORB1DTESPYTactic._evaluate_symbol] ORB ブレイクアウト: %s "
            "direction=%s price=%.2f ORB=%.2f-%.2f key=%s",
            symbol, direction, current_price, orb.low, orb.high, idem_key,
        )
        return ORBEntryDecision(
            should_enter=True,
            symbol=symbol,
            side="buy",
            quantity=1,
            direction=direction,
            orb_high=orb.high,
            orb_low=orb.low,
            current_price=current_price,
            reason=f"ORB breakout {direction}: price={current_price:.2f} "
                   f"range={orb.low:.2f}-{orb.high:.2f}",
            idempotency_key=idem_key,
        )

    def build_order(self, decision: ORBEntryDecision) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest

        if not decision.should_enter:
            raise ValueError(
                f"[ORB1DTESPYTactic.build_order] should_enter=False: {decision}"
            )

        return OrderRequest(
            symbol=decision.symbol,
            side="buy",
            quantity=decision.quantity,
            order_type="limit",
            tactic_name=self.tactic_name,
            idempotency_key=decision.idempotency_key,
        )

    def should_exit(
        self, position: Position, env: MarketEnvironment
    ) -> ORBExitDecision:
        """エグジット判定。

        判定順:
        1. Kill Switch ARMED → force_close
        2. profit_target 到達 → 利確
        3. stop_loss 超過 → 損切り

        Args:
            position: 現在ポジション（entry_value 設定済みであること）
            env:      現在の市場環境
        """
        if kill_switch_is_active():
            log.warning(
                "[ORB1DTESPYTactic.should_exit] Kill Switch ARMED: 強制クローズ (%s)",
                position.symbol,
            )
            return ORBExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        if position.entry_value <= 0:
            log.warning(
                "[ORB1DTESPYTactic.should_exit] entry_value=0: exit 判定不能 (%s)",
                position.symbol,
            )
            return ORBExitDecision(should_exit=False, reason="entry_value_not_set")

        profit_threshold = position.entry_value * self._cfg.profit_target_pct
        loss_threshold = -position.entry_value * self._cfg.stop_loss_pct

        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[ORB1DTESPYTactic.should_exit] 利確: pnl=%.2f >= target=%.2f (%s)",
                position.unrealized_pnl, profit_threshold, position.symbol,
            )
            return ORBExitDecision(
                should_exit=True,
                reason=f"profit_target: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[ORB1DTESPYTactic.should_exit] 損切り: pnl=%.2f <= stop=%.2f (%s)",
                position.unrealized_pnl, loss_threshold, position.symbol,
            )
            return ORBExitDecision(
                should_exit=True,
                reason=f"stop_loss: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        return ORBExitDecision(should_exit=False, reason="holding", exit_type="none")

    def build_exit_order(
        self, position: Position, decision: ORBExitDecision
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest

        if not decision.should_exit:
            raise ValueError(
                f"[ORB1DTESPYTactic.build_exit_order] should_exit=False"
            )

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit",
            symbol=position.symbol,
            trigger_time=trigger_time,
        )

        return OrderRequest(
            symbol=position.symbol,
            side="sell",    # options long position の close は sell_to_close
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )

    def persist_state(self, storage: StorageBackend) -> None:
        """ORB レンジ state を StorageBackend に永続化する（再起動耐性）。"""
        state_data = {
            "orb_ranges": {
                sym: {
                    "high": orb.high,
                    "low": orb.low,
                    "is_confirmed": orb.is_confirmed,
                    "observed_at": orb.observed_at.isoformat(),
                    "symbol": orb.symbol,
                }
                for sym, orb in self._orb_ranges.items()
            },
            "persisted_at": datetime.now(timezone.utc).isoformat(),
        }
        storage.save(self._STATE_KEY, state_data)
        log.debug(
            "[ORB1DTESPYTactic.persist_state] state 永続化: %d 銘柄",
            len(self._orb_ranges),
        )

    def restore_state(self, storage: StorageBackend) -> None:
        """StorageBackend から ORB state を復元する（再起動後の継続）。"""
        data = storage.load(self._STATE_KEY)
        if data is None:
            log.info("[ORB1DTESPYTactic.restore_state] 保存 state なし: 初期状態を使用")
            return

        for sym, raw in data.get("orb_ranges", {}).items():
            try:
                self._orb_ranges[sym] = ORBRange(
                    high=raw["high"],
                    low=raw["low"],
                    is_confirmed=raw["is_confirmed"],
                    observed_at=datetime.fromisoformat(raw["observed_at"]),
                    symbol=raw["symbol"],
                )
            except (KeyError, ValueError) as exc:
                log.error(
                    "[ORB1DTESPYTactic.restore_state] state 復元失敗 (%s): %s",
                    sym, exc,
                )
                raise

        log.info(
            "[ORB1DTESPYTactic.restore_state] state 復元: %d 銘柄 (保存: %s)",
            len(self._orb_ranges),
            data.get("persisted_at", "unknown"),
        )
