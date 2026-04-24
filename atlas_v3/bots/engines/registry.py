"""atlas_v3/bots/engines/registry.py — TacticRegistry

全 10 戦術を一括 instantiate し AtlasEngine に登録するファクトリ。

責務:
- TACTIC_NAMES（10 件）を列挙し名前→インスタンスのマップを保持する。
- build_engine(market_data, broker) で AtlasEngine を組み立て返す。
- 個別戦術の設定は各エンジンのデフォルト DTO を使用（Phase 2 で yaml から注入）。

禁則:
- spy_bot.py / common/* への書き換え禁止
- asyncio 禁止
- CC ≤ 20 規律
"""
from __future__ import annotations

import logging
from typing import Any

from atlas_v3.bots.engines.broken_wing_butterfly import BrokenWingButterflyEngine
from atlas_v3.bots.engines.diagonal_spread import DiagonalSpreadTactic
from atlas_v3.bots.engines.earnings_straddle_buy import EarningsStraddleBuyTactic
from atlas_v3.bots.engines.iron_fly import IronFlyEngine
from atlas_v3.bots.engines.jade_lizard import JadeLizardTactic
from atlas_v3.bots.engines.orb_native import ORBNativeEngine
from atlas_v3.bots.engines.pmcc import PMCCTactic
from atlas_v3.bots.engines.ratio_spread import RatioSpreadEngine
from atlas_v3.bots.engines.short_strangle_0dte import ShortStrangle0DTEEngine
from atlas_v3.bots.engines.vix_tail_hedge import VixTailHedgeEngine
from atlas_v3.bots.engines.weekly_gamma_scalp import WeeklyGammaScalpTactic
from atlas_v3.core.engine import AtlasEngine
from atlas_v3.strategies.base import TacticBase

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全 10 戦術の識別子（tactic_name と対応）
# ---------------------------------------------------------------------------

TACTIC_NAMES: tuple[str, ...] = (
    "iron_fly",
    "weekly_gamma_scalp",
    "orb_native",
    "short_strangle_0dte",
    "broken_wing_butterfly",
    "diagonal_spread",
    "earnings_straddle_buy",
    "jade_lizard",
    "pmcc",
    "ratio_spread",
    "vix_tail_hedge",
)

#: 登録済み戦術の合計数
TACTIC_COUNT: int = len(TACTIC_NAMES)


# ---------------------------------------------------------------------------
# TacticRegistry
# ---------------------------------------------------------------------------

class TacticRegistry:
    """全 10 戦術を instantiate し保持するレジストリ。

    Args:
        extra_kwargs: 各戦術コンストラクタへの追加引数 dict（テスト用 DI）
                      キーは tactic_name、値は kwargs dict。

    Usage::

        registry = TacticRegistry()
        engine = registry.build_engine(market_data=mkt, broker=broker)
    """

    def __init__(self, extra_kwargs: dict[str, dict[str, Any]] | None = None) -> None:
        self._extra: dict[str, dict[str, Any]] = extra_kwargs or {}
        self._tactics: dict[str, TacticBase] = {}
        self._instantiate_all()

    # ------------------------------------------------------------------
    # 内部: 全戦術 instantiate
    # ------------------------------------------------------------------

    def _kw(self, name: str) -> dict[str, Any]:
        """戦術 name に対する追加 kwargs を返す（未設定時は空 dict）。"""
        return self._extra.get(name, {})

    def _instantiate_all(self) -> None:
        """10 戦術を instantiate して _tactics に格納する。"""
        tactics: list[TacticBase] = [
            IronFlyEngine(**self._kw("iron_fly")),
            WeeklyGammaScalpTactic(**self._kw("weekly_gamma_scalp")),
            ORBNativeEngine(**self._kw("orb_native")),
            ShortStrangle0DTEEngine(**self._kw("short_strangle_0dte")),
            BrokenWingButterflyEngine(**self._kw("broken_wing_butterfly")),
            DiagonalSpreadTactic(**self._kw("diagonal_spread")),
            EarningsStraddleBuyTactic(**self._kw("earnings_straddle_buy")),
            JadeLizardTactic(**self._kw("jade_lizard")),
            PMCCTactic(**self._kw("pmcc")),
            RatioSpreadEngine(**self._kw("ratio_spread")),
            VixTailHedgeEngine(**self._kw("vix_tail_hedge")),
        ]
        for tactic in tactics:
            self._tactics[tactic.tactic_name] = tactic
            log.debug(
                "[TacticRegistry] instantiated: %s (type=%s)",
                tactic.tactic_name,
                tactic.tactic_type,
            )

    # ------------------------------------------------------------------
    # 公開: 取得・照会
    # ------------------------------------------------------------------

    def get(self, tactic_name: str) -> TacticBase:
        """戦術名でインスタンスを取得する。

        Args:
            tactic_name: 戦術識別子

        Raises:
            KeyError: 未登録の戦術名が指定された場合
        """
        if tactic_name not in self._tactics:
            raise KeyError(
                f"TacticRegistry: 未登録の戦術名={tactic_name!r}. "
                f"登録済み: {list(self._tactics.keys())}"
            )
        return self._tactics[tactic_name]

    def all_tactics(self) -> list[TacticBase]:
        """全戦術インスタンスのリストを登録順に返す。"""
        return list(self._tactics.values())

    def tactic_names(self) -> list[str]:
        """登録済み戦術名リストを返す。"""
        return list(self._tactics.keys())

    def __len__(self) -> int:
        return len(self._tactics)

    # ------------------------------------------------------------------
    # 公開: AtlasEngine 組み立て
    # ------------------------------------------------------------------

    def build_engine(
        self,
        market_data: Any,
        broker: Any,
    ) -> AtlasEngine:
        """全 10 戦術を AtlasEngine に登録して返す。

        Args:
            market_data: MarketDataClient 実装（MarketEnvironment を返す）
            broker:      BrokerClient 実装（place_order を持つ）

        Returns:
            AtlasEngine — 10 戦術登録済み・本番 entry path で使用可能
        """
        engine = AtlasEngine(
            market_data=market_data,
            broker=broker,
            tactics=self.all_tactics(),
        )
        log.info(
            "[TacticRegistry.build_engine] AtlasEngine 組み立て完了: %d 戦術登録",
            len(self._tactics),
        )
        return engine
