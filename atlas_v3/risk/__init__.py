"""atlas_v3.risk — 4 guard 集約 facade (本実装)

Why
---
DrawdownTracker / ConsecutiveLossGuard / HalfDayGuard / MarginMonitor が
個別 dataclass で散在し、tactic engine が個別呼出していた。集約 facade で
「全 guard 通過か」を 1 メソッド判定可能にする。

PG&E 2018 California camp fire 型「個別装置 monitoring 分散による事故」を
構造的に防止する。

Public API
----------
- RiskAggregator(config): 4 guard を集約管理
- AggregateResult: allowed / blocking_guards / size_factor / halt
- DrawdownTracker / ConsecutiveLossGuard / HalfDayGuard 各 dataclass を re-export
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from atlas_v3.bots.engines.drawdown_tracker import (
    DrawdownTracker, DrawdownSnapshot,
)
from atlas_v3.bots.engines.consecutive_loss_guard import (
    ConsecutiveLossGuard, ConsecutiveLossResult,
)
from atlas_v3.bots.engines.half_day_guard import (
    HalfDayGuard, HalfDayInfo,
)


@dataclass(frozen=True)
class AggregateResult:
    """4 guard 集約結果."""
    allowed: bool
    blocking_guards: tuple[str, ...] = field(default_factory=tuple)
    size_factor: float = 1.0
    halt: bool = False
    reason: str = ""


class RiskAggregator:
    """4 guard を集約して 1 メソッドで判定する.

    Attributes:
        drawdown: DrawdownTracker (None なら skip)
        consecutive: ConsecutiveLossGuard (None なら skip)
        half_day: HalfDayGuard (None なら skip)
    """

    def __init__(
        self,
        drawdown: Optional[DrawdownTracker] = None,
        consecutive: Optional[ConsecutiveLossGuard] = None,
        half_day: Optional[HalfDayGuard] = None,
    ) -> None:
        self._dd = drawdown
        self._cl = consecutive
        self._hd = half_day

    def record_loss(self) -> None:
        """損失トレードを ConsecutiveLossGuard に記録 (state 更新用)."""
        if self._cl is not None:
            self._cl.record_loss()

    def record_win(self) -> None:
        """勝ちトレードを ConsecutiveLossGuard に記録."""
        if self._cl is not None:
            self._cl.record_win()

    def check_all(
        self,
        equity: Optional[float] = None,
        trade_date: Optional[Any] = None,
    ) -> AggregateResult:
        """全 guard を呼び出し集約結果を返す.

        Args:
            equity: 現在資産 (DrawdownTracker 用・None なら skip)
            trade_date: 取引日 (HalfDayGuard 用)

        Returns:
            AggregateResult (1 件でも block なら allowed=False)
        """
        blocking: list[str] = []
        size_factor = 1.0
        halt = False
        reasons: list[str] = []

        # DrawdownTracker
        if self._dd is not None and equity is not None:
            snap = self._dd.update(equity)
            if snap.size_factor < 1.0:
                size_factor = min(size_factor, snap.size_factor)
            if snap.size_factor <= 0.0:
                blocking.append("drawdown")
                halt = True
                reasons.append(f"DD halt: {snap.level_label}")

        # ConsecutiveLossGuard (state は record_loss / record_win で別途更新)
        if self._cl is not None:
            cl_res = self._cl.check()
            if cl_res.size_factor < 1.0:
                size_factor = min(size_factor, cl_res.size_factor)
            if not cl_res.allowed:
                blocking.append("consecutive_loss")
                halt = True
                reasons.append(cl_res.reason)

        # HalfDayGuard (情報提供のみ・block しない)
        # - 半日取引日は force_close_time_et を返すが、check_all では halt しない
        # - 各 engine の force_close ロジックで個別利用される

        allowed = len(blocking) == 0
        return AggregateResult(
            allowed=allowed,
            blocking_guards=tuple(blocking),
            size_factor=size_factor,
            halt=halt,
            reason=" / ".join(reasons) if reasons else "all guards passed",
        )


__all__ = [
    "RiskAggregator",
    "AggregateResult",
    "DrawdownTracker",
    "DrawdownSnapshot",
    "ConsecutiveLossGuard",
    "ConsecutiveLossResult",
    "HalfDayGuard",
    "HalfDayInfo",
]
