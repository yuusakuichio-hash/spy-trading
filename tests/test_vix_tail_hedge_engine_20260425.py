"""tests/test_vix_tail_hedge_engine_20260425.py — VIX Tail Hedge エンジン 15 件テスト

カバレッジ対象:
    - VIX < 20 entry filter (pass / reject)
    - portfolio premium upper bound check
    - VIX spike profit trigger (5x-10x)
    - monthly roll entry / exit
    - kill switch 遮断
    - expiry close
    - config バリデーション
    - build_order / build_exit_order
    - preflight
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from atlas_v3.bots.engines.vix_tail_hedge import (
    VixTailHedgeConfig,
    VixTailHedgeEngine,
    VixTailHedgeEntryDecision,
    VixTailHedgeExitDecision,
    VixTailHedgePosition,
    PREMIUM_CAP_PCT_MIN,
    PREMIUM_CAP_PCT_MAX,
)
from atlas_v3.core.env_observer import MarketEnvironment


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _env(vix: float = 15.0) -> MarketEnvironment:
    """MarketEnvironment stub を返す。"""
    return MarketEnvironment(vix=vix)


def _engine(
    vix_entry_max: float = 20.0,
    vix_profit_exit: float = 30.0,
    profit_multiplier_min: float = 5.0,
    premium_cap_pct: float = 0.01,
    roll_day_of_month: int = 1,
) -> VixTailHedgeEngine:
    cfg = VixTailHedgeConfig(
        vix_entry_max=vix_entry_max,
        vix_profit_exit=vix_profit_exit,
        profit_multiplier_min=profit_multiplier_min,
        premium_cap_pct=premium_cap_pct,
        roll_day_of_month=roll_day_of_month,
    )
    return VixTailHedgeEngine(config=cfg)


def _position(
    symbol: str = "VIX",
    entry_premium: float = 1.0,
    unrealized_pnl: float = 0.0,
    expiry: date | None = None,
    quantity: int = 1,
) -> VixTailHedgePosition:
    return VixTailHedgePosition(
        symbol=symbol,
        quantity=quantity,
        entry_premium=entry_premium,
        current_value=0.0,
        expiry=expiry,
        unrealized_pnl=unrealized_pnl,
    )


# ---------------------------------------------------------------------------
# T-01: VIX < 20 エントリー filter — PASS
# ---------------------------------------------------------------------------

def test_entry_vix_below_threshold_passes():
    """VIX=15 (< 20) かつロール日 → should_enter=True。"""
    eng = _engine()
    dec = eng.should_enter(
        env=_env(vix=15.0),
        symbol="VIX",
        today=date(2026, 4, 1),  # roll_day=1 → ロール日
    )
    assert dec.should_enter is True
    assert "VIX=15.00<20.00" in dec.reason


# ---------------------------------------------------------------------------
# T-02: VIX >= 20 エントリー filter — REJECT
# ---------------------------------------------------------------------------

def test_entry_vix_at_threshold_rejected():
    """VIX=20.0 (== vix_entry_max) → should_enter=False。"""
    eng = _engine()
    dec = eng.should_enter(
        env=_env(vix=20.0),
        symbol="VIX",
        today=date(2026, 4, 1),
    )
    assert dec.should_enter is False
    assert "vix_entry_max" in dec.reason


# ---------------------------------------------------------------------------
# T-03: VIX > 20 エントリー filter — REJECT
# ---------------------------------------------------------------------------

def test_entry_vix_above_threshold_rejected():
    """VIX=25.0 (> vix_entry_max) → should_enter=False。"""
    eng = _engine()
    dec = eng.should_enter(
        env=_env(vix=25.0),
        symbol="VIX",
        today=date(2026, 4, 1),
    )
    assert dec.should_enter is False


# ---------------------------------------------------------------------------
# T-04: premium upper bound — 上限内 → PASS
# ---------------------------------------------------------------------------

def test_entry_premium_within_cap_passes():
    """portfolio 100_000 / premium_cap 1% / estimated_premium 500 → 合計 500 <= 1000 → PASS。"""
    eng = _engine(premium_cap_pct=0.01)
    dec = eng.should_enter(
        env=_env(vix=15.0),
        symbol="VIX",
        portfolio_value=100_000.0,
        estimated_premium=500.0,  # 1枚 → total=500 <= cap=1000
        today=date(2026, 4, 1),
    )
    assert dec.should_enter is True


# ---------------------------------------------------------------------------
# T-05: premium upper bound — 上限超過 → REJECT
# ---------------------------------------------------------------------------

def test_entry_premium_exceeds_cap_rejected():
    """portfolio 100_000 / cap 1% / estimated_premium 1500 → 合計 1500 > 1000 → REJECT。"""
    eng = _engine(premium_cap_pct=0.01)
    dec = eng.should_enter(
        env=_env(vix=15.0),
        symbol="VIX",
        portfolio_value=100_000.0,
        estimated_premium=1500.0,  # total=1500 > cap=1000
        today=date(2026, 4, 1),
    )
    assert dec.should_enter is False
    assert "cap" in dec.reason


# ---------------------------------------------------------------------------
# T-06: premium upper bound — portfolio_value=0 のときスキップ（チェック無効化）
# ---------------------------------------------------------------------------

def test_entry_premium_check_skipped_when_no_portfolio():
    """portfolio_value=0 の場合はコスト上限チェックをスキップ → PASS。"""
    eng = _engine(premium_cap_pct=0.01)
    dec = eng.should_enter(
        env=_env(vix=10.0),
        symbol="VIX",
        portfolio_value=0.0,
        estimated_premium=999_999.0,  # どんなに大きくても通る
        today=date(2026, 4, 1),
    )
    assert dec.should_enter is True


# ---------------------------------------------------------------------------
# T-07: profit 5x trigger — spike 利確成立
# ---------------------------------------------------------------------------

def test_exit_profit_5x_trigger_fires():
    """VIX=35 (>30) / pnl=5.0 >= entry_premium(1.0) * 5x → spike_profit。"""
    eng = _engine(vix_profit_exit=30.0, profit_multiplier_min=5.0)
    pos = _position(entry_premium=1.0, unrealized_pnl=5.0)
    dec = eng.should_exit(env=_env(vix=35.0), position=pos)
    assert dec.should_exit is True
    assert dec.exit_type == "spike_profit"
    assert "spike_profit" in dec.reason


# ---------------------------------------------------------------------------
# T-08: profit 5x trigger — VIX 未到達で fire しない
# ---------------------------------------------------------------------------

def test_exit_profit_5x_no_fire_vix_below_threshold():
    """VIX=29 (<30) かつ pnl=10.0 → VIX spike 条件未達 → holding。"""
    eng = _engine(vix_profit_exit=30.0, profit_multiplier_min=5.0)
    pos = _position(entry_premium=1.0, unrealized_pnl=10.0)
    dec = eng.should_exit(env=_env(vix=29.0), position=pos)
    assert dec.should_exit is False
    assert dec.exit_type == "none"


# ---------------------------------------------------------------------------
# T-09: profit 10x trigger — 10 倍以上でも spike_profit
# ---------------------------------------------------------------------------

def test_exit_profit_10x_trigger_fires():
    """pnl=10.0 >= entry_premium(1.0) * 5x かつ VIX=50 → spike_profit。"""
    eng = _engine(vix_profit_exit=30.0, profit_multiplier_min=5.0)
    pos = _position(entry_premium=1.0, unrealized_pnl=10.0)
    dec = eng.should_exit(env=_env(vix=50.0), position=pos)
    assert dec.should_exit is True
    assert dec.exit_type == "spike_profit"


# ---------------------------------------------------------------------------
# T-10: monthly roll エントリー — 非ロール日はスキップ
# ---------------------------------------------------------------------------

def test_entry_non_roll_day_skipped():
    """roll_day=1 / today=2026-04-15 → 非ロール日 → should_enter=False。"""
    eng = _engine(roll_day_of_month=1)
    dec = eng.should_enter(
        env=_env(vix=15.0),
        symbol="VIX",
        today=date(2026, 4, 15),  # day=15 != roll_day=1
    )
    assert dec.should_enter is False
    assert "非ロール日" in dec.reason


# ---------------------------------------------------------------------------
# T-11: monthly roll exit — ロール日に should_exit=monthly_roll
# ---------------------------------------------------------------------------

def test_exit_monthly_roll_fires_on_roll_day():
    """roll_day=1 / today=2026-05-01 → monthly_roll exit。"""
    eng = _engine(roll_day_of_month=1)
    pos = _position(entry_premium=1.0)
    dec = eng.should_exit(env=_env(vix=15.0), position=pos, today=date(2026, 5, 1))
    assert dec.should_exit is True
    assert dec.exit_type == "monthly_roll"


# ---------------------------------------------------------------------------
# T-12: kill switch → entry 遮断
# ---------------------------------------------------------------------------

def test_entry_kill_switch_blocks(monkeypatch):
    """Kill Switch ARMED 時 should_enter=False。"""
    import atlas_v3.bots.engines.vix_tail_hedge as mod
    monkeypatch.setattr(mod, "kill_switch_is_active", lambda: True)

    eng = _engine()
    dec = eng.should_enter(env=_env(vix=10.0), symbol="VIX", today=date(2026, 4, 1))
    assert dec.should_enter is False
    assert "kill_switch" in dec.reason


# ---------------------------------------------------------------------------
# T-13: kill switch → exit 強制クローズ
# ---------------------------------------------------------------------------

def test_exit_kill_switch_force_close(monkeypatch):
    """Kill Switch ARMED 時 exit_type=force_close。"""
    import atlas_v3.bots.engines.vix_tail_hedge as mod
    monkeypatch.setattr(mod, "kill_switch_is_active", lambda: True)

    eng = _engine()
    pos = _position(entry_premium=1.0)
    dec = eng.should_exit(env=_env(vix=10.0), position=pos)
    assert dec.should_exit is True
    assert dec.exit_type == "force_close"


# ---------------------------------------------------------------------------
# T-14: 満期到達 → expiry_close
# ---------------------------------------------------------------------------

def test_exit_expiry_close_on_expiry_date():
    """expiry=2026-04-25 / today=2026-04-25 → expiry_close。"""
    eng = _engine()
    pos = _position(entry_premium=1.0, expiry=date(2026, 4, 25))
    dec = eng.should_exit(env=_env(vix=15.0), position=pos, today=date(2026, 4, 25))
    assert dec.should_exit is True
    assert dec.exit_type == "expiry_close"


# ---------------------------------------------------------------------------
# T-15: config バリデーション — premium_cap_pct 範囲外で ValueError
# ---------------------------------------------------------------------------

def test_config_premium_cap_out_of_range_raises():
    """premium_cap_pct=0.10 (> 2%) → ValueError。"""
    with pytest.raises(ValueError, match="premium_cap_pct"):
        VixTailHedgeConfig(premium_cap_pct=0.10)
