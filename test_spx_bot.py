#!/usr/bin/env python3
"""
Unit tests for spx_bot.py
Tests: SMA calc, position sizing, no-trade判定, 時刻境界, 満期日, 異常入力
"""

import sys
import os
import datetime
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

# Allow import without futu installed
sys.modules.setdefault("futu", MagicMock())

# Use temp dir for logs so tests run without root
import tempfile
os.environ["SPX_LOG_DIR"] = tempfile.mkdtemp()

# Patch zoneinfo before import so tests run without TZ issues
import zoneinfo  # noqa: E402

# ── Import target module ───────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
import spx_bot as bot


# ── Helpers ────────────────────────────────────────────────────────────────────
ET = zoneinfo.ZoneInfo("America/New_York")


def make_et(year, month, day, hour=10, minute=30) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=ET)


# ═════════════════════════════════════════════════════════════════════════════
class TestSMA(unittest.TestCase):
    """SMA / get_sma20 dry-run value is 558.0"""

    def test_dry_run_sma_is_float(self):
        mkt = bot.MarketData()
        val = mkt.get_sma20()
        self.assertIsInstance(val, float)
        self.assertGreater(val, 0)

    def test_direction_bull_when_spy_above_sma(self):
        """SPY > SMA → bull_put"""
        spy, sma = 560.0, 558.0
        direction = "bull_put" if spy > sma else "bear_call"
        self.assertEqual(direction, "bull_put")

    def test_direction_bear_when_spy_below_sma(self):
        """SPY < SMA → bear_call"""
        spy, sma = 555.0, 558.0
        direction = "bull_put" if spy > sma else "bear_call"
        self.assertEqual(direction, "bear_call")

    def test_direction_bear_when_spy_equals_sma(self):
        """SPY == SMA → bear_call (not strictly greater)"""
        spy, sma = 558.0, 558.0
        direction = "bull_put" if spy > sma else "bear_call"
        self.assertEqual(direction, "bear_call")


# ═════════════════════════════════════════════════════════════════════════════
class TestPositionSizing(unittest.TestCase):
    """calc_position_size: base/VIX spike/seasonal rules"""

    def _engine(self):
        eng = bot.TradeEngine.__new__(bot.TradeEngine)
        eng.trade_env = None
        eng.trade_ctx = None
        eng.account_id = ""
        eng.unlock_ok = False
        return eng

    def test_base_ratio_20pct(self):
        """Normal day, VIX=15: ratio=20%, $2500 capital → min 1"""
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 11, 10, 30)  # Wed
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            eng = self._engine()
            result = eng.calc_position_size(2500.0, 15.0, 14.0)
        # 2500 * 0.20 / 500 = 1 contract
        self.assertEqual(result, 1)

    def test_vix_spike_doubles_ratio(self):
        """VIX spike +20%: ratio becomes 40%"""
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 11, 10, 30)
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            eng = self._engine()
            # VIX 14 → 17 (>= 14*1.20=16.8)
            result = eng.calc_position_size(5000.0, 17.0, 14.0)
        # 5000 * 0.40 / 500 = 4 contracts
        self.assertEqual(result, 4)

    def test_seasonal_sep_oct_halves(self):
        """Sep/Oct: ratio × 0.5"""
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 9, 16, 10, 30)  # Sep, Wed
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            eng = self._engine()
            result = eng.calc_position_size(10000.0, 15.0, 14.0)
        # 10000 * (0.20 * 0.5) / 500 = 2 contracts
        self.assertEqual(result, 2)

    def test_minimum_one_contract(self):
        """Even with tiny capital, returns at least 1."""
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 11, 10, 30)
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            eng = self._engine()
            result = eng.calc_position_size(100.0, 15.0, 14.0)
        self.assertEqual(result, 1)

    def test_abnormal_zero_capital(self):
        """Zero capital still returns 1 (max(1, 0) = 1)."""
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 11, 10, 30)
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            eng = self._engine()
            result = eng.calc_position_size(0.0, 15.0, 14.0)
        self.assertEqual(result, 1)

    def test_abnormal_negative_capital(self):
        """Negative capital treated like zero → 1 contract."""
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 11, 10, 30)
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            eng = self._engine()
            result = eng.calc_position_size(-999.0, 15.0, 14.0)
        self.assertGreaterEqual(result, 1)


# ═════════════════════════════════════════════════════════════════════════════
class TestNoTrade(unittest.TestCase):
    """is_notrade_today: holiday, OpEx, VIX gate"""

    def test_no_trade_on_holiday_eve(self):
        """Day before MLK Day 2026 (Jan 18 Sun → skip; check Jan 16 Fri → Jan 19 holiday)"""
        # Jan 19, 2026 = MLK Day → Jan 18 (Sun) is before it, but we test Jan 16 (Fri)
        with patch("spx_bot.datetime") as mock_dt:
            # Today = Jan 16, 2026 (Fri); tomorrow = Jan 17 (Sat) - not in holidays
            # Let's use Jan 18 (Sun ET) → tomorrow = Jan 19 (MLK Day)
            mock_dt.datetime.now.return_value = make_et(2026, 1, 18, 10, 30)
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            result = bot.is_notrade_today()
        self.assertTrue(result)

    def test_trade_on_normal_day(self):
        """Normal Tuesday in March 2026 → no restriction"""
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 10, 10, 30)  # Tue
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            with patch("spx_bot.EVENTS_FILE", Path("/nonexistent_events.json")):
                result = bot.is_notrade_today()
        self.assertFalse(result)

    def test_no_trade_on_quarterly_opex(self):
        """3rd Friday of June 2026 = June 19 → OpEx → no trade"""
        # June 2026: 1st=Mon, 1st Fri=5th, 3rd Fri=19th
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 6, 19, 10, 30)
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            with patch("spx_bot.EVENTS_FILE", Path("/nonexistent_events.json")):
                result = bot.is_notrade_today()
        self.assertTrue(result)

    def test_vix_gate_no_trade(self):
        """VIX >= 25 → entry method marks no-trade (tested via run_entry path)"""
        b = bot.SPXBot.__new__(bot.SPXBot)
        b.traded_times = {}
        b.mkt = MagicMock()
        b.mkt.get_spy_price.return_value = 560.0
        b.mkt.get_sma20.return_value = 558.0
        b.mkt.get_vix.return_value = 26.0
        b.mkt.get_vix_prev_close.return_value = 20.0
        b.eng = MagicMock()
        b.eng.get_account_cash.return_value = 2500.0

        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 11, 10, 30)
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            b.run_entry()

        # place_spread should NOT have been called
        b.eng.place_spread.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
class TestTimeBoundary(unittest.TestCase):
    """should_enter: entry at exact times only, no duplicate"""

    def _bot(self):
        b = bot.SPXBot.__new__(bot.SPXBot)
        b.traded_times = {}
        return b

    def test_enters_at_10_30(self):
        b = self._bot()
        self.assertTrue(b.should_enter(10, 30))

    def test_enters_at_14_00(self):
        b = self._bot()
        self.assertTrue(b.should_enter(14, 0))

    def test_no_entry_at_10_31(self):
        b = self._bot()
        self.assertFalse(b.should_enter(10, 31))

    def test_no_entry_at_13_59(self):
        b = self._bot()
        self.assertFalse(b.should_enter(13, 59))

    def test_no_duplicate_entry(self):
        b = self._bot()
        b.traded_times["10:30"] = True
        self.assertFalse(b.should_enter(10, 30))

    def test_no_entry_at_market_close(self):
        b = self._bot()
        self.assertFalse(b.should_enter(16, 0))


# ═════════════════════════════════════════════════════════════════════════════
class TestExpiry(unittest.TestCase):
    """get_expiry: 0DTE on Mon/Wed/Fri, 1DTE on Tue/Thu"""

    def _bot_with_time(self, dt: datetime.datetime):
        b = bot.SPXBot.__new__(bot.SPXBot)
        b.traded_times = {}
        return b, dt

    def test_0dte_monday(self):
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 9, 10, 30)  # Mon
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            b = bot.SPXBot.__new__(bot.SPXBot)
            result = b.get_expiry()
        self.assertEqual(result, "2026-03-09")

    def test_0dte_wednesday(self):
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 11, 10, 30)  # Wed
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            b = bot.SPXBot.__new__(bot.SPXBot)
            result = b.get_expiry()
        self.assertEqual(result, "2026-03-11")

    def test_0dte_friday(self):
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 13, 10, 30)  # Fri
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            b = bot.SPXBot.__new__(bot.SPXBot)
            result = b.get_expiry()
        self.assertEqual(result, "2026-03-13")

    def test_1dte_tuesday(self):
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 10, 10, 30)  # Tue
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            b = bot.SPXBot.__new__(bot.SPXBot)
            result = b.get_expiry()
        self.assertEqual(result, "2026-03-11")

    def test_1dte_thursday(self):
        with patch("spx_bot.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = make_et(2026, 3, 12, 10, 30)  # Thu
            mock_dt.date = datetime.date
            mock_dt.timedelta = datetime.timedelta
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            b = bot.SPXBot.__new__(bot.SPXBot)
            result = b.get_expiry()
        self.assertEqual(result, "2026-03-13")

    def test_holiday_in_holidays_set(self):
        """Jan 1, 2026 must be in US_HOLIDAYS."""
        self.assertIn(datetime.date(2026, 1, 1), bot.US_HOLIDAYS)

    def test_good_friday_2026_in_holidays(self):
        """Good Friday Apr 3, 2026 must be in US_HOLIDAYS."""
        self.assertIn(datetime.date(2026, 4, 3), bot.US_HOLIDAYS)

    def test_normal_date_not_in_holidays(self):
        """Regular Wednesday is not a holiday."""
        self.assertNotIn(datetime.date(2026, 3, 11), bot.US_HOLIDAYS)


# ═════════════════════════════════════════════════════════════════════════════
class TestAbnormalInput(unittest.TestCase):
    """Abnormal input handling"""

    def test_pushover_handles_network_error(self):
        """pushover() returns False on network error without raising."""
        with patch("spx_bot.requests.post", side_effect=ConnectionError("no net")):
            result = bot.pushover("title", "msg")
        self.assertFalse(result)

    def test_load_failures_returns_zero_on_missing_file(self):
        with patch("spx_bot.FAILURES_FILE", Path("/nonexistent/failures.json")):
            result = bot.load_failures()
        self.assertEqual(result, 0)

    def test_load_failures_returns_zero_on_stale(self):
        """Counter resets if last failure was >24h ago."""
        import json, tempfile
        stale_data = {
            "count": 5,
            "last": "2000-01-01T00:00:00"
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(stale_data, f)
            fname = f.name
        with patch("spx_bot.FAILURES_FILE", Path(fname)):
            result = bot.load_failures()
        os.unlink(fname)
        self.assertEqual(result, 0)

    def test_find_strike_by_delta_empty_chain(self):
        """Empty option chain returns None."""
        mkt = bot.MarketData()
        result = mkt.find_strike_by_delta([], 0.20, "sell")
        self.assertIsNone(result)

    def test_append_monthly_csv_creates_file(self):
        """append_monthly_csv writes a file without exception."""
        import tempfile
        tmpdir = Path(tempfile.mkdtemp())
        with patch("spx_bot.MONTHLY_CSV_DIR", tmpdir):
            with patch("spx_bot.ET", ET):
                bot.append_monthly_csv({
                    "direction": "bull_put",
                    "sell_strike": 550.0,
                    "buy_strike": 545.0,
                    "qty": 1,
                    "net_credit": 1.50,
                    "result": "entered",
                })
        csv_files = list(tmpdir.glob("*.csv"))
        self.assertEqual(len(csv_files), 1)
        content = csv_files[0].read_text()
        self.assertIn("bull_put", content)


if __name__ == "__main__":
    verbosity = 2
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [TestSMA, TestPositionSizing, TestNoTrade, TestTimeBoundary,
                TestExpiry, TestAbnormalInput]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
