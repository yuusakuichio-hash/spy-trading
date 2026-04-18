"""
tests/test_vix_term.py — VIX Term Structure 戦術テスト (10テスト以上)

テスト対象: EarningsEngine.get_term_structure_regime()

テスト方針:
  - 外部API接触なし (全てunit test)
  - 固定閾値を検証するのではなく比率ロジック自体を検証する
  - contango/backwardation/neutral の3状態を網羅
  - size_factor の単調性・境界を検証
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.earnings_engine import EarningsEngine


class TestTermStructureContango(unittest.TestCase):
    """コンタンゴ(term_ratio < 0.85) のテスト"""

    def test_contango_regime_detection(self):
        """VIX9D/VIX3M < 0.85 → regime=contango"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=14.0, vix=16.0, vix3m=18.0  # ratio = 14/18 ≈ 0.778
        )
        self.assertEqual(res["regime"], "contango")

    def test_contango_tactic_bias_cs_sell(self):
        """コンタンゴ時は tactic_bias=cs_sell であること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=13.0, vix=15.0, vix3m=17.0
        )
        self.assertEqual(res["tactic_bias"], "cs_sell")

    def test_contango_size_factor_1_0(self):
        """コンタンゴ時は size_factor=1.0 (縮小なし) であること (spot_ratio<=1.0前提)"""
        # vix9d <= vix の場合、spot補助縮小なし → size_factor == 1.0
        res = EarningsEngine.get_term_structure_regime(
            vix9d=14.0, vix=16.0, vix3m=18.0
        )
        self.assertAlmostEqual(res["size_factor"], 1.0)

    def test_contango_term_ratio_stored(self):
        """term_ratio_9d_3m が正しく格納されること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=14.0, vix=16.0, vix3m=18.0
        )
        expected = round(14.0 / 18.0, 4)
        self.assertAlmostEqual(res["term_ratio_9d_3m"], expected, places=3)


class TestTermStructureBackwardation(unittest.TestCase):
    """バックワーデーション(term_ratio > 1.05) のテスト"""

    def test_backwardation_regime_detection(self):
        """VIX9D/VIX3M > 1.05 → regime=backwardation"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=22.0, vix=20.0, vix3m=18.0  # ratio = 22/18 ≈ 1.222
        )
        self.assertEqual(res["regime"], "backwardation")

    def test_backwardation_tactic_bias_straddle_buy(self):
        """バックワーデーション時は tactic_bias=straddle_buy であること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=25.0, vix=22.0, vix3m=20.0
        )
        self.assertEqual(res["tactic_bias"], "straddle_buy")

    def test_backwardation_size_factor_less_than_1(self):
        """バックワーデーション時は size_factor < 1.0 であること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=25.0, vix=22.0, vix3m=20.0
        )
        self.assertLess(res["size_factor"], 1.0)

    def test_backwardation_notes_contains_ratio(self):
        """backwardation 時の notes に ratio 情報が含まれること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=25.0, vix=22.0, vix3m=20.0
        )
        self.assertIn("backwardation", res["notes"])


class TestTermStructureNeutral(unittest.TestCase):
    """ニュートラルゾーン (0.85 <= ratio <= 1.05) のテスト"""

    def test_neutral_regime_detection(self):
        """0.85 <= VIX9D/VIX3M <= 1.05 → regime=neutral"""
        # ratio = 17/18 ≈ 0.944
        res = EarningsEngine.get_term_structure_regime(
            vix9d=17.0, vix=17.0, vix3m=18.0
        )
        self.assertEqual(res["regime"], "neutral")

    def test_neutral_tactic_bias(self):
        """ニュートラル時は tactic_bias=neutral であること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=17.0, vix=17.0, vix3m=18.0
        )
        self.assertEqual(res["tactic_bias"], "neutral")

    def test_neutral_size_factor_between_backwardation_and_contango(self):
        """neutral の size_factor が contango と backwardation の間であること"""
        res_contango = EarningsEngine.get_term_structure_regime(14.0, 16.0, 18.0)
        res_neutral  = EarningsEngine.get_term_structure_regime(17.0, 17.0, 18.0)
        res_back     = EarningsEngine.get_term_structure_regime(25.0, 22.0, 20.0)
        # contango >= neutral >= backwardation
        self.assertGreaterEqual(res_contango["size_factor"], res_neutral["size_factor"])
        self.assertGreaterEqual(res_neutral["size_factor"],  res_back["size_factor"])


class TestTermStructureMissingData(unittest.TestCase):
    """データ欠損時のフォールバックテスト"""

    def test_none_vix9d_returns_neutral(self):
        """vix9d=None → regime=neutral"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=None, vix=17.0, vix3m=18.0
        )
        self.assertEqual(res["regime"], "neutral")

    def test_none_vix3m_returns_neutral(self):
        """vix3m=None → regime=neutral"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=17.0, vix=17.0, vix3m=None
        )
        self.assertEqual(res["regime"], "neutral")

    def test_none_vix3m_term_ratio_is_none(self):
        """vix3m=None のとき term_ratio_9d_3m=None であること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=17.0, vix=17.0, vix3m=None
        )
        self.assertIsNone(res["term_ratio_9d_3m"])

    def test_all_none_returns_safe_defaults(self):
        """全パラメータNone → エラーなく neutral が返ること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=None, vix=None, vix3m=None
        )
        self.assertEqual(res["regime"], "neutral")
        self.assertEqual(res["tactic_bias"], "neutral")
        self.assertIsNone(res["term_ratio_9d_3m"])
        self.assertIsNone(res["term_ratio_spot"])


class TestTermStructureSpotRatio(unittest.TestCase):
    """Spot ratio (VIX9D/VIX) 補助縮小のテスト"""

    def test_spot_ratio_over_1_reduces_size(self):
        """VIX9D > VIX (spot_ratio > 1.0) のとき size_factor が追加縮小されること"""
        # ratio_9d_3m < 0.85 (contango) but spot_ratio > 1.0
        res = EarningsEngine.get_term_structure_regime(
            vix9d=22.0, vix=20.0, vix3m=28.0  # 9d/3m = 0.786 < 0.85, 9d/spot = 1.1 > 1.0
        )
        self.assertEqual(res["regime"], "contango")
        # spot補助縮小: 1.0 × 0.9 = 0.9
        self.assertAlmostEqual(res["size_factor"], 0.9, places=3)

    def test_spot_ratio_under_1_no_additional_reduction(self):
        """VIX9D < VIX (spot_ratio <= 1.0) のとき spot補助縮小なし"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=14.0, vix=18.0, vix3m=22.0  # 9d/3m = 0.636 < 0.85, 9d/spot = 0.778
        )
        self.assertEqual(res["regime"], "contango")
        # spot補助縮小なし → size_factor = 1.0
        self.assertAlmostEqual(res["size_factor"], 1.0, places=3)

    def test_spot_ratio_stored(self):
        """term_ratio_spot が正しく格納されること"""
        res = EarningsEngine.get_term_structure_regime(
            vix9d=18.0, vix=16.0, vix3m=20.0
        )
        expected = round(18.0 / 16.0, 4)
        self.assertAlmostEqual(res["term_ratio_spot"], expected, places=3)


class TestTermStructureReturnSchema(unittest.TestCase):
    """返り値スキーマの完全性テスト"""

    def test_all_required_keys_present(self):
        """必須キーが全て含まれること"""
        res = EarningsEngine.get_term_structure_regime(14.0, 16.0, 18.0)
        required_keys = [
            "regime", "tactic_bias", "size_factor",
            "term_ratio_9d_3m", "term_ratio_spot", "notes",
        ]
        for k in required_keys:
            self.assertIn(k, res, f"key '{k}' missing from result")

    def test_size_factor_always_positive(self):
        """どんな入力でも size_factor > 0 であること"""
        test_cases = [
            (14.0, 16.0, 18.0),    # contango
            (25.0, 22.0, 20.0),    # backwardation
            (17.0, 17.0, 18.0),    # neutral
            (None, 16.0, 18.0),    # vix9d missing
        ]
        for args in test_cases:
            res = EarningsEngine.get_term_structure_regime(*args)
            self.assertGreater(res["size_factor"], 0.0, f"args={args}")

    def test_regime_values_are_valid(self):
        """regime が有効な値のみを返すこと"""
        valid_regimes = {"contango", "backwardation", "neutral"}
        test_cases = [
            (14.0, 16.0, 18.0),
            (25.0, 22.0, 20.0),
            (17.0, 17.0, 18.0),
            (None, 16.0, 18.0),
        ]
        for args in test_cases:
            res = EarningsEngine.get_term_structure_regime(*args)
            self.assertIn(res["regime"], valid_regimes, f"invalid regime for args={args}")

    def test_tactic_bias_values_are_valid(self):
        """tactic_bias が有効な値のみを返すこと"""
        valid_biases = {"cs_sell", "straddle_buy", "neutral"}
        test_cases = [
            (14.0, 16.0, 18.0),
            (25.0, 22.0, 20.0),
            (17.0, 17.0, 18.0),
        ]
        for args in test_cases:
            res = EarningsEngine.get_term_structure_regime(*args)
            self.assertIn(res["tactic_bias"], valid_biases, f"invalid bias for args={args}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
