"""tests/test_iron_condor_sell_native_engine_20260425.py

atlas_v3/bots/engines/iron_condor_sell_native.py のテストスイート。
25 件以上・spy_bot.py 書換ゼロ。

テスト分類:
  A. 設定 DTO (IronCondorSellConfig)
  B. ポジション DTO (IronCondorSellPosition)
  C. reset_daily
  D. preflight
  E. premarket_check
  F. execute_entry (dry_test)
  G. check_exit / should_exit_decision
  H. PDT ガード
  I. 決算近接ブロック
  J. kill_switch 統合
  K. should_enter_decision (DTO ベース)
  L. 内部算出ヘルパー
"""
from __future__ import annotations

import datetime
from typing import Optional
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from atlas_v3.bots.engines.iron_condor_sell_native import (
    IronCondorSellConfig,
    IronCondorSellEngine,
    IronCondorSellExitDecision,
    IronCondorSellPosition,
    NoOpTradeEngine,
)
from atlas_v3.bots.engines.pdt_guard import PDTBlockedError
from atlas_v3.core.env_observer import MarketEnvironment

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# 共通 fixture ヘルパー
# ---------------------------------------------------------------------------

def _make_engine(
    dry_test: bool = True,
    paper: bool = True,
    vix: float = 22.0,
    vix_history: Optional[list] = None,
    atr: float = 4.0,
    config: Optional[IronCondorSellConfig] = None,
) -> IronCondorSellEngine:
    """テスト用 IronCondorSellEngine を生成する。"""
    eng_stub = NoOpTradeEngine()
    eng_stub.get_vix = lambda: vix
    eng_stub.get_vix_history = lambda days=60: (vix_history or ([20.0] * days))
    eng_stub.get_symbol_atr = lambda symbol, period=14: atr
    return IronCondorSellEngine(
        trade_engine=eng_stub,
        config=config,
        paper=paper,
        dry_test=dry_test,
    )


def _make_env(
    vix: float = 22.0,
    ivr: float = 55.0,
    symbol: str = "US.SPY",
) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        ivr_by_symbol={symbol: ivr},
    )


def _et_time(h: int, m: int) -> datetime.datetime:
    """ET タイムゾーン付き datetime を返す。"""
    today = datetime.datetime.now(ET).date()
    return datetime.datetime(today.year, today.month, today.day, h, m, 0, tzinfo=ET)


# ===========================================================================
# A. 設定 DTO
# ===========================================================================

class TestIronCondorSellConfig:
    def test_default_values_match_spy_bot_constants(self):
        """デフォルト値が spy_bot.py の定数と一致する。"""
        cfg = IronCondorSellConfig()
        assert cfg.call_delta_base == 0.20
        assert cfg.put_delta_base == 0.20
        assert cfg.vix_min == 18.0
        assert cfg.vix_max == 40.0
        assert cfg.vix_high_threshold == 28.0
        assert cfg.ivr_min_pct == 40.0
        assert cfg.profit_target_pct == 0.50
        assert cfg.stop_loss_mult == 2.0
        assert cfg.width_default == 5
        assert cfg.force_close_h == 15
        assert cfg.force_close_m == 45

    def test_override_works(self):
        """フィールドを上書きできる。"""
        cfg = IronCondorSellConfig(vix_min=20.0, max_qty=5)
        assert cfg.vix_min == 20.0
        assert cfg.max_qty == 5

    def test_config_is_mutable(self):
        """frozen=False なので実行時変更可能。"""
        cfg = IronCondorSellConfig()
        cfg.vix_max = 35.0
        assert cfg.vix_max == 35.0


# ===========================================================================
# B. ポジション DTO
# ===========================================================================

class TestIronCondorSellPosition:
    def _make_pos(self, call_credit=0.40, put_credit=0.40, spread_width=5.0):
        return IronCondorSellPosition(
            symbol="US.SPY", expiry="2026-04-25", qty=2,
            call_sell_code="CS_CODE", call_buy_code="CB_CODE",
            put_sell_code="PS_CODE", put_buy_code="PB_CODE",
            call_sell_strike=570.0, call_buy_strike=575.0,
            put_sell_strike=550.0, put_buy_strike=545.0,
            call_net_credit=call_credit, put_net_credit=put_credit,
            spread_width=spread_width, vix=22.0,
        )

    def test_net_credit_is_sum_of_call_and_put(self):
        pos = self._make_pos(call_credit=0.35, put_credit=0.45)
        assert pos.net_credit == pytest.approx(0.80, rel=1e-4)

    def test_max_loss_per_contract(self):
        """max_loss = (spread_width - net_credit) * 100"""
        pos = self._make_pos(call_credit=0.40, put_credit=0.40, spread_width=5.0)
        expected = (5.0 - 0.80) * 100
        assert pos.max_loss_per_contract == pytest.approx(expected, rel=1e-4)

    def test_entry_time_is_set(self):
        pos = self._make_pos()
        assert pos.entry_time  # None でも空でもないこと

    def test_symbol_stored_correctly(self):
        pos = self._make_pos()
        assert pos.symbol == "US.SPY"

    def test_qty_stored_correctly(self):
        pos = self._make_pos()
        assert pos.qty == 2


# ===========================================================================
# C. reset_daily
# ===========================================================================

class TestResetDaily:
    def test_reset_clears_all_daily_state(self):
        engine = _make_engine()
        engine.today_vix = 25.0
        engine.trade_done = True
        engine.entry_done = True
        engine._vix_spike_30 = True
        engine.position = MagicMock()
        engine._assessment = MagicMock()

        engine.reset_daily()

        assert engine.today_vix is None
        assert engine.position is None
        assert engine.trade_done is False
        assert engine.entry_done is False
        assert engine._assessment is None
        assert engine._vix_spike_30 is False

    def test_reset_after_entry_allows_reentry(self):
        engine = _make_engine()
        engine.entry_done = True
        engine.reset_daily()
        assert not engine.entry_done


# ===========================================================================
# D. preflight
# ===========================================================================

class TestPreflight:
    def test_preflight_ok_normal_env(self):
        engine = _make_engine()
        env = _make_env(vix=22.0)
        assert engine.preflight(env) is True

    def test_preflight_false_when_vix_too_high(self):
        engine = _make_engine(vix=42.0)
        env = _make_env(vix=42.0)
        assert engine.preflight(env) is False

    def test_preflight_false_when_env_none(self):
        engine = _make_engine()
        assert engine.preflight(None) is False  # type: ignore[arg-type]

    def test_preflight_false_when_kill_switch_active(self):
        engine = _make_engine()
        env = _make_env(vix=20.0)
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            assert engine.preflight(env) is False


# ===========================================================================
# E. premarket_check
# ===========================================================================

class TestPremarketCheck:
    def test_dry_test_always_passes(self):
        engine = _make_engine(dry_test=True)
        assert engine.premarket_check("US.SPY") is True

    def test_dry_test_sets_vix_22(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        assert engine.today_vix == 22.0

    def test_vix_below_min_returns_false(self):
        engine = _make_engine(dry_test=False, vix=15.0)
        assert engine.premarket_check("US.SPY") is False

    def test_vix_above_max_returns_false(self):
        engine = _make_engine(dry_test=False, vix=41.0)
        assert engine.premarket_check("US.SPY") is False

    def test_normal_vix_passes(self):
        engine = _make_engine(dry_test=False, vix=22.0)
        # IVR 履歴: vix 22.0 が 60 日中 40 件以上あれば 66%ile >= 40%ile OK
        engine._eng.get_vix_history = lambda days=60: [22.0] * 60
        assert engine.premarket_check("US.SPY") is True

    def test_kill_switch_blocks_premarket(self):
        engine = _make_engine(dry_test=False, vix=22.0)
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            assert engine.premarket_check("US.SPY") is False

    def test_assessment_is_set_on_ok(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        assert engine._assessment is not None
        assert engine._assessment.ok is True

    def test_assessment_is_set_on_fail(self):
        engine = _make_engine(dry_test=False, vix=15.0)
        engine.premarket_check("US.SPY")
        assert engine._assessment is not None
        assert engine._assessment.ok is False


# ===========================================================================
# F. execute_entry (dry_test)
# ===========================================================================

class TestExecuteEntry:
    def test_dry_entry_returns_position(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        assert isinstance(pos, IronCondorSellPosition)

    def test_dry_entry_sets_entry_done(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        engine.execute_entry("US.SPY")
        assert engine.entry_done is True

    def test_dry_entry_sets_position(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        engine.execute_entry("US.SPY")
        assert engine.position is not None

    def test_second_execute_returns_none(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        engine.execute_entry("US.SPY")
        pos2 = engine.execute_entry("US.SPY")
        assert pos2 is None

    def test_position_symbol_matches_input(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        assert pos.symbol == "US.SPY"

    def test_net_credit_positive(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        assert pos.net_credit > 0

    def test_call_sell_above_atm(self):
        """CALL 売りストライクは ATM より高い。"""
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        # ATM = 560, CALL 売り = ATM + spread_width*2
        assert pos.call_sell_strike > 560.0

    def test_put_sell_below_atm(self):
        """PUT 売りストライクは ATM より低い。"""
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        assert pos.put_sell_strike < 560.0

    def test_kill_switch_blocks_entry(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            pos = engine.execute_entry("US.SPY")
        assert pos is None

    def test_entry_cutoff_blocks_when_past_cutoff(self):
        """エントリーカットオフ後は None を返す。"""
        engine = _make_engine(dry_test=False, vix=22.0)
        engine.premarket_check("US.SPY")
        engine.entry_done = False  # 手動リセット
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.datetime"
        ) as mock_dt:
            # 15:31 ET をモック
            mock_dt.now.return_value = _et_time(15, 31)
            mock_dt.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            pos = engine.execute_entry("US.SPY")
        # cutoff h=15,m=30 を過ぎているのでエントリーしない
        # dry_test=False なので cutoff チェックが走る
        assert pos is None


# ===========================================================================
# G. check_exit / should_exit_decision
# ===========================================================================

class TestCheckExit:
    def _make_active_engine(self) -> tuple[IronCondorSellEngine, IronCondorSellPosition]:
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        # entry_done を保ちつつ check_exit をテストするため position だけ戻す
        engine.position = pos
        return engine, pos

    def test_no_position_returns_false(self):
        engine = _make_engine(dry_test=True)
        assert engine.check_exit() is False

    def test_force_close_triggers_at_cutoff(self):
        engine, pos = self._make_active_engine()
        engine.dry_test = False  # タイムストップを有効化
        t = _et_time(15, 46)  # force_close_m=45 を過ぎた
        result = engine.check_exit(now_et=t)
        assert result is True
        assert engine.position is None

    def test_early_close_triggers_earlier(self):
        engine, pos = self._make_active_engine()
        engine.dry_test = False
        t = _et_time(12, 51)  # early_close_m=50 を過ぎた
        result = engine.check_exit(now_et=t, is_early_close=True)
        assert result is True

    def test_kill_switch_triggers_exit(self):
        engine, pos = self._make_active_engine()
        engine.dry_test = False
        t = _et_time(11, 0)
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            result = engine.check_exit(now_et=t)
        assert result is True
        assert engine.position is None

    def test_profit_target_exit(self):
        """PnL が TP 閾値を超えたら利確。"""
        engine, pos = self._make_active_engine()
        engine.dry_test = False
        # net_credit=0.80, profit_target_pct=0.50 → threshold=0.40
        # current_value = net_credit * (1 - decay) で decay < 0.5 は維持
        # 手動で current_value を設定するため _estimate_current_value を mock
        low_value = pos.net_credit * 0.4  # pnl = 0.80 * 0.6 = 0.48 >= 0.40
        t = _et_time(13, 0)
        with patch.object(engine, "_estimate_current_value", return_value=low_value):
            result = engine.check_exit(now_et=t)
        assert result is True

    def test_stop_loss_exit(self):
        """損失が SL 閾値を超えたら損切り。"""
        engine, pos = self._make_active_engine()
        engine.dry_test = False
        # net_credit=0.80, stop_loss_mult=2.0 → threshold=1.60
        # pnl = net_credit - current_value = 0.80 - 2.42 = -1.62 <= -1.60
        high_value = pos.net_credit + pos.net_credit * 2.0 + 0.02
        t = _et_time(13, 0)
        with patch.object(engine, "_estimate_current_value", return_value=high_value):
            result = engine.check_exit(now_et=t)
        assert result is True


class TestShouldExitDecision:
    def _make_pos(self, net_credit: float = 0.80) -> IronCondorSellPosition:
        return IronCondorSellPosition(
            symbol="US.SPY", expiry="2026-04-25", qty=1,
            call_sell_code="C", call_buy_code="C2",
            put_sell_code="P", put_buy_code="P2",
            call_sell_strike=570.0, call_buy_strike=575.0,
            put_sell_strike=550.0, put_buy_strike=545.0,
            call_net_credit=net_credit / 2, put_net_credit=net_credit / 2,
            spread_width=5.0, vix=22.0,
        )

    def test_holding_returns_none_exit(self):
        engine = _make_engine(dry_test=True)
        pos = self._make_pos()
        t = _et_time(11, 0)
        with patch.object(engine, "_estimate_current_value", return_value=pos.net_credit * 0.8):
            d = engine.should_exit_decision(pos, now_et=t)
        assert d.should_exit is False
        assert d.exit_type == "none"

    def test_force_close_decision(self):
        engine = _make_engine(dry_test=True)
        pos = self._make_pos()
        t = _et_time(15, 46)
        d = engine.should_exit_decision(pos, now_et=t)
        assert d.should_exit is True
        assert d.exit_type == "force_close"

    def test_kill_switch_decision(self):
        engine = _make_engine(dry_test=True)
        pos = self._make_pos()
        t = _et_time(11, 0)
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            d = engine.should_exit_decision(pos, now_et=t)
        assert d.should_exit is True
        assert d.exit_type == "kill_switch"


# ===========================================================================
# H. PDT ガード
# ===========================================================================

class TestPDTGuard:
    def test_paper_mode_does_not_raise(self):
        """paper=True では PDTBlockedError を raise しない。"""
        engine = _make_engine(dry_test=True, paper=True)
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        assert isinstance(pos, IronCondorSellPosition)

    def test_pdt_blocked_raises_error(self):
        """PDT ブロック時は PDTBlockedError を raise する。"""
        engine = _make_engine(dry_test=True, paper=False)
        engine.premarket_check("US.SPY")
        # PDTGuard.check_can_trade が blocked を返すよう mock
        mock_result = MagicMock()
        mock_result.allowed = False
        mock_result.reason = "PDT_TEST_BLOCK"
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.PDTGuard.check_can_trade",
            return_value=mock_result,
        ):
            with pytest.raises(PDTBlockedError):
                engine.execute_entry("US.SPY")


# ===========================================================================
# I. 決算近接ブロック
# ===========================================================================

class TestEarningsProximity:
    def test_earnings_block_prevents_entry(self):
        """決算 5 営業日以内は execute_entry が None を返す。"""
        import datetime as dt
        near_date = dt.date.today() + dt.timedelta(days=3)
        engine = IronCondorSellEngine(
            trade_engine=NoOpTradeEngine(),
            earnings_date_fn=lambda sym: near_date,
            paper=True,
            dry_test=True,
        )
        engine.premarket_check("US.NVDA")
        pos = engine.execute_entry("US.NVDA")
        assert pos is None

    def test_far_earnings_allows_entry(self):
        """決算が十分先なら通過する。"""
        import datetime as dt
        far_date = (dt.date.today() + dt.timedelta(days=60))
        engine = IronCondorSellEngine(
            trade_engine=NoOpTradeEngine(),
            earnings_date_fn=lambda sym: far_date,
            paper=True,
            dry_test=True,
        )
        engine.premarket_check("US.SPY")
        pos = engine.execute_entry("US.SPY")
        assert isinstance(pos, IronCondorSellPosition)


# ===========================================================================
# J. Kill Switch 統合
# ===========================================================================

class TestKillSwitchIntegration:
    def test_preflight_blocked_by_kill_switch(self):
        engine = _make_engine()
        env = _make_env()
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            assert engine.preflight(env) is False

    def test_premarket_check_blocked_by_kill_switch(self):
        engine = _make_engine(dry_test=False, vix=22.0)
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            assert engine.premarket_check("US.SPY") is False

    def test_execute_entry_blocked_by_kill_switch(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        with patch(
            "atlas_v3.bots.engines.iron_condor_sell_native.kill_switch_is_active",
            return_value=True,
        ):
            assert engine.execute_entry("US.SPY") is None


# ===========================================================================
# K. should_enter_decision (DTO ベース)
# ===========================================================================

class TestShouldEnterDecision:
    def test_returns_true_in_valid_window(self):
        engine = _make_engine(dry_test=True)
        engine._vix_spike_30 = False
        env = _make_env(vix=22.0, ivr=55.0)
        t = _et_time(11, 0)
        d = engine.should_enter_decision(env, "US.SPY", now_et=t)
        assert d.should_enter is True
        assert d.symbol == "US.SPY"

    def test_returns_false_when_preflight_fails(self):
        engine = _make_engine()
        env = _make_env(vix=42.0)  # vix_max=40 超
        t = _et_time(11, 0)
        d = engine.should_enter_decision(env, "US.SPY", now_et=t)
        assert d.should_enter is False

    def test_returns_false_when_past_cutoff(self):
        engine = _make_engine(dry_test=True)
        env = _make_env(vix=22.0)
        t = _et_time(15, 31)
        d = engine.should_enter_decision(env, "US.SPY", now_et=t)
        assert d.should_enter is False
        assert "cutoff" in d.reason

    def test_idempotency_key_is_generated(self):
        engine = _make_engine(dry_test=True)
        env = _make_env(vix=22.0)
        t = _et_time(11, 0)
        d = engine.should_enter_decision(env, "US.SPY", now_et=t)
        assert d.should_enter is True
        assert len(d.idempotency_key) > 0

    def test_returns_false_when_entry_already_done(self):
        engine = _make_engine(dry_test=True)
        engine.entry_done = True
        env = _make_env(vix=22.0)
        t = _et_time(11, 0)
        d = engine.should_enter_decision(env, "US.SPY", now_et=t)
        assert d.should_enter is False


# ===========================================================================
# L. 内部算出ヘルパー
# ===========================================================================

class TestInternalHelpers:
    def test_calc_dynamic_deltas_high_vix_shrinks(self):
        engine = _make_engine()
        cfg = engine._cfg
        call_d, put_d = engine._calc_dynamic_deltas(
            vix=cfg.vix_high_threshold + 1, ivr_pct=50.0
        )
        assert call_d < cfg.call_delta_base
        assert put_d < cfg.put_delta_base

    def test_calc_dynamic_deltas_high_ivr_expands(self):
        engine = _make_engine()
        cfg = engine._cfg
        call_d, put_d = engine._calc_dynamic_deltas(
            vix=20.0, ivr_pct=75.0
        )
        assert call_d > cfg.call_delta_base

    def test_calc_dynamic_deltas_floor_at_010(self):
        """デルタの下限は 0.10。"""
        cfg = IronCondorSellConfig(call_delta_base=0.12, put_delta_base=0.12)
        engine = _make_engine(config=cfg)
        call_d, put_d = engine._calc_dynamic_deltas(
            vix=cfg.vix_high_threshold + 1, ivr_pct=50.0
        )
        assert call_d >= 0.10
        assert put_d >= 0.10

    def test_calc_dynamic_width_uses_atr(self):
        """ATR 取得可能時は ATR × mult で幅を算出。"""
        engine = _make_engine(atr=10.0)
        width = engine._calc_dynamic_width("US.SPY")
        # 10.0 * 0.50 = 5 → max(1, 5) = 5
        assert width == 5

    def test_calc_dynamic_width_fallback_when_atr_none(self):
        engine = _make_engine()
        engine._eng.get_symbol_atr = lambda symbol, period=14: None
        width = engine._calc_dynamic_width("US.SPY")
        assert width == engine._cfg.width_default

    def test_calc_capital_pct_normal_vix(self):
        engine = _make_engine()
        pct = engine._calc_capital_pct(22.0)
        assert pct == engine._cfg.capital_pct_base

    def test_calc_capital_pct_high_vix(self):
        engine = _make_engine()
        pct = engine._calc_capital_pct(30.0)
        assert pct == engine._cfg.capital_pct_high

    def test_calc_qty_small_account_capped_at_1(self):
        engine = _make_engine()
        qty = engine._calc_qty(
            cash=10_000.0,  # < small_account_usd=15000
            spread_width=5,
            capital_pct=0.40,
        )
        assert qty == 1

    def test_calc_qty_respects_max_qty(self):
        engine = _make_engine(paper=False)
        # cash=100000, spread_width=1, capital_pct=0.40 → raw=400 → clipped by max_qty=3
        qty = engine._calc_qty(cash=100_000.0, spread_width=1, capital_pct=0.40)
        assert qty == engine._cfg.max_qty

    def test_is_active_true_when_position_held(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        engine.execute_entry("US.SPY")
        assert engine.is_active() is True

    def test_is_active_false_after_reset(self):
        engine = _make_engine(dry_test=True)
        engine.premarket_check("US.SPY")
        engine.execute_entry("US.SPY")
        engine.reset_daily()
        assert engine.is_active() is False
