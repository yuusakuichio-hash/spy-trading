"""tests/test_diagonal_spread_engine_20260425.py — DiagonalSpreadTactic 単体テスト 15 件

カバレッジ:
  T-01: TacticBase 継承 + tactic_type / tactic_name
  T-02: DiagonalSpreadConfig バリデーション (delta min>=max は ValueError)
  T-03: DiagonalSpreadConfig バリデーション (short_dte_max >= long_dte_min は ValueError)
  T-04: preflight — VIX 範囲外でスキップ
  T-05: preflight — term_ratio < 1.0 (backwardation) でスキップ
  T-06: preflight — Kill Switch ARMED で False
  T-07: should_enter — IVR NaN → TypeError
  T-08: should_enter — IVR 範囲外 (>100) → TypeError
  T-09: should_enter — IVR < ivr_min (下限未満) → should_enter=False
  T-10: should_enter — IVR > ivr_max (上限超) → should_enter=False
  T-11: should_enter — bias != "bull" (neutral) → should_enter=False
  T-12: should_enter — エントリーウィンドウ外 (ET 09:00) → should_enter=False
  T-13: should_enter — 全条件 pass → should_enter=True + idempotency_key 設定
  T-14: should_exit — roll: short_expiry 翌日到達 → exit_type="roll_short_expiry"
  T-15: should_exit — stop_loss 1.5x net_debit 超過 → exit_type="stop_loss"
  T-16 (bonus): should_exit — profit_target 30% 到達 → exit_type="profit_target"
  T-17 (bonus): should_enter — Kill Switch ARMED → None
  T-18 (bonus): build_order — should_enter=False で ValueError
  T-19 (bonus): build_exit_order — should_exit=False で ValueError
  T-20 (bonus): 異 expiry 発注: short_dte_target < long_dte_target 保証
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.bots.engines.diagonal_spread import (
    DIAG_DELTA_SHORT_MAX,
    DIAG_DELTA_SHORT_MIN,
    DIAG_LONG_DTE_MAX,
    DIAG_LONG_DTE_MIN,
    DiagonalPosition,
    DiagonalSpreadConfig,
    DiagonalSpreadEntryDecision,
    DiagonalSpreadExitDecision,
    DiagonalSpreadTactic,
)
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_kill_switch(tmp_path, monkeypatch):
    """Kill Switch を tmp_path に隔離してテスト間干渉を防ぐ。"""
    import common_v3.risk.kill_switch as ks_module

    tmp_state = tmp_path / "state_v3"
    tmp_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_module, "_STATE_DIR", tmp_state)
    monkeypatch.setattr(ks_module, "FLAG_FILE", tmp_state / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", tmp_state / "kill_switch_audit.jsonl")
    yield


def _env(
    vix: float = 20.0,
    ivr: float = 50.0,
    bias: str = "bull",
    term_ratio: float = 1.10,
    symbol: str = "SPY",
) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=2.0,
        gex=0.0,
        term_ratio=term_ratio,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={symbol: ivr},
    )


def _tactic(
    vix_min: float = 15.0,
    vix_max: float = 25.0,
    ivr_min: float = 30.0,
    ivr_max: float = 70.0,
) -> DiagonalSpreadTactic:
    cfg = DiagonalSpreadConfig(vix_min=vix_min, vix_max=vix_max,
                               ivr_min=ivr_min, ivr_max=ivr_max)
    return DiagonalSpreadTactic(config=cfg)


def _position(
    symbol: str = "SPY",
    net_debit: float = 2.0,
    unrealized_pnl: float = 0.0,
    short_expiry: date | None = None,
) -> DiagonalPosition:
    return DiagonalPosition(
        symbol=symbol,
        quantity=1,
        entry_price=net_debit,
        net_debit=net_debit,
        unrealized_pnl=unrealized_pnl,
        short_expiry=short_expiry,
    )


def _in_window_now(monkeypatch) -> None:
    """should_enter の _is_in_entry_window を ET 11:00 固定にする。"""
    monkeypatch.setattr(
        DiagonalSpreadTactic,
        "_is_in_entry_window",
        lambda self, now_et=None: True,
    )


def _out_window_now(monkeypatch) -> None:
    """should_enter の _is_in_entry_window を ET 09:00 固定にする（ウィンドウ外）。"""
    monkeypatch.setattr(
        DiagonalSpreadTactic,
        "_is_in_entry_window",
        lambda self, now_et=None: False,
    )


# ---------------------------------------------------------------------------
# T-01: TacticBase 継承 + プロパティ確認
# ---------------------------------------------------------------------------

def test_t01_tactic_base_inheritance_and_properties():
    """T-01: TacticBase 継承・tactic_type / tactic_name が正しい。"""
    tactic = DiagonalSpreadTactic()
    assert isinstance(tactic, TacticBase)
    assert tactic.tactic_type == "enter_exit"
    assert tactic.tactic_name == "diagonal_spread"


# ---------------------------------------------------------------------------
# T-02: Config バリデーション — delta 矛盾
# ---------------------------------------------------------------------------

def test_t02_config_delta_validation_raises():
    """T-02: delta_short_min >= delta_short_max で ValueError。"""
    with pytest.raises(ValueError, match="delta_short_min"):
        DiagonalSpreadConfig(delta_short_min=0.30, delta_short_max=0.20)


# ---------------------------------------------------------------------------
# T-03: Config バリデーション — short_dte >= long_dte_min
# ---------------------------------------------------------------------------

def test_t03_config_dte_expiry_order_raises():
    """T-03: short_dte_max >= long_dte_min で ValueError（異 expiry 保証）。"""
    with pytest.raises(ValueError, match="short_dte_max"):
        DiagonalSpreadConfig(short_dte_max=35, long_dte_min=30)


# ---------------------------------------------------------------------------
# T-04: preflight — VIX 範囲外
# ---------------------------------------------------------------------------

def test_t04_preflight_vix_out_of_range_returns_false():
    """T-04: VIX が設定範囲外なら preflight=False。"""
    tactic = _tactic(vix_min=15.0, vix_max=25.0)
    env = _env(vix=30.0)  # 範囲外
    assert tactic.preflight(env) is False


# ---------------------------------------------------------------------------
# T-05: preflight — term_ratio < 1.0 (backwardation)
# ---------------------------------------------------------------------------

def test_t05_preflight_backwardation_returns_false():
    """T-05: term_ratio < 1.0 (backwardation) なら preflight=False。"""
    tactic = _tactic()
    env = _env(term_ratio=0.90)  # backwardation
    assert tactic.preflight(env) is False


# ---------------------------------------------------------------------------
# T-06: preflight — Kill Switch ARMED
# ---------------------------------------------------------------------------

def test_t06_preflight_kill_switch_armed_returns_false():
    """T-06: Kill Switch ARMED なら preflight=False。"""
    from common_v3.risk.kill_switch import activate as ks_activate

    tactic = _tactic()
    env = _env()
    ks_activate(reason="test_t06")
    assert tactic.preflight(env) is False


# ---------------------------------------------------------------------------
# T-07: should_enter — IVR NaN → TypeError
# ---------------------------------------------------------------------------

def test_t07_should_enter_ivr_nan_raises_type_error(monkeypatch):
    """T-07: IVR が NaN なら TypeError。"""
    _in_window_now(monkeypatch)
    tactic = _tactic()
    env = MarketEnvironment(
        vix=20.0, vrp=2.0, gex=0.0, term_ratio=1.1,
        bias="bull",
        ivr_by_symbol={"SPY": float("nan")},
    )
    with pytest.raises(TypeError, match="NaN"):
        tactic.should_enter(env, "SPY")


# ---------------------------------------------------------------------------
# T-08: should_enter — IVR > 100 → TypeError
# ---------------------------------------------------------------------------

def test_t08_should_enter_ivr_out_of_scale_raises_type_error(monkeypatch):
    """T-08: IVR > 100 (スケール外) なら TypeError。"""
    _in_window_now(monkeypatch)
    tactic = _tactic()
    env = MarketEnvironment(
        vix=20.0, vrp=2.0, gex=0.0, term_ratio=1.1,
        bias="bull",
        ivr_by_symbol={"SPY": 105.0},
    )
    with pytest.raises(TypeError, match="0-100"):
        tactic.should_enter(env, "SPY")


# ---------------------------------------------------------------------------
# T-09: should_enter — IVR < ivr_min
# ---------------------------------------------------------------------------

def test_t09_should_enter_ivr_below_min_returns_false(monkeypatch):
    """T-09: IVR が ivr_min 未満なら should_enter=False。"""
    _in_window_now(monkeypatch)
    tactic = _tactic(ivr_min=30.0)
    env = _env(ivr=20.0)  # 範囲下限より低い
    result = tactic.should_enter(env, "SPY")
    assert result is not None
    assert result.should_enter is False
    assert "範囲外" in result.reason


# ---------------------------------------------------------------------------
# T-10: should_enter — IVR > ivr_max
# ---------------------------------------------------------------------------

def test_t10_should_enter_ivr_above_max_returns_false(monkeypatch):
    """T-10: IVR が ivr_max 超なら should_enter=False。"""
    _in_window_now(monkeypatch)
    tactic = _tactic(ivr_max=70.0)
    env = _env(ivr=80.0)  # 範囲上限より高い
    result = tactic.should_enter(env, "SPY")
    assert result is not None
    assert result.should_enter is False


# ---------------------------------------------------------------------------
# T-11: should_enter — bias != "bull"
# ---------------------------------------------------------------------------

def test_t11_should_enter_non_bull_bias_returns_false(monkeypatch):
    """T-11: bias が neutral なら should_enter=False（Diagonal は bull 専用）。"""
    _in_window_now(monkeypatch)
    tactic = _tactic()
    env = _env(bias="neutral")
    result = tactic.should_enter(env, "SPY")
    assert result is not None
    assert result.should_enter is False
    assert "bull" in result.reason


# ---------------------------------------------------------------------------
# T-12: should_enter — エントリーウィンドウ外
# ---------------------------------------------------------------------------

def test_t12_should_enter_outside_window_returns_false(monkeypatch):
    """T-12: ET 09:00 (ウィンドウ外) なら should_enter=False。"""
    _out_window_now(monkeypatch)
    tactic = _tactic()
    env = _env()
    result = tactic.should_enter(env, "SPY")
    assert result is not None
    assert result.should_enter is False
    assert "ウィンドウ" in result.reason


# ---------------------------------------------------------------------------
# T-13: should_enter — 全条件 pass → should_enter=True + 異 expiry 確認
# ---------------------------------------------------------------------------

def test_t13_should_enter_all_conditions_pass(monkeypatch):
    """T-13: 全条件 pass → should_enter=True、異 expiry 確認（short < long DTE）。"""
    _in_window_now(monkeypatch)
    tactic = _tactic()
    env = _env(vix=20.0, ivr=50.0, bias="bull", term_ratio=1.10)
    result = tactic.should_enter(env, "SPY")

    assert result is not None
    assert result.should_enter is True
    assert result.idempotency_key.startswith("v3_")
    # 異 expiry 保証: short DTE < long DTE
    assert result.short_dte_target < result.long_dte_target
    # delta が設定範囲内
    assert DIAG_DELTA_SHORT_MIN <= result.short_delta_target <= DIAG_DELTA_SHORT_MAX
    # long DTE が設定範囲内
    assert DIAG_LONG_DTE_MIN <= result.long_dte_target <= DIAG_LONG_DTE_MAX


# ---------------------------------------------------------------------------
# T-14: should_exit — short_expiry 翌日到達 → roll
# ---------------------------------------------------------------------------

def test_t14_should_exit_roll_on_short_expiry_next_day(monkeypatch):
    """T-14: short_expiry 翌日到達なら exit_type='roll_short_expiry'。"""
    tactic = _tactic()
    env = _env()

    # short_expiry を昨日に設定（今日が翌日 = ロールトリガー）
    yesterday = datetime.now(_ET).date() - timedelta(days=1)
    pos = _position(net_debit=2.0, short_expiry=yesterday)

    result = tactic.should_exit(pos, env)
    assert result.should_exit is True
    assert result.exit_type == "roll_short_expiry"


# ---------------------------------------------------------------------------
# T-15: should_exit — stop_loss 1.5x net_debit
# ---------------------------------------------------------------------------

def test_t15_should_exit_stop_loss_1_5x():
    """T-15: 含み損が net_debit * 1.5 超なら exit_type='stop_loss'。"""
    tactic = _tactic()
    env = _env()
    # net_debit=2.0, stop threshold = -3.0 → unrealized_pnl=-3.1 で発動
    pos = _position(net_debit=2.0, unrealized_pnl=-3.1)

    result = tactic.should_exit(pos, env)
    assert result.should_exit is True
    assert result.exit_type == "stop_loss"
    assert "1.5x" in result.reason


# ---------------------------------------------------------------------------
# T-16 (bonus): should_exit — profit_target 30%
# ---------------------------------------------------------------------------

def test_t16_should_exit_profit_target_30pct():
    """T-16: 含み益が net_debit * 0.30 超なら exit_type='profit_target'。"""
    tactic = _tactic()
    env = _env()
    # net_debit=2.0, profit threshold = 0.60 → unrealized_pnl=0.61 で発動
    pos = _position(net_debit=2.0, unrealized_pnl=0.61)

    result = tactic.should_exit(pos, env)
    assert result.should_exit is True
    assert result.exit_type == "profit_target"


# ---------------------------------------------------------------------------
# T-17 (bonus): should_enter — Kill Switch ARMED → None
# ---------------------------------------------------------------------------

def test_t17_should_enter_kill_switch_returns_none(monkeypatch):
    """T-17: Kill Switch ARMED なら should_enter が None を返す。"""
    from common_v3.risk.kill_switch import activate as ks_activate

    _in_window_now(monkeypatch)
    tactic = _tactic()
    env = _env()
    ks_activate(reason="test_t17")

    result = tactic.should_enter(env, "SPY")
    assert result is None


# ---------------------------------------------------------------------------
# T-18 (bonus): build_order — should_enter=False で ValueError
# ---------------------------------------------------------------------------

def test_t18_build_order_raises_on_no_enter():
    """T-18: should_enter=False の decision を build_order に渡すと ValueError。"""
    tactic = _tactic()
    bad_decision = DiagonalSpreadEntryDecision(
        should_enter=False, symbol="SPY", reason="test"
    )
    with pytest.raises(ValueError, match="should_enter=False"):
        tactic.build_order(bad_decision)


# ---------------------------------------------------------------------------
# T-19 (bonus): build_exit_order — should_exit=False で ValueError
# ---------------------------------------------------------------------------

def test_t19_build_exit_order_raises_on_no_exit():
    """T-19: should_exit=False の decision を build_exit_order に渡すと ValueError。"""
    tactic = _tactic()
    pos = _position()
    bad_decision = DiagonalSpreadExitDecision(should_exit=False, reason="test")
    with pytest.raises(ValueError, match="should_exit=False"):
        tactic.build_exit_order(pos, bad_decision)


# ---------------------------------------------------------------------------
# T-20 (bonus): 異 expiry 発注 — short_dte < long_dte が構造的に保証される
# ---------------------------------------------------------------------------

def test_t20_different_expiry_structural_guarantee():
    """T-20: Config の short_dte_max < long_dte_min 制約で異 expiry が構造的に保証される。

    DiagonalSpreadConfig は __post_init__ で short_dte_max < long_dte_min を強制する。
    これにより発注される 2 legs は必ず異なる満期を持つことが保証される。
    """
    cfg = DiagonalSpreadConfig(
        short_dte_max=14,   # short leg: 最大 14 DTE
        long_dte_min=30,    # long leg: 最低 30 DTE
        long_dte_max=60,
    )
    # 構成が通ること（ValueError が発生しないこと）で保証を確認
    tactic = DiagonalSpreadTactic(config=cfg)
    assert tactic._cfg.short_dte_max < tactic._cfg.long_dte_min
    # long_dte_target > short_dte_target を直接確認
    long_dte_target = (cfg.long_dte_min + cfg.long_dte_max) // 2
    short_dte_target = max(1, cfg.short_dte_max // 2)
    assert short_dte_target < long_dte_target
