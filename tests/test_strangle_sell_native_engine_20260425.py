"""tests/test_strangle_sell_native_engine_20260425.py — StrangleSellNativeEngine テスト 27 件

観点:
  T-01: TacticBase ABC 継承確認
  T-02: tactic_type / tactic_name プロパティ
  T-03: StrangleSellNativeConfig デフォルト値検証
  T-04: StrangleSellNativePosition dataclass フィールド検証
  T-05: should_trade_today — paper=True → 常に True
  T-06: should_trade_today — VIX 範囲外 → False
  T-07: should_trade_today — IVR 不足 → False
  T-08: should_trade_today — 全条件 OK (本番) → True
  T-09: _expiry_for_dte — dte=0 → 当日日付
  T-10: _expiry_for_dte — dte=1 → 翌営業日
  T-11: _expiry_for_dte — dte=1 金曜 → 翌月曜
  T-12: _is_in_entry_window — 10:30 ET → True / 10:29 → False
  T-13: _is_force_close_time — 0DTE 15:45 以降 → True
  T-14: _calc_qty_from_risk — リスク率から枚数算出
  T-15: _estimate_otm_strike — CALL/PUT sigma 近似
  T-16: preflight — Kill Switch ARMED → False
  T-17: preflight — VIX 範囲外 (本番) → False
  T-18: preflight — 全条件 OK → True
  T-19: reset_daily — 状態リセット確認
  T-20: reset_daily — ポジション残存で警告ログ
  T-21: execute_entry — エントリー窓外 → None
  T-22: execute_entry — Kill Switch ARMED → None
  T-23: execute_entry — 決算近接ブロック → None
  T-24: execute_entry — 全条件 OK → StrangleSellNativePosition 返却
  T-25: execute_entry — 二重エントリー防止
  T-26: check_exit — フォースクローズ時刻 → force_close
  T-27: check_exit — 利確条件 → profit_target
  T-28: check_exit — 損切り条件 → stop_loss
  T-29: check_exit — ポジションなし → None
  T-30: _reason_to_exit_type — 各 reason 変換確認
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from atlas_v3.bots.engines.strangle_sell_native import (
    DEFAULT_CALL_DELTA,
    DEFAULT_ENTRY_CLOSE_H,
    DEFAULT_ENTRY_CLOSE_M,
    DEFAULT_ENTRY_OPEN_H,
    DEFAULT_ENTRY_OPEN_M,
    DEFAULT_FORCE_CLOSE_0DTE_H,
    DEFAULT_FORCE_CLOSE_0DTE_M,
    DEFAULT_IVR_MIN,
    DEFAULT_MAX_QTY,
    DEFAULT_MAX_RISK_PCT,
    DEFAULT_PROFIT_TARGET_PCT,
    DEFAULT_PUT_DELTA,
    DEFAULT_STOP_LOSS_MULT,
    DEFAULT_VIX_MAX,
    DEFAULT_VIX_MIN,
    StrangleSellEntryDecision,
    StrangleSellExitDecision,
    StrangleSellNativeConfig,
    StrangleSellNativeEngine,
    StrangleSellNativePosition,
    _calc_qty_from_risk,
    _estimate_otm_strike,
    _expiry_for_dte,
    _is_force_close_time,
    _is_in_entry_window,
    _reason_to_exit_type,
    should_trade_today,
)
from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.strategies.base import TacticBase

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_engine(
    dte: int = 0,
    paper: bool = True,
    now_et: datetime | None = None,
    earnings_date_fn=None,
    pdt_allowed: bool = True,
    option_chain_fn=None,
    account_cash_fn=None,
    place_order_fn=None,
    get_price_fn=None,
    earnings_proximity_days: int = 5,
) -> StrangleSellNativeEngine:
    """テスト用エンジンを構築する。"""
    cfg = StrangleSellNativeConfig(
        dte=dte,
        paper=paper,
        earnings_proximity_days=earnings_proximity_days,
    )
    mock_pdt = MagicMock()
    mock_pdt.check_can_trade.return_value = MagicMock(allowed=pdt_allowed, reason="ok")

    _now = now_et
    return StrangleSellNativeEngine(
        config=cfg,
        symbol="US.SPY",
        option_chain_fn=option_chain_fn,
        account_cash_fn=account_cash_fn or (lambda: 10_000.0),
        place_order_fn=place_order_fn,
        get_price_fn=get_price_fn,
        earnings_date_fn=earnings_date_fn,
        pdt_guard=mock_pdt,
        now_fn=(lambda: _now) if _now else None,
    )


def _make_env(vix: float = 20.0, ivr: float = 70.0) -> MarketEnvironment:
    return MarketEnvironment(vix=vix, ivr_by_symbol={"US.SPY": ivr})


def _entry_time_et(hour: int = 11, minute: int = 0) -> datetime:
    """エントリー窓内の ET 時刻を UTC で返す。"""
    return datetime(2026, 4, 25, hour, minute, 0, tzinfo=ET)


def _make_position() -> StrangleSellNativePosition:
    return StrangleSellNativePosition(
        symbol="US.SPY",
        call_code="DRY_SPY_260425C550000",
        put_code="DRY_SPY_260425P540000",
        call_strike=550.0,
        put_strike=540.0,
        qty=1,
        call_entry_price=0.30,
        put_entry_price=0.30,
        net_credit=60.0,
        entry_time="2026-04-25T11:00:00",
        expiry="2026-04-25",
        call_delta=0.15,
        put_delta=0.15,
        dte=0,
    )


# ---------------------------------------------------------------------------
# T-01: TacticBase ABC 継承確認
# ---------------------------------------------------------------------------

class TestTacticBaseInheritance:
    def test_is_tactic_base_subclass(self):
        assert issubclass(StrangleSellNativeEngine, TacticBase)

    def test_instance_isinstance_tactic_base(self):
        eng = _make_engine()
        assert isinstance(eng, TacticBase)


# ---------------------------------------------------------------------------
# T-02: tactic_type / tactic_name プロパティ
# ---------------------------------------------------------------------------

class TestProperties:
    def test_tactic_type_is_enter_exit(self):
        eng = _make_engine()
        assert eng.tactic_type == "enter_exit"

    def test_tactic_name(self):
        eng = _make_engine()
        assert eng.tactic_name == "strangle_sell_native"


# ---------------------------------------------------------------------------
# T-03: StrangleSellNativeConfig デフォルト値
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_dte_is_zero(self):
        cfg = StrangleSellNativeConfig()
        assert cfg.dte == 0

    def test_default_vix_range(self):
        cfg = StrangleSellNativeConfig()
        assert cfg.vix_min == DEFAULT_VIX_MIN
        assert cfg.vix_max == DEFAULT_VIX_MAX

    def test_default_ivr_min(self):
        cfg = StrangleSellNativeConfig()
        assert cfg.ivr_min == DEFAULT_IVR_MIN

    def test_default_profit_stop(self):
        cfg = StrangleSellNativeConfig()
        assert cfg.profit_target_pct == DEFAULT_PROFIT_TARGET_PCT
        assert cfg.stop_loss_mult == DEFAULT_STOP_LOSS_MULT

    def test_default_max_qty(self):
        cfg = StrangleSellNativeConfig()
        assert cfg.max_qty == DEFAULT_MAX_QTY

    def test_dte1_config(self):
        cfg = StrangleSellNativeConfig(dte=1)
        assert cfg.dte == 1


# ---------------------------------------------------------------------------
# T-04: StrangleSellNativePosition フィールド
# ---------------------------------------------------------------------------

class TestPositionDataclass:
    def test_position_fields(self):
        pos = _make_position()
        assert pos.symbol == "US.SPY"
        assert pos.qty == 1
        assert pos.dte == 0
        assert pos.tactic_name == "strangle_sell_native"
        assert pos.net_credit == 60.0


# ---------------------------------------------------------------------------
# T-05〜T-08: should_trade_today
# ---------------------------------------------------------------------------

class TestShouldTradeToday:
    def test_paper_bypasses_conditions(self):
        assert should_trade_today("US.SPY", vix=10.0, ivr=None, ivr_min=60.0, paper=True) is True

    def test_vix_out_of_range_returns_false(self):
        assert should_trade_today("US.SPY", vix=60.0, ivr=70.0, ivr_min=60.0,
                                   paper=False, vix_max=50.0) is False

    def test_ivr_below_min_returns_false(self):
        assert should_trade_today("US.SPY", vix=20.0, ivr=50.0, ivr_min=60.0, paper=False) is False

    def test_all_conditions_ok_returns_true(self):
        assert should_trade_today("US.SPY", vix=20.0, ivr=70.0, ivr_min=60.0, paper=False) is True

    def test_vix_none_returns_false(self):
        assert should_trade_today("US.SPY", vix=None, ivr=70.0, ivr_min=60.0, paper=False) is False

    def test_ivr_none_live_returns_false(self):
        assert should_trade_today("US.SPY", vix=20.0, ivr=None, ivr_min=60.0, paper=False) is False


# ---------------------------------------------------------------------------
# T-09〜T-11: _expiry_for_dte
# ---------------------------------------------------------------------------

class TestExpiryForDte:
    def test_dte0_returns_today(self):
        now_et = datetime(2026, 4, 25, 11, 0, tzinfo=ET)  # 土曜でない金曜
        result = _expiry_for_dte(now_et, 0)
        assert result == "2026-04-25"

    def test_dte1_returns_next_business_day(self):
        now_et = datetime(2026, 4, 23, 11, 0, tzinfo=ET)  # 木曜
        result = _expiry_for_dte(now_et, 1)
        assert result == "2026-04-24"  # 翌金曜

    def test_dte1_friday_returns_monday(self):
        # 2026-04-24 (金) → 翌営業日は 2026-04-27 (月)
        now_et = datetime(2026, 4, 24, 11, 0, tzinfo=ET)
        result = _expiry_for_dte(now_et, 1)
        assert result == "2026-04-27"


# ---------------------------------------------------------------------------
# T-12: _is_in_entry_window
# ---------------------------------------------------------------------------

class TestEntryWindow:
    def test_at_open_boundary_true(self):
        now_et = datetime(2026, 4, 25, 10, 30, tzinfo=ET)
        assert _is_in_entry_window(now_et, 10, 30, 12, 0) is True

    def test_one_minute_before_open_false(self):
        now_et = datetime(2026, 4, 25, 10, 29, tzinfo=ET)
        assert _is_in_entry_window(now_et, 10, 30, 12, 0) is False

    def test_at_close_boundary_false(self):
        now_et = datetime(2026, 4, 25, 12, 0, tzinfo=ET)
        assert _is_in_entry_window(now_et, 10, 30, 12, 0) is False

    def test_inside_window_true(self):
        now_et = datetime(2026, 4, 25, 11, 30, tzinfo=ET)
        assert _is_in_entry_window(now_et, 10, 30, 12, 0) is True


# ---------------------------------------------------------------------------
# T-13: _is_force_close_time
# ---------------------------------------------------------------------------

class TestForceCloseTime:
    def test_0dte_force_close_exact_time(self):
        now_et = datetime(2026, 4, 25, 15, 45, tzinfo=ET)
        assert _is_force_close_time(now_et, DEFAULT_FORCE_CLOSE_0DTE_H,
                                     DEFAULT_FORCE_CLOSE_0DTE_M) is True

    def test_0dte_before_force_close_false(self):
        now_et = datetime(2026, 4, 25, 15, 44, tzinfo=ET)
        assert _is_force_close_time(now_et, DEFAULT_FORCE_CLOSE_0DTE_H,
                                     DEFAULT_FORCE_CLOSE_0DTE_M) is False


# ---------------------------------------------------------------------------
# T-14: _calc_qty_from_risk
# ---------------------------------------------------------------------------

class TestCalcQty:
    def test_normal_case(self):
        # cash=10000, net_per_share=0.60, stop_mult=2.0, risk_pct=0.03
        # max_risk=300, risk_per_contract=0.60*2.0*100=120 → qty=2 (int(300/120)=2)
        qty = _calc_qty_from_risk(10_000, 0.60, 2.0, 0.03, 10)
        assert qty == 2

    def test_max_qty_cap(self):
        qty = _calc_qty_from_risk(1_000_000, 0.60, 2.0, 0.03, 2)
        assert qty == 2  # max_qty に clamp

    def test_min_qty_floor(self):
        qty = _calc_qty_from_risk(100, 0.60, 2.0, 0.03, 10)
        assert qty >= 1  # 最低 1 契約

    def test_zero_risk_per_contract_fallback(self):
        qty = _calc_qty_from_risk(10_000, 0.0, 2.0, 0.03, 10)
        assert qty == 1


# ---------------------------------------------------------------------------
# T-15: _estimate_otm_strike
# ---------------------------------------------------------------------------

class TestEstimateOtmStrike:
    def test_call_strike_above_underlying(self):
        strike = _estimate_otm_strike(500.0, 20.0, "CALL", 0.15)
        assert strike > 500.0

    def test_put_strike_below_underlying(self):
        strike = _estimate_otm_strike(500.0, 20.0, "PUT", 0.15)
        assert strike < 500.0

    def test_higher_vix_wider_strikes(self):
        strike_low = _estimate_otm_strike(500.0, 15.0, "CALL", 0.15)
        strike_high = _estimate_otm_strike(500.0, 40.0, "CALL", 0.15)
        assert strike_high > strike_low


# ---------------------------------------------------------------------------
# T-16〜T-18: preflight
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_kill_switch_armed_returns_false(self):
        eng = _make_engine(paper=False)
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=True):
            env = _make_env(vix=20.0)
            assert eng.preflight(env) is False

    def test_vix_out_of_range_live_returns_false(self):
        eng = _make_engine(paper=False)
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False):
            env = _make_env(vix=60.0)
            assert eng.preflight(env) is False

    def test_all_ok_returns_true(self):
        eng = _make_engine(paper=False)
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False):
            env = _make_env(vix=20.0)
            assert eng.preflight(env) is True

    def test_env_none_returns_false(self):
        eng = _make_engine()
        assert eng.preflight(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T-19〜T-20: reset_daily
# ---------------------------------------------------------------------------

class TestResetDaily:
    def test_reset_clears_state(self):
        eng = _make_engine(now_et=_entry_time_et())
        # 強制的に状態を設定
        eng._position = _make_position()
        eng._entry_done = True
        eng._trade_done = True
        eng._entry_attempted = True
        eng.reset_daily()
        assert eng._position is None
        assert eng._entry_done is False
        assert eng._trade_done is False
        assert eng._entry_attempted is False

    def test_reset_with_leftover_position_logs_warning(self, caplog):
        eng = _make_engine(now_et=_entry_time_et())
        eng._position = _make_position()
        import logging
        with caplog.at_level(logging.WARNING, logger="atlas_v3.bots.engines.strangle_sell_native"):
            eng.reset_daily()
        assert any("残存" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# T-21〜T-25: execute_entry
# ---------------------------------------------------------------------------

class TestExecuteEntry:
    def test_outside_entry_window_returns_none(self):
        # 09:00 ET はエントリー窓外
        eng = _make_engine(now_et=datetime(2026, 4, 25, 9, 0, tzinfo=ET))
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False):
            result = eng.execute_entry(underlying_price=500.0, vix=20.0, ivr=70.0)
        assert result is None

    def test_kill_switch_returns_none(self):
        eng = _make_engine(now_et=_entry_time_et())
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=True):
            result = eng.execute_entry(underlying_price=500.0, vix=20.0, ivr=70.0)
        assert result is None

    def test_earnings_proximity_blocks_entry(self):
        # earnings_date_fn が proximity ブロックを返す
        def _blocked_earnings(symbol):
            from datetime import date
            return date.today()  # 今日 = 0 営業日前

        # is_near_earnings をモックでブロック返却
        eng = _make_engine(now_et=_entry_time_et(), earnings_date_fn=_blocked_earnings)
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False), \
             patch("atlas_v3.bots.engines.strangle_sell_native.is_near_earnings",
                   return_value=(True, "決算 0 営業日前")):
            result = eng.execute_entry(underlying_price=500.0, vix=20.0, ivr=70.0)
        assert result is None

    def test_successful_entry_returns_position(self):
        eng = _make_engine(now_et=_entry_time_et(), pdt_allowed=True)
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False), \
             patch("atlas_v3.bots.engines.strangle_sell_native.is_near_earnings",
                   return_value=(False, "")), \
             patch("atlas_v3.bots.engines.strangle_sell_native.check_order_critical_only",
                   return_value=MagicMock(allowed=True, reason="")):
            pos = eng.execute_entry(underlying_price=500.0, vix=20.0, ivr=70.0)
        assert pos is not None
        assert isinstance(pos, StrangleSellNativePosition)
        assert pos.symbol == "US.SPY"
        assert pos.qty >= 1
        assert pos.net_credit > 0

    def test_double_entry_prevention(self):
        eng = _make_engine(now_et=_entry_time_et(), pdt_allowed=True)
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False), \
             patch("atlas_v3.bots.engines.strangle_sell_native.is_near_earnings",
                   return_value=(False, "")), \
             patch("atlas_v3.bots.engines.strangle_sell_native.check_order_critical_only",
                   return_value=MagicMock(allowed=True, reason="")):
            pos1 = eng.execute_entry(underlying_price=500.0, vix=20.0, ivr=70.0)
            pos2 = eng.execute_entry(underlying_price=500.0, vix=20.0, ivr=70.0)
        assert pos1 is not None
        assert pos2 is None  # 二重エントリー防止


# ---------------------------------------------------------------------------
# T-26〜T-29: check_exit
# ---------------------------------------------------------------------------

class TestCheckExit:
    def _engine_with_position(self) -> StrangleSellNativeEngine:
        eng = _make_engine(now_et=_entry_time_et())
        eng._position = _make_position()
        eng._entry_done = True
        return eng

    def test_no_position_returns_none(self):
        eng = _make_engine()
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False):
            assert eng.check_exit() is None

    def test_force_close_time_triggers_exit(self):
        # 15:45 ET = フォースクローズ時刻
        eng = _make_engine(now_et=datetime(2026, 4, 25, 15, 45, tzinfo=ET))
        eng._position = _make_position()
        eng._entry_done = True
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False):
            decision = eng.check_exit()
        assert decision is not None
        assert decision.should_exit is True
        assert decision.exit_type == "force_close"

    def test_profit_target_triggers_exit(self):
        eng = _make_engine(now_et=_entry_time_et())
        pos = _make_position()  # net_credit=60.0
        eng._position = pos
        eng._entry_done = True
        # profit_threshold = 60.0 * (1.0 - 0.50) = 30.0
        # current_cost = (0.10 + 0.05) * 1 * 100 = 15.0 <= 30.0 → 利確
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False):
            decision = eng.check_exit(call_current_price=0.10, put_current_price=0.05)
        assert decision is not None
        assert decision.exit_type == "profit_target"

    def test_stop_loss_triggers_exit(self):
        eng = _make_engine(now_et=_entry_time_et())
        pos = _make_position()  # net_credit=60.0
        eng._position = pos
        eng._entry_done = True
        # stop_threshold = 60.0 * 2.0 = 120.0
        # current_cost = (0.70 + 0.70) * 1 * 100 = 140.0 >= 120.0 → 損切り
        with patch("atlas_v3.bots.engines.strangle_sell_native.kill_switch_is_active",
                   return_value=False):
            decision = eng.check_exit(call_current_price=0.70, put_current_price=0.70)
        assert decision is not None
        assert decision.exit_type == "stop_loss"


# ---------------------------------------------------------------------------
# T-30: _reason_to_exit_type
# ---------------------------------------------------------------------------

class TestReasonToExitType:
    def test_profit_reason(self):
        assert _reason_to_exit_type("profit_target") == "profit_target"

    def test_stop_reason(self):
        assert _reason_to_exit_type("stop_loss") == "stop_loss"

    def test_force_reason(self):
        assert _reason_to_exit_type("force_close_time") == "force_close"

    def test_kill_reason(self):
        assert _reason_to_exit_type("force_close_kill_switch") == "force_close"

    def test_unknown_reason(self):
        assert _reason_to_exit_type("unknown") == "none"
