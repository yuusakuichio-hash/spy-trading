"""tests/test_ivcrush_native_engine_20260425.py — IVCrushNativeEngine テスト 25 件

観点:
  T-01: TacticBase ABC 継承確認
  T-02: tactic_type / tactic_name プロパティ
  T-03: preflight — Kill Switch 未発動 → True
  T-04: preflight — Kill Switch 発動中 → False
  T-05: reset_daily — state クリア確認
  T-06: premarket_check — enable=False → False
  T-07: premarket_check — Kill Switch 発動中 → False
  T-08: premarket_check — dry_test=True → True かつ TSLA がセット
  T-09: premarket_check — EarningsEngine 候補あり → True・info セット
  T-10: premarket_check — 候補なし → False
  T-11: check_entry — entry_done=True → False（二重エントリー防止）
  T-12: check_entry — _today_earnings_info=None → False
  T-13: check_entry — dry_test 5分未満 → False / 5分以降 → True
  T-14: check_entry — Kill Switch 発動中 → False
  T-15: _get_entry_expiry — 土曜起点 → 翌月曜
  T-16: _calc_iv_crush_params — vix_band=high → iv_pct_min=0.75
  T-17: _calc_iv_crush_params — vix_band=low → profit_target_pct=0.35
  T-18: _calc_iv_crush_params — cash<8000 → phase=1 max_qty=1
  T-19: _calc_iv_crush_params — pnl_history 勝率<40% → max_qty-1
  T-20: check_exit — position=None → None
  T-21: check_exit — dry_test 10分未満 → None / 10分超 → dict(reason=vol_crush_drytest)
  T-22: check_exit — 時刻 < exit_start → None
  T-23: check_exit — タイムストップ（exit_deadline 以降）→ reason=time_stop
  T-24: check_exit — 利確条件（premium 50%低下）→ reason=vol_crush_profit
  T-25: check_exit — 損切条件（premium 10%上昇）→ reason=stop_loss
"""
from __future__ import annotations

import datetime
import sys
import os
from typing import Optional
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from atlas_v3.bots.engines.ivcrush_native import (
    IV_CRUSH_EXIT_DEADLINE_H,
    IV_CRUSH_EXIT_DEADLINE_M,
    IV_CRUSH_EXIT_H,
    IV_CRUSH_EXIT_M,
    IVCrushNativeEngine,
    IVCrushNativePosition,
    _calc_iv_crush_params,
)
from atlas_v3.bots.engines.pdt_guard import PDTCheckResult, PDTGuard
from atlas_v3.strategies.base import TacticBase

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_kill_switch(tmp_path, monkeypatch):
    """Kill Switch state_v3 を tmp_path に隔離。"""
    import common_v3.risk.kill_switch as ks_module
    tmp_state = tmp_path / "state_v3"
    tmp_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ks_module, "_STATE_DIR", tmp_state)
    monkeypatch.setattr(ks_module, "FLAG_FILE", tmp_state / "kill_switch.flag")
    monkeypatch.setattr(ks_module, "AUDIT_FILE", tmp_state / "kill_switch_audit.jsonl")
    yield


@pytest.fixture(autouse=True)
def isolate_pnl_file(tmp_path, monkeypatch):
    """PnL ファイルを tmp_path に隔離。"""
    import atlas_v3.bots.engines.ivcrush_native as mod
    monkeypatch.setattr(mod, "IV_CRUSH_PNL_FILE", tmp_path / "iv_crush_native_pnl.json")
    yield


def _make_mkt(
    vix: float = 22.0,
    atm_strike: float = 500.0,
    call_code: str = "US.TSLA_CALL_dummy",
    put_code: str = "US.TSLA_PUT_dummy",
    call_greeks: Optional[dict] = None,
    put_greeks: Optional[dict] = None,
) -> MagicMock:
    mkt = MagicMock()
    mkt.get_vix.return_value = vix
    mkt.get_atm_strike.return_value = atm_strike
    mkt.get_option_code.side_effect = lambda sym, exp, strike, side: (
        call_code if side == "CALL" else put_code
    )
    default_greeks = {"iv": 0.85, "ask": 5.0, "last": 5.0}
    mkt.get_option_greeks.side_effect = lambda code: (
        call_greeks if (call_greeks and code == call_code)
        else (put_greeks if put_greeks else default_greeks)
    )
    return mkt


def _make_eng(cash: float = 15_000.0) -> MagicMock:
    eng = MagicMock()
    eng.get_account_cash.return_value = cash
    eng._place_single_leg.return_value = ("order123", "ok")
    eng._reverse_leg.return_value = None
    return eng


def _make_pdt_guard(allowed: bool = True) -> MagicMock:
    guard = MagicMock(spec=PDTGuard)
    guard.check_can_trade.return_value = PDTCheckResult(
        allowed=allowed,
        reason="paper_mode=True: PDT チェックスキップ",
        paper_mode=True,
    )
    guard.check_can_trade_with_count.return_value = PDTCheckResult(
        allowed=allowed,
        reason="paper_mode=True: PDT チェックスキップ",
        paper_mode=True,
    )
    return guard


def _make_earnings_engine(symbols: list[str] = ["TSLA"]) -> MagicMock:
    ee = MagicMock()
    candidates = []
    for sym in symbols:
        c = MagicMock()
        c.symbol = sym
        c.full_code = f"US.{sym}"
        c.iv_crush_rate = 0.35
        c.report_time = "amc"
        c.estimated_dt = datetime.datetime.now(ET)
        candidates.append(c)
    ee.get_today_candidates.return_value = candidates
    return ee


def _make_engine(
    dry_test: bool = False,
    enable: bool = True,
    paper: bool = True,
    earnings_symbols: list[str] = ["TSLA"],
    cash: float = 15_000.0,
    vix: float = 22.0,
    pdt_allowed: bool = True,
) -> IVCrushNativeEngine:
    return IVCrushNativeEngine(
        mkt=_make_mkt(vix=vix),
        eng=_make_eng(cash=cash),
        paper=paper,
        dry_test=dry_test,
        enable=enable,
        earnings_engine=_make_earnings_engine(earnings_symbols),
        pdt_guard=_make_pdt_guard(pdt_allowed),
    )


def _make_position(
    entry_premium: float = 10.0,
    call_entry_price: float = 5.0,
    put_entry_price: float = 5.0,
    qty: int = 1,
) -> IVCrushNativePosition:
    return IVCrushNativePosition(
        symbol="US.TSLA",
        call_code="US.TSLA_CALL_dummy",
        put_code="US.TSLA_PUT_dummy",
        strike=500.0,
        qty=qty,
        call_entry_price=call_entry_price,
        put_entry_price=put_entry_price,
        entry_premium=entry_premium,
        entry_iv=0.85,
        entry_time=datetime.datetime.now(ET).isoformat(),
        earnings_date=datetime.date.today().isoformat(),
        earnings_hour="amc",
        expiry=(datetime.date.today() + datetime.timedelta(days=1)).isoformat(),
        idempotency_key="v3_test_key",
    )


# ---------------------------------------------------------------------------
# T-01: TacticBase ABC 継承
# ---------------------------------------------------------------------------

class TestTacticBaseInheritance:
    def test_is_tacticbase_subclass(self):
        assert issubclass(IVCrushNativeEngine, TacticBase)

    def test_instance_is_tacticbase(self):
        eng = _make_engine()
        assert isinstance(eng, TacticBase)


# ---------------------------------------------------------------------------
# T-02: tactic_type / tactic_name
# ---------------------------------------------------------------------------

class TestTacticProperties:
    def test_tactic_type(self):
        eng = _make_engine()
        assert eng.tactic_type == "state_carrying"

    def test_tactic_name(self):
        eng = _make_engine()
        assert eng.tactic_name == "iv_crush_native"


# ---------------------------------------------------------------------------
# T-03 / T-04: preflight
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_preflight_kill_switch_inactive(self):
        eng = _make_engine()
        env = MagicMock()
        assert eng.preflight(env) is True

    def test_preflight_kill_switch_active(self):
        eng = _make_engine()
        env = MagicMock()
        with patch(
            "atlas_v3.bots.engines.ivcrush_native.kill_switch_is_active",
            return_value=True,
        ):
            assert eng.preflight(env) is False


# ---------------------------------------------------------------------------
# T-05: reset_daily
# ---------------------------------------------------------------------------

class TestResetDaily:
    def test_reset_clears_state(self):
        eng = _make_engine()
        eng.position    = _make_position()
        eng.trade_done  = True
        eng.entry_done  = True
        eng._today_earnings_info = {"ticker": "TSLA"}
        eng._dynamic_params = {"profit_target_pct": 0.5}

        eng.reset_daily()

        assert eng.position is None
        assert eng.trade_done is False
        assert eng.entry_done is False
        assert eng._today_earnings_info is None
        assert eng._dynamic_params is None


# ---------------------------------------------------------------------------
# T-06 / T-07 / T-08 / T-09 / T-10: premarket_check
# ---------------------------------------------------------------------------

class TestPremarketCheck:
    def test_disable_returns_false(self):
        eng = _make_engine(enable=False)
        assert eng.premarket_check() is False

    def test_kill_switch_active_returns_false(self):
        eng = _make_engine()
        with patch(
            "atlas_v3.bots.engines.ivcrush_native.kill_switch_is_active",
            return_value=True,
        ):
            assert eng.premarket_check() is False

    def test_dry_test_sets_tsla(self):
        eng = _make_engine(dry_test=True)
        result = eng.premarket_check()
        assert result is True
        assert eng._today_earnings_info is not None
        assert eng._today_earnings_info["ticker"] == "TSLA"

    def test_candidates_available_returns_true(self):
        eng = _make_engine(earnings_symbols=["NVDA"])
        result = eng.premarket_check()
        assert result is True
        assert eng._today_earnings_info["ticker"] == "NVDA"

    def test_no_candidates_returns_false(self):
        eng = _make_engine()
        eng._earnings_engine.get_today_candidates.return_value = []
        result = eng.premarket_check()
        assert result is False
        assert eng._today_earnings_info is None


# ---------------------------------------------------------------------------
# T-11 / T-12 / T-13 / T-14: check_entry
# ---------------------------------------------------------------------------

class TestCheckEntry:
    def test_entry_done_prevents_reentry(self):
        eng = _make_engine(dry_test=True)
        eng._today_earnings_info = {"ticker": "TSLA", "date": "2026-04-25", "hour": "amc"}
        eng.entry_done = True
        assert eng.check_entry() is False

    def test_no_earnings_info_returns_false(self):
        eng = _make_engine(dry_test=True)
        eng._today_earnings_info = None
        assert eng.check_entry() is False

    def test_dry_test_under_5min_returns_false(self):
        eng = _make_engine(dry_test=True)
        eng._today_earnings_info = {"ticker": "TSLA", "date": "2026-04-25", "hour": "amc"}
        # dry_test_start を今に設定
        eng._dry_test_start = datetime.datetime.now(ET)
        result = eng.check_entry()
        assert result is False

    def test_dry_test_over_5min_returns_true(self):
        eng = _make_engine(dry_test=True)
        eng._today_earnings_info = {"ticker": "TSLA", "date": "2026-04-25", "hour": "amc"}
        # 6 分前に設定
        eng._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=6)
        result = eng.check_entry()
        assert result is True
        assert eng.entry_done is True

    def test_kill_switch_blocks_check_entry(self):
        eng = _make_engine(dry_test=True)
        eng._today_earnings_info = {"ticker": "TSLA", "date": "2026-04-25", "hour": "amc"}
        eng._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=6)
        with patch(
            "atlas_v3.bots.engines.ivcrush_native.kill_switch_is_active",
            return_value=True,
        ):
            assert eng.check_entry() is False


# ---------------------------------------------------------------------------
# T-15: _get_entry_expiry — 土曜起点
# ---------------------------------------------------------------------------

class TestGetEntryExpiry:
    def test_saturday_skips_to_monday(self, monkeypatch):
        eng = _make_engine()
        # 金曜日 2026-04-24 → 翌営業日は月曜 2026-04-27
        friday = datetime.date(2026, 4, 24)
        with patch("atlas_v3.bots.engines.ivcrush_native.datetime") as mock_dt:
            mock_dt.date.today.return_value = friday
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            expiry = eng._get_entry_expiry()
        # 金曜の翌日 = 土曜 → +2 = 日曜 → +2 = 月曜 2026-04-27
        assert expiry == "2026-04-27"


# ---------------------------------------------------------------------------
# T-16 / T-17 / T-18 / T-19: _calc_iv_crush_params
# ---------------------------------------------------------------------------

class TestCalcIVCrushParams:
    def test_high_vix_lowers_iv_percentile(self):
        p = _calc_iv_crush_params("TSLA", vix_current=25.0, cash_usd=15_000.0)
        assert p["iv_percentile_min"] == 0.75

    def test_low_vix_profit_target(self):
        p = _calc_iv_crush_params("TSLA", vix_current=12.0, cash_usd=15_000.0)
        assert p["profit_target_pct"] == pytest.approx(0.35)

    def test_small_account_phase1(self):
        p = _calc_iv_crush_params("TSLA", vix_current=22.0, cash_usd=5_000.0)
        assert p["max_qty"] == 1
        assert p["max_risk_pct"] == pytest.approx(0.015)

    def test_pnl_history_low_winrate_reduces_qty(self):
        pnl = [-1, -2, -3, -4, 5]   # win_rate = 1/5 = 20% < 40%
        p = _calc_iv_crush_params(
            "TSLA", vix_current=22.0, cash_usd=15_000.0, pnl_history=pnl
        )
        # phase=2 → max_qty_base=2 → kelly_qty-1 → 1
        assert p["max_qty"] == 1

    def test_pnl_history_high_winrate_boosts_tp(self):
        pnl = [1, 2, 3, 4, 5, 6, 0.5]   # win_rate = 7/7 = 100% >= 65%
        # vix_current=18.0 → vix_band=mid → tp_map["mid"]=0.50 + 0.05 = 0.55
        p = _calc_iv_crush_params(
            "TSLA", vix_current=18.0, cash_usd=15_000.0, pnl_history=pnl
        )
        assert p["profit_target_pct"] == pytest.approx(0.55)

    def test_source_contains_vix_band(self):
        p = _calc_iv_crush_params("TSLA", vix_current=18.0, cash_usd=15_000.0)
        assert "vix_band=mid" in p["_source"]


# ---------------------------------------------------------------------------
# T-20 / T-21 / T-22 / T-23 / T-24 / T-25: check_exit
# ---------------------------------------------------------------------------

class TestCheckExit:
    def test_no_position_returns_none(self):
        eng = _make_engine()
        assert eng.check_exit() is None

    def test_trade_done_returns_none(self):
        eng = _make_engine()
        eng.position   = _make_position()
        eng.trade_done = True
        assert eng.check_exit() is None

    def test_dry_test_under_10min_returns_none(self):
        eng = _make_engine(dry_test=True)
        eng.position = _make_position()
        eng._dry_test_start = datetime.datetime.now(ET)
        assert eng.check_exit() is None

    def test_dry_test_over_10min_returns_close(self):
        eng = _make_engine(dry_test=True)
        eng.position = _make_position(entry_premium=10.0)
        eng._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=11)
        result = eng.check_exit()
        assert result is not None
        assert result["reason"] == "vol_crush_drytest"
        assert result["pnl_usd"] > 0

    def test_before_exit_window_returns_none(self):
        """exit_start より前の時刻は None。"""
        eng = _make_engine()
        eng.position = _make_position()
        # 8:00 ET（exit_start 9:45 より前）
        mock_time = datetime.datetime(2026, 4, 25, 8, 0, tzinfo=ET)
        with patch("atlas_v3.bots.engines.ivcrush_native.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_time
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            result = eng.check_exit()
        assert result is None

    def test_time_stop_at_deadline(self):
        """exit_deadline 以降 → time_stop。"""
        eng = _make_engine()
        eng.position = _make_position(entry_premium=10.0)
        # 10:16 ET（deadline 10:15 超過）
        mock_time = datetime.datetime(
            2026, 4, 25,
            IV_CRUSH_EXIT_DEADLINE_H,
            IV_CRUSH_EXIT_DEADLINE_M + 1,
            tzinfo=ET,
        )
        with patch("atlas_v3.bots.engines.ivcrush_native.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_time
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            result = eng.check_exit()
        assert result is not None
        assert result["reason"] == "time_stop"

    def test_profit_target_triggers_close(self):
        """プレミアム 50% 低下 → vol_crush_profit。

        vix=18 → vix_band=mid → tp=0.50.
        entry_premium=10.0, current=3.8 → chg=-62% → <= -50% → 利確。
        """
        # vix=18 → band=mid → profit_target_pct=0.50
        eng = _make_engine(vix=18.0)
        pos = _make_position(
            entry_premium=10.0,
            call_entry_price=5.0,
            put_entry_price=5.0,
        )
        eng.position = pos

        # 現在プレミアム = 3.8（entry 10.0 の 62% 低下 > tp=50%）
        mkt = eng.mkt
        mkt.get_option_greeks.side_effect = lambda code: {"last": 1.9}

        mock_time = datetime.datetime(
            2026, 4, 25,
            IV_CRUSH_EXIT_H,
            IV_CRUSH_EXIT_M + 5,
            tzinfo=ET,
        )
        with patch("atlas_v3.bots.engines.ivcrush_native.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_time
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            result = eng.check_exit()

        assert result is not None
        assert result["reason"] == "vol_crush_profit"

    def test_stop_loss_triggers_close(self):
        """プレミアム 10% 上昇 → stop_loss。"""
        eng = _make_engine()
        pos = _make_position(
            entry_premium=10.0,
            call_entry_price=5.0,
            put_entry_price=5.0,
        )
        eng.position = pos

        # 現在プレミアム = 11.5（entry 10.0 の 15% 上昇 > sl=10%）
        mkt = eng.mkt
        mkt.get_option_greeks.side_effect = lambda code: {"last": 5.75}

        mock_time = datetime.datetime(
            2026, 4, 25,
            IV_CRUSH_EXIT_H,
            IV_CRUSH_EXIT_M + 5,
            tzinfo=ET,
        )
        with patch("atlas_v3.bots.engines.ivcrush_native.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_time
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            result = eng.check_exit()

        assert result is not None
        assert result["reason"] == "stop_loss"

    def test_kill_switch_forces_close(self):
        """Kill Switch 発動中は force_close。"""
        eng = _make_engine(dry_test=True)
        eng.position = _make_position()
        with patch(
            "atlas_v3.bots.engines.ivcrush_native.kill_switch_is_active",
            return_value=True,
        ):
            result = eng.check_exit()
        assert result is not None
        assert result["reason"] == "kill_switch_force_close"
