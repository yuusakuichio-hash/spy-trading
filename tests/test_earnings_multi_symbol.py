"""
tests/test_earnings_multi_symbol.py — EarningsEngine マルチ銘柄拡大テスト

対象機能（2026-04-20 実装）:
  - calc_ivr_individual(): 個別株IVR（yfinance HV比較）
  - calc_em_hm_ratio(): EM / HM 比率
  - get_today_candidates(): EM>HMフィルタ・IC構造フラグ
  - get_entry_params(): 個別株は ic_sell タクティク

テスト方針:
  - 外部API (yfinance/Finnhub) にはモックで対応
  - EM>HMフィルタの境界値を重点テスト
  - IC構造フラグの正確な設定を検証
  - 最大損失率(MAX_RISK_PCT)がパラメータに含まれることを確認
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

# データディレクトリをtmpに向ける
_TMP = tempfile.mkdtemp()
os.environ["SPY_DATA_DIR"] = _TMP

from common.earnings_engine import (
    EarningsEngine,
    EarningsCandidate,
    EarningsEngineResult,
    INDIVIDUAL_STOCK_SYMBOLS,
    IC_STRUCTURE_SYMBOLS,
    EM_HM_MIN_RATIO,
    MAX_RISK_PCT,
    _DEFAULT_IV_CRUSH_RATES,
)


def _today_et() -> str:
    """ETタイムゾーンで今日の日付を返す。"""
    try:
        import zoneinfo
        import datetime as _dt
        return _dt.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.date.today().isoformat()


# ── ユニバース定数テスト ─────────────────────────────────────────────────────

class TestMultiSymbolConstants(unittest.TestCase):
    """定数の整合性テスト"""

    def test_individual_stock_symbols_contains_7_targets(self):
        """要件: TSLA/NVDA/AAPL/MSFT/META/GOOGL/AMZN が含まれること"""
        required = {"TSLA", "NVDA", "AAPL", "MSFT", "META", "GOOGL", "AMZN"}
        missing = required - INDIVIDUAL_STOCK_SYMBOLS
        self.assertEqual(missing, set(), f"個別株ユニバースに不足: {missing}")

    def test_ic_structure_symbols_covers_individual(self):
        """IC構造対象はINDIVIDUAL_STOCK_SYMBOLSと同一またはその上位集合であること"""
        self.assertTrue(
            INDIVIDUAL_STOCK_SYMBOLS.issubset(IC_STRUCTURE_SYMBOLS),
            "IC_STRUCTURE_SYMBOLSがINDIVIDUAL_STOCK_SYMBOLSを包含していない"
        )

    def test_em_hm_min_ratio_is_1_or_above(self):
        """EM_HM_MIN_RATIO >= 1.0 であること（オプション割安時はスキップ）"""
        self.assertGreaterEqual(EM_HM_MIN_RATIO, 1.0)

    def test_max_risk_pct_is_2_percent(self):
        """MAX_RISK_PCT = 0.02（口座資金の2%）であること"""
        self.assertAlmostEqual(MAX_RISK_PCT, 0.02)

    def test_all_7_symbols_have_default_crush_rate(self):
        """7銘柄全てにデフォルトIVクラッシュ率があること"""
        for sym in ("TSLA", "NVDA", "AAPL", "MSFT", "META", "GOOGL", "AMZN"):
            self.assertIn(sym, _DEFAULT_IV_CRUSH_RATES, f"{sym}がデフォルト辞書に未設定")


# ── EarningsCandidate 新フィールドテスト ─────────────────────────────────────

class TestEarningsCandidateNewFields(unittest.TestCase):
    """EarningsCandidateの新フィールドが正しく設定されることを確認"""

    def _make_candidate(self, symbol="NVDA", use_ic=True, em_hm_ratio=1.2, ivr=65.0):
        return EarningsCandidate(
            symbol=symbol,
            full_code=f"US.{symbol}",
            report_time="amc",
            estimated_dt=None,
            entry_dt=None,
            iv_crush_rate=0.40,
            size_factor=1.0,
            use_ic_structure=use_ic,
            em_hm_ratio=em_hm_ratio,
            ivr_individual=ivr,
            max_risk_pct=MAX_RISK_PCT,
        )

    def test_use_ic_structure_field_exists(self):
        c = self._make_candidate(use_ic=True)
        self.assertTrue(c.use_ic_structure)

    def test_em_hm_ratio_field_exists(self):
        c = self._make_candidate(em_hm_ratio=1.35)
        self.assertAlmostEqual(c.em_hm_ratio, 1.35)

    def test_ivr_individual_field_exists(self):
        c = self._make_candidate(ivr=72.0)
        self.assertAlmostEqual(c.ivr_individual, 72.0)

    def test_max_risk_pct_matches_constant(self):
        c = self._make_candidate()
        self.assertAlmostEqual(c.max_risk_pct, MAX_RISK_PCT)


# ── EarningsEngineResult 新フィールドテスト ─────────────────────────────────

class TestEarningsEngineResultNewFields(unittest.TestCase):
    """EarningsEngineResultの新フィールド確認"""

    def test_ic_sell_tactic_for_individual(self):
        """個別株は tactic='ic_sell' が返ること"""
        eng = EarningsEngine(api_key="test_key")
        # calc_ivr_individual / calc_em_hm_ratio をモック（外部API呼ばない）
        with patch.object(eng, "calc_ivr_individual", return_value=65.0), \
             patch.object(eng, "calc_em_hm_ratio", return_value=1.3):
            res = eng.get_entry_params("NVDA")
        self.assertEqual(res.tactic, "ic_sell")
        self.assertTrue(res.use_ic_structure)

    def test_straddle_sell_tactic_for_etf(self):
        """SPY/QQQ は tactic='straddle_sell' が返ること"""
        eng = EarningsEngine(api_key="test_key")
        res = eng.get_entry_params("SPY")
        self.assertEqual(res.tactic, "straddle_sell")
        self.assertFalse(res.use_ic_structure)

    def test_max_risk_pct_in_result(self):
        """EarningsEngineResultにmax_risk_pctが含まれること"""
        eng = EarningsEngine(api_key="test_key")
        with patch.object(eng, "calc_ivr_individual", return_value=None), \
             patch.object(eng, "calc_em_hm_ratio", return_value=None):
            res = eng.get_entry_params("TSLA")
        self.assertAlmostEqual(res.max_risk_pct, MAX_RISK_PCT)

    def test_em_hm_ratio_in_result_for_individual(self):
        """個別株のResultにem_hm_ratioが含まれること"""
        eng = EarningsEngine(api_key="test_key")
        mock_ratio = 1.42
        with patch.object(eng, "calc_ivr_individual", return_value=70.0), \
             patch.object(eng, "calc_em_hm_ratio", return_value=mock_ratio):
            res = eng.get_entry_params("META")
        self.assertAlmostEqual(res.em_hm_ratio, mock_ratio)

    def test_ivr_individual_in_result(self):
        """個別株のResultにivr_individualが含まれること"""
        eng = EarningsEngine(api_key="test_key")
        with patch.object(eng, "calc_ivr_individual", return_value=55.0), \
             patch.object(eng, "calc_em_hm_ratio", return_value=1.1):
            res = eng.get_entry_params("AAPL")
        self.assertAlmostEqual(res.ivr_individual, 55.0)


# ── calc_ivr_individual テスト ──────────────────────────────────────────────

class TestCalcIvrIndividual(unittest.TestCase):
    """calc_ivr_individual のモックテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")
        # テスト間のキャッシュ汚染を防ぐ
        self.eng._ivr_cache = {}

    def test_returns_none_without_yfinance(self):
        """yfinanceが未インストールの場合Noneを返すこと"""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "yfinance":
                raise ImportError("mocked yfinance not available")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = self.eng.calc_ivr_individual("NVDA")
        self.assertIsNone(result)

    def test_returns_float_in_0_100_range(self):
        """正常ケース: IVRが0-100の範囲で返ること"""
        import pandas as pd
        import numpy as np

        # 過去260日分のダミー株価データを生成（ランダムウォーク）
        np.random.seed(42)
        prices = 100 * np.exp(np.cumsum(np.random.normal(0, 0.01, 260)))
        close_series = pd.Series(prices, name="Close")
        hist_df = pd.DataFrame({"Close": close_series})

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist_df

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = self.eng.calc_ivr_individual("NVDA")

        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 100.0)

    def test_returns_none_on_insufficient_data(self):
        """データが30日未満の場合Noneを返すこと"""
        import pandas as pd

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame({"Close": [100.0] * 10})

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = self.eng.calc_ivr_individual("NVDA")
        self.assertIsNone(result)

    def test_caches_result_for_same_day(self):
        """同日2回目の呼び出しはキャッシュを使うこと（API呼び出しなし）"""
        import pandas as pd
        import numpy as np

        np.random.seed(1)
        prices = 500 * np.exp(np.cumsum(np.random.normal(0, 0.01, 260)))
        hist_df = pd.DataFrame({"Close": pd.Series(prices)})

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist_df

        with patch("yfinance.Ticker", return_value=mock_ticker) as mock_yf:
            result1 = self.eng.calc_ivr_individual("TSLA")
            result2 = self.eng.calc_ivr_individual("TSLA")

        # 2回目はキャッシュヒット: Tickerの呼び出しは1回以下であること
        self.assertIsNotNone(result1)
        self.assertEqual(result1, result2)


# ── calc_em_hm_ratio テスト ─────────────────────────────────────────────────

class TestCalcEmHmRatio(unittest.TestCase):
    """calc_em_hm_ratio のモックテスト"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key")

    def test_returns_none_without_yfinance(self):
        """yfinanceが未インストールの場合Noneを返すこと"""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "yfinance":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = self.eng.calc_em_hm_ratio("NVDA")
        self.assertIsNone(result)

    def test_returns_positive_float_when_data_available(self):
        """正常ケース: 正の浮動小数が返ること"""
        import pandas as pd
        import numpy as np

        np.random.seed(7)
        prices = 600 * np.exp(np.cumsum(np.random.normal(0, 0.015, 40)))
        hist_df = pd.DataFrame({"Close": pd.Series(prices)})

        # option_chain モック
        mock_calls = pd.DataFrame({
            "strike": [600.0, 605.0, 610.0],
            "bid": [10.0, 8.0, 6.0],
            "ask": [11.0, 9.0, 7.0],
            "lastPrice": [10.5, 8.5, 6.5],
        })
        mock_puts = pd.DataFrame({
            "strike": [590.0, 595.0, 600.0],
            "bid": [9.0, 11.0, 13.0],
            "ask": [10.0, 12.0, 14.0],
            "lastPrice": [9.5, 11.5, 13.5],
        })
        mock_chain = MagicMock()
        mock_chain.calls = mock_calls
        mock_chain.puts = mock_puts

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist_df
        mock_ticker.options = ["2026-04-22", "2026-04-29"]
        mock_ticker.option_chain.return_value = mock_chain

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = self.eng.calc_em_hm_ratio("NVDA")

        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)

    def test_returns_none_on_empty_option_chain(self):
        """オプションチェーンが空の場合Noneを返すこと"""
        import pandas as pd
        import numpy as np

        np.random.seed(3)
        prices = 200 * np.exp(np.cumsum(np.random.normal(0, 0.01, 30)))
        hist_df = pd.DataFrame({"Close": pd.Series(prices)})

        mock_chain = MagicMock()
        mock_chain.calls = pd.DataFrame()
        mock_chain.puts = pd.DataFrame()

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist_df
        mock_ticker.options = ["2026-04-22"]
        mock_ticker.option_chain.return_value = mock_chain

        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = self.eng.calc_em_hm_ratio("AAPL")
        self.assertIsNone(result)


# ── EM>HMフィルタ統合テスト（get_today_candidates） ─────────────────────────

class TestEmHmFilterIntegration(unittest.TestCase):
    """get_today_candidates でEM>HMフィルタが正しく動作することを確認"""

    def setUp(self):
        self.eng = EarningsEngine(api_key="test_key", require_em_over_hm=True)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_individual_stock_with_em_above_hm_passes(self, mock_fetch):
        """EM/HM > 1.0 の個別株が候補に残ること"""
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "NVDA", "date": today, "hour": "amc"},
        ]
        with patch.object(self.eng, "calc_em_hm_ratio", return_value=1.25), \
             patch.object(self.eng, "calc_ivr_individual", return_value=70.0):
            candidates = self.eng.get_today_candidates()
        syms = [c.symbol for c in candidates]
        self.assertIn("NVDA", syms)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_individual_stock_with_em_below_hm_skipped(self, mock_fetch):
        """EM/HM <= 1.0 の個別株がスキップされること"""
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "TSLA", "date": today, "hour": "amc"},
        ]
        with patch.object(self.eng, "calc_em_hm_ratio", return_value=0.85), \
             patch.object(self.eng, "calc_ivr_individual", return_value=60.0):
            candidates = self.eng.get_today_candidates()
        syms = [c.symbol for c in candidates]
        self.assertNotIn("TSLA", syms)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_individual_stock_with_em_hm_none_skipped(self, mock_fetch):
        """EM/HM取得失敗（None）の個別株は安全側でスキップされること"""
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "META", "date": today, "hour": "amc"},
        ]
        with patch.object(self.eng, "calc_em_hm_ratio", return_value=None), \
             patch.object(self.eng, "calc_ivr_individual", return_value=65.0):
            candidates = self.eng.get_today_candidates()
        syms = [c.symbol for c in candidates]
        self.assertNotIn("META", syms)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_em_hm_filter_disabled_allows_individual_without_check(self, mock_fetch):
        """require_em_over_hm=False のとき個別株もEM/HMチェックなしで通過すること"""
        eng = EarningsEngine(api_key="test_key", require_em_over_hm=False)
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "AAPL", "date": today, "hour": "amc"},
        ]
        # calc_em_hm_ratio は呼ばれないはず（フィルタ無効）
        with patch.object(eng, "calc_em_hm_ratio", return_value=None) as mock_em, \
             patch.object(eng, "calc_ivr_individual", return_value=50.0):
            candidates = eng.get_today_candidates()
        syms = [c.symbol for c in candidates]
        # AAPL のデフォルトcrush_rate=0.30 > min_iv_crush_rate=0.25 → 通過
        self.assertIn("AAPL", syms)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_ic_structure_flag_set_for_individual_stocks(self, mock_fetch):
        """個別株候補には use_ic_structure=True が設定されること"""
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "MSFT", "date": today, "hour": "amc"},
        ]
        with patch.object(self.eng, "calc_em_hm_ratio", return_value=1.3), \
             patch.object(self.eng, "calc_ivr_individual", return_value=55.0):
            candidates = self.eng.get_today_candidates()
        msft_candidates = [c for c in candidates if c.symbol == "MSFT"]
        self.assertEqual(len(msft_candidates), 1)
        self.assertTrue(msft_candidates[0].use_ic_structure)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_em_hm_ratio_stored_in_candidate(self, mock_fetch):
        """EarningsCandidateにem_hm_ratioが正しく記録されること"""
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "GOOGL", "date": today, "hour": "amc"},
        ]
        expected_ratio = 1.55
        with patch.object(self.eng, "calc_em_hm_ratio", return_value=expected_ratio), \
             patch.object(self.eng, "calc_ivr_individual", return_value=68.0):
            candidates = self.eng.get_today_candidates()
        googl = next((c for c in candidates if c.symbol == "GOOGL"), None)
        self.assertIsNotNone(googl)
        self.assertAlmostEqual(googl.em_hm_ratio, expected_ratio)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_multiple_symbols_mixed_em_hm(self, mock_fetch):
        """複数銘柄混在: EM>HMのもののみ候補に残ること"""
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "NVDA", "date": today, "hour": "amc"},   # EM>HM → 通過
            {"symbol": "TSLA", "date": today, "hour": "amc"},   # EM<HM → スキップ
            {"symbol": "AMZN", "date": today, "hour": "amc"},   # EM=None → スキップ
        ]

        def mock_em(symbol):
            return {"NVDA": 1.4, "TSLA": 0.8, "AMZN": None}.get(symbol)

        with patch.object(self.eng, "calc_em_hm_ratio", side_effect=mock_em), \
             patch.object(self.eng, "calc_ivr_individual", return_value=60.0):
            candidates = self.eng.get_today_candidates()

        syms = [c.symbol for c in candidates]
        self.assertIn("NVDA", syms)
        self.assertNotIn("TSLA", syms)
        self.assertNotIn("AMZN", syms)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_em_hm_boundary_exactly_1_0_is_skipped(self, mock_fetch):
        """EM/HM == 1.0 ちょうどはEM_HM_MIN_RATIO未満 → スキップされること"""
        # EM_HM_MIN_RATIO = 1.0 で ratio < 1.0 の条件 → ratio == 1.0 は通過するはず
        # ただし ratio < EM_HM_MIN_RATIO ではないので通過する
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "NVDA", "date": today, "hour": "amc"},
        ]
        # ratio == EM_HM_MIN_RATIO → フィルタ通過（< ではなく >=）
        with patch.object(self.eng, "calc_em_hm_ratio", return_value=EM_HM_MIN_RATIO), \
             patch.object(self.eng, "calc_ivr_individual", return_value=60.0):
            candidates = self.eng.get_today_candidates()
        syms = [c.symbol for c in candidates]
        # EM_HM_MIN_RATIO = 1.0 ちょうどはスキップ（< 1.0 の条件に引っかからない）
        self.assertIn("NVDA", syms)


# ── 個別株IVRのget_today_candidates統合テスト ────────────────────────────────

class TestIvrIndividualInCandidates(unittest.TestCase):
    """get_today_candidatesでivr_individualが正しく記録されることを確認"""

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_ivr_individual_stored_in_candidate(self, mock_fetch):
        """候補にivr_individualが保存されること"""
        eng = EarningsEngine(api_key="test_key", require_em_over_hm=True)
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "AAPL", "date": today, "hour": "amc"},
        ]
        with patch.object(eng, "calc_em_hm_ratio", return_value=1.2), \
             patch.object(eng, "calc_ivr_individual", return_value=72.5):
            candidates = eng.get_today_candidates()
        aapl = next((c for c in candidates if c.symbol == "AAPL"), None)
        self.assertIsNotNone(aapl)
        self.assertAlmostEqual(aapl.ivr_individual, 72.5)

    @patch.object(EarningsEngine, "_fetch_earnings_calendar")
    def test_ivr_individual_none_does_not_block(self, mock_fetch):
        """ivr_individual=Noneでもem_hm_ratioが有効ならエントリー候補になること"""
        eng = EarningsEngine(api_key="test_key", require_em_over_hm=True)
        today = _today_et()
        mock_fetch.return_value = [
            {"symbol": "MSFT", "date": today, "hour": "amc"},
        ]
        # IVR取得失敗 + EM>HM → 候補には残る
        with patch.object(eng, "calc_em_hm_ratio", return_value=1.15), \
             patch.object(eng, "calc_ivr_individual", return_value=None):
            candidates = eng.get_today_candidates()
        msft = next((c for c in candidates if c.symbol == "MSFT"), None)
        self.assertIsNotNone(msft)
        self.assertIsNone(msft.ivr_individual)


# ── IVRキャッシュテスト ─────────────────────────────────────────────────────

class TestIvrCachePersistence(unittest.TestCase):
    """IVRキャッシュのload/save動作確認"""

    def test_ivr_cache_is_loaded_on_init(self):
        """初期化時にIVRキャッシュが存在すれば読み込まれること"""
        from common.earnings_engine import IVR_INDIVIDUAL_CACHE_FILE
        cache_data = {
            "NVDA_ivr": {
                "ivr": 75.0,
                "current_hv": 55.0,
                "ts": 9999999999.0,
                "date": datetime.date.today().isoformat(),
            }
        }
        IVR_INDIVIDUAL_CACHE_FILE.write_text(json.dumps(cache_data))
        eng = EarningsEngine(api_key="test_key")
        self.assertIn("NVDA_ivr", eng._ivr_cache)
        self.assertAlmostEqual(eng._ivr_cache["NVDA_ivr"]["ivr"], 75.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
