"""tests/test_earnings_straddle_buy_engine_20260425.py

EarningsStraddleBuyTactic 単体テスト（15 件）

観点:
  TC-01  earnings calendar mock でカレンダー更新
  TC-02  IVR < ivr_max → should_enter=True
  TC-03  IVR >= ivr_max → should_enter=False
  TC-04  決算まで 1 日 → エントリー許可
  TC-05  決算まで 2 日（days_until_earnings_max=1）→ エントリー拒否
  TC-06  エントリーウィンドウ内（ET 15:20）→ should_enter=True
  TC-07  エントリーウィンドウ外（ET 14:59）→ should_enter=False
  TC-08  エントリーウィンドウ外（ET 15:46）→ should_enter=False
  TC-09  call leg の build_order → side="buy" symbol="NVDA_CALL"
  TC-10  put leg の build_order → side="buy" symbol="NVDA_PUT"
  TC-11  open+30min 経過後 → post_earnings_time_exit
  TC-12  profit 40% 到達 → profit_target exit
  TC-13  loss 40% 超過 → stop_loss exit
  TC-14  Kill Switch ARMED → force_close
  TC-15  冪等性: mark_entered 後の再評価 → should_enter=False
"""
from __future__ import annotations

import sys
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# プロジェクトルートを sys.path に追加
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# SPY_DATA_DIR を tmp に向けて本番 data/ を汚染しない
_TMP = tempfile.mkdtemp()
os.environ.setdefault("SPY_DATA_DIR", _TMP)

from atlas_v3.bots.engines.earnings_straddle_buy import (
    EarningsStraddleBuyTactic,
    StraddleBuyConfig,
    StraddleBuyEntryDecision,
    StraddleBuyExitDecision,
    StraddlePosition,
)
from atlas_v3.core.env_observer import MarketEnvironment


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ET = None
try:
    import zoneinfo
    ET = zoneinfo.ZoneInfo("America/New_York")
except ImportError:
    pass


def _et(hour: int, minute: int, day_offset: int = 0) -> datetime:
    """テスト用の ET 時刻を生成する。"""
    base = date.today() + timedelta(days=day_offset)
    if ET:
        return datetime(base.year, base.month, base.day, hour, minute, tzinfo=ET)
    return datetime(base.year, base.month, base.day, hour, minute,
                    tzinfo=timezone.utc)


def _env(ivr_nvda: float = 40.0, vix: float = 20.0) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=2.0,
        gex=0.0,
        term_ratio=0.95,
        bias="neutral",
        ivr_by_symbol={"NVDA": ivr_nvda, "TSLA": 55.0},
    )


def _tactic(
    earnings_symbols: dict[str, str] | None = None,
    ivr_max: float = 60.0,
) -> EarningsStraddleBuyTactic:
    cfg = StraddleBuyConfig(ivr_max=ivr_max, slippage_tolerance_bps=20)
    return EarningsStraddleBuyTactic(
        config=cfg,
        earnings_symbols=earnings_symbols or {
            "NVDA": (date.today() + timedelta(days=1)).isoformat(),
        },
    )


def _position(
    symbol: str = "NVDA",
    entry_value: float = 5.0,
    unrealized_pnl: float = 0.0,
    earnings_open_dt: Optional[datetime] = None,
) -> StraddlePosition:
    return StraddlePosition(
        symbol=symbol,
        quantity=1,
        entry_price_call=2.5,
        entry_price_put=2.5,
        earnings_date=(date.today() + timedelta(days=1)).isoformat(),
        earnings_open_dt=earnings_open_dt,
        entry_value=entry_value,
        unrealized_pnl=unrealized_pnl,
    )


# ---------------------------------------------------------------------------
# Kill Switch 隔離 fixture
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_kill_switch(tmp_path, monkeypatch):
    import common_v3.risk.kill_switch as ks_mod
    state = tmp_path / "state_v3"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_mod, "_STATE_DIR", state)
    monkeypatch.setattr(ks_mod, "FLAG_FILE", state / "kill_switch.flag")
    monkeypatch.setattr(ks_mod, "AUDIT_FILE", state / "kill_switch_audit.jsonl")
    yield


# ---------------------------------------------------------------------------
# TC-01: earnings calendar mock でカレンダー更新
# ---------------------------------------------------------------------------

def test_tc01_observe_updates_calendar():
    """observe() が market_data.get_earnings_calendar を呼んでカレンダーを更新する。"""
    tactic = _tactic(earnings_symbols={})
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    mock_data = MagicMock()
    mock_data.get_earnings_calendar.return_value = {"NVDA": tomorrow, "TSLA": tomorrow}
    env = _env()

    tactic.observe(env, mock_data)

    assert tactic._earnings_calendar["NVDA"] == tomorrow
    assert tactic._earnings_calendar["TSLA"] == tomorrow
    mock_data.get_earnings_calendar.assert_called_once()


# ---------------------------------------------------------------------------
# TC-02: IVR < ivr_max → should_enter=True
# ---------------------------------------------------------------------------

def test_tc02_low_ivr_enter():
    """IVR=40 < ivr_max=60 の環境でエントリーが承認される。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=40.0)

    decisions = tactic.should_enter(env, ["NVDA"], now_et=None)

    assert len(decisions) == 1
    d = decisions[0]
    assert d.should_enter is True
    assert d.symbol == "NVDA"
    assert d.idempotency_key != ""


# ---------------------------------------------------------------------------
# TC-03: IVR >= ivr_max → should_enter=False
# ---------------------------------------------------------------------------

def test_tc03_high_ivr_skip():
    """IVR=65 >= ivr_max=60 の環境でエントリーが拒否される。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow}, ivr_max=60.0)
    env = _env(ivr_nvda=65.0)

    decisions = tactic.should_enter(env, ["NVDA"], now_et=None)

    assert len(decisions) == 1
    assert decisions[0].should_enter is False
    assert "IVR" in decisions[0].reason


# ---------------------------------------------------------------------------
# TC-04: 決算まで 1 日 → エントリー許可
# ---------------------------------------------------------------------------

def test_tc04_days_until_1_allowed():
    """決算まで 1 日以内（days_until_earnings_max=1）でエントリーが承認される。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=30.0)

    decisions = tactic.should_enter(env, ["NVDA"], now_et=None)

    assert decisions[0].should_enter is True


# ---------------------------------------------------------------------------
# TC-05: 決算まで 2 日 → エントリー拒否
# ---------------------------------------------------------------------------

def test_tc05_days_until_2_rejected():
    """決算まで 2 日（days_until_earnings_max=1）でエントリーが拒否される。"""
    in_2_days = (date.today() + timedelta(days=2)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": in_2_days})
    env = _env(ivr_nvda=30.0)

    decisions = tactic.should_enter(env, ["NVDA"], now_et=None)

    assert len(decisions) == 1
    assert decisions[0].should_enter is False
    assert "days_until" in decisions[0].reason


# ---------------------------------------------------------------------------
# TC-06: エントリーウィンドウ内（ET 15:20）→ should_enter=True
# ---------------------------------------------------------------------------

def test_tc06_within_entry_window():
    """ET 15:20 はウィンドウ内（15:00–15:45）→ エントリー承認。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=40.0)
    now = _et(15, 20)

    decisions = tactic.should_enter(env, ["NVDA"], now_et=now)

    assert decisions[0].should_enter is True


# ---------------------------------------------------------------------------
# TC-07: エントリーウィンドウ外（ET 14:59）→ should_enter=False
# ---------------------------------------------------------------------------

def test_tc07_before_entry_window():
    """ET 14:59 はウィンドウ前（15:00 未満）→ エントリー拒否。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=40.0)
    now = _et(14, 59)

    decisions = tactic.should_enter(env, ["NVDA"], now_et=now)

    assert decisions[0].should_enter is False
    assert "outside_entry_window" in decisions[0].reason


# ---------------------------------------------------------------------------
# TC-08: エントリーウィンドウ外（ET 15:46）→ should_enter=False
# ---------------------------------------------------------------------------

def test_tc08_after_entry_window():
    """ET 15:46 はウィンドウ後（15:45 超）→ エントリー拒否。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=40.0)
    now = _et(15, 46)

    decisions = tactic.should_enter(env, ["NVDA"], now_et=now)

    assert decisions[0].should_enter is False
    assert "outside_entry_window" in decisions[0].reason


# ---------------------------------------------------------------------------
# TC-09: call leg の build_order
# ---------------------------------------------------------------------------

def test_tc09_build_order_call():
    """build_order(leg='call') が side='buy', symbol='NVDA_CALL' の OrderRequest を返す。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=40.0)
    decisions = tactic.should_enter(env, ["NVDA"], now_et=None)
    decision = decisions[0]
    assert decision.should_enter

    from unittest.mock import patch as _patch
    with _patch(
        "common_v3.risk.pre_trade_check.check_order_critical_only",
        lambda *a, **k: type("_GR", (), {"allowed": True, "reason": ""})(),
    ):
        order = tactic.build_order(decision, leg="call")

    assert order.side == "buy"
    assert order.symbol == "NVDA_CALL"
    assert order.quantity == 1
    assert order.tactic_name == "earnings_straddle_buy"
    assert "call" in order.idempotency_key


# ---------------------------------------------------------------------------
# TC-10: put leg の build_order
# ---------------------------------------------------------------------------

def test_tc10_build_order_put():
    """build_order(leg='put') が side='buy', symbol='NVDA_PUT' の OrderRequest を返す。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=40.0)
    decisions = tactic.should_enter(env, ["NVDA"], now_et=None)
    decision = decisions[0]

    from unittest.mock import patch as _patch
    with _patch(
        "common_v3.risk.pre_trade_check.check_order_critical_only",
        lambda *a, **k: type("_GR", (), {"allowed": True, "reason": ""})(),
    ):
        order = tactic.build_order(decision, leg="put")

    assert order.side == "buy"
    assert order.symbol == "NVDA_PUT"
    assert "put" in order.idempotency_key


# ---------------------------------------------------------------------------
# TC-11: open+30min 以降 → post_earnings_time_exit
# ---------------------------------------------------------------------------

def test_tc11_time_exit_after_open_offset():
    """決算発表日 open + 30 分以降に post_earnings_time_exit が発動する。"""
    tactic = _tactic()
    env = _env()

    # earnings_open = 現在より 40 分前（= open+30min を 10 分経過）
    earnings_open = datetime.now(timezone.utc) - timedelta(minutes=40)
    pos = _position(entry_value=5.0, unrealized_pnl=0.0, earnings_open_dt=earnings_open)
    now_et = datetime.now(timezone.utc)

    dec = tactic.should_exit(pos, env, now_et=now_et)

    assert dec.should_exit is True
    assert dec.exit_type == "post_earnings_time_exit"


# ---------------------------------------------------------------------------
# TC-12: profit 40% 到達 → profit_target exit
# ---------------------------------------------------------------------------

def test_tc12_profit_target_exit():
    """unrealized_pnl >= entry_value * 0.40 で profit_target exit が発動する。"""
    tactic = _tactic()
    env = _env()
    # entry_value=5.0, profit=40% → threshold=2.0 → pnl=2.1 で発動
    pos = _position(entry_value=5.0, unrealized_pnl=2.1)

    dec = tactic.should_exit(pos, env, now_et=None)

    assert dec.should_exit is True
    assert dec.exit_type == "profit_target"


# ---------------------------------------------------------------------------
# TC-13: loss 40% 超過 → stop_loss exit
# ---------------------------------------------------------------------------

def test_tc13_stop_loss_exit():
    """unrealized_pnl <= -entry_value * 0.40 で stop_loss exit が発動する。"""
    tactic = _tactic()
    env = _env()
    # entry_value=5.0, stop=40% → threshold=-2.0 → pnl=-2.1 で発動
    pos = _position(entry_value=5.0, unrealized_pnl=-2.1)

    dec = tactic.should_exit(pos, env, now_et=None)

    assert dec.should_exit is True
    assert dec.exit_type == "stop_loss"


# ---------------------------------------------------------------------------
# TC-14: Kill Switch ARMED → force_close
# ---------------------------------------------------------------------------

def test_tc14_kill_switch_force_close():
    """Kill Switch が ARMED の状態で should_exit が force_close を返す。"""
    from common_v3.risk.kill_switch import activate as ks_activate

    tactic = _tactic()
    env = _env()
    ks_activate(reason="test_tc14")
    pos = _position(entry_value=5.0, unrealized_pnl=0.0)

    dec = tactic.should_exit(pos, env, now_et=None)

    assert dec.should_exit is True
    assert dec.exit_type == "force_close"
    assert "kill_switch" in dec.reason


# ---------------------------------------------------------------------------
# TC-15: 冪等性 — mark_entered 後の再評価 → should_enter=False
# ---------------------------------------------------------------------------

def test_tc15_idempotency_no_double_entry():
    """mark_entered 後に should_enter を再呼び出しすると already_entered_today で拒否される。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    tactic = _tactic(earnings_symbols={"NVDA": tomorrow})
    env = _env(ivr_nvda=40.0)

    # 初回エントリー
    decisions = tactic.should_enter(env, ["NVDA"], now_et=None)
    assert decisions[0].should_enter is True

    # エントリー完了を記録
    tactic.mark_entered("NVDA", decisions[0].idempotency_key)

    # 再評価 → 拒否されること
    decisions2 = tactic.should_enter(env, ["NVDA"], now_et=None)
    assert len(decisions2) == 1
    assert decisions2[0].should_enter is False
    assert "already_entered_today" in decisions2[0].reason
