"""tests/test_atlas_v3_earnings_iv_crush.py — EarningsIVCrushTactic 単体テスト

仕様: data/specs/v3/atlas_spec_v3_20260422.md B5 (earnings_iv_crush)
観点: entry/exit/stop/kill_switch連動/idempotency/slippage/engine.register_tactic
     state 保持 / persist_state / restore_state / observe
"""
from __future__ import annotations

import pytest

from atlas_v3.core.engine import AtlasEngine, OrderRequest
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase
from atlas_v3.strategies.earnings_iv_crush import (
    EarningsEntryDecision,
    EarningsIVCrushConfig,
    EarningsIVCrushTactic,
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


def _env(vix: float = 20.0, ivr_tsla: float = 80.0) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=3.0,
        gex=0.0,
        term_ratio=1.0,
        bias="neutral",
        ivr_by_symbol={"TSLA": ivr_tsla, "AAPL": 65.0, "NVDA": 55.0},
    )


def _tactic(
    earnings_symbols: dict[str, str] | None = None,
    ivr_min: float = 50.0,
) -> EarningsIVCrushTactic:
    cfg = EarningsIVCrushConfig(ivr_min=ivr_min, slippage_tolerance_bps=15)
    return EarningsIVCrushTactic(
        config=cfg,
        earnings_symbols=earnings_symbols or {"TSLA": "2026-04-24", "AAPL": "2026-04-25"},
    )


# ---------------------------------------------------------------------------
# T-EIC-01: TacticBase ABC 継承
# ---------------------------------------------------------------------------

def test_earnings_is_tactic_base_instance():
    """T-EIC-01: EarningsIVCrushTactic は TacticBase のインスタンス"""
    t = _tactic()
    assert isinstance(t, TacticBase)


def test_earnings_tactic_type_and_name():
    """T-EIC-02: tactic_type is state_carrying and tactic_name is earnings_iv_crush"""
    t = _tactic()
    assert t.tactic_type == "state_carrying"
    assert t.tactic_name == "earnings_iv_crush"


# ---------------------------------------------------------------------------
# T-EIC-03: preflight
# ---------------------------------------------------------------------------

def test_preflight_pass_when_env_ok():
    """T-EIC-03a: 正常環境なら preflight=True"""
    t = _tactic()
    env = _env(vix=20.0)
    assert t.preflight(env) is True


def test_preflight_fail_on_none_env():
    """T-EIC-03b: env=None は False"""
    t = _tactic()
    assert t.preflight(None) is False  # type: ignore[arg-type]


def test_preflight_fail_when_vix_too_high():
    """T-EIC-03c: VIX > vix_max → False"""
    t = EarningsIVCrushTactic(config=EarningsIVCrushConfig(vix_max=30.0))
    env = _env(vix=36.0)
    assert t.preflight(env) is False


def test_preflight_fail_on_kill_switch():
    """T-EIC-03d: Kill Switch ARMED → False
    (autouse fixture が state_v3 を tmp_path に隔離済み)
    """
    ks_activate(reason="test", activator="test")
    t = _tactic()
    assert t.preflight(_env()) is False


# ---------------------------------------------------------------------------
# T-EIC-04: should_enter (state_carrying: 複数銘柄評価)
# ---------------------------------------------------------------------------

def test_should_enter_returns_list_of_decisions():
    """T-EIC-04a: should_enter はリストを返す"""
    t = _tactic(earnings_symbols={"TSLA": "2026-04-24"})
    env = _env()
    decisions = t.should_enter(env, ["TSLA", "AAPL"])
    assert isinstance(decisions, list)


def test_should_enter_returns_true_for_calendar_symbol_high_ivr():
    """T-EIC-04b: 決算カレンダー登録済み + IVR >= ivr_min → should_enter=True"""
    t = _tactic(earnings_symbols={"TSLA": "2026-04-24"}, ivr_min=50.0)
    env = _env(ivr_tsla=80.0)
    decisions = t.should_enter(env, ["TSLA"])
    entering = [d for d in decisions if d.should_enter]
    assert len(entering) == 1
    assert entering[0].symbol == "TSLA"


def test_should_enter_returns_false_for_unknown_symbol():
    """T-EIC-04c: 決算カレンダー未登録 symbol → should_enter=False (skip)"""
    t = _tactic(earnings_symbols={"TSLA": "2026-04-24"})
    env = _env()
    decisions = t.should_enter(env, ["GOOGL"])
    # GOOGL はカレンダーにないため decisions に含まれない（skip）
    assert all(not d.should_enter for d in decisions) or len(decisions) == 0


def test_should_enter_idempotency_key_set():
    """T-EIC-05: should_enter=True の決定に idempotency_key が設定される"""
    t = _tactic(earnings_symbols={"TSLA": "2026-04-24"}, ivr_min=50.0)
    env = _env(ivr_tsla=80.0)
    decisions = t.should_enter(env, ["TSLA"])
    entering = [d for d in decisions if d.should_enter]
    assert len(entering) >= 1
    assert entering[0].idempotency_key.startswith("v3_")


# ---------------------------------------------------------------------------
# T-EIC-06: build_order
# ---------------------------------------------------------------------------

def test_build_order_returns_order_request():
    """T-EIC-06: build_order は OrderRequest を返す"""
    t = _tactic(earnings_symbols={"TSLA": "2026-04-24"})
    env = _env(ivr_tsla=80.0)
    decisions = t.should_enter(env, ["TSLA"])
    entering = [d for d in decisions if d.should_enter]
    assert len(entering) >= 1
    order = t.build_order(entering[0])
    assert isinstance(order, OrderRequest)
    assert order.tactic_name == "earnings_iv_crush"


def test_build_order_raises_on_no_enter():
    """T-EIC-07: should_enter=False の decision → ValueError"""
    t = _tactic()
    bad = EarningsEntryDecision(should_enter=False, symbol="TSLA")
    with pytest.raises(ValueError):
        t.build_order(bad)


# ---------------------------------------------------------------------------
# T-EIC-08: should_exit
# ---------------------------------------------------------------------------

def test_should_exit_profit_target():
    """T-EIC-08a: profit_target 到達 → profit_target exit
    earnings_date は未来日付にして post_earnings 判定をスキップ。
    """
    t = EarningsIVCrushTactic(config=EarningsIVCrushConfig(profit_target_pct=0.4))
    pos = Position(
        symbol="TSLA", quantity=1, entry_price=5.0,
        entry_value=5.0, unrealized_pnl=2.1,  # 2.1 >= 5.0*0.4=2.0
        earnings_date="2099-12-31",   # 未来 → post_earnings_close には入らない
    )
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "profit_target"


def test_should_exit_stop_loss():
    """T-EIC-08b: stop_loss 超過 → stop_loss exit"""
    t = EarningsIVCrushTactic(config=EarningsIVCrushConfig(stop_loss_pct=1.5))
    pos = Position(
        symbol="TSLA", quantity=1, entry_price=5.0,
        entry_value=5.0, unrealized_pnl=-8.0,  # -8.0 <= -5.0*1.5=-7.5
        earnings_date="2099-12-31",
    )
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "stop_loss"


def test_should_exit_post_earnings():
    """T-EIC-08c: 決算通過後 → post_earnings_close"""
    t = _tactic()
    pos = Position(
        symbol="TSLA", quantity=1, entry_price=5.0,
        entry_value=5.0, unrealized_pnl=0.0,
        earnings_date="2020-01-01",  # 大昔 → 通過後
    )
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "post_earnings_close"


def test_should_exit_force_close_on_kill_switch():
    """T-EIC-09: Kill Switch ARMED → force_close
    (autouse fixture が state_v3 を tmp_path に隔離済み)
    """
    ks_activate(reason="test", activator="test")
    t = _tactic()
    pos = Position(symbol="TSLA", quantity=1, entry_price=5.0, entry_value=5.0)
    dec = t.should_exit(pos, _env())
    assert dec.should_exit is True
    assert dec.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-EIC-10: observe / state
# ---------------------------------------------------------------------------

def test_observe_uses_market_data_if_available():
    """T-EIC-10: observe は market_data.get_earnings_calendar があれば更新する"""
    t = EarningsIVCrushTactic()

    class FakeMarketData:
        def get_earnings_calendar(self, date_str):
            return {"NVDA": "2026-04-25"}

    env = _env()
    t.observe(env, FakeMarketData())
    assert "NVDA" in t._earnings_calendar


def test_observe_keeps_existing_state_without_market_data():
    """T-EIC-11: get_earnings_calendar がなければ既存 state 保持"""
    t = _tactic(earnings_symbols={"TSLA": "2026-04-24"})

    class FakeMarketDataNoCalendar:
        pass

    t.observe(_env(), FakeMarketDataNoCalendar())
    assert "TSLA" in t._earnings_calendar


# ---------------------------------------------------------------------------
# T-EIC-12: persist_state / restore_state
# ---------------------------------------------------------------------------

def test_persist_and_restore_state():
    """T-EIC-12: persist_state → restore_state で calendar が復元される"""
    t1 = _tactic(earnings_symbols={"TSLA": "2026-04-24", "AAPL": "2026-04-25"})

    stored: dict = {}

    class FakeStorage:
        def save(self, key, data):
            stored[key] = data
        def load(self, key):
            return stored.get(key)

    storage = FakeStorage()
    t1.persist_state(storage)

    t2 = EarningsIVCrushTactic()
    t2.restore_state(storage)
    assert t2._earnings_calendar == {"TSLA": "2026-04-24", "AAPL": "2026-04-25"}


# ---------------------------------------------------------------------------
# T-EIC-13: slippage_tolerance_bps
# ---------------------------------------------------------------------------

def test_slippage_tolerance_bps_config():
    """T-EIC-13: slippage_tolerance_bps が config に保存される"""
    t = EarningsIVCrushTactic(config=EarningsIVCrushConfig(slippage_tolerance_bps=20))
    assert t._cfg.slippage_tolerance_bps == 20


# ---------------------------------------------------------------------------
# T-EIC-14: engine.register_tactic
# ---------------------------------------------------------------------------

def test_engine_register_tactic_accepts_earnings(tmp_path):
    """T-EIC-14: AtlasEngine.register_tactic() が EarningsIVCrushTactic を受け入れる"""
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
    assert engine._tactics[0].tactic_name == "earnings_iv_crush"
