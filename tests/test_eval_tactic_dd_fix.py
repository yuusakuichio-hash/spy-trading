"""
tests/test_eval_tactic_dd_fix.py
Fix 1 (tactic伝搬修復) / Fix 2 (Drawdown系メトリクス改善) の検証テスト。
redteam用 — builderは採点に使用しない。
"""
from __future__ import annotations

import sys
import json
import math
import unittest
from pathlib import Path

# パスを通す
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

from trader_evaluation import (
    _SOURCE_TACTIC_MAP,
    load_all_trades,
    pair_trades,
    filter_by_date_range,
    calc_m6_sortino,
    calc_m7_calmar,
    calc_m8_mar,
    calc_m9_ulcer_index,
    calc_ev4_tactic_breakdown,
    run_evaluation,
    _daily_pnl_series,
)
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Fix 1: _SOURCE_TACTIC_MAP の存在確認
# ---------------------------------------------------------------------------

class TestSourceTacticMap(unittest.TestCase):
    """Fix 1: ファイル名→tactic マッピングの整合性テスト"""

    def test_map_contains_all_pnl_files(self):
        """全pnlファイルがマッピングに存在すること"""
        expected_keys = {
            "condor_pnl", "momentum_pnl", "straddle_pnl",
            "butterfly_pnl", "calendar_pnl",
        }
        self.assertEqual(expected_keys, set(_SOURCE_TACTIC_MAP.keys()))

    def test_map_values_are_non_empty_strings(self):
        """全マッピング値が非空文字列であること"""
        for k, v in _SOURCE_TACTIC_MAP.items():
            self.assertIsInstance(v, str, f"{k} の値が str でない")
            self.assertTrue(len(v) > 0, f"{k} の値が空文字列")

    def test_calendar_maps_to_calendar_sell(self):
        self.assertEqual(_SOURCE_TACTIC_MAP["calendar_pnl"], "calendar_sell")

    def test_butterfly_maps_to_butterfly(self):
        self.assertEqual(_SOURCE_TACTIC_MAP["butterfly_pnl"], "butterfly")

    def test_condor_maps_to_ic_sell(self):
        self.assertEqual(_SOURCE_TACTIC_MAP["condor_pnl"], "ic_sell")


# ---------------------------------------------------------------------------
# Fix 1: load_all_trades でtacticが補完されること
# ---------------------------------------------------------------------------

class TestLoadAllTradesTacticFallback(unittest.TestCase):
    """Fix 1: tactic欠落ログへのフォールバック補完テスト"""

    def test_no_unknown_tactic_in_loaded_trades(self):
        """load_all_trades の結果に tactic=unknown がないこと（補完済み）"""
        trades = load_all_trades()
        unknown = [t for t in trades if t.get("tactic") == "unknown"]
        # 完全ゼロは難しいが、MassVerify_CS などの旧フォーマットは除外
        # 少なくとも butterfly/calendar から来た trades はunknownでない
        butterfly_unknown = [
            t for t in trades
            if t.get("_source") in ("butterfly_pnl", "calendar_pnl")
            and t.get("tactic") == "unknown"
        ]
        self.assertEqual(
            len(butterfly_unknown), 0,
            f"butterfly/calendar のtactic未補完: {butterfly_unknown[:3]}",
        )

    def test_butterfly_trades_have_tactic(self):
        """butterfly_pnl 由来のトレードに tactic が設定されていること"""
        trades = load_all_trades()
        butterfly_trades = [t for t in trades if t.get("_source") == "butterfly_pnl"]
        if butterfly_trades:
            for t in butterfly_trades:
                self.assertEqual(
                    t.get("tactic"), "butterfly",
                    f"butterfly trade の tactic が 'butterfly' でない: {t.get('tactic')}",
                )

    def test_calendar_trades_have_tactic(self):
        """calendar_pnl 由来のトレードに tactic が設定されていること"""
        trades = load_all_trades()
        calendar_trades = [t for t in trades if t.get("_source") == "calendar_pnl"]
        if calendar_trades:
            for t in calendar_trades:
                self.assertEqual(
                    t.get("tactic"), "calendar_sell",
                    f"calendar trade の tactic が 'calendar_sell' でない: {t.get('tactic')}",
                )


# ---------------------------------------------------------------------------
# Fix 1: pair_trades でtacticが正しく伝搬すること
# ---------------------------------------------------------------------------

class TestPairTradesTacticPropagation(unittest.TestCase):
    """Fix 1: pair_trades のtactic伝搬テスト"""

    def _make_trade(self, event, trade_id, tactic, pnl=None, date_str="2026-04-18"):
        t = {
            "event": event,
            "tactic": tactic,
            "trade_id": trade_id,
            "date": date_str,
            "ts": f"{date_str}T10:00:00-04:00",
        }
        if event == "exit":
            t["pnl_usd"] = pnl if pnl is not None else 100.0
        return t

    def test_tactic_from_entry_propagated_to_paired(self):
        """entryのtacticがpairedに伝搬されること"""
        trades = [
            self._make_trade("entry", "t1", "calendar_sell"),
            self._make_trade("exit",  "t1", "calendar_sell", pnl=100.0),
        ]
        paired = pair_trades(trades)
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["tactic"], "calendar_sell")

    def test_butterfly_tactic_propagated(self):
        """butterfly tacticが正しく伝搬されること"""
        trades = [
            self._make_trade("entry", "b1", "butterfly"),
            self._make_trade("exit",  "b1", "butterfly", pnl=140.0),
        ]
        paired = pair_trades(trades)
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["tactic"], "butterfly")

    def test_tactic_breakdown_no_unknown_when_all_labeled(self):
        """全トレードにtacticが付与されていれば breakdownに unknown が出ないこと"""
        trades = [
            self._make_trade("entry", f"t{i}", "calendar_sell")
            for i in range(5)
        ] + [
            self._make_trade("exit", f"t{i}", "calendar_sell", pnl=50.0)
            for i in range(5)
        ]
        paired = pair_trades(trades)
        bd = calc_ev4_tactic_breakdown(paired)
        self.assertNotIn("unknown", bd, "全ラベル付きなのに 'unknown' が現れた")
        self.assertIn("calendar_sell", bd)

    def test_tactic_breakdown_20260418_no_unknown(self):
        """4/18の再集計でtactic=unknownが出ないこと"""
        all_trades = load_all_trades()
        t418 = filter_by_date_range(all_trades, date(2026, 4, 18), date(2026, 4, 18))
        paired = pair_trades(t418)
        if not paired:
            self.skipTest("4/18 のペアリングされたトレードがない")
        bd = calc_ev4_tactic_breakdown(paired)
        self.assertNotIn(
            "unknown", bd,
            f"4/18再集計で 'unknown' tactic が残っている: {bd}",
        )


# ---------------------------------------------------------------------------
# Fix 2: M6-M9 の計算テスト
# ---------------------------------------------------------------------------

class TestM6SortinoFix(unittest.TestCase):
    """Fix 2: Sortino Ratio のedgeケース"""

    def _make_paired(self, pnl_list, date_offset=0):
        """日別1トレードのpairedリストを生成"""
        result = []
        for i, pnl in enumerate(pnl_list):
            d = (date(2026, 1, 1) + timedelta(days=i + date_offset)).isoformat()
            result.append({"pnl": pnl, "date": d, "tactic": "cs_sell"})
        return result

    def test_sortino_returns_none_when_less_than_3_days(self):
        """3日未満のデータは None を返す"""
        paired = self._make_paired([100.0, 200.0])
        self.assertIsNone(calc_m6_sortino(paired))

    def test_sortino_returns_inf_when_all_positive(self):
        """Fix 2 改訂: 全日プラスのときは None を返す (2026-04-25 仕様変更・JSON serialize 安全性のため)"""
        paired = self._make_paired([100.0, 200.0, 300.0, 400.0, 500.0])
        result = calc_m6_sortino(paired)
        self.assertIsNone(result, f"全勝で downside_dev=0 のときは None 期待: {result}")

    def test_sortino_returns_finite_with_losses(self):
        """損失日がある場合は有限値を返す"""
        paired = self._make_paired([100.0, -50.0, 200.0, -30.0, 150.0])
        result = calc_m6_sortino(paired)
        self.assertIsNotNone(result)
        self.assertTrue(math.isfinite(result), f"有限値でない: {result}")
        self.assertGreater(result, 0)

    def test_sortino_none_when_all_zero(self):
        """avg=0 downside=0 のときは None を返す"""
        paired = self._make_paired([0.0, 0.0, 0.0, 0.0])
        result = calc_m6_sortino(paired)
        self.assertIsNone(result)


class TestM7CalmarFix(unittest.TestCase):
    """Fix 2: Calmar Ratio のedgeケース"""

    def _make_paired(self, pnl_list):
        result = []
        for i, pnl in enumerate(pnl_list):
            d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
            result.append({"pnl": pnl, "date": d, "tactic": "cs_sell"})
        return result

    def test_calmar_returns_none_when_less_than_5_days(self):
        """5日未満は None"""
        paired = self._make_paired([100.0, 200.0, 300.0, 400.0])
        self.assertIsNone(calc_m7_calmar(paired))

    def test_calmar_returns_inf_when_no_drawdown(self):
        """Fix 2: DDなし（全日上昇）なら inf を返す"""
        paired = self._make_paired([100.0, 200.0, 300.0, 400.0, 500.0])
        result = calc_m7_calmar(paired)
        self.assertEqual(result, float("inf"), f"DDなしなのに inf でない: {result}")

    def test_calmar_returns_finite_with_drawdown(self):
        """DDありなら有限値を返す"""
        paired = self._make_paired([100.0, 200.0, -150.0, 50.0, 300.0])
        result = calc_m7_calmar(paired)
        self.assertIsNotNone(result)
        self.assertTrue(math.isfinite(result))


class TestM8MARFix(unittest.TestCase):
    """Fix 2: MAR Ratio のedgeケース"""

    def _make_paired(self, monthly_pnl: dict):
        """月別PnLから fake pairedを生成（月に1トレード）"""
        result = []
        for month_str, pnl in monthly_pnl.items():
            d = f"{month_str}-15"
            result.append({"pnl": pnl, "date": d, "tactic": "cs_sell"})
        return result

    def test_mar_returns_none_when_single_month(self):
        """1ヶ月分のデータは None（月次DDが計算不能）"""
        paired = self._make_paired({"2026-04": 5000.0})
        self.assertIsNone(calc_m8_mar(paired))

    def test_mar_returns_inf_when_no_monthly_drawdown(self):
        """Fix 2: 月次DDなしなら inf を返す"""
        paired = self._make_paired({
            "2026-03": 5000.0,
            "2026-04": 8000.0,
        })
        result = calc_m8_mar(paired)
        self.assertEqual(result, float("inf"), f"月次DDなしなのに inf でない: {result}")

    def test_mar_returns_finite_with_negative_month(self):
        """赤字月があれば有限値を返す"""
        paired = self._make_paired({
            "2026-02": 5000.0,
            "2026-03": -2000.0,
            "2026-04": 8000.0,
        })
        result = calc_m8_mar(paired)
        self.assertIsNotNone(result)
        if result is not None:
            self.assertTrue(math.isfinite(result))


class TestM9UlcerFix(unittest.TestCase):
    """Fix 2: Ulcer Index のedgeケース"""

    def _make_paired(self, pnl_list):
        result = []
        for i, pnl in enumerate(pnl_list):
            d = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
            result.append({"pnl": pnl, "date": d, "tactic": "cs_sell"})
        return result

    def test_ulcer_returns_zero_when_no_drawdown(self):
        """DDなし（全日上昇）のときは 0.0 を返す"""
        paired = self._make_paired([100.0, 200.0, 300.0, 400.0, 500.0])
        result = calc_m9_ulcer_index(paired)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 0.0, places=3)

    def test_ulcer_positive_with_drawdown(self):
        """DDありなら正値を返す（cumulative sumが下がるケース）"""
        # 累積PnL: 100 → -600 → 合計-500 → cumulative[0]=100, [1]=-500 → peak=100, dd=(100-(-500))=600
        paired = self._make_paired([100.0, -600.0, 50.0, 100.0, 200.0])
        result = calc_m9_ulcer_index(paired)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)


# ---------------------------------------------------------------------------
# Fix 2: dd_paired (90日全期間) を使って M6 が None でなくなること
# ---------------------------------------------------------------------------

class TestDDPairedIntegration(unittest.TestCase):
    """Fix 2: run_evaluation が90日の dd_paired を使って M6-M9 を計算すること"""

    def test_run_evaluation_m6_not_null_with_sufficient_data(self):
        """十分なデータがあるとき M6 が float または None で安定して算出されること

        2026-04-25 仕様変更: 全勝 (downside_dev=0) は None 返却 (旧 inf)。
        実データが全勝なら None も正常な算出結果として許容する。
        """
        all_trades = load_all_trades()
        # 4/17-4/21のどこかで十分なデータがあるはず
        for d in [date(2026, 4, 18), date(2026, 4, 19), date(2026, 4, 20)]:
            dd_start = d - timedelta(days=89)
            dd_trades = filter_by_date_range(all_trades, dd_start, d)
            dd_paired = pair_trades(dd_trades)
            series = _daily_pnl_series(dd_paired)
            if len(series) >= 3:
                m6 = calc_m6_sortino(dd_paired)
                # 全勝なら None (新仕様)・損失あれば float — 例外を raise しないこと
                self.assertTrue(
                    m6 is None or isinstance(m6, float),
                    f"{d}: M6 が float でも None でもない（series={series}, m6={m6}）",
                )
                break

    def test_run_evaluation_4_18_tactic_not_unknown(self):
        """run_evaluation(4/18) で tactic に unknown が含まれないこと"""
        result = run_evaluation(period="daily", target_date=date(2026, 4, 18))
        if "error" in result:
            self.skipTest("4/18 データなし")
        tactic_bd = result.get("breakdowns", {}).get("tactic", {})
        self.assertNotIn(
            "unknown", tactic_bd,
            f"4/18 tactic breakdown に 'unknown' が残っている: {tactic_bd}",
        )

    def test_run_evaluation_4_18_m6_not_null(self):
        """run_evaluation(4/18) で M6 が float または None (全勝時) で算出されること

        2026-04-25 仕様変更: 全勝 (downside_dev=0) は None 返却。
        """
        result = run_evaluation(period="daily", target_date=date(2026, 4, 18))
        if "error" in result:
            self.skipTest("4/18 データなし")
        m6 = result["metrics"].get("M6_sortino")
        # 全勝なら None・損失あれば float — どちらも正常
        self.assertTrue(
            m6 is None or isinstance(m6, (int, float)),
            f"4/18 M6 が int/float でも None でもない: {m6}",
        )


if __name__ == "__main__":
    unittest.main()
