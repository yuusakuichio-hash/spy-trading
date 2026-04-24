"""atlas_v3/core/engine.py — AtlasEngine メインループ（Sprint 1-B Phase B B1）

仕様: data/specs/v3/atlas_spec_v3_20260422.md B1 L77-L85

責務:
- 戦術 dispatch 中核（isinstance(tactic, TacticBase) で統一）
- C-001 sync_only 整合（asyncio main loop 回避・sync context 専用）
- common_v3/risk/kill_switch.py 連携（main loop で is_active() 即停止）
- common_v3/idempotency/store.py で二重発注防止
- common_v3/self_healing/instances.py の moomoo_breaker 使用

禁則:
- asyncio event loop 内での直接呼び出し禁止（sync_guard が物理ブロック）
- TacticBase 未継承の戦術を dispatch した場合は TypeError（silent AttributeError 封鎖）
- preflight が False の戦術は silent skip 禁止（log 必須）

CC 規律: 各メソッド CC ≤ 20

Redteam r1 修正（2026-04-23）:
  C-r1-01: idempotency key 非決定 → tick 開始時刻 _tick_started_at で決定化
  C-r1-02: place_order 例外 + key 残存 → with_idempotency + OrderNotSentError wrap
  C-r1-03: moomoo_breaker 空振り → state == "OPEN" で BrokerUnavailable raise
  C-r1-04: kill_switch race → _submit_order_with_idempotency 冒頭で再チェック
  C-r1-05: preflight 例外道連れ → _dispatch_tactic を try/except で隔離（HIGH）
  C-r1-06: quantity sanity check → isinstance + 0 < q ≤ MAX_QUANTITY_PER_ORDER
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase
from common_v3.idempotency.store import (
    IdempotencyStore,
    OrderNotSentError,
    make_job_key,
    with_idempotency,
)
from common_v3.risk.kill_switch import is_active as kill_switch_is_active
from common_v3.self_healing.instances import moomoo_breaker

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: 1 発注あたりの最大 quantity（sanity guard / C-r1-06）
MAX_QUANTITY_PER_ORDER: int = 100


# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------

class BrokerUnavailable(RuntimeError):
    """moomoo_breaker が OPEN 状態で発注不可の場合に raise される。

    C-r1-03: breaker.state == "OPEN" 検出時に raise し発注を物理 block する。
    """


# ---------------------------------------------------------------------------
# Data Transfer Objects（spec B1 interface に対応する最小 DTO）
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class OrderRequest:
    """発注リクエスト DTO（broker client に渡す最小表現）。"""

    symbol: str
    side: str  # "buy" | "sell"
    quantity: int
    order_type: str = "market"
    tactic_name: str = ""
    idempotency_key: str = ""


@dataclasses.dataclass(frozen=True)
class OrderResult:
    """発注結果 DTO。"""

    order_id: str
    symbol: str
    status: str  # "submitted" | "rejected" | "skipped_idempotent" | "skipped_kill_switch" | "skipped_preflight" | "skipped_breaker" | "skipped_tactic_error"
    tactic_name: str = ""
    detail: str = ""


@dataclasses.dataclass
class SessionResult:
    """セッション実行結果。"""

    session_id: str
    order_results: list[OrderResult] = dataclasses.field(default_factory=list)
    ticks_completed: int = 0
    terminated_by_kill_switch: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Broker / MarketData Protocol（Phase 2 で concrete 実装に差し替え）
# ---------------------------------------------------------------------------

@runtime_checkable
class MarketDataClient(Protocol):
    """市場データ取得 Protocol（Phase 2 MoomooClient 等が実装する）。"""

    def get_environment(self) -> MarketEnvironment:
        """現在の MarketEnvironment スナップショットを返す。"""
        ...


@runtime_checkable
class BrokerClient(Protocol):
    """発注 Protocol（Phase 2 MoomooClient が実装する）。"""

    def place_order(self, request: OrderRequest) -> OrderResult:
        """発注を実行し結果を返す。"""
        ...


# ---------------------------------------------------------------------------
# AtlasEngine
# ---------------------------------------------------------------------------

class AtlasEngine:
    """Atlas メインエンジン（B1 Interface 凍結仕様）。

    Args:
        market_data: 市場データクライアント（MarketDataClient を実装していること）
        broker:      ブローカークライアント（BrokerClient を実装していること）
        tactics:     登録戦術リスト（全て TacticBase 継承を必須とする）
        idempotency_store: 二重発注防止ストア（None のとき共有デフォルトインスタンスを使用）

    Raises:
        TypeError: tactics 内に TacticBase 未継承の要素がある場合
    """

    def __init__(
        self,
        market_data: Any,
        broker: Any,
        tactics: list[TacticBase] | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._market_data = market_data
        self._broker = broker
        self._tactics: list[TacticBase] = []
        self._idempotency_store = idempotency_store or IdempotencyStore()
        # C-r1-01: tick 開始時刻を保持するインスタンス変数（tick() 冒頭で設定）
        self._tick_started_at: datetime | None = None

        if tactics:
            for tactic in tactics:
                self.register_tactic(tactic)

    # ------------------------------------------------------------------
    # 戦術登録
    # ------------------------------------------------------------------

    def register_tactic(self, tactic: TacticBase) -> None:
        """戦術を登録する。TacticBase 継承を強制（silent AttributeError 封鎖）。

        Args:
            tactic: 登録する戦術インスタンス

        Raises:
            TypeError: TacticBase を継承していない場合
        """
        if not isinstance(tactic, TacticBase):
            raise TypeError(
                f"tactic must be an instance of TacticBase, "
                f"got {type(tactic).__name__!r}. "
                "spec ref: atlas_spec_v3_20260422.md B5 L134"
            )
        self._tactics.append(tactic)
        log.info("[AtlasEngine] tactic registered: %s (%s)", tactic.tactic_name, tactic.tactic_type)

    # ------------------------------------------------------------------
    # 1 tick 処理（B1 Interface: tick -> list[OrderResult]）
    # ------------------------------------------------------------------

    def tick(self) -> list[OrderResult]:
        """1 tick（約 60 秒）の処理を実行する。

        処理順:
        1. Kill Switch チェック → ARMED なら即リターン（空リスト）
        2. tick 開始時刻を _tick_started_at に固定（C-r1-01: 決定的 idempotency key）
        3. 市場環境スナップショット取得
        4. 各戦術に対して preflight → dispatch → 発注（C-r1-05: 個別 try/except）

        Returns:
            本 tick で生成した OrderResult のリスト（0 件も正常）

        C-001 規律: asyncio event loop 内から直接呼び出し禁止。
        """
        if kill_switch_is_active():
            log.warning("[AtlasEngine.tick] Kill Switch ARMED: tick をスキップします")
            return [OrderResult(
                order_id="",
                symbol="",
                status="skipped_kill_switch",
                detail="kill switch is active",
            )]

        # C-r1-01: tick 開始時刻を固定 → 同 tick 内では全戦術が同一 trigger_time を使用
        self._tick_started_at = datetime.now(timezone.utc)

        env = self._market_data.get_environment()
        results: list[OrderResult] = []

        for tactic in self._tactics:
            # C-r1-05 HIGH: 戦術個別の try/except — 失敗戦術のみ skip・他戦術に伝播しない
            try:
                tick_results = self._dispatch_tactic(tactic, env)
                results.extend(tick_results)
            except Exception as exc:
                log.error(
                    "[AtlasEngine.tick] tactic=%s で例外発生・この戦術のみスキップ: %s",
                    tactic.tactic_name,
                    exc,
                )
                results.append(OrderResult(
                    order_id="",
                    symbol="",
                    status="skipped_tactic_error",
                    tactic_name=tactic.tactic_name,
                    detail=str(exc),
                ))

        return results

    def _dispatch_tactic(
        self,
        tactic: TacticBase,
        env: MarketEnvironment,
    ) -> list[OrderResult]:
        """単一戦術の dispatch（preflight → enter → order）。

        isinstance(tactic, TacticBase) 保証済みのため AttributeError 経路を封鎖。

        C-r1-05 対応: このメソッド自体は例外を re-raise する。
        caller（tick() の for ループ）で個別 try/except する設計。
        """
        # preflight チェック
        try:
            ok = tactic.preflight(env)
        except Exception as exc:
            log.error(
                "[AtlasEngine] preflight exception in tactic=%s: %s",
                tactic.tactic_name,
                exc,
            )
            raise

        if not ok:
            log.info(
                "[AtlasEngine] preflight=False, tactic=%s をスキップ（silent skip 禁止・log 済）",
                tactic.tactic_name,
            )
            return [OrderResult(
                order_id="",
                symbol="",
                status="skipped_preflight",
                tactic_name=tactic.tactic_name,
                detail="preflight returned False",
            )]

        # EnterExitTactic（Type A）dispatch
        if tactic.tactic_type in ("enter_exit",):
            return self._dispatch_enter_exit(tactic, env)

        # PortfolioReactiveTactic（Type B）dispatch
        if tactic.tactic_type == "portfolio_reactive":
            return self._dispatch_portfolio_reactive(tactic, env)

        # StateCarryingTactic（Type C）dispatch
        if tactic.tactic_type == "state_carrying":
            return self._dispatch_state_carrying(tactic, env)

        # HybridTactic（Type D）dispatch
        if tactic.tactic_type == "hybrid":
            return self._dispatch_hybrid(tactic, env)

        log.warning(
            "[AtlasEngine] 未知の tactic_type=%s (tactic=%s): dispatch をスキップ",
            tactic.tactic_type,
            tactic.tactic_name,
        )
        return []

    def _dispatch_enter_exit(
        self, tactic: Any, env: MarketEnvironment
    ) -> list[OrderResult]:
        """Type A: EnterExitTactic dispatch（Phase 2 で完全実装）。"""
        if not hasattr(tactic, "should_enter"):
            log.warning(
                "[AtlasEngine] tactic=%s: should_enter が未実装（Phase 2 待ち）",
                tactic.tactic_name,
            )
            return []

        try:
            decision = tactic.should_enter(env, symbol="")
        except NotImplementedError:
            return []

        if decision is None or not getattr(decision, "should_enter", False):
            return []

        return self._submit_order_with_idempotency(tactic, decision, env)

    def _dispatch_portfolio_reactive(
        self, tactic: Any, env: MarketEnvironment
    ) -> list[OrderResult]:
        """Type B: PortfolioReactiveTactic dispatch（Phase 2 で完全実装）。"""
        if not hasattr(tactic, "should_react"):
            return []
        return []

    def _dispatch_state_carrying(
        self, tactic: Any, env: MarketEnvironment
    ) -> list[OrderResult]:
        """Type C: StateCarryingTactic dispatch（Phase 2 で完全実装）。"""
        if not hasattr(tactic, "observe"):
            return []
        return []

    def _dispatch_hybrid(
        self, tactic: Any, env: MarketEnvironment
    ) -> list[OrderResult]:
        """Type D: HybridTactic dispatch（Phase 2 で完全実装）。"""
        if not hasattr(tactic, "observe"):
            return []
        return []

    def _submit_order_with_idempotency(
        self,
        tactic: TacticBase,
        decision: Any,
        env: MarketEnvironment,
    ) -> list[OrderResult]:
        """冪等性キー確認後に発注する。

        C-r1-01: _tick_started_at で決定的 key 生成（同 tick 内は同一キー）
        C-r1-02: with_idempotency + OrderNotSentError wrap でキーロールバック保証
        C-r1-03: moomoo_breaker.state == "OPEN" で BrokerUnavailable raise
        C-r1-04: kill_switch 再チェック（tick 冒頭以降に ARMED になった場合に対応）
        C-r1-06: quantity sanity check（0 < q ≤ MAX_QUANTITY_PER_ORDER）
        """
        # C-r1-04: kill_switch race 対策 — 発注直前に再チェック
        if kill_switch_is_active():
            log.warning(
                "[AtlasEngine] Kill Switch ARMED（発注直前再チェック）: tactic=%s をスキップ",
                tactic.tactic_name,
            )
            return [OrderResult(
                order_id="",
                symbol=getattr(decision, "symbol", "UNKNOWN"),
                status="skipped_kill_switch",
                tactic_name=tactic.tactic_name,
                detail="kill switch armed at order submit time",
            )]

        symbol = getattr(decision, "symbol", "UNKNOWN")

        # C-r1-06: quantity sanity check
        raw_quantity = getattr(decision, "quantity", None)
        if not isinstance(raw_quantity, int) or not (0 < raw_quantity <= MAX_QUANTITY_PER_ORDER):
            raise ValueError(
                f"invalid quantity={raw_quantity!r} for tactic={tactic.tactic_name!r} "
                f"symbol={symbol!r}: must be int in range (0, {MAX_QUANTITY_PER_ORDER}]"
            )
        quantity: int = raw_quantity

        # C-r1-01: tick_started_at で決定的キー（None の場合は fallback で現在時刻・通常は tick() から呼ばれる）
        trigger_time = self._tick_started_at or datetime.now(timezone.utc)
        key = make_job_key(
            strategy=tactic.tactic_name,
            symbol=symbol,
            trigger_time=trigger_time,
        )

        # C-r1-03: moomoo_breaker 状態チェック — fail-closed (Sprint 1 state 実装済み)
        breaker_state = moomoo_breaker.state
        if breaker_state == "OPEN":
            log.error(
                "[AtlasEngine] moomoo_breaker OPEN: 発注をブロック tactic=%s symbol=%s",
                tactic.tactic_name,
                symbol,
            )
            raise BrokerUnavailable(
                f"moomoo_breaker is OPEN: tactic={tactic.tactic_name!r} symbol={symbol!r}"
            )

        request = OrderRequest(
            symbol=symbol,
            side=getattr(decision, "side", "buy"),
            quantity=quantity,
            tactic_name=tactic.tactic_name,
            idempotency_key=key,
        )

        # C-r1-02: with_idempotency でラップ — place_order が OrderNotSentError を raise した場合のみキーロールバック
        def _place() -> OrderResult:
            return self._broker.place_order(request)

        result = with_idempotency(
            store=self._idempotency_store,
            key=key,
            func=_place,
            ttl_sec=300,
        )

        if result is None:
            # with_idempotency が重複と判断してスキップした場合
            log.info(
                "[AtlasEngine] 重複発注をブロック: tactic=%s symbol=%s key=%s",
                tactic.tactic_name,
                symbol,
                key,
            )
            return [OrderResult(
                order_id="",
                symbol=symbol,
                status="skipped_idempotent",
                tactic_name=tactic.tactic_name,
                detail=f"idempotency key={key}",
            )]

        return [result]

    # ------------------------------------------------------------------
    # セッション実行（B1 Interface: run_session -> SessionResult）
    # ------------------------------------------------------------------

    def run_session(self, session_id: str) -> SessionResult:
        """セッション全体を実行する（B1 Interface）。

        Kill Switch が ARMED になった時点でループ停止。
        本実装は Phase 2 で tick_interval / force_close 等を追加する。

        Returns:
            SessionResult（発注結果・tick 数・kill switch 停止フラグを含む）
        """
        session = SessionResult(session_id=session_id)

        if kill_switch_is_active():
            log.warning(
                "[AtlasEngine.run_session] Kill Switch ARMED: session=%s を開始しません",
                session_id,
            )
            session.terminated_by_kill_switch = True
            return session

        log.info("[AtlasEngine.run_session] session=%s 開始", session_id)

        # Phase 2 では tick_count / market hours によるループに変更する
        # 現在は単一 tick のみ実行（テスト可能な最小実装）
        tick_results = self.tick()
        session.order_results.extend(tick_results)
        session.ticks_completed = 1

        if any(r.status == "skipped_kill_switch" for r in tick_results):
            session.terminated_by_kill_switch = True

        log.info(
            "[AtlasEngine.run_session] session=%s 完了: ticks=%d orders=%d",
            session_id,
            session.ticks_completed,
            len(session.order_results),
        )
        return session
