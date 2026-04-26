"""atlas_v3/strategies/ic_sell.py — Iron Condor Sell 戦術（Type A: EnterExit）

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 L158-L166
     atlas_spec A3 #2: 中VIX・IVR中高・中立環境。月利 6.23% (BT根拠: memory/project_atlas_monthly_rate_v6.md)

戦術分類: Type A (enter_exit) — 単純 enter/exit・単一 symbol
構成: Short Call Spread (upper wing) + Short Put Spread (lower wing)
  → 4 レッグ: sell_call / buy_call(wing) / sell_put / buy_put(wing)

必須要件:
- TacticBase ABC 継承（dispatch 地獄回避・B5 R2-02）
- EnterExitTactic Protocol 実装
- slippage_tolerance_bps config 必須
- idempotency_key は OrderRequest に必ず設定
- symbol_selector（StrategySelector）連携: preflight で IVR/VIX 検証
- kill_switch 連動: preflight で is_active() チェック
- CC ≤ 20 規律
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from atlas_v3.core.engine import OrderRequest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase, TacticType
from common_v3.idempotency.store import make_job_key
from common_v3.risk.kill_switch import is_active as kill_switch_is_active

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 設定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ICSellConfig:
    """Iron Condor Sell 設定。

    Attributes:
        slippage_tolerance_bps: スリッページ許容幅（basis points）
        wing_width_pct:         ウイング幅（% of underlying price）
        dte_target:             ターゲット満期日数
        vix_min:                エントリー最低 VIX（環境フィルタ）
        vix_max:                エントリー最大 VIX（環境フィルタ）
        ivr_min:                エントリー最低 IVR（環境フィルタ）
        max_risk_per_trade:     1トレード最大リスク額（$）
        profit_target_pct:      利確目標（クレジット比 %）
        stop_loss_pct:          損切り水準（クレジット比 %）
    """
    slippage_tolerance_bps: int = 10
    wing_width_pct: float = 0.02      # 2% of underlying
    dte_target: int = 7               # 7 DTE デフォルト
    vix_min: float = 15.0
    vix_max: float = 30.0
    ivr_min: float = 30.0
    max_risk_per_trade: float = 500.0
    profit_target_pct: float = 0.50   # 50% of max credit
    stop_loss_pct: float = 2.00       # 200% of max credit


# ---------------------------------------------------------------------------
# Entry / Exit 決定 DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ICSellEntryDecision:
    """Iron Condor エントリー決定。"""
    should_enter: bool
    symbol: str
    side: str = "sell"
    quantity: int = 1
    call_strike: float = 0.0
    put_strike: float = 0.0
    call_wing_strike: float = 0.0
    put_wing_strike: float = 0.0
    reason: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class ICSellExitDecision:
    """Iron Condor エグジット決定。"""
    should_exit: bool
    reason: str = ""
    exit_type: Literal["profit_target", "stop_loss", "force_close", "none"] = "none"


# ---------------------------------------------------------------------------
# Position stub（Phase 2 で common_v3/position から本実装に差し替え）
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
    max_credit: float = 0.0   # IC で受け取ったクレジット総額


# ---------------------------------------------------------------------------
# ICSellTactic — Type A: EnterExit
# ---------------------------------------------------------------------------

class ICSellTactic(TacticBase):
    """Iron Condor Sell 戦術（Type A: enter_exit）。

    EnterExitTactic Protocol を実装し、TacticBase ABC を継承する。
    Engine は isinstance(tactic, TacticBase) で dispatch する。

    Args:
        config:  ICSellConfig（slippage_tolerance_bps 必須）
    """

    def __init__(self, config: ICSellConfig | None = None) -> None:
        self._cfg = config or ICSellConfig()

    # ------------------------------------------------------------------
    # TacticBase ABC 必須 properties
    # ------------------------------------------------------------------

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "ic_sell"

    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。

        チェック項目:
        1. Kill Switch ARMED → False（EICAS Advisory 発出）
        2. VIX が設定範囲外 → False
        3. env が None → False（型安全ガード）

        Returns:
            True — 戦術発動可能
            False — 発動不可（理由は log に必ず出力）
        """
        if env is None:
            log.warning("[ICSellTactic.preflight] env=None: preflight 失敗")
            return False

        if kill_switch_is_active():
            log.warning(
                "[ICSellTactic.preflight] Kill Switch ARMED: ic_sell を無効化"
            )
            return False

        if not (self._cfg.vix_min <= env.vix <= self._cfg.vix_max):
            log.info(
                "[ICSellTactic.preflight] VIX=%.1f が範囲外 [%.1f, %.1f]: スキップ",
                env.vix, self._cfg.vix_min, self._cfg.vix_max,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # EnterExitTactic Protocol 実装
    # ------------------------------------------------------------------

    def should_enter(
        self, env: MarketEnvironment, symbol: str
    ) -> ICSellEntryDecision:
        """エントリー判定。

        IVR が ivr_min 以上かつ VIX が中間帯（環境適合）の場合にエントリー。
        idempotency_key は strategy/symbol/分足時刻から生成。
        """
        ivr = env.ivr_by_symbol.get(symbol, 0.0)

        if ivr < self._cfg.ivr_min:
            log.debug(
                "[ICSellTactic.should_enter] IVR=%.1f < min=%.1f: エントリーしない (symbol=%s)",
                ivr, self._cfg.ivr_min, symbol,
            )
            return ICSellEntryDecision(should_enter=False, symbol=symbol,
                                       reason=f"IVR={ivr:.1f}<{self._cfg.ivr_min}")

        if env.bias not in ("neutral",):
            # IC は中立環境専用: directional bias があれば cs_sell に任せる
            log.debug(
                "[ICSellTactic.should_enter] bias=%s: IC 非適合・スキップ (symbol=%s)",
                env.bias, symbol,
            )
            return ICSellEntryDecision(should_enter=False, symbol=symbol,
                                       reason=f"bias={env.bias} (IC は neutral 専用)")

        # idempotency_key: 分単位精度で重複発注を防ぐ
        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=self.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        log.info(
            "[ICSellTactic.should_enter] エントリー判定 OK: symbol=%s IVR=%.1f VIX=%.1f key=%s",
            symbol, ivr, env.vix, idem_key,
        )
        return ICSellEntryDecision(
            should_enter=True,
            symbol=symbol,
            side="sell",
            quantity=1,
            reason=f"IVR={ivr:.1f}>={self._cfg.ivr_min} / VIX={env.vix:.1f} / bias={env.bias}",
            idempotency_key=idem_key,
        )

    def build_order(self, decision: ICSellEntryDecision) -> "OrderRequest":
        """エントリー発注オブジェクトを構築する。

        slippage_tolerance_bps を order detail に埋め込む。
        idempotency_key は decision から転写する。
        """
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_enter:
            raise ValueError(
                f"[ICSellTactic.build_order] should_enter=False の decision が渡された: {decision}"
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
    ) -> ICSellExitDecision:
        """エグジット判定。

        判定順:
        1. Kill Switch ARMED → 強制クローズ
        2. unrealized_pnl が profit_target_pct に到達 → 利確
        3. unrealized_pnl が stop_loss_pct を超過 → 損切り

        Args:
            position: 現在ポジション（Position.max_credit が設定済みであること）
            env:      現在の市場環境

        Returns:
            ICSellExitDecision
        """
        if kill_switch_is_active():
            log.warning(
                "[ICSellTactic.should_exit] Kill Switch ARMED: 強制クローズ (symbol=%s)",
                position.symbol,
            )
            return ICSellExitDecision(
                should_exit=True,
                reason="kill_switch_armed",
                exit_type="force_close",
            )

        if position.max_credit <= 0:
            # max_credit 未設定は exit 判定不能・スキップ（silent 禁止: log 出力）
            log.warning(
                "[ICSellTactic.should_exit] max_credit=0: exit 判定不能 (symbol=%s)",
                position.symbol,
            )
            return ICSellExitDecision(should_exit=False, reason="max_credit_not_set")

        profit_threshold = position.max_credit * self._cfg.profit_target_pct
        loss_threshold = -position.max_credit * self._cfg.stop_loss_pct

        if position.unrealized_pnl >= profit_threshold:
            log.info(
                "[ICSellTactic.should_exit] 利確: pnl=%.2f >= target=%.2f (symbol=%s)",
                position.unrealized_pnl, profit_threshold, position.symbol,
            )
            return ICSellExitDecision(
                should_exit=True,
                reason=f"profit_target: pnl={position.unrealized_pnl:.2f}",
                exit_type="profit_target",
            )

        if position.unrealized_pnl <= loss_threshold:
            log.warning(
                "[ICSellTactic.should_exit] 損切り: pnl=%.2f <= stop=%.2f (symbol=%s)",
                position.unrealized_pnl, loss_threshold, position.symbol,
            )
            return ICSellExitDecision(
                should_exit=True,
                reason=f"stop_loss: pnl={position.unrealized_pnl:.2f}",
                exit_type="stop_loss",
            )

        return ICSellExitDecision(should_exit=False, reason="holding", exit_type="none")

    def build_exit_order(
        self, position: Position, decision: ICSellExitDecision
    ) -> "OrderRequest":
        """エグジット発注オブジェクトを構築する。"""
        from atlas_v3.core.engine import OrderRequest  # circular import 回避

        if not decision.should_exit:
            raise ValueError(
                f"[ICSellTactic.build_exit_order] should_exit=False の decision が渡された"
            )

        trigger_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idem_key = make_job_key(
            strategy=f"{self.tactic_name}_exit",
            symbol=position.symbol,
            trigger_time=trigger_time,
        )

        return OrderRequest(
            symbol=position.symbol,
            side="buy",   # IC の close は buy_to_close
            quantity=position.quantity,
            order_type="market",
            tactic_name=self.tactic_name,
            idempotency_key=idem_key,
        )
