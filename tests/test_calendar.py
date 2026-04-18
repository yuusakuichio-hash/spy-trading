"""
tests/test_calendar.py — CalendarEngine テスト（10テスト以上）

futu未接続のdry_testモードで動作する。外部API依存なし。
"""

import sys
import os
import datetime
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# spy_bot.pyからCalendarEngine関連をimport
import spy_bot as bot
from spy_bot import (
    CalendarEngine,
    CalendarPosition,
    CALENDAR_VIX_MIN,
    CALENDAR_VIX_MAX,
    CALENDAR_MAX_LOSS_PCT,
    CALENDAR_IV_CRUSH_PCT,
    ENABLE_CALENDAR,
    ET,
)


# ── モックオブジェクト ─────────────────────────────────────────────────────────

class MockMarketData:
    """テスト用MarketDataモック。"""
    underlying_code = "US.SPY"
    quote_ctx = None

    def get_vix(self):
        return 22.0

    def get_spy_current(self):
        return 560.0

    def get_option_chain_with_greeks(self, expiry, opt_type, center_strike=None):
        return []

    def find_by_strike(self, chain, price):
        return None

    def get_option_greeks(self, code):
        return {"last": 0.30, "iv": 0.25, "delta": 0.50}

    def calc_ivr(self):
        return 70.0

    def get_ivr_percentiles(self):
        return {"p75": 65.0, "p70": 60.0}


class MockTradeEngine:
    """テスト用TradeEngineモック。"""

    def __init__(self):
        self._virtual_pos = MockVirtualPos()

    def get_account_cash(self):
        return 15000.0

    def _place_single_leg(self, code, side, qty, tag):
        return "order_id_mock", "ok"


class MockVirtualPos:
    def add_position(self, code, qty, price, side):
        pass


# ── CalendarEngineテスト ──────────────────────────────────────────────────────

class TestCalendarEngineInit(unittest.TestCase):
    """CalendarEngineの初期化テスト。"""

    def setUp(self):
        self.mkt = MockMarketData()
        self.eng = MockTradeEngine()

    def test_01_init_default_symbol(self):
        """symbol未指定時はmkt.underlying_codeを使う。"""
        eng = CalendarEngine(self.mkt, self.eng, dry_test=True)
        self.assertEqual(eng.symbol, "US.SPY")

    def test_02_init_custom_symbol(self):
        """symbol引数を指定できる。"""
        eng = CalendarEngine(self.mkt, self.eng, dry_test=True, symbol="US.QQQ")
        self.assertEqual(eng.symbol, "US.QQQ")

    def test_03_excluded_symbols_constant(self):
        """US..SPXがEXCLUDED_SYMBOLSに含まれる。"""
        self.assertIn("US..SPX", CalendarEngine.EXCLUDED_SYMBOLS)

    def test_04_init_state(self):
        """初期状態は全フラグFalse・position=None。"""
        eng = CalendarEngine(self.mkt, self.eng, dry_test=True)
        self.assertIsNone(eng.position)
        self.assertFalse(eng.entry_done)
        self.assertFalse(eng.trade_done)
        self.assertFalse(eng._entry_attempted)


class TestCalendarShouldTradeToday(unittest.TestCase):
    """CalendarEngine.should_trade_today() のロジックテスト。"""

    def test_05_disabled_returns_false(self):
        """ENABLE_CALENDAR=False のときは False。"""
        original = bot.ENABLE_CALENDAR
        try:
            bot.ENABLE_CALENDAR = False
            result = CalendarEngine.should_trade_today(
                vix=25.0, ivr=80.0, ivr_high_threshold=65.0,
                vix_history=[20, 21, 22, 21, 20], paper=False
            )
            self.assertFalse(result)
        finally:
            bot.ENABLE_CALENDAR = original

    def test_06_none_vix_returns_false(self):
        """VIX=Noneのときは False。"""
        result = CalendarEngine.should_trade_today(
            vix=None, ivr=80.0, ivr_high_threshold=65.0,
            vix_history=[], paper=False
        )
        self.assertFalse(result)

    def test_07_vix_below_min_returns_false(self):
        """VIX < CALENDAR_VIX_MIN のときはFalse。"""
        result = CalendarEngine.should_trade_today(
            vix=CALENDAR_VIX_MIN - 1, ivr=80.0, ivr_high_threshold=65.0,
            vix_history=[], paper=False
        )
        self.assertFalse(result)

    def test_08_vix_above_max_returns_false(self):
        """VIX > CALENDAR_VIX_MAX のときはFalse。"""
        result = CalendarEngine.should_trade_today(
            vix=CALENDAR_VIX_MAX + 1, ivr=80.0, ivr_high_threshold=65.0,
            vix_history=[], paper=False
        )
        self.assertFalse(result)

    def test_09_low_ivr_returns_false(self):
        """IVRがivr_high_threshold未満のときはFalse。"""
        result = CalendarEngine.should_trade_today(
            vix=25.0, ivr=50.0, ivr_high_threshold=65.0,
            vix_history=[], paper=False
        )
        self.assertFalse(result)

    def test_10_paper_mode_bypasses_conditions(self):
        """ペーパーモードはVIX/IVR条件をバイパスしてTrue。"""
        result = CalendarEngine.should_trade_today(
            vix=10.0, ivr=20.0, ivr_high_threshold=65.0,
            vix_history=[], paper=True
        )
        self.assertTrue(result)

    def test_11_rising_vix_trend_returns_false(self):
        """VIX上昇トレンドのときはFalse。"""
        result = CalendarEngine.should_trade_today(
            vix=25.0, ivr=80.0, ivr_high_threshold=65.0,
            vix_history=[20.0, 21.0, 22.0, 23.0, 24.0],  # 上昇
            paper=False
        )
        self.assertFalse(result)

    def test_12_falling_vix_trend_returns_true(self):
        """VIX下降トレンドのときはTrue。"""
        result = CalendarEngine.should_trade_today(
            vix=25.0, ivr=80.0, ivr_high_threshold=65.0,
            vix_history=[28.0, 27.0, 26.0, 25.0, 24.0],  # 下降
            paper=False
        )
        self.assertTrue(result)


class TestCalendarExecuteEntry(unittest.TestCase):
    """CalendarEngine.execute_entry() のdry_testテスト。"""

    def setUp(self):
        self.mkt = MockMarketData()
        self.eng = MockTradeEngine()

    def test_13_dry_test_entry_returns_position(self):
        """dry_testモードでエントリーするとCalendarPositionを返す。"""
        engine = CalendarEngine(self.mkt, self.eng, dry_test=True)
        pos = engine.execute_entry(spy_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertIsInstance(pos, CalendarPosition)

    def test_14_excluded_symbol_returns_none(self):
        """US..SPXに対してはNoneを返す（除外ガード）。"""
        engine = CalendarEngine(self.mkt, self.eng, dry_test=True, symbol="US..SPX")
        pos = engine.execute_entry(spy_price=5600.0, vix=22.0)
        self.assertIsNone(pos)

    def test_15_entry_sets_flags(self):
        """エントリー後にentry_done=True、positionがセットされる。"""
        engine = CalendarEngine(self.mkt, self.eng, dry_test=True)
        pos = engine.execute_entry(spy_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        self.assertTrue(engine.entry_done)
        self.assertIsNotNone(engine.position)

    def test_16_reset_daily_clears_state(self):
        """reset_daily()で状態がリセットされる。"""
        engine = CalendarEngine(self.mkt, self.eng, dry_test=True)
        engine.execute_entry(spy_price=560.0, vix=22.0)
        engine.reset_daily()
        self.assertIsNone(engine.position)
        self.assertFalse(engine.entry_done)
        self.assertFalse(engine.trade_done)


class TestCalendarPosition(unittest.TestCase):
    """CalendarPositionデータクラスのテスト。"""

    def test_17_initial_debit_calculation(self):
        """initial_debit = back_entry_price - front_entry_price。"""
        pos = CalendarPosition(
            front_code="DRY_FRONT",
            back_code="DRY_BACK",
            strike=560.0,
            qty=1,
            direction="CALL",
            front_entry_price=0.30,
            back_entry_price=0.60,
            front_iv=0.25,
        )
        self.assertAlmostEqual(pos.initial_debit, 0.30, places=6)
        self.assertFalse(pos.front_closed)

    def test_18_multi_symbol_spy(self):
        """US.SPYシンボルでエントリー。"""
        engine = CalendarEngine(MockMarketData(), MockTradeEngine(),
                                dry_test=True, symbol="US.SPY")
        pos = engine.execute_entry(spy_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)

    def test_19_multi_symbol_qqq(self):
        """US.QQQシンボルでエントリー（マルチ銘柄対応確認）。"""
        mkt = MockMarketData()
        mkt.underlying_code = "US.QQQ"
        engine = CalendarEngine(mkt, MockTradeEngine(), dry_test=True, symbol="US.QQQ")
        pos = engine.execute_entry(spy_price=450.0, vix=22.0)
        self.assertIsNotNone(pos)

    def test_20_check_exit_force_close_drytest(self):
        """dry_testで7分後にforce closeが発動する。"""
        engine = CalendarEngine(MockMarketData(), MockTradeEngine(), dry_test=True)
        pos = engine.execute_entry(spy_price=560.0, vix=22.0)
        self.assertIsNotNone(pos)
        # 起動時刻を10分前に偽装
        engine._dry_test_start = datetime.datetime.now(ET) - datetime.timedelta(minutes=10)
        result = engine.check_exit()
        self.assertIsNotNone(result)
        self.assertIn("reason", result)
        self.assertIn("pnl_usd", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
