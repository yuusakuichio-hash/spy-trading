"""tests/test_calendar_native_engine_20260425.py
CalendarNativeEngine 移植検証テスト（25 件以上）

設計方針:
- 全テスト dry_test=True または mkt/eng=mock で外部 API 接続ゼロ
- spy_bot.py 書換なし
- common_v3 / common imports を monkeypatch でロード失敗ケースにも対応
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# import 確認
# ---------------------------------------------------------------------------


def test_import_calendar_native():
    """モジュールがインポートできること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    assert CalendarNativeEngine is not None


def test_import_calendar_native_position():
    """CalendarNativePosition がインポートできること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativePosition
    assert CalendarNativePosition is not None


def test_import_constants():
    """Calendar 定数が spy_bot 同値であること（独立コピー確認）。"""
    from atlas_v3.bots.engines.calendar_native import (
        CALENDAR_VIX_MIN,
        CALENDAR_VIX_MAX,
        CALENDAR_BACK_DAYS,
        CALENDAR_FORCE_CLOSE_H,
        CALENDAR_FORCE_CLOSE_M,
        CALENDAR_MAX_LOSS_PCT,
        CALENDAR_IV_CRUSH_PCT,
        CALENDAR_MAX_RISK_PCT,
        CALENDAR_MAX_QTY,
    )
    assert CALENDAR_VIX_MIN == 20.0
    assert CALENDAR_VIX_MAX == 50.0
    assert CALENDAR_BACK_DAYS == 7
    assert CALENDAR_FORCE_CLOSE_H == 15
    assert CALENDAR_FORCE_CLOSE_M == 45
    assert CALENDAR_MAX_LOSS_PCT == 0.30
    assert CALENDAR_IV_CRUSH_PCT == 0.10
    assert CALENDAR_MAX_RISK_PCT == 0.02
    assert CALENDAR_MAX_QTY == 2


# ---------------------------------------------------------------------------
# TacticBase 必須プロパティ
# ---------------------------------------------------------------------------


def test_tactic_type():
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    eng = CalendarNativeEngine(dry_test=True)
    assert eng.tactic_type == "state_carrying"


def test_tactic_name():
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    eng = CalendarNativeEngine(dry_test=True)
    assert eng.tactic_name == "calendar_native"


def test_preflight_kill_switch_armed():
    """Kill Switch ARMED → preflight False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    from atlas_v3.core.env_observer import MarketEnvironment
    with patch("atlas_v3.bots.engines.calendar_native.kill_switch_is_active", return_value=True):
        eng = CalendarNativeEngine(dry_test=True)
        env = MarketEnvironment(vix=22.0)
        assert eng.preflight(env) is False


def test_preflight_vix_too_high():
    """VIX > CALENDAR_VIX_MAX → preflight False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CALENDAR_VIX_MAX
    from atlas_v3.core.env_observer import MarketEnvironment
    with patch("atlas_v3.bots.engines.calendar_native.kill_switch_is_active", return_value=False):
        eng = CalendarNativeEngine(dry_test=True)
        env = MarketEnvironment(vix=CALENDAR_VIX_MAX + 1.0)
        assert eng.preflight(env) is False


def test_preflight_normal_ok():
    """正常環境 → preflight True。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    from atlas_v3.core.env_observer import MarketEnvironment
    with patch("atlas_v3.bots.engines.calendar_native.kill_switch_is_active", return_value=False):
        eng = CalendarNativeEngine(dry_test=True)
        env = MarketEnvironment(vix=22.0)
        assert eng.preflight(env) is True


def test_preflight_env_none():
    """env=None → preflight False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.kill_switch_is_active", return_value=False):
        eng = CalendarNativeEngine(dry_test=True)
        assert eng.preflight(None) is False


# ---------------------------------------------------------------------------
# reset_daily
# ---------------------------------------------------------------------------


def test_reset_daily_clears_state():
    """reset_daily 後に日次状態がクリアされること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    eng = CalendarNativeEngine(dry_test=True)
    eng.entry_done = True
    eng.trade_done = True
    eng.today_vix = 25.0
    eng._entry_attempted = True

    eng.reset_daily()

    assert eng.entry_done is False
    assert eng.trade_done is False
    assert eng.today_vix is None
    assert eng._entry_attempted is False


def test_reset_daily_preserves_back_leg_position():
    """front_closed=True のポジションは reset_daily で保持されること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    eng = CalendarNativeEngine(dry_test=True)
    pos = CalendarNativePosition(
        front_code="DRY_FRONT_560C_2026-01-01",
        back_code="DRY_BACK_560C_2026-01-08",
        strike=560.0, qty=1, direction="CALL",
        front_entry_price=0.30, back_entry_price=0.60,
        front_iv=0.30,
    )
    pos.front_closed = True
    eng.position = pos

    eng.reset_daily()

    assert eng.position is not None
    assert eng.position.front_closed is True


def test_reset_daily_discards_open_position():
    """front_closed=False のポジションは reset_daily で破棄されること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    eng = CalendarNativeEngine(dry_test=True)
    pos = CalendarNativePosition(
        front_code="DRY_FRONT_560C_2026-01-01",
        back_code="DRY_BACK_560C_2026-01-08",
        strike=560.0, qty=1, direction="CALL",
        front_entry_price=0.30, back_entry_price=0.60,
        front_iv=0.30,
    )
    pos.front_closed = False
    eng.position = pos

    eng.reset_daily()

    assert eng.position is None


# ---------------------------------------------------------------------------
# premarket_check
# ---------------------------------------------------------------------------


def test_premarket_check_dry_test():
    """dry_test=True → premarket_check は True を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    eng = CalendarNativeEngine(dry_test=True)
    result = eng.premarket_check()
    assert result is True
    assert eng.today_vix == 22.0


def test_premarket_check_enable_calendar_false():
    """ENABLE_CALENDAR=False → premarket_check は False を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.ENABLE_CALENDAR", False):
        eng = CalendarNativeEngine(dry_test=True)
        result = eng.premarket_check()
    assert result is False


def test_premarket_check_paper_bypasses_vix():
    """paper=True → VIX 条件をバイパスして True を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    mock_mkt = MagicMock()
    mock_mkt.get_vix.return_value = 10.0  # 通常は VIX_MIN 未満でスキップ
    mock_mkt.get_vix_history.return_value = []
    eng = CalendarNativeEngine(mkt=mock_mkt, paper=True, dry_test=False)
    result = eng.premarket_check()
    assert result is True


def test_premarket_check_vix_too_low():
    """VIX < CALENDAR_VIX_MIN → premarket_check False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    mock_mkt = MagicMock()
    mock_mkt.get_vix.return_value = 15.0
    mock_mkt.get_vix_history.return_value = []
    eng = CalendarNativeEngine(mkt=mock_mkt, paper=False, dry_test=False)
    result = eng.premarket_check()
    assert result is False


def test_premarket_check_vix_trend_rising():
    """VIX 5 日トレンドが上昇 → premarket_check False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    mock_mkt = MagicMock()
    mock_mkt.get_vix.return_value = 25.0
    # 末尾 > 先頭 → slope > 0
    mock_mkt.get_vix_history.return_value = [20.0, 21.0, 22.0, 23.0, 25.0]
    eng = CalendarNativeEngine(mkt=mock_mkt, paper=False, dry_test=False)
    result = eng.premarket_check()
    assert result is False


def test_premarket_check_vix_trend_falling_ok():
    """VIX 5 日トレンドが下降 → premarket_check True。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    mock_mkt = MagicMock()
    mock_mkt.get_vix.return_value = 25.0
    # 末尾 < 先頭 → slope < 0
    mock_mkt.get_vix_history.return_value = [30.0, 28.0, 27.0, 26.0, 25.0]
    eng = CalendarNativeEngine(mkt=mock_mkt, paper=False, dry_test=False)
    result = eng.premarket_check()
    assert result is True


# ---------------------------------------------------------------------------
# execute_entry (dry_test=True)
# ---------------------------------------------------------------------------


def test_execute_entry_dry_test_returns_position(tmp_path):
    """dry_test=True → CalendarNativePosition を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"):
        eng = CalendarNativeEngine(dry_test=True)
        pos = eng.execute_entry(spy_price=560.0, vix=25.0)
    assert pos is not None
    assert pos.direction == "CALL"
    assert pos.qty >= 1
    assert pos.front_entry_price > 0
    assert pos.back_entry_price > pos.front_entry_price


def test_execute_entry_dry_test_sets_entry_done(tmp_path):
    """dry_test=True → entry_done=True、position が設定されること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"):
        eng = CalendarNativeEngine(dry_test=True)
        eng.execute_entry(spy_price=560.0, vix=25.0)
    assert eng.entry_done is True
    assert eng.position is not None


def test_execute_entry_kill_switch_blocks():
    """Kill Switch ARMED → execute_entry は None を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.kill_switch_is_active", return_value=True):
        eng = CalendarNativeEngine(dry_test=True)
        pos = eng.execute_entry(spy_price=560.0, vix=25.0)
    assert pos is None


def test_execute_entry_past_cutoff_blocks():
    """15:30 ET 以降 → execute_entry は None を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native._is_past_entry_cutoff", return_value=True):
        eng = CalendarNativeEngine(dry_test=True)
        pos = eng.execute_entry(spy_price=560.0, vix=25.0)
    assert pos is None


def test_execute_entry_second_call_skipped(tmp_path):
    """2 回目の execute_entry は _entry_attempted=True でスキップ。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"):
        eng = CalendarNativeEngine(dry_test=True)
        pos1 = eng.execute_entry(spy_price=560.0, vix=25.0)
        assert pos1 is not None
        eng.position = None  # 強制クリア
        pos2 = eng.execute_entry(spy_price=560.0, vix=25.0)
    assert pos2 is None  # 2 回目はスキップ


def test_execute_entry_position_initial_debit():
    """CalendarNativePosition.initial_debit が back - front で計算されること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativePosition
    pos = CalendarNativePosition(
        front_code="F", back_code="B",
        strike=560.0, qty=1, direction="CALL",
        front_entry_price=0.40, back_entry_price=0.80,
        front_iv=0.30,
    )
    assert abs(pos.initial_debit - 0.40) < 1e-9


# ---------------------------------------------------------------------------
# check_exit
# ---------------------------------------------------------------------------


def test_check_exit_no_position():
    """ポジションなし → check_exit は None を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    eng = CalendarNativeEngine(dry_test=True)
    assert eng.check_exit() is None


def test_check_exit_dry_test_early_returns_none():
    """dry_test=True で経過時間 < 7 分 → check_exit は None を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    eng = CalendarNativeEngine(dry_test=True)
    pos = CalendarNativePosition(
        front_code="F", back_code="B",
        strike=560.0, qty=1, direction="CALL",
        front_entry_price=0.30, back_entry_price=0.60,
        front_iv=0.30,
    )
    eng.position = pos
    eng._dry_test_start = datetime.datetime.now(ET)
    result = eng.check_exit()
    assert result is None


def test_check_exit_dry_test_7min_triggers(tmp_path):
    """dry_test=True で経過時間 >= 7 分 → check_exit が iv_crush_drytest で決済。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"):
        eng = CalendarNativeEngine(dry_test=True)
        pos = CalendarNativePosition(
            front_code="F", back_code="B",
            strike=560.0, qty=1, direction="CALL",
            front_entry_price=0.30, back_entry_price=0.60,
            front_iv=0.30,
        )
        eng.position = pos
        # 10 分前に開始したとみなす
        eng._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=10)
        result = eng.check_exit()
    assert result is not None
    assert result["reason"] == "iv_crush_drytest"
    assert eng.position is None
    assert eng.trade_done is True


def test_check_exit_iv_crush(tmp_path):
    """IV が entry 比 -10% 以上低下 → iv_crush で決済されること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    mock_mkt = MagicMock()
    # front IV: entry=0.30 → current=0.25 (-16.7%)
    mock_mkt.get_option_greeks.side_effect = lambda code: (
        {"iv": 0.25, "last": 0.25} if "FRONT" in code else {"iv": 0.40, "last": 0.55}
    )
    # 10:30 ET — フォースクローズ時刻 (15:45) よりはるかに前
    fake_now = datetime.datetime(2026, 4, 25, 10, 30, 0, tzinfo=ET)
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"), \
         patch("atlas_v3.bots.engines.calendar_native.datetime") as mock_dt, \
         patch("atlas_v3.bots.engines.calendar_native._is_early_close_today", return_value=False):
        mock_dt.datetime.now.return_value = fake_now
        mock_dt.timedelta = datetime.timedelta
        mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
        eng = CalendarNativeEngine(mkt=mock_mkt, dry_test=False)
        pos = CalendarNativePosition(
            front_code="US.SPY_FRONT_260417C560000",
            back_code="US.SPY_BACK_260424C560000",
            strike=560.0, qty=1, direction="CALL",
            front_entry_price=0.30, back_entry_price=0.60,
            front_iv=0.30,
        )
        pos.entry_time = "2026-04-25T10:00:00+00:00"
        eng.position = pos
        result = eng.check_exit()
    assert result is not None
    assert result["reason"] == "iv_crush"


def test_check_exit_max_loss(tmp_path):
    """debit が初期比 +30% 以上 → max_loss で決済されること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    mock_mkt = MagicMock()
    # initial_debit = 0.30 / current_debit = 0.30 * (1 + 0.35) = 0.405
    # front_last=0.20, back_last=0.605 → current_debit=0.405
    mock_mkt.get_option_greeks.side_effect = lambda code: (
        {"iv": 0.30, "last": 0.20} if "FRONT" in code else {"iv": 0.40, "last": 0.605}
    )
    # 10:30 ET — フォースクローズ時刻よりはるかに前
    fake_now = datetime.datetime(2026, 4, 25, 10, 30, 0, tzinfo=ET)
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"), \
         patch("atlas_v3.bots.engines.calendar_native.datetime") as mock_dt, \
         patch("atlas_v3.bots.engines.calendar_native._is_early_close_today", return_value=False):
        mock_dt.datetime.now.return_value = fake_now
        mock_dt.timedelta = datetime.timedelta
        mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
        eng = CalendarNativeEngine(mkt=mock_mkt, dry_test=False)
        pos = CalendarNativePosition(
            front_code="US.SPY_FRONT_260417C560000",
            back_code="US.SPY_BACK_260424C560000",
            strike=560.0, qty=1, direction="CALL",
            front_entry_price=0.30, back_entry_price=0.60,
            front_iv=0.30,
        )
        pos.entry_time = "2026-04-25T10:00:00+00:00"
        eng.position = pos
        result = eng.check_exit()
    assert result is not None
    assert result["reason"] == "max_loss"


# ---------------------------------------------------------------------------
# check_back_leg
# ---------------------------------------------------------------------------


def test_check_back_leg_no_position():
    """ポジションなし → check_back_leg は None を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    eng = CalendarNativeEngine(dry_test=True)
    assert eng.check_back_leg() is None


def test_check_back_leg_front_not_closed():
    """front_closed=False → check_back_leg は None を返すこと（front 生存中は対象外）。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    eng = CalendarNativeEngine(dry_test=True)
    pos = CalendarNativePosition(
        front_code="F", back_code="B",
        strike=560.0, qty=1, direction="CALL",
        front_entry_price=0.30, back_entry_price=0.60,
        front_iv=0.30,
    )
    pos.front_closed = False
    eng.position = pos
    assert eng.check_back_leg() is None


def test_check_back_leg_dry_test_triggers(tmp_path):
    """dry_test=True + front_closed=True → back_profit_target_drytest で即決済。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CalendarNativePosition
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"):
        eng = CalendarNativeEngine(dry_test=True)
        pos = CalendarNativePosition(
            front_code="F", back_code="B",
            strike=560.0, qty=1, direction="CALL",
            front_entry_price=0.30, back_entry_price=0.60,
            front_iv=0.30,
        )
        pos.front_closed = True
        eng.position = pos
        result = eng.check_back_leg()
    assert result is not None
    assert result["reason"] == "back_profit_target_drytest"
    assert eng.position is None


# ---------------------------------------------------------------------------
# should_trade_today（static）
# ---------------------------------------------------------------------------


def test_should_trade_today_enable_false():
    """ENABLE_CALENDAR=False → should_trade_today False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.ENABLE_CALENDAR", False):
        assert CalendarNativeEngine.should_trade_today(vix=25.0) is False


def test_should_trade_today_vix_none():
    """vix=None → should_trade_today False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    assert CalendarNativeEngine.should_trade_today(vix=None) is False


def test_should_trade_today_paper_bypasses():
    """paper=True → VIX 範囲外でも should_trade_today True。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    assert CalendarNativeEngine.should_trade_today(vix=10.0, paper=True) is True


def test_should_trade_today_vix_in_range_no_trend():
    """VIX 範囲内・トレンドなし → should_trade_today True。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    assert CalendarNativeEngine.should_trade_today(vix=25.0) is True


def test_should_trade_today_rising_vix_trend():
    """VIX 上昇トレンド → should_trade_today False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    assert CalendarNativeEngine.should_trade_today(
        vix=25.0,
        vix_history=[20.0, 21.0, 22.0, 23.0, 25.0],
    ) is False


def test_should_trade_today_ivr_below_threshold():
    """IVR < ivr_high_threshold → should_trade_today False。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    assert CalendarNativeEngine.should_trade_today(
        vix=25.0,
        ivr=0.50,
        ivr_high_threshold=0.75,
    ) is False


# ---------------------------------------------------------------------------
# ヘルパー関数単体テスト
# ---------------------------------------------------------------------------


def test_get_expiry_today_format():
    """_get_expiry_today が YYYY-MM-DD 形式を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import _get_expiry_today
    now = datetime.datetime(2026, 4, 25, 10, 0, 0, tzinfo=ET)
    assert _get_expiry_today(now) == "2026-04-25"


def test_extract_symbol_from_code():
    """_extract_symbol_from_code が正しく銘柄を抽出すること。"""
    from atlas_v3.bots.engines.calendar_native import _extract_symbol_from_code
    assert _extract_symbol_from_code("US.SPY260417C710000") == "US.SPY"
    assert _extract_symbol_from_code("US.QQQ260417P480000") == "US.QQQ"
    assert _extract_symbol_from_code("") == ""


def test_reason_to_exit_type():
    """_reason_to_exit_type が正しい PDT exit_type を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import _reason_to_exit_type
    assert _reason_to_exit_type("iv_crush") == "manual_close"
    assert _reason_to_exit_type("force_close_time") == "manual_close"
    assert _reason_to_exit_type("expired_worthless") == "expired_worthless"
    assert _reason_to_exit_type("broker_auto_expired") == "expired_worthless"
    assert _reason_to_exit_type("max_loss") == "manual_close"
    assert _reason_to_exit_type("back_profit_target") == "manual_close"


def test_is_past_entry_cutoff_dry_test():
    """dry_test=True → _is_past_entry_cutoff は常に False。"""
    from atlas_v3.bots.engines.calendar_native import _is_past_entry_cutoff
    assert _is_past_entry_cutoff(dry_test=True) is False


def test_atomic_json_write(tmp_path):
    """_atomic_json_write が正しく JSON ファイルを書き込むこと。"""
    from atlas_v3.bots.engines.calendar_native import _atomic_json_write
    target = tmp_path / "test.json"
    data = {"trades": [{"event": "entry", "pnl_usd": 0.0}]}
    _atomic_json_write(target, data)
    loaded = json.loads(target.read_text())
    assert loaded["trades"][0]["event"] == "entry"


def test_calc_qty_basic():
    """_calc_qty が risk 上限内の枚数を返すこと。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine, CALENDAR_MAX_QTY
    eng = CalendarNativeEngine(dry_test=True)
    # cash=10000, risk_pct=0.02 → max_risk=200, net_debit=0.30 → 200/(0.30*100)=6 → cap to MAX_QTY
    qty = eng._calc_qty(cash=10_000.0, net_debit=0.30)
    assert 1 <= qty <= CALENDAR_MAX_QTY


def test_record_pnl_creates_file(tmp_path):
    """_record_pnl が PnL JSON ファイルを作成すること。"""
    from atlas_v3.bots.engines.calendar_native import CalendarNativeEngine
    with patch("atlas_v3.bots.engines.calendar_native.CALENDAR_PNL_FILE", tmp_path / "pnl.json"):
        eng = CalendarNativeEngine(dry_test=True)
        eng._record_pnl("entry", 0.0, "CALL", 560.0, 1)
        pnl_file = tmp_path / "pnl.json"
    assert pnl_file.exists()
    data = json.loads(pnl_file.read_text())
    assert len(data["trades"]) == 1
    assert data["trades"][0]["event"] == "entry"


# ---------------------------------------------------------------------------
# Protocol 実装確認（isinstance チェック）
# ---------------------------------------------------------------------------


def test_market_data_protocol_mock_passes():
    """MarketDataProtocol を実装したモックが isinstance チェックを通ること。"""
    from atlas_v3.bots.engines.calendar_native import MarketDataProtocol

    class MockMarket:
        @property
        def underlying_code(self) -> str:
            return "US.SPY"

        @underlying_code.setter
        def underlying_code(self, v: str) -> None:
            pass

        def get_vix(self): return 25.0
        def get_vix_history(self, days=60): return []
        def get_option_chain_with_greeks(self, expiry, direction, center_strike=0.0): return []
        def find_by_strike(self, chain, strike): return None
        def get_last_price(self, symbol): return 560.0
        def get_option_greeks(self, code): return {}
        def get_cached_option_price(self, code, max_age_sec=15.0): return None

    assert isinstance(MockMarket(), MarketDataProtocol)


def test_trade_engine_protocol_mock_passes():
    """TradeEngineProtocol を実装したモックが isinstance チェックを通ること。"""
    from atlas_v3.bots.engines.calendar_native import TradeEngineProtocol

    class MockEngine:
        def get_account_cash(self): return 10_000.0
        def place_buy(self, code, qty, label, init_price=None, use_limit=False, signal_id=None): return "oid"
        def place_sell(self, code, qty, label): return "oid"

    assert isinstance(MockEngine(), TradeEngineProtocol)
