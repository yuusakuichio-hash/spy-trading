"""atlas_v3 — Atlas v3 自動売買エンジン（Sprint 1-B Phase B）

公開 API:
    AtlasEngine         — メインエンジン（B1）
    StrategySelector    — 戦術動的選択（B4）
    PercentileSelector  — percentile 動的算出（B2）
    TacticDecision      — 戦術選択結果 DTO
    MarketEnvironment   — 市場環境スナップショット（B2 stub）
    TacticBase          — 全戦術の共通基底 ABC（B5）
"""
from atlas_v3.core.engine import AtlasEngine
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.core.strategy_selector import (
    PercentileSelector,
    StrategySelector,
    TacticDecision,
)
from atlas_v3.strategies.base import TacticBase

__all__ = [
    "AtlasEngine",
    "MarketEnvironment",
    "PercentileSelector",
    "StrategySelector",
    "TacticBase",
    "TacticDecision",
]
