"""TacticBase ABC — 全戦術の共通基底
仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 L134-L154
背景: Engine からの dispatch 地獄回避 + silent AttributeError 経路封鎖（Redteam R2-02）
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from atlas_v3.core.env_observer import MarketEnvironment


TacticType = Literal["enter_exit", "portfolio_reactive", "state_carrying", "hybrid"]


class TacticBase(ABC):
    """全戦術の共通基底・Engine から dispatch される必須メソッド。

    Engine は `isinstance(tactic, TacticBase)` で統一 dispatch する。
    個別 Protocol (EnterExitTactic / PortfolioReactiveTactic / StateCarryingTactic /
    HybridTactic) は本 ABC の継承を前提に宣言される。
    """

    @property
    @abstractmethod
    def tactic_type(self) -> TacticType:
        """戦術の type 分類。Engine の dispatch 判定で使用。"""

    @property
    @abstractmethod
    def tactic_name(self) -> str:
        """戦術識別子（例: "cs_sell" / "gamma_scalp"）。ログ・メトリクス用。"""

    @abstractmethod
    def preflight(self, env: MarketEnvironment) -> bool:
        """起動前 health check。False を返した戦術は Engine が無効化する。

        silent skip 禁止: 無効化理由は戦術側で EICAS Advisory / Caution 発出してから
        False を返すこと（Engine 側での silent 無効化は仕様違反）。
        """
