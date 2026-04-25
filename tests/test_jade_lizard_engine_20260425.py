"""tests/test_jade_lizard_engine_20260425.py — JadeLizardTactic 単体テスト (15 件以上)

観点:
  T-JL-01: TacticBase ABC 継承・tactic_type / tactic_name
  T-JL-02: JadeLizardConfig バリデーション (spread_width_pts 範囲外)
  T-JL-03: JadeLizardConfig バリデーション (delta min >= max)
  T-JL-04: JadeLizardConfig バリデーション (profit_target_pct 範囲外)
  T-JL-05: preflight — Kill Switch ARMED → False
  T-JL-06: preflight — env=None → False
  T-JL-07: preflight — 正常 → True
  T-JL-08: should_enter — エントリー窓外 → should_enter=False
  T-JL-09: should_enter — IVR < ivr_min → should_enter=False
  T-JL-10: should_enter — IVR NaN → TypeError
  T-JL-11: should_enter — IVR 範囲外 → TypeError
  T-JL-12: should_enter — Kill Switch ARMED → should_enter=False
  T-JL-13: should_enter — 正常 (IVR >= 60 / 窓内) → 3 legs / total_credit / no_risk_upside
  T-JL-14: qty 計算 — compute_jade_lizard_qty 正常系
  T-JL-15: qty 計算 — account_risk_budget <= 0 → ValueError
  T-JL-16: build_orders — 3 件の OrderRequest が生成される / leg label がシンボルに含まれる
  T-JL-17: build_orders — should_enter=False → ValueError
  T-JL-18: should_exit — profit_target 達成 → exit_type=profit_target
  T-JL-19: should_exit — stop_loss 超過 → exit_type=stop_loss
  T-JL-20: should_exit — 15:50 ET 強制クローズ → exit_type=force_close
  T-JL-21: should_exit — Kill Switch ARMED → exit_type=force_close
  T-JL-22: IVR フィルタ — IVR=60 ちょうど → should_enter=True (境界値)
  T-JL-23: AtlasEngine.register_tactic — TacticBase 継承検証通過
  T-JL-24: spy_bot.py への非接触検証 (schg lock)
  T-JL-25: no_risk_upside 検証ロジック — total_credit >= spread×100 で True
"""
from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.bots.engines.jade_lizard import (
    JADE_LIZARD_PREFIX,
    JadeLizardConfig,
    JadeLizardEntryDecision,
    JadeLizardPosition,
    JadeLizardTactic,
    compute_jade_lizard_qty,
)
from atlas_v3.core.engine import AtlasEngine
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase
from common_v3.risk.kill_switch import activate as ks_activate

_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_kill_switch(tmp_path, monkeypatch):
    """Kill Switch state_v3 を tmp_path に隔離してテスト間干渉を防ぐ。"""
    import common_v3.risk.kill_switch as ks_module

    tmp_state = tmp_path / "state_v3"
    tmp_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_module, "_STATE_DIR", tmp_state)
    monkeypatch.setattr(ks_module, "FLAG_FILE", tmp_state / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", tmp_state / "kill_switch_audit.jsonl")
    yield


def _env(ivr_spy: float = 65.0, vix: float = 20.0) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=100.0,  # underlying_price の stub として利用
        gex=0.0,
        term_ratio=1.1,
        bias="neutral",
        ivr_by_symbol={"SPY": ivr_spy},
    )


def _clock_in_window() -> datetime:
    """エントリー窓内の時刻（10:30 ET）を返す。"""
    return datetime(2026, 4, 25, 10, 30, 0, tzinfo=_ET)


def _clock_out_window() -> datetime:
    """エントリー窓外の時刻（09:30 ET）を返す。"""
    return datetime(2026, 4, 25, 9, 30, 0, tzinfo=_ET)


def _clock_force_close() -> datetime:
    """強制クローズ時刻以降（15:55 ET）を返す。"""
    return datetime(2026, 4, 25, 15, 55, 0, tzinfo=_ET)


def _tactic(
    ivr_min: float = 60.0,
    spread_width_pts: float = 5.0,
    clock_fn=_clock_in_window,
    paper_mode: bool = True,
) -> JadeLizardTactic:
    cfg = JadeLizardConfig(
        ivr_min=ivr_min,
        spread_width_pts=spread_width_pts,
        paper_mode=paper_mode,
    )
    return JadeLizardTactic(config=cfg, clock_fn=clock_fn)


def _position(
    total_credit: float = 50.0,
    unrealized_pnl: float = 0.0,
) -> JadeLizardPosition:
    return JadeLizardPosition(
        symbol="SPY",
        quantity=1,
        total_credit=total_credit,
        unrealized_pnl=unrealized_pnl,
    )


# ---------------------------------------------------------------------------
# T-JL-01: TacticBase ABC 継承・tactic_type / tactic_name
# ---------------------------------------------------------------------------


def test_jade_lizard_is_tactic_base_instance():
    """T-JL-01a: JadeLizardTactic は TacticBase のインスタンス"""
    t = _tactic()
    assert isinstance(t, TacticBase)


def test_jade_lizard_tactic_type_is_enter_exit():
    """T-JL-01b: tactic_type は enter_exit"""
    t = _tactic()
    assert t.tactic_type == "enter_exit"


def test_jade_lizard_tactic_name_is_prefix():
    """T-JL-01c: tactic_name は JADE_LIZARD_PREFIX と一致"""
    t = _tactic()
    assert t.tactic_name == JADE_LIZARD_PREFIX


# ---------------------------------------------------------------------------
# T-JL-02: config バリデーション — spread_width_pts 範囲外
# ---------------------------------------------------------------------------


def test_config_spread_width_too_small():
    """T-JL-02a: spread_width_pts=4 (< 5) → ValueError"""
    with pytest.raises(ValueError, match="spread_width_pts"):
        JadeLizardConfig(spread_width_pts=4.0)


def test_config_spread_width_too_large():
    """T-JL-02b: spread_width_pts=11 (> 10) → ValueError"""
    with pytest.raises(ValueError, match="spread_width_pts"):
        JadeLizardConfig(spread_width_pts=11.0)


# ---------------------------------------------------------------------------
# T-JL-03: config バリデーション — delta min >= max
# ---------------------------------------------------------------------------


def test_config_put_delta_min_ge_max():
    """T-JL-03: short_put_delta_min >= short_put_delta_max → ValueError"""
    with pytest.raises(ValueError, match="short_put_delta_min"):
        JadeLizardConfig(short_put_delta_min=0.20, short_put_delta_max=0.20)


# ---------------------------------------------------------------------------
# T-JL-04: config バリデーション — profit_target_pct 範囲外
# ---------------------------------------------------------------------------


def test_config_profit_target_pct_out_of_range():
    """T-JL-04: profit_target_pct=1.0 (境界値・範囲外) → ValueError"""
    with pytest.raises(ValueError, match="profit_target_pct"):
        JadeLizardConfig(profit_target_pct=1.0)


# ---------------------------------------------------------------------------
# T-JL-05: preflight — Kill Switch ARMED
# ---------------------------------------------------------------------------


def test_preflight_kill_switch_armed():
    """T-JL-05: Kill Switch ARMED 時は preflight=False"""
    ks_activate(reason="test")
    t = _tactic()
    env = _env()
    assert t.preflight(env) is False


# ---------------------------------------------------------------------------
# T-JL-06: preflight — env=None
# ---------------------------------------------------------------------------


def test_preflight_env_none():
    """T-JL-06: env=None の場合は preflight=False"""
    t = _tactic()
    assert t.preflight(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T-JL-07: preflight — 正常
# ---------------------------------------------------------------------------


def test_preflight_normal():
    """T-JL-07: 正常環境では preflight=True"""
    t = _tactic()
    env = _env()
    assert t.preflight(env) is True


# ---------------------------------------------------------------------------
# T-JL-08: should_enter — エントリー窓外
# ---------------------------------------------------------------------------


def test_should_enter_outside_window():
    """T-JL-08: エントリー窓外（09:30 ET）は should_enter=False"""
    t = _tactic(clock_fn=_clock_out_window)
    decision = t.should_enter(_env(), "SPY")
    assert decision.should_enter is False
    assert "entry_window_closed" in decision.reason


# ---------------------------------------------------------------------------
# T-JL-09: should_enter — IVR < ivr_min
# ---------------------------------------------------------------------------


def test_should_enter_ivr_below_min():
    """T-JL-09: IVR=50 < ivr_min=60 → should_enter=False"""
    t = _tactic()
    env = _env(ivr_spy=50.0)
    decision = t.should_enter(env, "SPY")
    assert decision.should_enter is False
    assert "IVR" in decision.reason


# ---------------------------------------------------------------------------
# T-JL-10: should_enter — IVR NaN
# ---------------------------------------------------------------------------


def test_should_enter_ivr_nan():
    """T-JL-10: IVR=NaN → TypeError"""
    t = _tactic()
    env = _env(ivr_spy=float("nan"))
    with pytest.raises(TypeError, match="NaN"):
        t.should_enter(env, "SPY")


# ---------------------------------------------------------------------------
# T-JL-11: should_enter — IVR 範囲外
# ---------------------------------------------------------------------------


def test_should_enter_ivr_out_of_scale():
    """T-JL-11: IVR=150 (0-100 範囲外) → TypeError"""
    t = _tactic()
    env = _env(ivr_spy=150.0)
    with pytest.raises(TypeError, match="0-100"):
        t.should_enter(env, "SPY")


# ---------------------------------------------------------------------------
# T-JL-12: should_enter — Kill Switch ARMED
# ---------------------------------------------------------------------------


def test_should_enter_kill_switch_armed():
    """T-JL-12: Kill Switch ARMED → should_enter=False"""
    ks_activate(reason="test")
    t = _tactic()
    decision = t.should_enter(_env(), "SPY")
    assert decision.should_enter is False
    assert "kill_switch_armed" in decision.reason


# ---------------------------------------------------------------------------
# T-JL-13: should_enter — 正常 (3 legs / total_credit / no_risk_upside)
# ---------------------------------------------------------------------------


def test_should_enter_normal_three_legs():
    """T-JL-13a: 正常エントリー → legs が 3 件返る"""
    t = _tactic()
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    assert decision.should_enter is True
    assert len(decision.legs) == 3


def test_should_enter_normal_leg_labels():
    """T-JL-13b: 3 legs の label が short_put / short_call / long_call"""
    t = _tactic()
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    labels = [leg.label for leg in decision.legs]
    assert labels == ["short_put", "short_call", "long_call"]


def test_should_enter_normal_short_put_side_sell():
    """T-JL-13c: short_put の side が sell"""
    t = _tactic()
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    short_put = next(l for l in decision.legs if l.label == "short_put")
    assert short_put.side == "sell"


def test_should_enter_normal_long_call_side_buy():
    """T-JL-13d: long_call の side が buy（翼ロング）"""
    t = _tactic()
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    long_call = next(l for l in decision.legs if l.label == "long_call")
    assert long_call.side == "buy"


def test_should_enter_normal_idempotency_key_not_empty():
    """T-JL-13e: idempotency_key が空文字でない"""
    t = _tactic()
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    assert decision.idempotency_key != ""


# ---------------------------------------------------------------------------
# T-JL-14: qty 計算 — 正常系
# ---------------------------------------------------------------------------


def test_compute_qty_normal():
    """T-JL-14: budget=1000 / spread_width=5pt → qty=2"""
    qty = compute_jade_lizard_qty(
        account_risk_budget=1000.0,
        spread_width_pts=5.0,
        total_credit_per_contract=2.0,
    )
    # 1 contract リスク = 5 × 100 = 500。1000 // 500 = 2
    assert qty == 2


def test_compute_qty_min_one():
    """T-JL-14b: budget < リスク 1 コントラクト → qty=1 (最低 1 保証)"""
    qty = compute_jade_lizard_qty(
        account_risk_budget=100.0,
        spread_width_pts=10.0,
        total_credit_per_contract=5.0,
    )
    # 1 contract リスク = 10 × 100 = 1000 > 100 → qty = max(1, 0) = 1
    assert qty == 1


# ---------------------------------------------------------------------------
# T-JL-15: qty 計算 — account_risk_budget <= 0 → ValueError
# ---------------------------------------------------------------------------


def test_compute_qty_negative_budget():
    """T-JL-15: account_risk_budget=0 → ValueError"""
    with pytest.raises(ValueError, match="account_risk_budget"):
        compute_jade_lizard_qty(
            account_risk_budget=0.0,
            spread_width_pts=5.0,
            total_credit_per_contract=2.0,
        )


# ---------------------------------------------------------------------------
# T-JL-16: build_orders — 3 件 OrderRequest / leg label がシンボルに含まれる
# ---------------------------------------------------------------------------


def _gate_pass():
    from unittest.mock import patch as _patch
    return _patch(
        "common_v3.risk.pre_trade_check.check_order_critical_only",
        lambda *a, **k: type("_GR", (), {"allowed": True, "reason": ""})(),
    )


def test_build_orders_returns_three_orders():
    """T-JL-16a: build_orders は 3 件の OrderRequest を返す"""
    t = _tactic()
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    with _gate_pass():
        orders = t.build_orders(decision)
    assert len(orders) == 3


def test_build_orders_leg_labels_in_symbol():
    """T-JL-16b: 各 OrderRequest の symbol に leg label が含まれる"""
    t = _tactic()
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    with _gate_pass():
        orders = t.build_orders(decision)
    symbols = [o.symbol for o in orders]
    assert any("short_put" in s for s in symbols)
    assert any("short_call" in s for s in symbols)
    assert any("long_call" in s for s in symbols)


def test_build_orders_paper_mode_order_type():
    """T-JL-16c: paper_mode=True のとき order_type=paper_limit"""
    t = _tactic(paper_mode=True)
    decision = t.should_enter(_env(ivr_spy=65.0), "SPY")
    with _gate_pass():
        orders = t.build_orders(decision)
    assert all(o.order_type == "paper_limit" for o in orders)


# ---------------------------------------------------------------------------
# T-JL-17: build_orders — should_enter=False → ValueError
# ---------------------------------------------------------------------------


def test_build_orders_raises_on_no_entry():
    """T-JL-17: should_enter=False の decision → ValueError"""
    t = _tactic()
    bad_decision = JadeLizardEntryDecision(should_enter=False, symbol="SPY")
    with pytest.raises(ValueError, match="should_enter=False"):
        t.build_orders(bad_decision)


# ---------------------------------------------------------------------------
# T-JL-18: should_exit — profit_target
# ---------------------------------------------------------------------------


def test_should_exit_profit_target():
    """T-JL-18: unrealized_pnl >= total_credit × 0.50 → profit_target"""
    t = _tactic()
    pos = _position(total_credit=100.0, unrealized_pnl=55.0)
    result = t.should_exit(pos, _env())
    assert result.should_exit is True
    assert result.exit_type == "profit_target"


# ---------------------------------------------------------------------------
# T-JL-19: should_exit — stop_loss
# ---------------------------------------------------------------------------


def test_should_exit_stop_loss():
    """T-JL-19: unrealized_pnl <= -total_credit × 2.0 → stop_loss"""
    t = _tactic()
    pos = _position(total_credit=100.0, unrealized_pnl=-210.0)
    result = t.should_exit(pos, _env())
    assert result.should_exit is True
    assert result.exit_type == "stop_loss"


# ---------------------------------------------------------------------------
# T-JL-20: should_exit — 15:50 ET 強制クローズ
# ---------------------------------------------------------------------------


def test_should_exit_force_close_time():
    """T-JL-20: 15:50 ET 以降は force_close"""
    t = _tactic(clock_fn=_clock_force_close)
    pos = _position(total_credit=100.0, unrealized_pnl=10.0)
    result = t.should_exit(pos, _env())
    assert result.should_exit is True
    assert result.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-JL-21: should_exit — Kill Switch ARMED
# ---------------------------------------------------------------------------


def test_should_exit_kill_switch_armed():
    """T-JL-21: Kill Switch ARMED → force_close"""
    ks_activate(reason="test")
    t = _tactic()
    pos = _position(total_credit=100.0, unrealized_pnl=0.0)
    result = t.should_exit(pos, _env())
    assert result.should_exit is True
    assert result.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-JL-22: IVR フィルタ — IVR=60 境界値
# ---------------------------------------------------------------------------


def test_should_enter_ivr_boundary():
    """T-JL-22: IVR=60.0 (ivr_min=60.0 ちょうど) → should_enter=True"""
    t = _tactic(ivr_min=60.0)
    env = _env(ivr_spy=60.0)
    decision = t.should_enter(env, "SPY")
    assert decision.should_enter is True


# ---------------------------------------------------------------------------
# T-JL-23: AtlasEngine.register_tactic — TacticBase 継承検証通過
# ---------------------------------------------------------------------------


def test_register_tactic_succeeds():
    """T-JL-23: JadeLizardTactic は AtlasEngine.register_tactic に登録できる"""
    from unittest.mock import MagicMock

    mock_market_data = MagicMock()
    mock_broker = MagicMock()
    engine = AtlasEngine(market_data=mock_market_data, broker=mock_broker)
    t = _tactic()
    engine.register_tactic(t)  # TypeError が出ないこと
    assert t in engine._tactics


# ---------------------------------------------------------------------------
# T-JL-24: spy_bot.py への非接触検証 (schg lock)
# ---------------------------------------------------------------------------


def test_spy_bot_not_imported_by_jade_lizard():
    """T-JL-24: jade_lizard モジュールが spy_bot.py を import していないこと (schg lock)"""
    import importlib
    import sys

    # jade_lizard をリロードして import グラフを確認
    mod_name = "atlas_v3.bots.engines.jade_lizard"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    importlib.import_module(mod_name)

    # spy_bot が sys.modules に含まれていないこと
    assert "spy_bot" not in sys.modules, (
        "jade_lizard が spy_bot.py を間接的に import しています。schg lock 違反。"
    )


# ---------------------------------------------------------------------------
# T-JL-25: no_risk_upside 検証ロジック
# ---------------------------------------------------------------------------


def test_no_risk_upside_true_when_credit_covers_spread():
    """T-JL-25a: total_credit >= spread_width × 100 → no_risk_upside=True"""
    from atlas_v3.bots.engines.jade_lizard import JadeLizardTactic

    t = JadeLizardTactic.__new__(JadeLizardTactic)
    # 静的メソッドとして直接呼べる
    assert JadeLizardTactic._check_no_risk_upside(
        total_credit=550.0, spread_width_pts=5.0
    ) is True


def test_no_risk_upside_false_when_credit_insufficient():
    """T-JL-25b: total_credit < spread_width × 100 → no_risk_upside=False"""
    assert JadeLizardTactic._check_no_risk_upside(
        total_credit=400.0, spread_width_pts=5.0
    ) is False


def test_no_risk_upside_boundary_exactly_equal():
    """T-JL-25c: total_credit == spread_width × 100 → no_risk_upside=True (境界値)"""
    assert JadeLizardTactic._check_no_risk_upside(
        total_credit=500.0, spread_width_pts=5.0
    ) is True
