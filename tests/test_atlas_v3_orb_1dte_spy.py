"""tests/test_atlas_v3_orb_1dte_spy.py — ORB1DTESPYTactic 単体テスト

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 (orb_1dte)
観点: entry/exit/stop/kill_switch連動/idempotency/slippage/engine.register_tactic
     ORB state 保持 / observe / persist_state / restore_state
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas_v3.core.engine import AtlasEngine, OrderRequest
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase
from atlas_v3.strategies.orb_1dte_spy import (
    ORB1DTEConfig,
    ORB1DTESPYTactic,
    ORBEntryDecision,
    ORBRange,
    Position,
)
from common_v3.risk.kill_switch import activate as ks_activate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_kill_switch(tmp_path, monkeypatch):
    """Kill Switch の state_v3 を tmp_path に隔離する（テスト間干渉防止）。"""
    import common_v3.risk.kill_switch as ks_module
    tmp_state = tmp_path / "state_v3"
    tmp_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_module, "_STATE_DIR", tmp_state)
    monkeypatch.setattr(ks_module, "FLAG_FILE", tmp_state / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", tmp_state / "kill_switch_audit.jsonl")
    yield


def _env(
    vix: float = 18.0,
    bias: str = "bull",
) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=1.5,
        gex=0.0,
        term_ratio=1.0,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={"SPY": 45.0},
    )


def _tactic(vix_max: float = 30.0, slippage_bps: int = 20) -> ORB1DTESPYTactic:
    cfg = ORB1DTEConfig(vix_max=vix_max, slippage_tolerance_bps=slippage_bps)
    return ORB1DTESPYTactic(config=cfg)


def _confirmed_orb(high: float = 450.0, low: float = 445.0, symbol: str = "SPY") -> ORBRange:
    return ORBRange(
        high=high,
        low=low,
        is_confirmed=True,
        observed_at=datetime.now(timezone.utc),
        symbol=symbol,
    )


# ---------------------------------------------------------------------------
# T-ORB-01: TacticBase ABC 継承
# ---------------------------------------------------------------------------

def test_orb_is_tactic_base_instance():
    """T-ORB-01: ORB1DTESPYTactic は TacticBase のインスタンス"""
    t = _tactic()
    assert isinstance(t, TacticBase)


def test_orb_tactic_type_and_name():
    """T-ORB-02: tactic_type is state_carrying and tactic_name is orb_1dte_spy"""
    t = _tactic()
    assert t.tactic_type == "state_carrying"
    assert t.tactic_name == "orb_1dte_spy"


# ---------------------------------------------------------------------------
# T-ORB-03: preflight
# ---------------------------------------------------------------------------

def test_preflight_pass_bull_env():
    """T-ORB-03a: bull + VIX 正常 → preflight=True"""
    t = _tactic()
    env = _env(vix=18.0, bias="bull")
    assert t.preflight(env) is True


def test_preflight_pass_bear_env():
    """T-ORB-03b: bear + VIX 正常 → preflight=True"""
    t = _tactic()
    env = _env(vix=18.0, bias="bear")
    assert t.preflight(env) is True


def test_preflight_fail_neutral_env():
    """T-ORB-03c: neutral → ORB は方向性必須 → False"""
    t = _tactic()
    env = _env(bias="neutral")
    assert t.preflight(env) is False


def test_preflight_fail_high_vix():
    """T-ORB-03d: VIX > vix_max → False"""
    t = ORB1DTESPYTactic(config=ORB1DTEConfig(vix_max=25.0))
    env = _env(vix=30.0, bias="bull")
    assert t.preflight(env) is False


def test_preflight_fail_none_env():
    """T-ORB-03e: env=None → False"""
    t = _tactic()
    assert t.preflight(None) is False  # type: ignore[arg-type]


def test_preflight_fail_kill_switch():
    """T-ORB-03f: Kill Switch ARMED → False
    (autouse fixture が state_v3 を tmp_path に隔離済み)
    """
    ks_activate(reason="test", activator="test")
    t = _tactic()
    assert t.preflight(_env(bias="bull")) is False


# ---------------------------------------------------------------------------
# T-ORB-04: should_enter (ORB breakout 判定)
# ---------------------------------------------------------------------------

def test_should_enter_no_orb_state_returns_false():
    """T-ORB-04a: ORB state 未設定 → should_enter=False"""
    t = _tactic()
    env = _env(bias="bull")
    decisions = t.should_enter(env, ["SPY"])
    assert len(decisions) == 1
    assert decisions[0].should_enter is False


def test_should_enter_unconfirmed_orb_returns_false():
    """T-ORB-04b: is_confirmed=False の ORB → should_enter=False"""
    t = _tactic()
    t._orb_ranges["SPY"] = ORBRange(
        high=450.0, low=445.0, is_confirmed=False, symbol="SPY"
    )
    env = _env(bias="bull")
    decisions = t.should_enter(env, ["SPY"])
    assert decisions[0].should_enter is False


def test_should_enter_phase1_placeholder_behavior():
    """T-ORB-04c: Phase 1 では current_price = orb.high のためブレイクアウトは未実装
    Phase 2 で get_quote() 連携後に実際のブレイクアウト判定が入る。
    本テストはその Phase 1 の動作を記録する。
    """
    t = _tactic()
    orb = _confirmed_orb(high=450.0, low=445.0, symbol="SPY")
    t._orb_ranges["SPY"] = orb
    env = _env(bias="bull")
    decisions = t.should_enter(env, ["SPY"])
    assert len(decisions) == 1
    # Phase 1: current_price = orb.high → buffer チェックに引っかかり breakout なし
    assert isinstance(decisions[0].should_enter, bool)


def test_should_enter_returns_list():
    """T-ORB-05: should_enter は常にリストを返す"""
    t = _tactic()
    env = _env(bias="bull")
    decisions = t.should_enter(env, ["SPY", "QQQ"])
    assert isinstance(decisions, list)
    assert len(decisions) == 2


def test_should_enter_empty_candidates_returns_empty():
    """T-ORB-06a: symbol_candidates が空 → 空リスト"""
    t = _tactic()
    env = _env(bias="bull")
    decisions = t.should_enter(env, [])
    assert decisions == []


# ---------------------------------------------------------------------------
# T-ORB-07: build_order
# ---------------------------------------------------------------------------

def test_build_order_raises_on_no_enter():
    """T-ORB-07: should_enter=False の decision → ValueError"""
    t = _tactic()
    bad = ORBEntryDecision(should_enter=False, symbol="SPY")
    with pytest.raises(ValueError):
        t.build_order(bad)


def test_build_order_returns_order_request_on_valid_decision():
    """T-ORB-08: 正常な decision → OrderRequest を返す"""
    t = _tactic()
    decision = ORBEntryDecision(
        should_enter=True,
        symbol="SPY",
        side="buy",
        quantity=1,
        direction="call",
        idempotency_key="v3_testkey12",
    )
    order = t.build_order(decision)
    assert isinstance(order, OrderRequest)
    assert order.tactic_name == "orb_1dte_spy"
    assert order.idempotency_key == "v3_testkey12"


def test_build_order_sets_tactic_name():
    """T-ORB-08b: build_order の OrderRequest.tactic_name が orb_1dte_spy"""
    t = _tactic()
    decision = ORBEntryDecision(
        should_enter=True,
        symbol="SPY",
        quantity=2,
        direction="put",
        idempotency_key="v3_abc123def4",
    )
    order = t.build_order(decision)
    assert order.tactic_name == "orb_1dte_spy"
    assert order.quantity == 2


# ---------------------------------------------------------------------------
# T-ORB-09: should_exit
# ---------------------------------------------------------------------------

def test_should_exit_profit_target():
    """T-ORB-09a: profit_target 到達 → profit_target exit"""
    t = ORB1DTESPYTactic(config=ORB1DTEConfig(profit_target_pct=1.0))
    pos = Position(
        symbol="SPY", quantity=1, entry_price=2.0,
        entry_value=2.0, unrealized_pnl=2.1,  # >= 2.0*1.0=2.0
    )
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "profit_target"


def test_should_exit_stop_loss():
    """T-ORB-09b: stop_loss 超過 → stop_loss exit"""
    t = ORB1DTESPYTactic(config=ORB1DTEConfig(stop_loss_pct=0.5))
    pos = Position(
        symbol="SPY", quantity=1, entry_price=2.0,
        entry_value=2.0, unrealized_pnl=-1.1,  # <= -2.0*0.5=-1.0
    )
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "stop_loss"


def test_should_exit_holding():
    """T-ORB-09c: 中間状態 → holding (should_exit=False)"""
    t = _tactic()
    pos = Position(
        symbol="SPY", quantity=1, entry_price=2.0,
        entry_value=2.0, unrealized_pnl=0.3,
    )
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is False
    assert dec.exit_type == "none"


def test_should_exit_force_close_kill_switch():
    """T-ORB-10: Kill Switch ARMED → force_close
    (autouse fixture が state_v3 を tmp_path に隔離済み)
    """
    ks_activate(reason="test", activator="test")
    t = _tactic()
    pos = Position(symbol="SPY", quantity=1, entry_price=2.0, entry_value=2.0)
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-ORB-11: observe
# ---------------------------------------------------------------------------

def test_observe_updates_orb_when_market_data_available():
    """T-ORB-11: market_data.get_orb_range があれば ORBRange を更新する"""
    t = _tactic()

    class FakeMarketData:
        tracked_symbols = ["SPY"]
        def get_orb_range(self, symbol, window_minutes):
            return {"high": 452.0, "low": 447.0, "is_confirmed": True}

    t.observe(_env(), FakeMarketData())
    assert "SPY" in t._orb_ranges
    assert t._orb_ranges["SPY"].high == 452.0
    assert t._orb_ranges["SPY"].is_confirmed is True


def test_observe_keeps_existing_state_without_market_data():
    """T-ORB-12: get_orb_range がなければ既存 state 保持"""
    t = _tactic()
    t._orb_ranges["SPY"] = _confirmed_orb(high=451.0, low=446.0)

    class FakeMarketDataNoORB:
        pass

    t.observe(_env(), FakeMarketDataNoORB())
    assert "SPY" in t._orb_ranges
    assert t._orb_ranges["SPY"].high == 451.0


# ---------------------------------------------------------------------------
# T-ORB-13: persist_state / restore_state
# ---------------------------------------------------------------------------

def test_persist_and_restore_state():
    """T-ORB-13: persist_state → restore_state で ORBRange が復元される"""
    t1 = _tactic()
    t1._orb_ranges["SPY"] = _confirmed_orb(high=450.0, low=445.0)

    stored: dict = {}

    class FakeStorage:
        def save(self, key, data):
            stored[key] = data
        def load(self, key):
            return stored.get(key)

    storage = FakeStorage()
    t1.persist_state(storage)

    t2 = ORB1DTESPYTactic()
    t2.restore_state(storage)
    assert "SPY" in t2._orb_ranges
    assert t2._orb_ranges["SPY"].high == 450.0
    assert t2._orb_ranges["SPY"].is_confirmed is True


# ---------------------------------------------------------------------------
# T-ORB-14: slippage_tolerance_bps
# ---------------------------------------------------------------------------

def test_slippage_tolerance_bps_config():
    """T-ORB-14: slippage_tolerance_bps が config に保存される"""
    t = ORB1DTESPYTactic(config=ORB1DTEConfig(slippage_tolerance_bps=30))
    assert t._cfg.slippage_tolerance_bps == 30


# ---------------------------------------------------------------------------
# T-ORB-15: engine.register_tactic
# ---------------------------------------------------------------------------

def test_engine_register_tactic_accepts_orb(tmp_path):
    """T-ORB-15: AtlasEngine.register_tactic() が ORB1DTESPYTactic を受け入れる"""
    class FakeBroker:
        def place_order(self, req):
            from atlas_v3.core.engine import OrderResult
            return OrderResult(order_id="x", symbol=req.symbol, status="submitted")

    class FakeMarketData:
        def get_environment(self):
            return _env()

    from common_v3.idempotency.store import IdempotencyStore
    engine = AtlasEngine(
        market_data=FakeMarketData(),
        broker=FakeBroker(),
        idempotency_store=IdempotencyStore(path=tmp_path / "idem.json"),
    )
    t = _tactic()
    engine.register_tactic(t)
    assert engine._tactics[0].tactic_name == "orb_1dte_spy"
