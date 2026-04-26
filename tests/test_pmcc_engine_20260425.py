"""tests/test_pmcc_engine_20260425.py — PMCCTactic 単体テスト 15 件

検証項目:
  T01: 2 leg 発注 — build_orders が long_call + short_call の 2 OrderRequest を返す
  T02: 2 leg の expiry が異なること (long DTE > short DTE)
  T03: long call が 60-90 DTE フィルタを通過
  T04: long call delta が 0.75-0.90 範囲内
  T05: short call delta が 0.25-0.35 範囲内
  T06: weekly short roll — short_call_dte <= 1 で weekly_short_roll
  T07: weekly short roll — short_call_expiry 翌日到達でロール
  T08: long call roll trigger — long_call_dte <= 60 で long_call_roll
  T09: 上方向 bias フィルタ — bias != "bull" でエントリー拒否
  T10: short call 50% profit exit
  T11: 全体損切り (stop_loss)
  T12: Kill Switch ARMED でエントリー拒否
  T13: Kill Switch ARMED で force_close
  T14: IVR 範囲外でエントリー拒否
  T15: PMCCConfig バリデーション (long_delta_min > short_delta_max 違反)
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.bots.engines.pmcc import (
    PMCC_LONG_DELTA_MAX,
    PMCC_LONG_DELTA_MIN,
    PMCC_LONG_DTE_MAX,
    PMCC_LONG_DTE_MIN,
    PMCC_LONG_ROLL_TRIGGER_DTE,
    PMCC_SHORT_DELTA_MAX,
    PMCC_SHORT_DELTA_MIN,
    PMCCConfig,
    PMCCPosition,
    PMCCTactic,
)
from atlas_v3.core.env_observer import MarketEnvironment

_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# テスト共通ヘルパー
# ---------------------------------------------------------------------------

def _make_env(
    bias: str = "bull",
    ivr: float = 40.0,
    vix: float = 18.0,
    symbol: str = "SPY",
) -> MarketEnvironment:
    """テスト用 MarketEnvironment を生成する。"""
    return MarketEnvironment(
        vix=vix,
        vrp=5.0,
        gex=0.0,
        term_ratio=1.1,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={symbol: ivr},
    )


def _make_tactic_in_window() -> PMCCTactic:
    """ET 11:00 (エントリーウィンドウ内) でのタクティクを返す。"""
    return PMCCTactic(
        clock_fn=lambda: datetime(2026, 4, 25, 11, 0, tzinfo=_ET)
    )


def _make_tactic_out_window() -> PMCCTactic:
    """ET 09:00 (エントリーウィンドウ外) でのタクティクを返す。"""
    return PMCCTactic(
        clock_fn=lambda: datetime(2026, 4, 25, 9, 0, tzinfo=_ET)
    )


def _make_position(
    symbol: str = "SPY",
    long_call_dte: int = 80,
    short_call_dte: int = 5,
    short_call_expiry: date | None = None,
    net_debit: float = 500.0,
    short_entry_premium: float = 100.0,
    unrealized_pnl: float = 0.0,
) -> PMCCPosition:
    """テスト用 PMCCPosition を生成する。"""
    return PMCCPosition(
        symbol=symbol,
        quantity=1,
        long_call_dte=long_call_dte,
        short_call_dte=short_call_dte,
        short_call_expiry=short_call_expiry,
        net_debit=net_debit,
        short_entry_premium=short_entry_premium,
        unrealized_pnl=unrealized_pnl,
    )


# ---------------------------------------------------------------------------
# T01: 2 leg 発注 — build_orders が long_call + short_call の 2 OrderRequest を返す
# ---------------------------------------------------------------------------

def test_t01_build_orders_returns_2_legs():
    """build_orders は long_call + short_call の 2 件の OrderRequest を返す。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter, f"エントリー判定が False: {decision.reason}"
    from unittest.mock import patch as _patch
    with _patch(
        "common_v3.risk.pre_trade_check.check_order_critical_only",
        lambda *a, **k: type("_GR", (), {"allowed": True, "reason": ""})(),
    ):
        orders = tactic.build_orders(decision)

    assert len(orders) == 2, f"発注件数が 2 でない: {len(orders)}"
    labels = [o.symbol for o in orders]
    assert any("long_call" in s for s in labels), "long_call leg が存在しない"
    assert any("short_call" in s for s in labels), "short_call leg が存在しない"


# ---------------------------------------------------------------------------
# T02: 2 leg の expiry が異なること (long DTE > short DTE)
# ---------------------------------------------------------------------------

def test_t02_legs_have_different_expiry():
    """long_call と short_call の dte_target が異なること (long > short)。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter
    legs = decision.legs
    assert len(legs) == 2

    long_leg = next(leg for leg in legs if leg.label == "long_call")
    short_leg = next(leg for leg in legs if leg.label == "short_call")

    assert long_leg.dte_target > short_leg.dte_target, (
        f"long DTE ({long_leg.dte_target}) <= short DTE ({short_leg.dte_target}): "
        "異なる expiry が必要"
    )


# ---------------------------------------------------------------------------
# T03: long call が 60-90 DTE フィルタを通過
# ---------------------------------------------------------------------------

def test_t03_long_call_dte_in_60_90_range():
    """long call の dte_target が 60-90 DTE 範囲内に収まること。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter
    long_leg = next(leg for leg in decision.legs if leg.label == "long_call")

    assert PMCC_LONG_DTE_MIN <= long_leg.dte_target <= PMCC_LONG_DTE_MAX, (
        f"long call DTE={long_leg.dte_target} が [{PMCC_LONG_DTE_MIN}, {PMCC_LONG_DTE_MAX}] 外"
    )


# ---------------------------------------------------------------------------
# T04: long call delta が 0.75-0.90 範囲内
# ---------------------------------------------------------------------------

def test_t04_long_call_delta_in_range():
    """long call の delta が 0.75-0.90 範囲内に収まること。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter
    long_leg = next(leg for leg in decision.legs if leg.label == "long_call")

    assert PMCC_LONG_DELTA_MIN <= long_leg.delta <= PMCC_LONG_DELTA_MAX, (
        f"long call delta={long_leg.delta:.3f} が [{PMCC_LONG_DELTA_MIN}, {PMCC_LONG_DELTA_MAX}] 外"
    )


# ---------------------------------------------------------------------------
# T05: short call delta が 0.25-0.35 範囲内
# ---------------------------------------------------------------------------

def test_t05_short_call_delta_in_range():
    """short call の delta が 0.25-0.35 範囲内に収まること。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter
    short_leg = next(leg for leg in decision.legs if leg.label == "short_call")

    assert PMCC_SHORT_DELTA_MIN <= short_leg.delta <= PMCC_SHORT_DELTA_MAX, (
        f"short call delta={short_leg.delta:.3f} が [{PMCC_SHORT_DELTA_MIN}, {PMCC_SHORT_DELTA_MAX}] 外"
    )


# ---------------------------------------------------------------------------
# T06: weekly short roll — short_call_dte <= 1 で weekly_short_roll
# ---------------------------------------------------------------------------

def test_t06_weekly_short_roll_by_dte():
    """short_call_dte <= 1 のとき should_exit が weekly_short_roll を返す。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    position = _make_position(short_call_dte=1)

    result = tactic.should_exit(position, env)

    assert result.should_exit, "weekly_short_roll でエグジットすべき"
    assert result.exit_type == "weekly_short_roll", (
        f"exit_type={result.exit_type}: weekly_short_roll を期待"
    )


# ---------------------------------------------------------------------------
# T07: weekly short roll — short_call_expiry 翌日到達でロール
# ---------------------------------------------------------------------------

def test_t07_weekly_short_roll_by_expiry_date(monkeypatch):
    """short_call_expiry の翌日に到達したとき weekly_short_roll を返す。"""
    # 今日を 2026-04-25 とし、expiry を 2026-04-24 (昨日) に設定
    fixed_today = datetime(2026, 4, 25, 11, 0, tzinfo=_ET)
    tactic = PMCCTactic(clock_fn=lambda: fixed_today)
    env = _make_env()
    position = _make_position(
        short_call_dte=2,  # DTE は 2 (dte ベースではロールしない)
        short_call_expiry=date(2026, 4, 24),  # 昨日 expiry
    )

    result = tactic.should_exit(position, env)

    assert result.should_exit
    assert result.exit_type == "weekly_short_roll"


# ---------------------------------------------------------------------------
# T08: long call roll trigger — long_call_dte <= 60 で long_call_roll
# ---------------------------------------------------------------------------

def test_t08_long_call_roll_trigger():
    """long_call_dte が long_roll_trigger_dte (60) 以下のとき long_call_roll を返す。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    position = _make_position(long_call_dte=PMCC_LONG_ROLL_TRIGGER_DTE)

    result = tactic.should_exit(position, env)

    assert result.should_exit
    assert result.exit_type == "long_call_roll", (
        f"exit_type={result.exit_type}: long_call_roll を期待"
    )


# ---------------------------------------------------------------------------
# T09: 上方向 bias フィルタ — bias != "bull" でエントリー拒否
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bias", ["bear", "neutral"])
def test_t09_upward_bias_filter(bias: str):
    """bias が bull 以外の場合、should_enter=False を返す。"""
    tactic = _make_tactic_in_window()
    env = _make_env(bias=bias)

    decision = tactic.should_enter(env, "SPY")

    assert not decision.should_enter, f"bias={bias} でエントリーが通過してはならない"
    assert "bull" in decision.reason or "bias" in decision.reason


# ---------------------------------------------------------------------------
# T10: short call 50% profit exit
# ---------------------------------------------------------------------------

def test_t10_short_call_profit_exit():
    """unrealized_pnl が short_entry_premium × 50% 以上のとき profit_exit_short を返す。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    position = _make_position(
        long_call_dte=80,
        short_call_dte=4,
        short_entry_premium=100.0,
        unrealized_pnl=50.0,  # 50% profit
    )

    result = tactic.should_exit(position, env)

    assert result.should_exit
    assert result.exit_type == "profit_exit_short", (
        f"exit_type={result.exit_type}: profit_exit_short を期待"
    )


# ---------------------------------------------------------------------------
# T11: 全体損切り (stop_loss)
# ---------------------------------------------------------------------------

def test_t11_stop_loss():
    """unrealized_pnl <= -net_debit × 2.0 のとき stop_loss を返す。"""
    tactic = _make_tactic_in_window()
    env = _make_env()
    position = _make_position(
        long_call_dte=80,
        short_call_dte=4,
        net_debit=500.0,
        short_entry_premium=100.0,
        unrealized_pnl=-1001.0,  # -2.0 × 500 = -1000 を超える損失
    )

    result = tactic.should_exit(position, env)

    assert result.should_exit
    assert result.exit_type == "stop_loss", (
        f"exit_type={result.exit_type}: stop_loss を期待"
    )


# ---------------------------------------------------------------------------
# T12: Kill Switch ARMED でエントリー拒否
# ---------------------------------------------------------------------------

def test_t12_kill_switch_blocks_entry(monkeypatch):
    """Kill Switch が ARMED のとき should_enter=False を返す。"""
    monkeypatch.setattr(
        "atlas_v3.bots.engines.pmcc.kill_switch_is_active",
        lambda: True,
    )
    tactic = _make_tactic_in_window()
    env = _make_env()

    decision = tactic.should_enter(env, "SPY")

    assert not decision.should_enter
    assert "kill_switch" in decision.reason


# ---------------------------------------------------------------------------
# T13: Kill Switch ARMED で force_close
# ---------------------------------------------------------------------------

def test_t13_kill_switch_forces_close(monkeypatch):
    """Kill Switch が ARMED のとき should_exit が force_close を返す。"""
    monkeypatch.setattr(
        "atlas_v3.bots.engines.pmcc.kill_switch_is_active",
        lambda: True,
    )
    tactic = _make_tactic_in_window()
    env = _make_env()
    position = _make_position()

    result = tactic.should_exit(position, env)

    assert result.should_exit
    assert result.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T14: IVR 範囲外でエントリー拒否
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ivr", [10.0, 90.0])
def test_t14_ivr_out_of_range_blocks_entry(ivr: float):
    """IVR が [ivr_min, ivr_max] 外の場合、should_enter=False を返す。"""
    tactic = _make_tactic_in_window()
    env = _make_env(ivr=ivr)

    decision = tactic.should_enter(env, "SPY")

    assert not decision.should_enter, f"IVR={ivr} でエントリーが通過してはならない"
    assert "IVR" in decision.reason


# ---------------------------------------------------------------------------
# T15: PMCCConfig バリデーション (long_delta_min <= short_delta_max で ValueError)
# ---------------------------------------------------------------------------

def test_t15_config_validation_long_delta_must_exceed_short():
    """long_delta_min <= short_delta_max のとき PMCCConfig が ValueError を raise する。"""
    with pytest.raises(ValueError, match="long call は short call より深く ITM"):
        PMCCConfig(
            long_delta_min=0.30,   # short_delta_max=0.35 以下 → 不正
            long_delta_max=0.60,
            short_delta_min=0.25,
            short_delta_max=0.35,
        )
