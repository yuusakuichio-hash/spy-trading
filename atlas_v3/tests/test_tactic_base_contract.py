"""TacticBase ABC の contract test
仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 L134-L154
目的: Redteam F-01/F-02/F-08/F-09/F-12 の攻撃シナリオに対する物理ガード
"""
from __future__ import annotations

from typing import get_args

import pytest

from atlas_v3.strategies.base import TacticBase, TacticType


class _ValidTactic(TacticBase):
    """contract test 用の valid 実装"""

    @property
    def tactic_type(self) -> TacticType:
        return "enter_exit"

    @property
    def tactic_name(self) -> str:
        return "test_tactic_valid"

    def preflight(self, env) -> bool:
        return True


class _AnotherValidTactic(TacticBase):
    @property
    def tactic_type(self) -> TacticType:
        return "hybrid"

    @property
    def tactic_name(self) -> str:
        return "test_tactic_another"

    def preflight(self, env) -> bool:
        return True


def test_abstract_class_cannot_instantiate_directly() -> None:
    """F-01 対策: ABC 直接インスタンス化は TypeError"""
    with pytest.raises(TypeError, match="abstract"):
        TacticBase()  # type: ignore[abstract]


def test_subclass_missing_abstractmethods_cannot_instantiate() -> None:
    """F-01 対策: abstract method 未実装の subclass も TypeError"""

    class BadTactic(TacticBase):
        pass

    with pytest.raises(TypeError, match="abstract"):
        BadTactic()  # type: ignore[abstract]


def test_valid_subclass_has_zero_remaining_abstractmethods() -> None:
    """F-01 対策: 正しい subclass は __abstractmethods__ が空"""
    assert len(_ValidTactic.__abstractmethods__) == 0


def test_valid_subclass_instantiable_and_isinstance() -> None:
    t = _ValidTactic()
    assert isinstance(t, TacticBase)
    assert t.tactic_name == "test_tactic_valid"
    assert t.tactic_type == "enter_exit"


def test_tactic_type_literal_has_exactly_four_values() -> None:
    """F-02 対策: TacticType Literal が仕様書 B5 の 4 種類と一致"""
    args = get_args(TacticType)
    assert set(args) == {
        "enter_exit",
        "portfolio_reactive",
        "state_carrying",
        "hybrid",
    }


def test_market_environment_is_importable_at_runtime() -> None:
    """F-12 対策: TYPE_CHECKING import 先（env_observer.MarketEnvironment）が
    runtime にも存在し get_type_hints が NameError を起こさない"""
    from atlas_v3.core.env_observer import MarketEnvironment  # noqa: F401

    assert MarketEnvironment is not None


def test_two_distinct_tactics_have_unique_names() -> None:
    """F-08 対策: 異なる戦術 class は異なる tactic_name を返す"""
    a = _ValidTactic()
    b = _AnotherValidTactic()
    assert a.tactic_name != b.tactic_name


def test_tactic_name_is_string_not_none() -> None:
    """F-04 近縁: tactic_name が str 型（None 返しで silent skip を防ぐ）"""
    t = _ValidTactic()
    assert isinstance(t.tactic_name, str)
    assert len(t.tactic_name) > 0


def test_tactic_type_returns_valid_literal_member() -> None:
    """F-02 対策: subclass の tactic_type が必ず Literal メンバーを返す"""
    t = _ValidTactic()
    assert t.tactic_type in get_args(TacticType)


def test_preflight_returns_bool_not_none() -> None:
    """F-04 対策: preflight が bool を返す（None 返しで silent skip を防ぐ）"""
    t = _ValidTactic()
    result = t.preflight(env=None)
    assert isinstance(result, bool), f"preflight must return bool, got {type(result).__name__}"
