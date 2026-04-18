#!/usr/bin/env python3
"""tests/test_trader_eval.py -- 優秀トレーダー判定フレームワーク テストスイート (20件以上)"""
import math
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

# パス設定
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.trader_evaluation import (
    pair_trades,
    filter_by_date_range,
    calc_m1_profit_factor,
    calc_m2_win_rate,
    calc_m3_expected_value,
    calc_m4_rom,
    calc_m5_monthly_consistency,
    calc_m6_sortino,
    calc_m7_calmar,
    calc_m8_mar,
    calc_m9_ulcer_index,
    calc_m10_e_ratio,
    calc_m11_slippage_rate,
    calc_m12_naked_leg_rate,
    calc_m13_discipline_score,
    calc_m14_consistency_score,
    calc_m15_revenge_trade_rate,
    calc_ev1_hypothesis_match,
    calc_ev4_tactic_breakdown,
    calc_dow_breakdown,
    calc_vix_band_breakdown,
    calc_premium_capture_rate,
    build_markdown_report,
    run_evaluation,
    DISCIPLINE_RULES,
    evaluate_discipline_for_trade,
)


# ---------------------------------------------------------------------------
# テスト用サンプルデータ生成ヘルパー
# ---------------------------------------------------------------------------

def make_entry(date_str: str, tactic: str = "standard", net_credit: float = 0.5,
               qty: int = 1, vix: float = 18.0, delta: float = 0.25,
               slippage: float = None, trade_id: str = None,
               ts: str = None) -> dict:
    base_ts = ts or f"{date_str}T11:00:00-04:00"
    e = {
        "event": "entry",
        "date": date_str,
        "tactic": tactic,
        "net_credit": net_credit,
        "qty": qty,
        "vix": vix,
        "delta_actual": delta,
        "ts": base_ts,
    }
    if slippage is not None:
        e["slippage"] = slippage
    if trade_id:
        e["trade_id"] = trade_id
    return e


def make_exit(date_str: str, pnl_usd: float, reason: str = "tp_50pct",
              trade_id: str = None, ts: str = None) -> dict:
    base_ts = ts or f"{date_str}T13:00:00-04:00"
    e = {
        "event": "exit",
        "date": date_str,
        "pnl_usd": pnl_usd,
        "reason": reason,
        "ts": base_ts,
    }
    if trade_id:
        e["trade_id"] = trade_id
    return e


def make_env_snap(date_str: str, vix: float = 18.0, regime: str = "normal",
                  capital_pct: float = 0.3) -> dict:
    return {
        "event": "env_snapshot",
        "date": date_str,
        "vix": vix,
        "regime": regime,
        "env_score": 80.0,
        "params": {"capital_pct": capital_pct},
        "ts": f"{date_str}T11:00:00-04:00",
    }


def make_paired_trades(records: list[tuple]) -> list[dict]:
    """(date, pnl, tactic, net_credit, qty, vix) のリストからpairedを直接生成"""
    result = []
    for i, rec in enumerate(records):
        date_str = rec[0]
        pnl = rec[1]
        tactic = rec[2] if len(rec) > 2 else "standard"
        nc = rec[3] if len(rec) > 3 else 0.5
        qty = rec[4] if len(rec) > 4 else 1
        vix = rec[5] if len(rec) > 5 else 18.0
        result.append({
            "entry": {"net_credit": nc, "qty": qty, "delta_actual": 0.25,
                      "ts": f"{date_str}T11:00:00-04:00"},
            "exit": {"pnl_usd": pnl, "ts": f"{date_str}T13:00:00-04:00"},
            "date": date_str,
            "tactic": tactic,
            "pnl": pnl,
            "net_credit": nc,
            "qty": qty,
            "entry_ts": f"{date_str}T11:00:00-04:00",
            "exit_ts": f"{date_str}T13:00:00-04:00",
            "exit_reason": "tp",
            "vix": vix,
            "regime": None,
            "slippage": None,
        })
    return result


# ---------------------------------------------------------------------------
# テストクラス
# ---------------------------------------------------------------------------

class TestPairTrades(unittest.TestCase):
    """pair_trades 基本動作"""

    def test_01_pair_by_trade_id(self):
        """trade_id でペアリングできる"""
        trades = [
            make_entry("2026-04-14", trade_id="AAA"),
            make_exit("2026-04-14", pnl_usd=25.0, trade_id="AAA"),
        ]
        paired = pair_trades(trades)
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["pnl"], 25.0)

    def test_02_pair_fifo_no_trade_id(self):
        """trade_id なしは FIFO でペアリング"""
        trades = [
            make_entry("2026-04-14"),
            make_entry("2026-04-14"),
            make_exit("2026-04-14", pnl_usd=10.0),
            make_exit("2026-04-14", pnl_usd=20.0),
        ]
        paired = pair_trades(trades)
        self.assertEqual(len(paired), 2)

    def test_03_unpaired_entry_ignored(self):
        """対応するexitのないentryは無視される"""
        trades = [make_entry("2026-04-14", trade_id="BBB")]
        paired = pair_trades(trades)
        self.assertEqual(len(paired), 0)

    def test_04_env_snapshot_not_paired(self):
        """env_snapshot イベントはペアリング対象外"""
        trades = [
            make_entry("2026-04-14", trade_id="CCC"),
            make_env_snap("2026-04-14"),
            make_exit("2026-04-14", pnl_usd=5.0, trade_id="CCC"),
        ]
        paired = pair_trades(trades)
        self.assertEqual(len(paired), 1)


class TestFilterByDateRange(unittest.TestCase):

    def test_05_filter_exact_date(self):
        trades = [
            {"event": "entry", "date": "2026-04-14"},
            {"event": "entry", "date": "2026-04-15"},
            {"event": "entry", "date": "2026-04-16"},
        ]
        result = filter_by_date_range(trades, date(2026, 4, 14), date(2026, 4, 15))
        self.assertEqual(len(result), 2)

    def test_06_filter_no_match(self):
        trades = [{"event": "entry", "date": "2026-04-10"}]
        result = filter_by_date_range(trades, date(2026, 4, 14), date(2026, 4, 15))
        self.assertEqual(len(result), 0)


class TestM1ProfitFactor(unittest.TestCase):

    def test_07_profit_factor_basic(self):
        """PF = 総利益 / 総損失"""
        paired = make_paired_trades([
            ("2026-04-14", 100.0),
            ("2026-04-15", -50.0),
        ])
        pf = calc_m1_profit_factor(paired)
        self.assertAlmostEqual(pf, 2.0, places=2)

    def test_08_profit_factor_all_wins(self):
        """全勝の場合は inf"""
        paired = make_paired_trades([("2026-04-14", 100.0)])
        pf = calc_m1_profit_factor(paired)
        self.assertEqual(pf, float("inf"))

    def test_09_profit_factor_no_trades(self):
        """トレードなしは None"""
        pf = calc_m1_profit_factor([])
        self.assertIsNone(pf)


class TestM2WinRate(unittest.TestCase):

    def test_10_win_rate_basic(self):
        paired = make_paired_trades([
            ("2026-04-14", 50.0),
            ("2026-04-15", -30.0),
            ("2026-04-16", 20.0),
        ])
        wr = calc_m2_win_rate(paired)
        self.assertAlmostEqual(wr, 2 / 3, places=3)

    def test_11_win_rate_empty(self):
        self.assertIsNone(calc_m2_win_rate([]))

    def test_12_win_rate_none_pnl_skipped(self):
        """pnl=None のトレードは除外"""
        paired = make_paired_trades([("2026-04-14", 50.0)])
        paired.append({"pnl": None, "date": "2026-04-15", "tactic": "standard",
                        "entry": {}, "exit": {}})
        wr = calc_m2_win_rate(paired)
        self.assertAlmostEqual(wr, 1.0, places=3)


class TestM3ExpectedValue(unittest.TestCase):

    def test_13_expected_value_positive(self):
        paired = make_paired_trades([
            ("2026-04-14", 100.0),
            ("2026-04-15", 100.0),
            ("2026-04-16", -50.0),
        ])
        ev = calc_m3_expected_value(paired)
        # 勝率=2/3, avg_win=100, avg_loss=50
        expected = (2 / 3) * 100 - (1 / 3) * 50
        self.assertAlmostEqual(ev, expected, places=1)


class TestM4ROM(unittest.TestCase):

    def test_14_rom_basic(self):
        paired = make_paired_trades([("2026-04-14", 3800.0)])
        rom = calc_m4_rom(paired, 380000.0)
        self.assertAlmostEqual(rom, 0.01, places=4)

    def test_15_rom_zero_margin(self):
        paired = make_paired_trades([("2026-04-14", 100.0)])
        self.assertIsNone(calc_m4_rom(paired, 0))


class TestM6Sortino(unittest.TestCase):

    def test_16_sortino_positive_returns(self):
        """全て正のリターンは下方偏差 = 0 なので None"""
        paired = make_paired_trades([
            ("2026-04-14", 100.0),
            ("2026-04-15", 80.0),
            ("2026-04-16", 60.0),
        ])
        sortino = calc_m6_sortino(paired)
        self.assertIsNone(sortino)

    def test_17_sortino_mixed(self):
        """混合リターンでは有限値が返る"""
        paired = make_paired_trades([
            ("2026-04-14", 100.0),
            ("2026-04-15", -50.0),
            ("2026-04-16", 80.0),
            ("2026-04-17", -30.0),
            ("2026-04-18", 60.0),
        ])
        sortino = calc_m6_sortino(paired)
        self.assertIsNotNone(sortino)
        self.assertIsInstance(sortino, float)


class TestM9UlcerIndex(unittest.TestCase):

    def test_18_ulcer_monotone_up(self):
        """単調増加では DD = 0 なので Ulcer = 0"""
        paired = make_paired_trades([
            ("2026-04-14", 100.0),
            ("2026-04-15", 50.0),
            ("2026-04-16", 80.0),
        ])
        # 日別集計: Apr14=100, Apr15=50, Apr16=80 → 累計: 100, 150, 230 (単調増加)
        # peak=100->150->230, DD=0 常時
        ulcer = calc_m9_ulcer_index(paired)
        self.assertEqual(ulcer, 0.0)

    def test_19_ulcer_with_drawdown(self):
        """DDがある場合は正の値が返る"""
        paired = make_paired_trades([
            ("2026-04-14", 1000.0),
            ("2026-04-15", -500.0),
            ("2026-04-16", -200.0),
        ])
        ulcer = calc_m9_ulcer_index(paired)
        self.assertIsNotNone(ulcer)
        self.assertGreater(ulcer, 0)


class TestM11SlippageRate(unittest.TestCase):

    def test_20_slippage_rate_basic(self):
        """slippage=0.1, net_credit=0.5 → rate=0.2"""
        paired = make_paired_trades([("2026-04-14", 30.0, "standard", 0.5)])
        paired[0]["slippage"] = 0.1
        rate = calc_m11_slippage_rate(paired)
        self.assertAlmostEqual(rate, 0.2, places=4)

    def test_21_slippage_no_data(self):
        """slippage なし → None"""
        paired = make_paired_trades([("2026-04-14", 30.0)])
        rate = calc_m11_slippage_rate(paired)
        self.assertIsNone(rate)


class TestM12NakedLegRate(unittest.TestCase):

    def test_22_no_naked_legs(self):
        """naked_leg_detected なし → 0.0"""
        trades = [
            make_entry("2026-04-14", trade_id="A"),
            make_exit("2026-04-14", 20.0, trade_id="A"),
        ]
        rate = calc_m12_naked_leg_rate(trades)
        self.assertEqual(rate, 0.0)

    def test_23_with_naked_leg(self):
        """naked_leg_detected があれば 0 超"""
        trades = [
            make_entry("2026-04-14", trade_id="B"),
            {"event": "naked_leg_detected", "date": "2026-04-14"},
        ]
        rate = calc_m12_naked_leg_rate(trades)
        self.assertGreater(rate, 0.0)


class TestM13DisciplineScore(unittest.TestCase):

    def test_24_discipline_score_range(self):
        """Discipline Score は 0.0〜1.0 の範囲"""
        paired = make_paired_trades([("2026-04-14", 30.0)])
        trades = [make_entry("2026-04-14", trade_id="X"),
                  make_env_snap("2026-04-14"),
                  make_exit("2026-04-14", 30.0, trade_id="X")]
        score = calc_m13_discipline_score(paired, trades)
        self.assertIsNotNone(score)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


class TestM14ConsistencyScore(unittest.TestCase):

    def test_25_consistency_score_dominated_by_one_day(self):
        """1日で全利益の80%を稼ぐと Consistency Score = 0.8 (POOR)"""
        paired = make_paired_trades([
            ("2026-04-14", 800.0),
            ("2026-04-15", 100.0),
            ("2026-04-16", 100.0),
        ])
        score = calc_m14_consistency_score(paired)
        self.assertAlmostEqual(score, 0.8, places=2)

    def test_26_consistency_score_even(self):
        """均等分散なら score < 0.34 (3日均等なら 0.333)"""
        paired = make_paired_trades([
            ("2026-04-14", 100.0),
            ("2026-04-15", 100.0),
            ("2026-04-16", 100.0),
        ])
        score = calc_m14_consistency_score(paired)
        self.assertAlmostEqual(score, 1 / 3, places=2)


class TestM15RevengeTrade(unittest.TestCase):

    def test_27_no_revenge_trades(self):
        """損失なし → リベンジトレード率 0"""
        trades = [
            make_entry("2026-04-14", trade_id="A"),
            make_exit("2026-04-14", 50.0, trade_id="A"),
        ]
        rate = calc_m15_revenge_trade_rate(trades)
        self.assertEqual(rate, 0.0)


class TestEV1HypothesisMatch(unittest.TestCase):

    def test_28_match_normal_regime_win(self):
        """normal regime で P&L > 0 はマッチ"""
        trades = [
            make_entry("2026-04-14", trade_id="A"),
            make_env_snap("2026-04-14", regime="normal"),
            make_exit("2026-04-14", 30.0, trade_id="A"),
        ]
        result = calc_ev1_hypothesis_match(trades)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["total"], 1)

    def test_29_match_normal_regime_loss(self):
        """normal regime で P&L < 0 はミスマッチ"""
        trades = [
            make_entry("2026-04-14", trade_id="B"),
            make_env_snap("2026-04-14", regime="normal"),
            make_exit("2026-04-14", -30.0, trade_id="B"),
        ]
        result = calc_ev1_hypothesis_match(trades)
        self.assertEqual(result["matched"], 0)

    def test_30_match_crisis_regime_small_loss(self):
        """crisis regime で 損失 < 50 はマッチ (損切り遵守と判定)"""
        trades = [
            make_entry("2026-04-14", trade_id="C"),
            make_env_snap("2026-04-14", regime="crisis"),
            make_exit("2026-04-14", -20.0, trade_id="C"),
        ]
        result = calc_ev1_hypothesis_match(trades)
        self.assertEqual(result["matched"], 1)


class TestEV4TacticBreakdown(unittest.TestCase):

    def test_31_tactic_breakdown_keys(self):
        """戦術別内訳のキーが正しく返る"""
        paired = make_paired_trades([
            ("2026-04-14", 50.0, "ic"),
            ("2026-04-15", -30.0, "ic"),
            ("2026-04-16", 40.0, "cs"),
        ])
        result = calc_ev4_tactic_breakdown(paired)
        self.assertIn("ic", result)
        self.assertIn("cs", result)
        self.assertEqual(result["ic"]["trades"], 2)
        self.assertEqual(result["cs"]["trades"], 1)

    def test_32_tactic_pf_target(self):
        """IC の target_pf = 2.0"""
        paired = make_paired_trades([
            ("2026-04-14", 100.0, "ic"),
            ("2026-04-15", -30.0, "ic"),
        ])
        result = calc_ev4_tactic_breakdown(paired)
        self.assertEqual(result["ic"]["target_pf"], 2.0)


class TestPremiumCaptureRate(unittest.TestCase):

    def test_33_pcr_full_capture(self):
        """net_credit=0.5, qty=1 のとき最大利益=50 → pnl=50 でPCR=100%"""
        paired = make_paired_trades([("2026-04-14", 50.0, "standard", 0.5, 1)])
        pcr = calc_premium_capture_rate(paired)
        self.assertAlmostEqual(pcr, 100.0, places=1)

    def test_34_pcr_half_capture(self):
        """pnl=25, max=50 → PCR=50%"""
        paired = make_paired_trades([("2026-04-14", 25.0, "standard", 0.5, 1)])
        pcr = calc_premium_capture_rate(paired)
        self.assertAlmostEqual(pcr, 50.0, places=1)

    def test_35_pcr_no_data(self):
        """pnl=None → None"""
        paired = [{"pnl": None, "net_credit": 0.5, "qty": 1, "tactic": "standard",
                   "date": "2026-04-14", "entry": {}, "exit": {}}]
        pcr = calc_premium_capture_rate(paired)
        self.assertIsNone(pcr)


class TestMarkdownReport(unittest.TestCase):

    def _make_result(self) -> dict:
        return {
            "generated_at": "2026-04-18T00:00:00",
            "period": "daily",
            "date_range": {"start": "2026-04-18", "end": "2026-04-18"},
            "trade_count": 3,
            "total_pnl_usd": 75.0,
            "margin_usd": 380000.0,
            "metrics": {
                "M1_profit_factor": 2.5,
                "M2_win_rate": 0.667,
                "M3_expected_value": 15.0,
                "M4_rom": 0.0002,
                "M5_monthly_consistency": 0.85,
                "M6_sortino": 2.3,
                "M7_calmar": 3.5,
                "M8_mar": 1.2,
                "M9_ulcer_index": 2.1,
                "M10_e_ratio": 1.6,
                "M11_slippage_rate": 0.15,
                "M12_naked_leg_rate": 0.0,
                "M13_discipline_score": 0.97,
                "M14_consistency_score": 0.25,
                "M15_revenge_trade_rate": 0.0,
            },
            "ev_gaps": {
                "EV1_hypothesis_match": {"match_rate": 0.75, "total": 4, "matched": 3},
            },
            "breakdowns": {
                "tactic": {"standard": {"trades": 3, "win_rate": 0.667, "profit_factor": 2.5, "target_pf": 1.5, "pf_ok": True}},
                "day_of_week": {"Mon": {"trades": 3, "win_rate": 0.667}},
                "vix_band": {"15-20": {"trades": 3, "win_rate": 0.667}},
            },
            "premium_capture_rate_pct": 62.5,
        }

    def test_36_markdown_contains_headers(self):
        result = self._make_result()
        md = build_markdown_report(result, date(2026, 4, 18))
        self.assertIn("優秀トレーダー判定レポート", md)
        self.assertIn("M1 Profit Factor", md)
        self.assertIn("M6 Sortino Ratio", md)
        self.assertIn("Theta Profits", md)

    def test_37_markdown_contains_benchmarks(self):
        result = self._make_result()
        md = build_markdown_report(result, date(2026, 4, 18))
        self.assertIn("Nick Magno", md)
        self.assertIn("Option Alpha", md)

    def test_38_markdown_rating_good(self):
        """PF=2.5 は GOOD が出る"""
        result = self._make_result()
        md = build_markdown_report(result, date(2026, 4, 18))
        # M1行に GOOD が含まれる
        for line in md.split("\n"):
            if "M1 Profit Factor" in line:
                self.assertIn("GOOD", line)
                break

    def test_39_markdown_tactic_section(self):
        result = self._make_result()
        md = build_markdown_report(result, date(2026, 4, 18))
        self.assertIn("戦術別内訳", md)
        self.assertIn("standard", md)

    def test_40_markdown_pcr_warn(self):
        """PCR=3.8% は WARN が出る"""
        result = self._make_result()
        result["premium_capture_rate_pct"] = 3.8
        md = build_markdown_report(result, date(2026, 4, 18))
        self.assertIn("WARN", md)


class TestRunEvaluationIntegration(unittest.TestCase):
    """run_evaluation の結合テスト (実データがなくてもエラーにならない)"""

    def test_41_run_evaluation_no_data_returns_error(self):
        """データファイルが全て存在しない状況でも graceful に error dict を返す"""
        import unittest.mock as mock
        with mock.patch("scripts.trader_evaluation.load_all_trades", return_value=[]):
            result = run_evaluation(period="daily", target_date=date(2026, 4, 18))
        self.assertIn("error", result)

    def test_42_run_evaluation_with_sample_data(self):
        """サンプルデータで全指標が計算される"""
        import unittest.mock as mock
        sample_trades = []
        for i in range(5):
            d = f"2026-04-{14+i:02d}"
            tid = f"trade_{i}"
            sample_trades.append(make_entry(d, trade_id=tid, net_credit=0.5, qty=1))
            sample_trades.append(make_env_snap(d))
            pnl = 25.0 if i % 2 == 0 else -15.0
            sample_trades.append(make_exit(d, pnl, trade_id=tid))

        with mock.patch("scripts.trader_evaluation.load_all_trades", return_value=sample_trades):
            result = run_evaluation(period="30", target_date=date(2026, 4, 18))
        self.assertNotIn("error", result)
        self.assertIn("metrics", result)
        self.assertIn("M1_profit_factor", result["metrics"])
        self.assertIn("M13_discipline_score", result["metrics"])

    def test_43_discipline_rules_count(self):
        """DISCIPLINE_RULES は正確に10件"""
        self.assertEqual(len(DISCIPLINE_RULES), 10)

    def test_44_evaluate_discipline_returns_all_rules(self):
        """evaluate_discipline_for_trade は10キーを返す"""
        trade = make_paired_trades([("2026-04-14", 30.0)])[0]
        trades_for_date = [make_env_snap("2026-04-14")]
        result = evaluate_discipline_for_trade(trade, trades_for_date)
        self.assertEqual(len(result), 10)
        for key, _ in DISCIPLINE_RULES:
            self.assertIn(key, result)

    def test_45_m5_monthly_consistency_multiple_months(self):
        """複数月のデータで月次一貫性が計算される"""
        trades = []
        for month in [3, 4]:
            for day in [14, 15]:
                d = f"2026-0{month}-{day:02d}"
                tid = f"t_{month}_{day}"
                trades.append(make_entry(d, trade_id=tid))
                pnl = 30.0 if month == 4 else -10.0
                trades.append(make_exit(d, pnl, trade_id=tid))
        score = calc_m5_monthly_consistency(trades)
        # 4月: 合計+60, 3月: 合計-20 → 1/2 = 0.5
        self.assertIsNotNone(score)
        self.assertAlmostEqual(score, 0.5, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
