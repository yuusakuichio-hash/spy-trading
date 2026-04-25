"""common_v3.position — PositionSnapshot 共通 schema (本実装)

Public API:
- PositionSnapshot: frozen dataclass (broker 横断統一形式)
- aggregate_positions(*broker_position_lists): 複数 broker から position を集約
- find_naked_shorts(positions): SHORT leg のみで LONG hedge 不在を検出
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class PositionSnapshot:
    """ポジションの統一 schema (broker / source 横断)."""
    symbol: str
    qty: int  # +long / -short
    avg_cost: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    open_dt: Optional[datetime.datetime] = None
    side: str = ""  # "LONG" / "SHORT" / ""
    strategy: str = ""  # tactic_name 等

    @property
    def is_long(self) -> bool:
        return self.qty > 0

    @property
    def is_short(self) -> bool:
        return self.qty < 0


def aggregate_positions(
    *broker_position_lists: Iterable[PositionSnapshot],
) -> list[PositionSnapshot]:
    """複数 broker から取得した position を 1 list に集約.

    同一 symbol の合算は意図的に行わない (broker ごとに口座が違うため)。
    """
    out: list[PositionSnapshot] = []
    for lst in broker_position_lists:
        out.extend(lst)
    return out


def find_naked_shorts(
    positions: Iterable[PositionSnapshot],
) -> list[PositionSnapshot]:
    """SHORT leg のみで LONG hedge 不在の position を抽出.

    判定: 同一 symbol 系列で qty<0 (SHORT) のみで qty>0 (LONG) なし。
    option の場合は underlying 別に集約する責務は本関数の外側 (engine 側)。
    """
    by_symbol: dict[str, list[PositionSnapshot]] = {}
    for p in positions:
        by_symbol.setdefault(p.symbol, []).append(p)
    naked: list[PositionSnapshot] = []
    for sym, ps in by_symbol.items():
        has_long = any(p.qty > 0 for p in ps)
        has_short = any(p.qty < 0 for p in ps)
        if has_short and not has_long:
            naked.extend([p for p in ps if p.qty < 0])
    return naked


__all__ = [
    "PositionSnapshot",
    "aggregate_positions",
    "find_naked_shorts",
]
