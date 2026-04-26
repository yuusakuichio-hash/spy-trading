"""MarketEnvironment 型 stub（Phase 2 本実装の placeholder）
仕様: data/specs/v3/atlas_spec_v3_20260422.md B2 L87-L112
目的: Redteam F-12 指摘（base.py の TYPE_CHECKING import 先が未実装で get_type_hints NameError）を解消する最小 stub。Phase 2 で PercentileSelector / EnvObserver を本実装する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class MarketEnvironment:
    """Phase 2 で拡張予定の市場環境スナップショット（frozen）。

    Phase 2 Builder は仕様書 B2 に従い `EnvObserver.snapshot()` から生成する。
    本 stub は TYPE_CHECKING import 先の存在証明のみを責務とする。
    """

    vix: float
    vrp: float = 0.0
    gex: float = 0.0
    term_ratio: float = 1.0
    bias: Literal["bull", "bear", "neutral"] = "neutral"
    ivr_by_symbol: dict[str, float] = field(default_factory=dict)
