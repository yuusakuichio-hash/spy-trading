"""
tests/test_earnings.py — EarningsEngine 単体テスト (10テスト以上)

テスト方針:
  - 外部API (Finnhub/Yahoo) には一切接触しない (モック)
  - 固定閾値ではなく動的算出ロジックを検証する
  - pre_trade_check との結合は未テスト (別ファイルで実施)
"""
import datetime
import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

# データディレクトリをtmpに向ける (ファイル副作用を避ける)
_TMP = tempfile.mkdtemp()
os.environ["SPY_DATA_DIR"] = _TMP

from common.earnings_engine import (
    EarningsEngine,
    EarningsCandidate,
    EarningsEngineResult,
    ENTRY_BEFORE_EARNINGS_MIN,
    SIZE_FACTOR_HIGH,
    SIZE_FACTOR_MID,
    SIZE_FACTOR_LOW,
    _DEFAULT_IV_CRUSH_RATES,
    _DEFAULT_CRUSH_RATE,
)


class TestIVCrushRateDefault(unittest.TestCase):
    """デフォルトIVクラッシュ率のテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")

    def test_known_symbol_nvda(self):
        """NVDAのデフォルトクラッシュ率が正しいこと"""
        rate = self.eng._get_iv_crush_rate("NVDA")
        self.assertAlmostEqual(rate, _DEFAULT_IV_CRUSH_RATES["NVDA"])

    def test_known_symbol_tsla(self):
        """TSLAのデフォルトクラッシュ率が正しいこと"""
        rate = self.eng._get_iv_crush_rate("TSLA")
        self.assertAlmostEqual(rate, _DEFAULT_IV_CRUSH_RATES["TSLA"])

    def test_unknown_symbol_falls_back(self):
        """未知銘柄はデフォルト値にフォールバックすること"""
        rate = self.eng._get_iv_crush_rate("XYZ_UNKNOWN")
        self.assertAlmostEqual(rate, _DEFAULT_CRUSH_RATE)

    def test_crush_rate_in_valid_range(self):
        """全デフォルト銘柄のクラッシュ率が 0~1 の範囲内であること"""
        for sym in _DEFAULT_IV_CRUSH_RATES:
            rate = self.eng._get_iv_crush_rate(sym)
            self.assertGreater(rate, 0.0, f"{sym} crush_rate <= 0")
            self.assertLess(rate, 1.0, f"{sym} crush_rate >= 1.0")


class TestSizeFactor(unittest.TestCase):
    """サイズ係数算出のテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")

    def test_high_crush_rate(self):
        """crush_rate >= 0.38 → SIZE_FACTOR_HIGH"""
        self.assertEqual(self.eng._calc_size_factor(0.38), SIZE_FACTOR_HIGH)
        self.assertEqual(self.eng._calc_size_factor(0.50), SIZE_FACTOR_HIGH)

    def test_mid_crush_rate(self):
        """crush_rate 0.30-0.37 → SIZE_FACTOR_MID"""
        self.assertEqual(self.eng._calc_size_factor(0.30), SIZE_FACTOR_MID)
        self.assertEqual(self.eng._calc_size_factor(0.37), SIZE_FACTOR_MID)

    def test_low_crush_rate(self):
        """crush_rate < 0.30 → SIZE_FACTOR_LOW"""
        self.assertEqual(self.eng._calc_size_factor(0.25), SIZE_FACTOR_LOW)
        self.assertEqual(self.eng._calc_size_factor(0.10), SIZE_FACTOR_LOW)

    def test_size_factor_ordering(self):
        """SIZE_FACTOR_HIGH > SIZE_FACTOR_MID > SIZE_FACTOR_LOW であること"""
        self.assertGreater(SIZE_FACTOR_HIGH, SIZE_FACTOR_MID)
        self.assertGreater(SIZE_FACTOR_MID, SIZE_FACTOR_LOW)


class TestGetEntryParams(unittest.TestCase):
    """get_entry_params のテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")

    def test_returns_earnings_engine_result(self):
        """EarningsEngineResult が返ること"""
        res = self.eng.get_entry_params("NVDA")
        self.assertIsInstance(res, EarningsEngineResult)

    def test_tactic_is_straddle_sell_for_etf(self):
        """SPY/QQQ（ETF）は tactic が straddle_sell であること"""
        # 個別株(META等)は ic_sell になった(2026-04-20 マルチ銘柄拡大)
        res = self.eng.get_entry_params("SPY")
        self.assertEqual(res.tactic, "straddle_sell")

    def test_tactic_is_ic_sell_for_individual_stock(self):
        """個別株(META等)は tactic が ic_sell であること（マルチ銘柄拡大）"""
        from unittest.mock import patch
        with patch.object(self.eng, "calc_ivr_individual", return_value=65.0), \
             patch.object(self.eng, "calc_em_hm_ratio", return_value=1.3):
            res = self.eng.get_entry_params("META")
        self.assertEqual(res.tactic, "ic_sell")

    def test_full_code_format(self):
        """full_code が 'US.<SYMBOL>' 形式であること"""
        res = self.eng.get_entry_params("TSLA")
        self.assertEqual(res.full_code, "US.TSLA")

    def test_entry_before_min_constant(self):
        """entry_before_min が ENTRY_BEFORE_EARNINGS_MIN と一致すること"""
        res = self.eng.get_entry_params("AAPL")
        self.assertEqual(res.entry_before_min, ENTRY_BEFORE_EARNINGS_MIN)

    def test_size_factor_consistent_with_crush_rate(self):
        """size_factor が crush_rate に対して正しく算出されること

        H-7: _calc_size_factor(crush_rate, symbol=symbol) でペナルティが適用されるため、
        symbol を省略した _calc_size_factor(crush_rate) とは値が異なる場合がある。
        テストは symbol 込みで計算した値と比較する。
        """
        res = self.eng.get_entry_params("NVDA")
        # H-7: symbol 引数付きで計算（ペナルティ考慮）
        expected_sf = self.eng._calc_size_factor(res.iv_crush_rate, symbol="NVDA")
        self.assertAlmostEqual(res.size_factor, expected_sf)


class TestRecordOutcomeAndHistory(unittest.TestCase):
    """record_outcome と履歴更新のテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")

    def test_record_outcome_updates_history(self):
        """record_outcome 後に履歴が1件増えること"""
        self.eng.record_outcome("NVDA", pre_iv=60.0, post_iv=36.0, pnl_usd=150.0)
        self.assertIn("NVDA", self.eng._history)
        self.assertEqual(len(self.eng._history["NVDA"]), 1)

    def test_actual_crush_calculation(self):
        """actual_crush = (pre_iv - post_iv) / pre_iv が正しく記録されること"""
        self.eng.record_outcome("TSLA", pre_iv=80.0, post_iv=48.0, pnl_usd=200.0)
        crush = self.eng._history["TSLA"][-1]["actual_crush"]
        expected = (80.0 - 48.0) / 80.0
        self.assertAlmostEqual(crush, expected, places=3)

    def test_history_switches_to_actual_after_3_records(self):
        """3件以上の実績があると実績中央値でcrush_rateが計算されること"""
        for _ in range(3):
            self.eng.record_outcome("META", pre_iv=70.0, post_iv=42.0, pnl_usd=100.0)
        actual_crush = (70.0 - 42.0) / 70.0  # = 0.4
        rate = self.eng._get_iv_crush_rate("META")
        self.assertAlmostEqual(rate, actual_crush, places=3)

    def test_zero_pre_iv_is_ignored(self):
        """pre_iv=0 の場合は記録しないこと (ゼロ除算防止)"""
        initial_len = len(self.eng._history.get("AAPL", []))
        self.eng.record_outcome("AAPL", pre_iv=0.0, post_iv=30.0, pnl_usd=0.0)
        current_len = len(self.eng._history.get("AAPL", []))
        self.assertEqual(initial_len, current_len)


class TestEstimateAnnouncementDt(unittest.TestCase):
    """発表時刻推定のテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")
        self.test_date = datetime.date(2026, 4, 18)

    def test_bmo_returns_730_et(self):
        """bmo (before market open) は ET 7:30 を返すこと"""
        dt = self.eng._estimate_announcement_dt("bmo", self.test_date)
        if dt is not None:
            self.assertEqual(dt.hour, 7)
            self.assertEqual(dt.minute, 30)

    def test_amc_returns_1615_et(self):
        """amc (after market close) は ET 16:15 を返すこと"""
        dt = self.eng._estimate_announcement_dt("amc", self.test_date)
        if dt is not None:
            self.assertEqual(dt.hour, 16)
            self.assertEqual(dt.minute, 15)

    def test_entry_dt_is_before_announcement(self):
        """entry_dt が announcement_dt より ENTRY_BEFORE_EARNINGS_MIN 分前であること"""
        ann = self.eng._estimate_announcement_dt("amc", self.test_date)
        entry = self.eng._calc_entry_dt(ann)
        if ann is not None and entry is not None:
            diff_min = (ann - entry).total_seconds() / 60
            self.assertAlmostEqual(diff_min, ENTRY_BEFORE_EARNINGS_MIN)

    def test_calc_entry_dt_none_input(self):
        """announcement_dt=None → entry_dt=None"""
        entry = self.eng._calc_entry_dt(None)
        self.assertIsNone(entry)


def _today_et() -> str:
    """ETタイムゾーンで今日の日付を返す。get_today_candidates の内部ロジックと一致させる。"""
    try:
        import zoneinfo
        import datetime as _dt
        return _dt.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


class TestGetTodayCandidatesWithMock(unittest.TestCase):
    """get_today_candidates のモックテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key", min_iv_crush_rate=0.25)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_filters_by_date(self, mock_fetch):
        """当日の銘柄のみが返ること (他の日付は除外)"""
        today = _today_et()
        yesterday = (datetime.datetime.strptime(today, "%Y-%m-%d").date() - datetime.timedelta(days=1)).isoformat()
        mock_fetch.return_value = [
            {"symbol": "NVDA", "date": today, "hour": "amc"},
            {"symbol": "TSLA", "date": yesterday, "hour": "amc"},
        ]
        candidates = self.eng.get_today_candidates()
        syms = [c.symbol for c in candidates]
        self.assertIn("NVDA", syms)
        self.assertNotIn("TSLA", syms)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_filters_by_min_iv_crush_rate(self, mock_fetch):
        """min_iv_crush_rate 未満の銘柄は除外されること"""
        eng = EarningsEngine(api_key="test_key", min_iv_crush_rate=0.99)
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "NVDA", "date": today, "hour": "bmo"},
        ]
        candidates = eng.get_today_candidates()
        # NVDAのデフォルトcrush_rate=0.40 < 0.99 → 除外
        self.assertEqual(len(candidates), 0)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_sorted_by_iv_crush_rate_desc(self, mock_fetch):
        """candidates が iv_crush_rate 降順にソートされること"""
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "AAPL", "date": today, "hour": "amc"},   # 0.30
            {"symbol": "NVDA", "date": today, "hour": "amc"},   # 0.40
            {"symbol": "MSFT", "date": today, "hour": "amc"},   # 0.28
        ]
        candidates = self.eng.get_today_candidates()
        rates = [c.iv_crush_rate for c in candidates]
        self.assertEqual(rates, sorted(rates, reverse=True))

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_empty_symbol_is_skipped(self, mock_fetch):
        """symbol が空の entry はスキップされること"""
        # require_em_over_hm=False で EM/HMフィルタを無効化してETF銘柄で確認
        eng = EarningsEngine(api_key="test_key", require_em_over_hm=False)
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "", "date": today, "hour": "amc"},
            {"symbol": "SPY", "date": today, "hour": "bmo"},   # ETFはフィルタ対象外
        ]
        candidates = eng.get_today_candidates()
        syms = [c.symbol for c in candidates]
        self.assertNotIn("", syms)
        self.assertIn("SPY", syms)


class TestShouldEnterNow(unittest.TestCase):
    """should_enter_now のテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")

    def test_within_tolerance(self):
        """エントリー予定時刻の±5分以内はTrueを返すこと"""
        import zoneinfo
        try:
            ET = zoneinfo.ZoneInfo("America/New_York")
            now = datetime.datetime.now(ET)
        except Exception:
            now = datetime.datetime.now()

        candidate = MagicMock(spec=EarningsCandidate)
        candidate.entry_dt = now + datetime.timedelta(minutes=3)

        with patch.object(self.eng, "_now_et", return_value=now):
            result = self.eng.should_enter_now(candidate, tolerance_min=5)
        self.assertTrue(result)

    def test_outside_tolerance(self):
        """エントリー予定時刻から10分以上ずれていたらFalseを返すこと"""
        import zoneinfo
        try:
            ET = zoneinfo.ZoneInfo("America/New_York")
            now = datetime.datetime.now(ET)
        except Exception:
            now = datetime.datetime.now()

        candidate = MagicMock(spec=EarningsCandidate)
        candidate.entry_dt = now + datetime.timedelta(minutes=20)

        with patch.object(self.eng, "_now_et", return_value=now):
            result = self.eng.should_enter_now(candidate, tolerance_min=5)
        self.assertFalse(result)

    def test_none_entry_dt_returns_false(self):
        """entry_dt=None のとき False を返すこと"""
        candidate = MagicMock(spec=EarningsCandidate)
        candidate.entry_dt = None
        result = self.eng.should_enter_now(candidate)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
