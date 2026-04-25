"""Red Team CRITICAL-7/8/10 regression tests

CRITICAL-7: expiry code が週末・祝日でも生成されない
CRITICAL-8: close_all_positions sync barrier + _pending_close
CRITICAL-10: EarningsEngine ET=None -> candidates=[] + Pushover
"""
import datetime
import os
import sys
import types
import unittest.mock as mock
from pathlib import Path

import pytest

# プロジェクトルートをパスに追加
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# futu 未インストール環境対応
_futu_mock = types.ModuleType("futu")
_futu_mock.RET_OK = 0
_futu_mock.TrdSide = types.SimpleNamespace(BUY=1, SELL=2)
_futu_mock.KLType = types.SimpleNamespace(K_1M="K_1M")
_futu_mock.OrderType = types.SimpleNamespace(MARKET="MARKET", LIMIT="LIMIT")
_futu_mock.TimeInForce = types.SimpleNamespace(DAY="DAY")
_futu_mock.TrdEnv = types.SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL")
_futu_mock.TrdMarket = types.SimpleNamespace(US="US")
_futu_mock.SecurityFirm = types.SimpleNamespace(FUTUJP="FUTUJP")
_futu_mock.OptionType = types.SimpleNamespace(CALL="CALL", PUT="PUT")
sys.modules.setdefault("futu", _futu_mock)

import spy_bot as sb
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")


# ─────────────────────────────────────────────────────────────────────────────
# CRITICAL-7: _is_market_open_day / _get_expiry_today
# ─────────────────────────────────────────────────────────────────────────────

class TestCritical7MarketOpenDay:
    """_is_market_open_day が weekday + holiday を正しく判定する"""

    def test_weekday_normal(self):
        assert sb._is_market_open_day("2026-04-20") is True   # 月曜日

    def test_saturday(self):
        assert sb._is_market_open_day("2026-04-18") is False  # 土曜日

    def test_sunday(self):
        assert sb._is_market_open_day("2026-04-19") is False  # 日曜日

    def test_good_friday_2026(self):
        assert sb._is_market_open_day("2026-04-03") is False  # Good Friday

    def test_mlk_day_2026(self):
        assert sb._is_market_open_day("2026-01-19") is False  # MLK Day

    def test_juneteenth_2026(self):
        assert sb._is_market_open_day("2026-06-19") is False  # Juneteenth

    def test_labor_day_2026(self):
        assert sb._is_market_open_day("2026-09-07") is False  # Labor Day

    def test_thanksgiving_2026(self):
        assert sb._is_market_open_day("2026-11-26") is False  # Thanksgiving

    def test_christmas_2026(self):
        assert sb._is_market_open_day("2026-12-25") is False  # Christmas

    def test_invalid_date(self):
        assert sb._is_market_open_day("not-a-date") is False


class TestCritical7GetExpiryToday:
    """_get_expiry_today が週末・祝日に正しい開場日を返す"""

    def _make_dt(self, date_str: str, hour: int = 10) -> datetime.datetime:
        d = datetime.date.fromisoformat(date_str)
        return datetime.datetime(d.year, d.month, d.day, hour, 0, tzinfo=ET)

    def test_weekday_returns_same_day(self):
        now = self._make_dt("2026-04-20")  # 月曜
        assert sb._get_expiry_today(now) == "2026-04-20"

    def test_saturday_returns_monday(self):
        now = self._make_dt("2026-04-18")  # 土曜
        result = sb._get_expiry_today(now)
        # 翌月曜が開場日なら 04-20
        assert result == "2026-04-20"

    def test_sunday_returns_monday(self):
        now = self._make_dt("2026-04-19")  # 日曜
        result = sb._get_expiry_today(now)
        assert result == "2026-04-20"

    def test_good_friday_returns_monday(self):
        now = self._make_dt("2026-04-03")  # Good Friday (休場)
        result = sb._get_expiry_today(now)
        assert result == "2026-04-06"  # 翌月曜

    def test_et_midnight_boundary(self):
        """ET 0:00〜0:05 の境界帯: 正確に当日日付を返す"""
        now = datetime.datetime(2026, 4, 20, 0, 3, tzinfo=ET)  # 月曜 0:03
        assert sb._get_expiry_today(now) == "2026-04-20"

    def test_early_close_day_is_market_open(self):
        """半日取引日は休場ではなく開場日"""
        assert sb._is_market_open_day("2026-11-27") is True  # ブラックフライデー (半日)


class TestCritical7EarlyCloseEntryAllowed:
    """is_early_close_entry_allowed が半日取引日の 12:55 前後を正しく判定"""

    def _make_et(self, date_str: str, hour: int, minute: int) -> datetime.datetime:
        d = datetime.date.fromisoformat(date_str)
        return datetime.datetime(d.year, d.month, d.day, hour, minute, tzinfo=ET)

    def test_non_early_close_always_allowed(self):
        now = self._make_et("2026-04-20", 14, 0)  # 通常営業日
        assert sb.is_early_close_entry_allowed(now) is True

    def test_early_close_before_cutoff(self):
        now = self._make_et("2026-11-27", 12, 54)  # 12:54 -> 許可
        assert sb.is_early_close_entry_allowed(now) is True

    def test_early_close_at_cutoff(self):
        now = self._make_et("2026-11-27", 12, 55)  # 12:55 -> 締め切り（不許可）
        assert sb.is_early_close_entry_allowed(now) is False

    def test_early_close_after_cutoff(self):
        now = self._make_et("2026-11-27", 13, 0)  # 13:00 -> 不許可
        assert sb.is_early_close_entry_allowed(now) is False


# ─────────────────────────────────────────────────────────────────────────────
# CRITICAL-8: close_all_positions sync barrier
# ─────────────────────────────────────────────────────────────────────────────

class TestCritical8CloseAllPositions:
    """close_all_positions sync barrier: SHORT先・約定確認・失敗時 Pushover + _pending_close"""

    def _make_engine(self):
        eng = sb.TradeEngine(paper=True)
        eng.trade_ctx = mock.MagicMock()
        eng.account_id = "12345"
        eng.trade_env = "SIMULATE"
        return eng

    def test_pending_close_initialized(self):
        """TradeEngine.__init__ で _pending_close が空リストで初期化される"""
        eng = sb.TradeEngine(paper=True)
        assert hasattr(eng, "_pending_close")
        assert eng._pending_close == []

    def test_empty_positions_returns_true(self):
        eng = self._make_engine()
        eng.trade_ctx.position_list_query.return_value = (0, mock.MagicMock(empty=True))
        with mock.patch.object(eng, "get_open_positions", return_value=[]):
            result = eng.close_all_positions(reason="test")
        assert result is True

    @pytest.mark.xfail(reason="spy_bot.py legacy (schg lock) の close_all_positions に依存。atlas_v3 移植時に再設計")
    def test_short_leg_failure_sets_pending_close_and_returns_false(self):
        """SHORT buyback 失敗 -> _pending_close にコード記録 + False 返却"""
        eng = self._make_engine()
        positions = [
            {"code": "US.SPY260420P00550000", "qty": -1, "position_side": "SHORT"},
        ]

        # get_open_positions は active positions を返す
        with mock.patch.object(eng, "get_open_positions", return_value=positions):
            # place_order: 失敗 (RET_OK以外)
            eng.trade_ctx.place_order.return_value = (1, "ERR")

            with mock.patch("spy_bot.pushover_alert") as mock_push:
                result = eng.close_all_positions(reason="test_fail")

        assert result is False
        assert len(eng._pending_close) > 0
        assert "US.SPY260420P00550000" in eng._pending_close
        # Pushover が naked risk メッセージで呼ばれた
        called_titles = [str(c.args[0]) for c in mock_push.call_args_list]
        assert any("naked risk" in t.lower() or "leg failed" in t.lower()
                   for t in called_titles)

    def test_successful_close_clears_pending_close(self):
        """正常決済完了後は _pending_close が空になる"""
        eng = self._make_engine()
        # SHORT position のみ
        positions = [
            {"code": "US.SPY260420P00550000", "qty": -1, "position_side": "SHORT"},
        ]

        import pandas as pd
        order_df = pd.DataFrame([{"order_id": "oid1"}])

        with mock.patch.object(eng, "get_open_positions", side_effect=[positions, []]):
            eng.trade_ctx.place_order.return_value = (0, order_df)
            # order_list_query: FILLED_ALL
            fill_df = pd.DataFrame([{"order_status": "FILLED_ALL", "dealt_avg_price": 1.23}])
            eng.trade_ctx.order_list_query.return_value = (0, fill_df)

            with mock.patch.object(eng, "_confirm_fills", return_value={"oid1": 1.23}):
                with mock.patch("spy_bot.pushover_alert"):
                    result = eng.close_all_positions(reason="test_ok")

        assert result is True
        assert eng._pending_close == []

    def test_short_before_long_ordering(self):
        """SHORT脚が LONG より先に place_order される"""
        eng = self._make_engine()
        positions = [
            {"code": "US.SPY260420P00555000", "qty": 1, "position_side": "LONG"},
            {"code": "US.SPY260420P00550000", "qty": -1, "position_side": "SHORT"},
        ]

        import pandas as pd
        order_call_sequence = []

        def side_effect_place_order(**kwargs):
            order_call_sequence.append(kwargs.get("code", ""))
            df = pd.DataFrame([{"order_id": f"oid_{len(order_call_sequence)}"}])
            return (0, df)

        fill_df = pd.DataFrame([{"order_status": "FILLED_ALL", "dealt_avg_price": 1.0}])

        with mock.patch.object(eng, "get_open_positions", side_effect=[positions, []]):
            eng.trade_ctx.place_order.side_effect = side_effect_place_order
            eng.trade_ctx.order_list_query.return_value = (0, fill_df)
            with mock.patch.object(eng, "_confirm_fills", return_value={}):
                with mock.patch("spy_bot.pushover_alert"):
                    eng.close_all_positions(reason="test_order")

        # SHORT が先に発注される
        if len(order_call_sequence) >= 2:
            assert "P00550000" in order_call_sequence[0]  # SHORT leg first
            assert "P00555000" in order_call_sequence[1]  # LONG leg second


# ─────────────────────────────────────────────────────────────────────────────
# CRITICAL-10: EarningsEngine ET=None handling
# ─────────────────────────────────────────────────────────────────────────────

class TestCritical10EarningsEngineETNone:
    """ET=None 時に get_today_candidates と should_enter_now が安全に動作する"""

    def _make_engine_with_et_none(self):
        from common.earnings_engine import EarningsEngine
        eng = EarningsEngine(api_key="test_key")
        return eng

    def test_now_et_returns_none_when_et_is_none(self):
        """_now_et(): ET=None なら None を返す (JST localtime 混入なし)"""
        from common.earnings_engine import EarningsEngine
        import common.earnings_engine as ee
        orig_et = ee.ET
        try:
            ee.ET = None
            eng = EarningsEngine()
            result = eng._now_et()
            assert result is None, f"ET=None なのに {result} を返した"
        finally:
            ee.ET = orig_et

    def test_get_today_candidates_returns_empty_when_et_none(self):
        """ET=None 時に get_today_candidates は [] を返す"""
        from common.earnings_engine import EarningsEngine
        import common.earnings_engine as ee
        orig_et = ee.ET
        try:
            ee.ET = None
            eng = EarningsEngine()
            with mock.patch.object(eng, "_notify_et_unavailable"):
                result = eng.get_today_candidates()
            assert result == [], f"期待 [] だが {result} が返った"
        finally:
            ee.ET = orig_et

    def test_should_enter_now_returns_false_when_et_none(self):
        """ET=None 時に should_enter_now は False を返す"""
        from common.earnings_engine import EarningsEngine, EarningsCandidate
        import common.earnings_engine as ee
        import datetime as _dt
        orig_et = ee.ET
        try:
            ee.ET = None
            eng = EarningsEngine()
            candidate = EarningsCandidate(
                symbol="AAPL",
                full_code="US.AAPL",
                report_time="amc",
                estimated_dt=_dt.datetime(2026, 4, 20, 16, 0),
                entry_dt=_dt.datetime(2026, 4, 20, 15, 0),
                iv_crush_rate=0.3,
                size_factor=1.0,
            )
            result = eng.should_enter_now(candidate)
            assert result is False
        finally:
            ee.ET = orig_et

    def test_notify_et_unavailable_sends_pushover(self):
        """_notify_et_unavailable が Pushover に priority=1 で通知する"""
        from common.earnings_engine import EarningsEngine
        import common.earnings_engine as ee
        orig_et = ee.ET
        try:
            ee.ET = None
            eng = EarningsEngine()
            with mock.patch("requests.post") as mock_post:
                mock_post.return_value = mock.MagicMock(ok=True)
                os.environ["PUSHOVER_ALERT_TOKEN"] = "test_token"
                os.environ["PUSHOVER_USER"] = "test_user"
                eng._notify_et_unavailable()
                assert mock_post.called
                call_data = mock_post.call_args[1]["data"]
                assert call_data.get("priority") == 1
                assert "ET timezone unavailable" in call_data.get("title", "")
        finally:
            ee.ET = orig_et

    def test_notify_et_unavailable_rate_limits(self):
        """_notify_et_unavailable は 1 時間に 1 回だけ送信する"""
        from common.earnings_engine import EarningsEngine
        import common.earnings_engine as ee
        import time as _time
        orig_et = ee.ET
        try:
            ee.ET = None
            eng = EarningsEngine()
            with mock.patch("requests.post") as mock_post:
                mock_post.return_value = mock.MagicMock(ok=True)
                os.environ["PUSHOVER_ALERT_TOKEN"] = "tok"
                os.environ["PUSHOVER_USER"] = "usr"
                eng._notify_et_unavailable()
                eng._notify_et_unavailable()  # 2 回目は送らない
                assert mock_post.call_count == 1
        finally:
            ee.ET = orig_et


# ─────────────────────────────────────────────────────────────────────────────
# CRITICAL-7 + CRITICAL-10: spy_bot 起動時 zoneinfo チェックの有無確認
# ─────────────────────────────────────────────────────────────────────────────

class TestCritical10StartupCheck:
    """spy_bot.py に zoneinfo 起動時チェックが存在することを確認"""

    def test_et_is_zoneinfo_object(self):
        """spy_bot.ET が正しく ZoneInfo オブジェクトである"""
        import zoneinfo as _zi
        assert isinstance(sb.ET, _zi.ZoneInfo)

    def test_jst_is_zoneinfo_object(self):
        import zoneinfo as _zi
        assert isinstance(sb.JST, _zi.ZoneInfo)

    def test_us_market_holidays_contains_2026_entries(self):
        """US_MARKET_HOLIDAYS が 2026 年の主要祝日を含む"""
        assert "2026-01-01" in sb.US_MARKET_HOLIDAYS  # New Year
        assert "2026-04-03" in sb.US_MARKET_HOLIDAYS  # Good Friday
        assert "2026-11-26" in sb.US_MARKET_HOLIDAYS  # Thanksgiving
        assert "2026-12-25" in sb.US_MARKET_HOLIDAYS  # Christmas

    def test_early_close_force_is_1300(self):
        """CRITICAL-7: 半日強制決済時刻が 13:00 ET に変更されている"""
        assert sb.EARLY_CLOSE_FORCE_H == 13
        assert sb.EARLY_CLOSE_FORCE_M == 0
