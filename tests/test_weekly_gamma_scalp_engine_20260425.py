"""tests/test_weekly_gamma_scalp_engine_20260425.py — WeeklyGammaScalpTactic 15 件テスト

テスト対象:
    atlas_v3/bots/engines/weekly_gamma_scalp.py

カバー範囲:
    T01  weekly_expiry フィルタ: 月曜から正しく金曜を算出
    T02  weekly_expiry フィルタ: 金曜基準（当日が gold）
    T03  weekly_expiry フィルタ: 水曜基準
    T04  is_monday / is_friday ヘルパー
    T05  delta hedge 計算: |delta| > band でヘッジ発動・hedge_units 符号正確性
    T06  delta hedge 計算: |delta| <= band でヘッジ不発動
    T07  delta hedge 計算: hedge_interval_min 未経過でスキップ
    T08  multi-symbol: SPY でエントリー決定 should_enter=True
    T09  multi-symbol: QQQ でエントリー決定 should_enter=True
    T10  multi-symbol: IWM でエントリー決定 should_enter=True
    T11  multi-symbol: 非対応銘柄 AAPL で ValueError
    T12  earnings 前日 close: is_earnings_eve が True → should_exit earnings_force_close
    T13  IVR > ivr_max で entry 拒否（straddle 割高フィルタ）
    T14  月曜 entry window 外（火曜 9:35）で entry 拒否
    T15  Kill Switch ARMED で should_enter / should_exit ともにブロック

注意:
    - ネットワーク接続不要・futu SDK 依存なし
    - kill_switch は unittest.mock.patch で制御
    - clock_fn 注入で決定論的な時刻制御
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from atlas_v3.bots.engines.weekly_gamma_scalp import (
    TACTIC_PREFIX,
    DeltaHedgeAction,
    WeeklyGammaScalpConfig,
    WeeklyGammaScalpEntry,
    WeeklyGammaScalpPosition,
    WeeklyGammaScalpTactic,
    compute_hedge_units,
    compute_portfolio_delta,
    estimate_delta,
    get_weekly_expiry,
    is_earnings_eve,
    is_friday,
    is_monday,
)
from atlas_v3.core.env_observer import MarketEnvironment

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# テスト用ヘルパー
# ---------------------------------------------------------------------------

def _make_monday_open_clock(trade_date: date) -> "() -> datetime":
    """月曜 9:35 ET の clock_fn を返す。"""
    dt = datetime(
        trade_date.year, trade_date.month, trade_date.day,
        9, 35, 0, tzinfo=ET
    )
    return lambda: dt


def _make_tuesday_clock(trade_date: date) -> "() -> datetime":
    """火曜 9:35 ET の clock_fn を返す。"""
    # trade_date が月曜なら +1 日
    tuesday = trade_date + timedelta(days=1)
    dt = datetime(
        tuesday.year, tuesday.month, tuesday.day,
        9, 35, 0, tzinfo=ET
    )
    return lambda: dt


def _make_friday_close_clock(trade_date: date) -> "() -> datetime":
    """金曜 15:51 ET（force close 後）の clock_fn を返す。"""
    friday = trade_date + timedelta(days=(_FRIDAY - trade_date.weekday()) % 7)
    dt = datetime(friday.year, friday.month, friday.day, 15, 51, 0, tzinfo=ET)
    return lambda: dt


_FRIDAY = 4


def _env(
    vix: float = 18.0,
    ivr: float = 30.0,
    symbol: str = "SPY",
) -> MarketEnvironment:
    """テスト用 MarketEnvironment を生成する。"""
    return MarketEnvironment(vix=vix, vrp=500.0, ivr_by_symbol={symbol: ivr})


def _make_tactic(
    clock_fn=None,
    ivr_max: float = 50.0,
    delta_band: float = 0.20,
    earnings_dates: "dict | None" = None,
) -> WeeklyGammaScalpTactic:
    cfg = WeeklyGammaScalpConfig(ivr_max=ivr_max, delta_band=delta_band)
    return WeeklyGammaScalpTactic(
        config=cfg, clock_fn=clock_fn, earnings_dates=earnings_dates
    )


def _make_entry(symbol: str = "SPY", total_cost: float = 1000.0) -> WeeklyGammaScalpEntry:
    """ダミー entry を生成する（should_exit テスト用）。"""
    expiry = date(2026, 5, 2)  # 金曜
    from atlas_v3.bots.engines.weekly_gamma_scalp import WeeklyStraddleLeg
    call_leg = WeeklyStraddleLeg("call", 500.0, expiry, 5.0, 1)
    put_leg = WeeklyStraddleLeg("put", 500.0, expiry, 5.0, 1)
    return WeeklyGammaScalpEntry(
        should_enter=True,
        symbol=symbol,
        legs=(call_leg, put_leg),
        total_cost=total_cost,
        underlying_price=500.0,
        weekly_expiry=expiry,
        idempotency_key="test_key",
        reason="test",
        ivr=25.0,
    )


def _make_position(
    symbol: str = "SPY",
    call_current: float = 6.0,
    put_current: float = 6.0,
    total_cost: float = 1000.0,
) -> WeeklyGammaScalpPosition:
    entry = _make_entry(symbol=symbol, total_cost=total_cost)
    pos = WeeklyGammaScalpPosition(symbol=symbol, entry=entry)
    pos.call_current = call_current
    pos.put_current = put_current
    return pos


# ---------------------------------------------------------------------------
# T01: weekly_expiry — 月曜から金曜を算出
# ---------------------------------------------------------------------------

def test_t01_weekly_expiry_from_monday() -> None:
    """月曜 2026-04-27 → 金曜 2026-05-01"""
    monday = date(2026, 4, 27)
    expiry = get_weekly_expiry(monday)
    assert expiry == date(2026, 5, 1)
    assert expiry.weekday() == _FRIDAY


# ---------------------------------------------------------------------------
# T02: weekly_expiry — 金曜当日が expiry
# ---------------------------------------------------------------------------

def test_t02_weekly_expiry_from_friday() -> None:
    """金曜 2026-05-01 → 同日 2026-05-01（金曜当日が expiry）"""
    friday = date(2026, 5, 1)
    assert friday.weekday() == _FRIDAY
    expiry = get_weekly_expiry(friday)
    assert expiry == date(2026, 5, 1)
    assert expiry.weekday() == _FRIDAY


# ---------------------------------------------------------------------------
# T03: weekly_expiry — 水曜基準
# ---------------------------------------------------------------------------

def test_t03_weekly_expiry_from_wednesday() -> None:
    """水曜 2026-04-29 → 金曜 2026-05-01"""
    wednesday = date(2026, 4, 29)
    assert wednesday.weekday() == 2  # 水曜
    expiry = get_weekly_expiry(wednesday)
    assert expiry == date(2026, 5, 1)
    assert expiry.weekday() == _FRIDAY


# ---------------------------------------------------------------------------
# T04: is_monday / is_friday ヘルパー
# ---------------------------------------------------------------------------

def test_t04_is_monday_is_friday() -> None:
    """is_monday / is_friday が曜日を正確に判定する。"""
    monday = date(2026, 4, 27)     # 月曜
    friday = date(2026, 5, 1)      # 金曜
    wednesday = date(2026, 4, 29)  # 水曜

    assert monday.weekday() == 0
    assert friday.weekday() == 4

    assert is_monday(monday) is True
    assert is_monday(friday) is False
    assert is_friday(friday) is True
    assert is_friday(monday) is False
    assert is_monday(wednesday) is False
    assert is_friday(wednesday) is False


# ---------------------------------------------------------------------------
# T05: delta hedge — |delta| > band でヘッジ発動・hedge_units 符号正確性
# ---------------------------------------------------------------------------

def test_t05_delta_hedge_triggers_above_band() -> None:
    """portfolio_delta=+0.35 (> band 0.20) → ヘッジ発動・hedge_units < 0（空売り）。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday), delta_band=0.20)
    pos = _make_position()

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        action = tactic.delta_hedge(pos, portfolio_delta=0.35, underlying_price=500.0)

    assert action is not None
    assert action.delta_before == pytest.approx(0.35)
    assert action.delta_after == pytest.approx(0.0)
    # delta > 0 → units = -0.35 × 100 = -35（空売り方向）
    assert action.hedge_units == pytest.approx(-35.0)
    assert len(pos.hedge_events) == 1


# ---------------------------------------------------------------------------
# T06: delta hedge — |delta| <= band でヘッジ不発動
# ---------------------------------------------------------------------------

def test_t06_delta_hedge_no_action_within_band() -> None:
    """|portfolio_delta|=0.10 <= band 0.20 → ヘッジ不発動。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday), delta_band=0.20)
    pos = _make_position()

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        action = tactic.delta_hedge(pos, portfolio_delta=0.10, underlying_price=500.0)

    assert action is None
    assert len(pos.hedge_events) == 0


# ---------------------------------------------------------------------------
# T07: delta hedge — hedge_interval_min 未経過でスキップ
# ---------------------------------------------------------------------------

def test_t07_delta_hedge_skips_within_interval() -> None:
    """2 回目のヘッジが interval 未経過でスキップされる。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    # hedge_interval_min=120 分（非常に長い）に設定
    cfg = WeeklyGammaScalpConfig(delta_band=0.05, hedge_interval_min=120.0)
    tactic = WeeklyGammaScalpTactic(
        config=cfg, clock_fn=_make_monday_open_clock(monday)
    )
    pos = _make_position()

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        action1 = tactic.delta_hedge(pos, portfolio_delta=0.30, underlying_price=500.0)
        action2 = tactic.delta_hedge(pos, portfolio_delta=0.30, underlying_price=500.0)

    assert action1 is not None  # 1 回目は実行
    assert action2 is None      # 2 回目はスキップ
    assert len(pos.hedge_events) == 1


# ---------------------------------------------------------------------------
# T08: multi-symbol — SPY でエントリー
# ---------------------------------------------------------------------------

def test_t08_entry_spy() -> None:
    """SPY: 月曜 open・低 IVR でエントリー should_enter=True。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday))
    env = _env(ivr=25.0, symbol="SPY")

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter is True
    assert decision.symbol == "SPY"
    assert len(decision.legs) == 2
    assert decision.legs[0].option_type == "call"
    assert decision.legs[1].option_type == "put"
    assert decision.weekly_expiry.weekday() == _FRIDAY


# ---------------------------------------------------------------------------
# T09: multi-symbol — QQQ でエントリー
# ---------------------------------------------------------------------------

def test_t09_entry_qqq() -> None:
    """QQQ: 月曜 open・低 IVR でエントリー should_enter=True。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday))
    env = _env(ivr=20.0, symbol="QQQ")

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        decision = tactic.should_enter(env, "QQQ")

    assert decision.should_enter is True
    assert decision.symbol == "QQQ"
    assert decision.weekly_expiry == get_weekly_expiry(date(2026, 4, 27))


# ---------------------------------------------------------------------------
# T10: multi-symbol — IWM でエントリー
# ---------------------------------------------------------------------------

def test_t10_entry_iwm() -> None:
    """IWM: 月曜 open・低 IVR でエントリー should_enter=True。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday))
    env = _env(ivr=18.0, symbol="IWM")

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        decision = tactic.should_enter(env, "IWM")

    assert decision.should_enter is True
    assert decision.symbol == "IWM"


# ---------------------------------------------------------------------------
# T11: multi-symbol — 非対応銘柄 AAPL で ValueError
# ---------------------------------------------------------------------------

def test_t11_unsupported_symbol_raises() -> None:
    """AAPL（非対応銘柄）で ValueError が発生する。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday))
    env = _env(ivr=20.0, symbol="AAPL")

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        with pytest.raises(ValueError, match="非対応銘柄"):
            tactic.should_enter(env, "AAPL")


# ---------------------------------------------------------------------------
# T12: earnings 前日クローズ — should_exit が earnings_force_close を返す
# ---------------------------------------------------------------------------

def test_t12_earnings_eve_force_close() -> None:
    """earnings 前日 15:51 ET → should_exit=True / exit_type=earnings_force_close。"""
    # 月曜 2026-04-27 の翌日（火曜 2026-04-28）が earnings
    earnings_day = date(2026, 4, 28)

    # 月曜 2026-04-27 の 15:51（force close 後）
    force_close_dt = datetime(2026, 4, 27, 15, 51, 0, tzinfo=ET)
    clock_fn = lambda: force_close_dt

    tactic = WeeklyGammaScalpTactic(
        config=WeeklyGammaScalpConfig(),
        clock_fn=clock_fn,
        earnings_dates={"SPY": frozenset({earnings_day})},
    )
    pos = _make_position(symbol="SPY", call_current=5.0, put_current=5.0, total_cost=1000.0)
    env = _env()

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        result = tactic.should_exit(pos, env)

    assert result.should_exit is True
    assert result.exit_type == "earnings_force_close"


# ---------------------------------------------------------------------------
# T13: IVR > ivr_max で entry 拒否
# ---------------------------------------------------------------------------

def test_t13_high_ivr_entry_rejected() -> None:
    """IVR=80 > ivr_max=50 → should_enter=False・straddle 割高フィルタ。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday), ivr_max=50.0)
    env = _env(ivr=80.0, symbol="SPY")

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter is False
    assert "ivr_max" in decision.reason.lower() or "ivr" in decision.reason.lower()


# ---------------------------------------------------------------------------
# T14: 月曜 entry window 外（火曜 9:35）で entry 拒否
# ---------------------------------------------------------------------------

def test_t14_non_monday_entry_rejected() -> None:
    """火曜 9:35 ET → is_monday=False → should_enter=False。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_tuesday_clock(monday))
    env = _env(ivr=20.0, symbol="SPY")

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=False):
        decision = tactic.should_enter(env, "SPY")

    assert decision.should_enter is False
    assert "entry_window_closed" in decision.reason


# ---------------------------------------------------------------------------
# T15: Kill Switch ARMED で should_enter / should_exit ともにブロック
# ---------------------------------------------------------------------------

def test_t15_kill_switch_blocks_entry_and_exit() -> None:
    """Kill Switch ARMED → should_enter=False / should_exit(exit_type=kill_switch)。"""
    monday = date(2026, 4, 27)  # 実際の月曜
    tactic = _make_tactic(clock_fn=_make_monday_open_clock(monday))
    env = _env(ivr=20.0, symbol="SPY")
    pos = _make_position()

    with patch("atlas_v3.bots.engines.weekly_gamma_scalp.kill_switch_is_active", return_value=True):
        entry = tactic.should_enter(env, "SPY")
        assert entry.should_enter is False
        assert entry.reason == "kill_switch_armed"

        exit_decision = tactic.should_exit(pos, env)
        assert exit_decision.should_exit is True
        assert exit_decision.exit_type == "kill_switch"
