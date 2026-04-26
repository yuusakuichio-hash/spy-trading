"""tests/test_atlas_v3_ic_sell.py — ICSellTactic 単体テスト

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 (ic_sell)
観点: entry/exit/stop/kill_switch連動/idempotency/slippage/engine.register_tactic
"""
from __future__ import annotations

import pytest

from atlas_v3.core.engine import AtlasEngine, OrderRequest
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase
from atlas_v3.strategies.ic_sell import (
    ICSellConfig,
    ICSellEntryDecision,
    ICSellTactic,
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


def _env(vix: float = 20.0, ivr_spy: float = 55.0, bias: str = "neutral") -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=2.0,
        gex=0.0,
        term_ratio=1.1,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={"SPY": ivr_spy},
    )


def _tactic(slippage_bps: int = 10) -> ICSellTactic:
    cfg = ICSellConfig(slippage_tolerance_bps=slippage_bps)
    return ICSellTactic(config=cfg)


# ---------------------------------------------------------------------------
# T-IC-01: TacticBase ABC 継承
# ---------------------------------------------------------------------------

def test_ic_sell_is_tactic_base_instance():
    """T-IC-01: ICSellTactic は TacticBase のインスタンス"""
    t = _tactic()
    assert isinstance(t, TacticBase)


def test_ic_sell_tactic_type_and_name():
    """T-IC-02: tactic_type is enter_exit and tactic_name is ic_sell"""
    t = _tactic()
    assert t.tactic_type == "enter_exit"
    assert t.tactic_name == "ic_sell"


# ---------------------------------------------------------------------------
# T-IC-03: preflight
# ---------------------------------------------------------------------------

def test_preflight_pass_when_env_ok():
    """T-IC-03a: VIX が適正範囲内なら preflight=True"""
    t = _tactic()
    env = _env(vix=20.0)
    assert t.preflight(env) is True


def test_preflight_fail_on_none_env():
    """T-IC-03b: env=None は preflight=False"""
    t = _tactic()
    assert t.preflight(None) is False  # type: ignore[arg-type]


def test_preflight_fail_when_vix_too_high():
    """T-IC-03c: VIX > vix_max は preflight=False"""
    t = ICSellTactic(config=ICSellConfig(vix_max=25.0))
    env = _env(vix=30.0)
    assert t.preflight(env) is False


def test_preflight_fail_when_kill_switch_armed():
    """T-IC-03d: Kill Switch ARMED なら preflight=False
    (autouse fixture が state_v3 を tmp_path に隔離済み)
    """
    ks_activate(reason="test", activator="test")
    t = _tactic()
    env = _env(vix=20.0)
    assert t.preflight(env) is False


# ---------------------------------------------------------------------------
# T-IC-04: should_enter
# ---------------------------------------------------------------------------

def test_should_enter_returns_true_when_conditions_met():
    """T-IC-04a: IVR >= ivr_min + neutral bias → should_enter=True"""
    t = ICSellTactic(config=ICSellConfig(ivr_min=40.0))
    env = _env(vix=20.0, ivr_spy=55.0, bias="neutral")
    decision = t.should_enter(env, "SPY")
    assert decision.should_enter is True
    assert decision.symbol == "SPY"


def test_should_enter_returns_false_when_ivr_too_low():
    """T-IC-04b: IVR < ivr_min → should_enter=False"""
    t = ICSellTactic(config=ICSellConfig(ivr_min=60.0))
    env = _env(vix=20.0, ivr_spy=40.0, bias="neutral")
    decision = t.should_enter(env, "SPY")
    assert decision.should_enter is False


def test_should_enter_returns_false_when_bias_directional():
    """T-IC-04c: bias=bull → IC は中立専用・should_enter=False"""
    t = _tactic()
    env = _env(vix=20.0, ivr_spy=55.0, bias="bull")
    decision = t.should_enter(env, "SPY")
    assert decision.should_enter is False


# ---------------------------------------------------------------------------
# T-IC-05: idempotency_key
# ---------------------------------------------------------------------------

def test_should_enter_sets_idempotency_key():
    """T-IC-05: should_enter=True のとき idempotency_key が設定される"""
    t = _tactic()
    env = _env(vix=20.0, ivr_spy=55.0, bias="neutral")
    decision = t.should_enter(env, "SPY")
    assert decision.should_enter is True
    assert decision.idempotency_key.startswith("v3_")


def test_build_order_carries_idempotency_key():
    """T-IC-06: build_order が decision の idempotency_key を OrderRequest に転写"""
    t = _tactic()
    env = _env(vix=20.0, ivr_spy=55.0, bias="neutral")
    decision = t.should_enter(env, "SPY")
    order = t.build_order(decision)
    assert isinstance(order, OrderRequest)
    assert order.idempotency_key == decision.idempotency_key
    assert order.tactic_name == "ic_sell"


def test_build_order_raises_on_no_enter_decision():
    """T-IC-07: should_enter=False の decision を build_order に渡すと ValueError"""
    t = _tactic()
    bad_decision = ICSellEntryDecision(should_enter=False, symbol="SPY")
    with pytest.raises(ValueError):
        t.build_order(bad_decision)


# ---------------------------------------------------------------------------
# T-IC-08: should_exit / stop loss
# ---------------------------------------------------------------------------

def test_should_exit_profit_target():
    """T-IC-08a: unrealized_pnl >= profit_threshold → profit_target exit"""
    t = ICSellTactic(config=ICSellConfig(profit_target_pct=0.5))
    pos = Position(
        symbol="SPY", quantity=1, entry_price=1.0,
        max_credit=2.0, unrealized_pnl=1.1,  # 1.1 >= 2.0*0.5=1.0
    )
    env = _env()
    dec = t.should_exit(pos, env)
    assert dec.should_exit is True
    assert dec.exit_type == "profit_target"


def test_should_exit_stop_loss():
    """T-IC-08b: unrealized_pnl <= -stop_loss_threshold → stop_loss exit"""
    t = ICSellTactic(config=ICSellConfig(stop_loss_pct=2.0))
    pos = Position(
        symbol="SPY", quantity=1, entry_price=1.0,
        max_credit=2.0, unrealized_pnl=-4.1,  # -4.1 <= -2.0*2.0=-4.0
    )
    env = _env()
    dec = t.should_exit(pos, env)
    assert dec.should_exit is True
    assert dec.exit_type == "stop_loss"


def test_should_exit_holding():
    """T-IC-08c: 中間状態 → holding (should_exit=False)"""
    t = _tactic()
    pos = Position(
        symbol="SPY", quantity=1, entry_price=1.0,
        max_credit=2.0, unrealized_pnl=0.2,
    )
    env = _env()
    dec = t.should_exit(pos, env)
    assert dec.should_exit is False
    assert dec.exit_type == "none"


def test_should_exit_force_close_on_kill_switch():
    """T-IC-09: Kill Switch ARMED → force_close
    (autouse fixture が state_v3 を tmp_path に隔離済み)
    """
    ks_activate(reason="test", activator="test")
    t = _tactic()
    pos = Position(symbol="SPY", quantity=1, entry_price=1.0, max_credit=2.0)
    env = _env()
    dec = t.should_exit(pos, env)
    assert dec.should_exit is True
    assert dec.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-IC-10: slippage_tolerance_bps config が設定されること
# ---------------------------------------------------------------------------

def test_slippage_tolerance_bps_config():
    """T-IC-10: slippage_tolerance_bps が config に保存される"""
    t = ICSellTactic(config=ICSellConfig(slippage_tolerance_bps=25))
    assert t._cfg.slippage_tolerance_bps == 25


# ---------------------------------------------------------------------------
# T-IC-11: engine.register_tactic
# ---------------------------------------------------------------------------

def test_engine_register_tactic_accepts_ic_sell(tmp_path):
    """T-IC-11: AtlasEngine.register_tactic() が ICSellTactic を受け入れる"""
    class FakeBroker:
        def place_order(self, req):
            from atlas_v3.core.engine import OrderResult
            return OrderResult(order_id="test", symbol=req.symbol, status="submitted")

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
    assert len(engine._tactics) == 1
    assert engine._tactics[0].tactic_name == "ic_sell"


def test_engine_register_tactic_rejects_non_tactic_base():
    """T-IC-12: TacticBase 未継承オブジェクトは register_tactic で TypeError"""

    class NotATactic:
        pass

    class FakeBroker:
        def place_order(self, req):
            pass

    class FakeMarketData:
        def get_environment(self):
            return _env()

    engine = AtlasEngine(market_data=FakeMarketData(), broker=FakeBroker())
    with pytest.raises(TypeError):
        engine.register_tactic(NotATactic())  # type: ignore[arg-type]
