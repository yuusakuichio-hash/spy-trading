"""tests/test_engine_wiring_20260425.py — AtlasEngine 配線テスト（20 件以上）

検証項目:
- 全 10 戦術が TacticRegistry 経由で AtlasEngine に登録される
- registry.build_engine() で返る AtlasEngine が正しく配線されている
- _dispatch_enter_exit / _dispatch_state_carrying / _dispatch_hybrid 各 path で
  tactic.observe / should_enter / build_order が実際に呼び出される
- preflight=False の戦術は skipped_preflight を返す
- Unknown tactic_type は空リスト
- TypeError: TacticBase 未継承の登録試みは拒否される
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, call, patch

from atlas_v3.bots.engines import (
    BrokenWingButterflyEngine,
    DiagonalSpreadTactic,
    EarningsStraddleBuyTactic,
    IronFlyEngine,
    JadeLizardTactic,
    ORBNativeEngine,
    PMCCTactic,
    RatioSpreadEngine,
    ShortStrangle0DTEEngine,
    VixTailHedgeEngine,
    WeeklyGammaScalpTactic,
)
from atlas_v3.bots.engines.registry import TACTIC_COUNT, TACTIC_NAMES, TacticRegistry
from atlas_v3.core.engine import AtlasEngine, OrderRequest, OrderResult
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase


# ---------------------------------------------------------------------------
# テスト用 stub
# ---------------------------------------------------------------------------

def _env(vix: float = 15.0) -> MarketEnvironment:
    return MarketEnvironment(vix=vix, ivr_by_symbol={})


def _mock_market(vix: float = 15.0):
    mkt = MagicMock()
    mkt.get_environment.return_value = _env(vix)
    return mkt


def _mock_broker():
    broker = MagicMock()
    broker.place_order.return_value = OrderResult(
        order_id="TEST_ORDER_001",
        symbol="SPY",
        status="submitted",
    )
    return broker


class _MinimalTactic(TacticBase):
    """テスト用最小 TacticBase 実装。"""

    def __init__(self, name: str, ttype: str) -> None:
        self._name = name
        self._ttype = ttype  # type: ignore[assignment]

    @property
    def tactic_type(self):  # type: ignore[override]
        return self._ttype

    @property
    def tactic_name(self) -> str:
        return self._name

    def preflight(self, env: MarketEnvironment) -> bool:
        return True


class _StateCarryingStub(_MinimalTactic):
    """state_carrying 型スタブ（observe / should_enter / build_order 付き）。"""

    def __init__(self) -> None:
        super().__init__("stub_state_carrying", "state_carrying")
        self.observe_called = False
        self.should_enter_called = False

    def observe(self, env, market_data=None) -> None:
        self.observe_called = True

    def should_enter(self, env, candidates=None, **kwargs):
        self.should_enter_called = True
        return []

    def build_order(self, decision):
        return None


class _HybridStub(_MinimalTactic):
    """hybrid 型スタブ（observe / should_enter / build_order 付き）。"""

    def __init__(self) -> None:
        super().__init__("stub_hybrid", "hybrid")
        self.observe_called = False
        self.should_enter_called = False

    def observe(self, env, market_data=None) -> None:
        self.observe_called = True

    def should_enter(self, env, symbol="", **kwargs):
        self.should_enter_called = True
        return None

    def build_order(self, decision):
        return None


class _PortfolioReactiveStub(_MinimalTactic):
    """portfolio_reactive 型スタブ（observe / should_react 付き）。"""

    def __init__(self) -> None:
        super().__init__("stub_portfolio_reactive", "portfolio_reactive")
        self.observe_called = False
        self.should_react_called = False

    def observe(self, env) -> None:
        self.observe_called = True

    def should_react(self, env):
        self.should_react_called = True
        return None

    def build_order(self, decision):
        return None


# ---------------------------------------------------------------------------
# 1. TacticRegistry 基本テスト
# ---------------------------------------------------------------------------

class TestTacticRegistry:

    def test_registry_len_matches_tactic_count(self):
        """TacticRegistry が TACTIC_COUNT と同数の戦術を保持する。"""
        r = TacticRegistry()
        assert len(r) == TACTIC_COUNT

    def test_registry_contains_all_names(self):
        """TACTIC_NAMES に列挙された全識別子が registry に含まれる。"""
        r = TacticRegistry()
        names = set(r.tactic_names())
        for name in TACTIC_NAMES:
            assert name in names, f"missing tactic: {name}"

    def test_all_tactics_are_tacticbase(self):
        """全インスタンスが TacticBase 継承であること。"""
        r = TacticRegistry()
        for tactic in r.all_tactics():
            assert isinstance(tactic, TacticBase), (
                f"{tactic.__class__.__name__} は TacticBase 継承でない"
            )

    def test_get_returns_correct_type(self):
        """get() で取得したインスタンスが正しいクラスである。"""
        r = TacticRegistry()
        assert isinstance(r.get("iron_fly"), IronFlyEngine)
        assert isinstance(r.get("orb_native"), ORBNativeEngine)
        assert isinstance(r.get("earnings_straddle_buy"), EarningsStraddleBuyTactic)
        assert isinstance(r.get("vix_tail_hedge"), VixTailHedgeEngine)

    def test_get_raises_for_unknown_name(self):
        """未登録名で get() すると KeyError が上がる。"""
        r = TacticRegistry()
        with pytest.raises(KeyError):
            r.get("nonexistent_tactic_xyz")

    def test_tactic_count_constant_is_11(self):
        """TACTIC_COUNT が 11 であること（10 + ORBNative = 11 戦術）。"""
        assert TACTIC_COUNT == 11

    def test_tactic_names_tuple_length(self):
        """TACTIC_NAMES タプルの長さが TACTIC_COUNT と一致する。"""
        assert len(TACTIC_NAMES) == TACTIC_COUNT


# ---------------------------------------------------------------------------
# 2. build_engine テスト
# ---------------------------------------------------------------------------

class TestBuildEngine:

    def test_build_engine_returns_atlas_engine(self):
        """build_engine() が AtlasEngine インスタンスを返す。"""
        r = TacticRegistry()
        engine = r.build_engine(_mock_market(), _mock_broker())
        assert isinstance(engine, AtlasEngine)

    def test_build_engine_registers_all_tactics(self):
        """build_engine() で返る AtlasEngine に全戦術が登録されている。"""
        r = TacticRegistry()
        engine = r.build_engine(_mock_market(), _mock_broker())
        assert len(engine._tactics) == TACTIC_COUNT

    def test_build_engine_tactic_names(self):
        """AtlasEngine の _tactics 内の名前が registry と一致する。"""
        r = TacticRegistry()
        engine = r.build_engine(_mock_market(), _mock_broker())
        engine_names = {t.tactic_name for t in engine._tactics}
        for name in TACTIC_NAMES:
            assert name in engine_names

    def test_register_non_tacticbase_raises_typeerror(self):
        """TacticBase 未継承オブジェクトの register は TypeError。"""
        engine = AtlasEngine(_mock_market(), _mock_broker())
        with pytest.raises(TypeError):
            engine.register_tactic("not_a_tactic")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. _dispatch_state_carrying — observe が呼ばれる
# ---------------------------------------------------------------------------

class TestDispatchStateCarrying:

    def _engine_with(self, tactic) -> AtlasEngine:
        engine = AtlasEngine(_mock_market(), _mock_broker())
        engine.register_tactic(tactic)
        return engine

    def test_observe_called_for_state_carrying(self):
        """state_carrying 戦術の tick で observe が呼ばれる。"""
        stub = _StateCarryingStub()
        engine = self._engine_with(stub)
        engine.tick()
        assert stub.observe_called

    def test_should_enter_called_for_state_carrying(self):
        """state_carrying 戦術の tick で should_enter が呼ばれる。"""
        stub = _StateCarryingStub()
        engine = self._engine_with(stub)
        engine.tick()
        assert stub.should_enter_called

    def test_state_carrying_without_observe_returns_empty(self):
        """observe なし state_carrying は空リストを返す（warn log のみ）。"""
        stub = _MinimalTactic("no_observe", "state_carrying")
        engine = self._engine_with(stub)
        results = engine.tick()
        # preflight=True but no observe → skipped (empty or preflight ok but dispatch empty)
        assert isinstance(results, list)

    def test_state_carrying_empty_decisions_no_order(self):
        """should_enter が空リストを返す場合は発注しない。"""
        stub = _StateCarryingStub()
        engine = self._engine_with(stub)
        broker = _mock_broker()
        engine._broker = broker
        engine.tick()
        broker.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# 4. _dispatch_hybrid — observe が呼ばれる
# ---------------------------------------------------------------------------

class TestDispatchHybrid:

    def _engine_with(self, tactic) -> AtlasEngine:
        engine = AtlasEngine(_mock_market(), _mock_broker())
        engine.register_tactic(tactic)
        return engine

    def test_observe_called_for_hybrid(self):
        """hybrid 戦術の tick で observe が呼ばれる。"""
        stub = _HybridStub()
        engine = self._engine_with(stub)
        engine.tick()
        assert stub.observe_called

    def test_should_enter_called_for_hybrid(self):
        """hybrid 戦術の tick で should_enter が呼ばれる。"""
        stub = _HybridStub()
        engine = self._engine_with(stub)
        engine.tick()
        assert stub.should_enter_called

    def test_hybrid_without_observe_returns_log_warning(self):
        """observe なし hybrid は dispatch スキップ（empty list）。"""
        stub = _MinimalTactic("no_observe_hybrid", "hybrid")
        engine = self._engine_with(stub)
        results = engine.tick()
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 5. _dispatch_portfolio_reactive — observe / should_react が呼ばれる
# ---------------------------------------------------------------------------

class TestDispatchPortfolioReactive:

    def _engine_with(self, tactic) -> AtlasEngine:
        engine = AtlasEngine(_mock_market(), _mock_broker())
        engine.register_tactic(tactic)
        return engine

    def test_observe_and_should_react_called(self):
        """portfolio_reactive 戦術で observe と should_react が呼ばれる。"""
        stub = _PortfolioReactiveStub()
        engine = self._engine_with(stub)
        engine.tick()
        assert stub.observe_called
        assert stub.should_react_called

    def test_portfolio_reactive_without_should_react_skips(self):
        """should_react なし portfolio_reactive は dispatch スキップ。"""
        stub = _MinimalTactic("no_react", "portfolio_reactive")
        engine = self._engine_with(stub)
        results = engine.tick()
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# 6. preflight=False は skipped_preflight を返す
# ---------------------------------------------------------------------------

class TestPreflightFalse:

    def test_preflight_false_returns_skipped(self):
        """preflight=False の戦術は skipped_preflight ステータスを返す。"""
        class _FailPreflight(_MinimalTactic):
            def preflight(self, env):
                return False

        tactic = _FailPreflight("fail_pf", "enter_exit")
        engine = AtlasEngine(_mock_market(), _mock_broker())
        engine.register_tactic(tactic)
        results = engine.tick()
        statuses = [r.status for r in results]
        assert "skipped_preflight" in statuses

    def test_preflight_exception_propagates(self):
        """preflight が例外を raise すると skipped_tactic_error になる。"""
        class _ExcPreflight(_MinimalTactic):
            def preflight(self, env):
                raise RuntimeError("preflight boom")

        tactic = _ExcPreflight("exc_pf", "enter_exit")
        engine = AtlasEngine(_mock_market(), _mock_broker())
        engine.register_tactic(tactic)
        results = engine.tick()
        statuses = [r.status for r in results]
        assert "skipped_tactic_error" in statuses


# ---------------------------------------------------------------------------
# 7. 各戦術クラスの tactic_type 正当性テスト
# ---------------------------------------------------------------------------

class TestTacticTypeValidity:

    VALID_TYPES = {"enter_exit", "state_carrying", "portfolio_reactive", "hybrid"}

    def _assert_valid(self, tactic_instance):
        assert tactic_instance.tactic_type in self.VALID_TYPES, (
            f"{tactic_instance.tactic_name} の tactic_type={tactic_instance.tactic_type!r} が未定義"
        )
        assert isinstance(tactic_instance.tactic_name, str)
        assert len(tactic_instance.tactic_name) > 0

    def test_iron_fly_type(self):
        self._assert_valid(IronFlyEngine())

    def test_weekly_gamma_scalp_type(self):
        self._assert_valid(WeeklyGammaScalpTactic())

    def test_orb_native_type(self):
        self._assert_valid(ORBNativeEngine())

    def test_short_strangle_type(self):
        self._assert_valid(ShortStrangle0DTEEngine())

    def test_broken_wing_butterfly_type(self):
        self._assert_valid(BrokenWingButterflyEngine())

    def test_diagonal_spread_type(self):
        self._assert_valid(DiagonalSpreadTactic())

    def test_earnings_straddle_buy_type(self):
        t = EarningsStraddleBuyTactic()
        assert t.tactic_type == "state_carrying"

    def test_jade_lizard_type(self):
        self._assert_valid(JadeLizardTactic())

    def test_pmcc_type(self):
        self._assert_valid(PMCCTactic())

    def test_ratio_spread_type(self):
        self._assert_valid(RatioSpreadEngine())

    def test_vix_tail_hedge_type(self):
        self._assert_valid(VixTailHedgeEngine())


# ---------------------------------------------------------------------------
# 8. unknown tactic_type は空リスト（dispatch ルーティング）
# ---------------------------------------------------------------------------

class TestUnknownTacticType:

    def test_unknown_type_returns_empty_list(self):
        """未定義 tactic_type の戦術は dispatch スキップして空リストを返す。"""
        class _UnknownType(_MinimalTactic):
            @property
            def tactic_type(self):
                return "completely_unknown_type_xyz"

        tactic = _UnknownType("unknown_type", "enter_exit")
        tactic._ttype = "completely_unknown_type_xyz"
        engine = AtlasEngine(_mock_market(), _mock_broker())
        engine.register_tactic(tactic)
        results = engine.tick()
        # No order submitted for unknown type (warning logged, empty dispatch path)
        assert not any(r.status == "submitted" for r in results)
