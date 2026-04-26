#!/usr/bin/env python3
"""
tests/test_bot_deviation_detector.py — DeviationDetector 単体テスト

観点A-D 各3ケース（正常/境界/異常）
エスカレーション判定
halt_flag ライフサイクル
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# プロジェクトルートを sys.path に追加
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

# ── テスト用一時ファイルをモジュールロード前に差し替え ────────────────────────
# detector は import 時点でパスを確定するため、env var で一時ディレクトリを指定する
_TMP_DIR = tempfile.mkdtemp(prefix="sora_test_deviation_")
_TMP_EXPECTATIONS = Path(_TMP_DIR) / "strategy_expectations.json"
_TMP_DECISION_LOG = Path(_TMP_DIR) / "decision_log.jsonl"
_TMP_STATE        = Path(_TMP_DIR) / "deviation_detector_state.json"

# ── モジュール内パスをテスト用に差し替え ────────────────────────────────────
import common.bot_deviation_detector as _mod
_mod.EXPECTATIONS_PATH = _TMP_EXPECTATIONS
_mod.DECISION_LOG_PATH = _TMP_DECISION_LOG
_mod.STATE_PATH        = _TMP_STATE

from common.bot_deviation_detector import (  # noqa: E402
    DeviationDetector,
    Deviation,
    _load_state,
    _save_state,
)

# テスト用 expectations
_SAMPLE_EXPECTATIONS = {
    "cs_sell": {
        "bt_monthly_return_pct": 8.0,
        "tolerance_pct": 50,
        "expected_delta_range": [-0.15, 0.15],
        "expected_gamma_range": [-0.005, 0.005],
        "expected_theta_range": [0.0, 5.0],
        "expected_vega_range": [-0.5, 0.0],
        "entry_structure": {"legs": 2, "credit": True, "width_ratio": [0.02, 0.05]},
        "max_fill_latency_sec": 30,
        "max_slippage_pct": 2.0,
    },
    "ic_sell": {
        "bt_monthly_return_pct": 7.0,
        "tolerance_pct": 50,
        "expected_delta_range": [-0.10, 0.10],
        "expected_gamma_range": [-0.003, 0.003],
        "entry_structure": {"legs": 4, "credit": True},
        "max_fill_latency_sec": 30,
        "max_slippage_pct": 2.0,
    },
}


def _setup_expectations():
    """テスト用 expectations ファイルを書き出す。"""
    _TMP_EXPECTATIONS.write_text(json.dumps(_SAMPLE_EXPECTATIONS), encoding="utf-8")


def _fresh_detector() -> DeviationDetector:
    """毎テスト新しい detector インスタンスを返す。"""
    _setup_expectations()
    # state ファイルを初期化
    _TMP_STATE.unlink(missing_ok=True)
    d = DeviationDetector()
    d.reload_expectations()
    return d


# ─────────────────────────────────────────────────────────────────────────────
# 観点A: パフォーマンス乖離
# ─────────────────────────────────────────────────────────────────────────────

class TestPerformanceDeviation(unittest.TestCase):

    def setUp(self):
        self.d = _fresh_detector()

    # A-1: 正常ケース (乖離率 40% < 閾値 50%)
    def test_a1_normal_within_tolerance(self):
        dev = self.d.check_performance_deviation(
            bot_name="atlas",
            tactic="cs_sell",
            realized_pnl=600.0,
            bt_expected_pnl=1000.0,  # -40% 乖離 → 許容内
            trade_count=20,
        )
        self.assertIsNone(dev, "許容内はNoneを返す")

    # A-2: 境界値 (乖離率ちょうど 50% = 境界 = 逸脱なし)
    def test_a2_boundary_exactly_at_tolerance(self):
        dev = self.d.check_performance_deviation(
            bot_name="atlas",
            tactic="cs_sell",
            realized_pnl=500.0,
            bt_expected_pnl=1000.0,  # -50.0% = ギリギリ許容内 (<=)
            trade_count=20,
        )
        self.assertIsNone(dev, "境界値ちょうどはNoneを返す")

    # A-3: 異常ケース (乖離率 -62% > 閾値 50%)
    def test_a3_anomaly_below_tolerance(self):
        dev = self.d.check_performance_deviation(
            bot_name="atlas",
            tactic="cs_sell",
            realized_pnl=380.0,
            bt_expected_pnl=1000.0,  # -62% 乖離
            trade_count=20,
        )
        self.assertIsNotNone(dev)
        self.assertEqual(dev.perspective, "A")
        self.assertIn("下振れ", dev.title)

    # A-4: 件数不足ではスキップ
    def test_a4_skip_insufficient_trades(self):
        dev = self.d.check_performance_deviation(
            bot_name="atlas",
            tactic="cs_sell",
            realized_pnl=0.0,
            bt_expected_pnl=1000.0,
            trade_count=5,  # MIN_TRADES_FOR_PERF_CHECK 未満
        )
        self.assertIsNone(dev, "件数不足はスキップ")

    # A-5: 上振れ乖離 (+80%)
    def test_a5_anomaly_above_tolerance(self):
        dev = self.d.check_performance_deviation(
            bot_name="atlas",
            tactic="cs_sell",
            realized_pnl=1800.0,
            bt_expected_pnl=1000.0,  # +80%
            trade_count=25,
        )
        self.assertIsNotNone(dev)
        self.assertIn("上振れ", dev.title)


# ─────────────────────────────────────────────────────────────────────────────
# 観点B: エントリー構造整合性
# ─────────────────────────────────────────────────────────────────────────────

class TestEntryIntegrity(unittest.TestCase):

    def setUp(self):
        self.d = _fresh_detector()

    def _cs_fills_correct(self):
        """cs_sell 正常 fills: 2レグ, credit"""
        return [
            {"side": "sell", "price": 2.50, "qty": 1, "strike": 500},
            {"side": "buy",  "price": 1.00, "qty": 1, "strike": 495},
        ]

    def _cs_fills_debit(self):
        """cs_sell 誤り: デビットになっている"""
        return [
            {"side": "sell", "price": 0.50, "qty": 1, "strike": 500},
            {"side": "buy",  "price": 2.00, "qty": 1, "strike": 495},
        ]

    def _ic_fills_incomplete(self):
        """ic_sell 誤り: 2レグしかない (ICは4レグ必要)"""
        return [
            {"side": "sell", "price": 2.50, "qty": 1, "strike": 500},
            {"side": "buy",  "price": 1.00, "qty": 1, "strike": 495},
        ]

    # B-1: 正常ケース (cs_sell 2レグ credit)
    def test_b1_normal_cs_correct(self):
        dev = self.d.check_entry_integrity(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD001",
            expected_structure={"legs": 2, "credit": True},
            actual_fills=self._cs_fills_correct(),
        )
        self.assertIsNone(dev)

    # B-2: 境界値 (期待構造を指定しない → expectations.json から自動ロード)
    def test_b2_auto_load_from_expectations(self):
        dev = self.d.check_entry_integrity(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD002",
            expected_structure={},  # 空 → 自動ロード
            actual_fills=self._cs_fills_correct(),
        )
        self.assertIsNone(dev, "expectations.jsonから自動ロードして正常判定")

    # B-3: 異常ケース (cs_sell なのに debit)
    def test_b3_anomaly_wrong_credit_debit(self):
        dev = self.d.check_entry_integrity(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD003",
            expected_structure={"legs": 2, "credit": True},
            actual_fills=self._cs_fills_debit(),
        )
        self.assertIsNotNone(dev)
        self.assertEqual(dev.perspective, "B")
        self.assertIn("Credit/Debit不一致", "\n".join(dev.details.get("violations", [])))

    # B-4: レグ数不一致 (ic_sell 4レグ必要 → 2レグしかない)
    def test_b4_anomaly_wrong_leg_count(self):
        dev = self.d.check_entry_integrity(
            bot_name="atlas",
            tactic="ic_sell",
            order_id="ORD004",
            expected_structure={"legs": 4, "credit": True},
            actual_fills=self._ic_fills_incomplete(),
        )
        self.assertIsNotNone(dev)
        self.assertIn("レグ数不一致", "\n".join(dev.details.get("violations", [])))

    # B-5: verify_ic_structure ショートカット
    def test_b5_verify_ic_shortcut(self):
        dev = self.d.verify_ic_structure(
            bot_name="atlas",
            order_id="ORD005",
            actual_fills=self._cs_fills_correct(),  # 2レグ → IC(4レグ)と不一致
        )
        self.assertIsNotNone(dev)


# ─────────────────────────────────────────────────────────────────────────────
# 観点C: Greeks レンジ監視
# ─────────────────────────────────────────────────────────────────────────────

class TestGreeksRange(unittest.TestCase):

    def setUp(self):
        self.d = _fresh_detector()

    # C-1: 正常ケース (delta 0.05 = cs_sell 範囲内)
    def test_c1_normal_delta_in_range(self):
        dev = self.d.check_greeks_range(
            bot_name="atlas",
            tactic="cs_sell",
            position_id="POS001",
            current_greeks={"delta": 0.05, "gamma": 0.001, "theta": 1.0, "vega": -0.2},
        )
        self.assertIsNone(dev)

    # C-2: 境界値 (delta = -0.15 = 下限ぴったり)
    def test_c2_boundary_delta_at_limit(self):
        dev = self.d.check_greeks_range(
            bot_name="atlas",
            tactic="cs_sell",
            position_id="POS002",
            current_greeks={"delta": -0.15},  # 下限 = 範囲内
        )
        self.assertIsNone(dev, "下限ちょうどはNone")

    # C-3: 異常ケース (IC 売りなのに delta > 0.5 → マーケット急変)
    def test_c3_anomaly_delta_critical(self):
        dev = self.d.check_greeks_range(
            bot_name="atlas",
            tactic="ic_sell",
            position_id="POS003",
            current_greeks={"delta": 0.65},  # ic_sell 範囲 [-0.10, 0.10] を大幅逸脱
        )
        self.assertIsNotNone(dev)
        self.assertEqual(dev.severity, "CRITICAL")
        self.assertEqual(dev.perspective, "C")

    # C-4: gamma のみ逸脱 → WARNING
    def test_c4_warning_gamma_out_of_range(self):
        dev = self.d.check_greeks_range(
            bot_name="atlas",
            tactic="cs_sell",
            position_id="POS004",
            current_greeks={"delta": 0.05, "gamma": 0.010},  # gamma 上限 0.005 超え
        )
        self.assertIsNotNone(dev)
        self.assertEqual(dev.severity, "WARNING")

    # C-5: expected_range を明示指定 (expectations.json を使わない)
    def test_c5_explicit_range(self):
        dev = self.d.check_greeks_range(
            bot_name="atlas",
            tactic="unknown_tactic",
            position_id="POS005",
            current_greeks={"delta": 0.99},
            expected_range={"expected_delta_range": [-0.20, 0.20]},
        )
        self.assertIsNotNone(dev)


# ─────────────────────────────────────────────────────────────────────────────
# 観点D: 発注フロー異常
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutionAnomaly(unittest.TestCase):

    def setUp(self):
        self.d = _fresh_detector()
        self.now = time.time()

    # D-1: 正常ケース (2秒で約定、スリッページ 0%)
    def test_d1_normal_fast_fill(self):
        dev = self.d.check_execution_anomaly(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD_D1",
            submitted_at=self.now,
            filled_at=self.now + 2.0,
            submitted_price=2.50,
            filled_price=2.50,
            status="filled",
        )
        self.assertIsNone(dev)

    # D-2: 境界値 (30秒ぴったり = 閾値ちょうど → 逸脱なし)
    def test_d2_boundary_latency_at_limit(self):
        dev = self.d.check_execution_anomaly(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD_D2",
            submitted_at=self.now,
            filled_at=self.now + 30.0,  # ちょうど30秒 = 許容内
            submitted_price=2.50,
            filled_price=2.50,
            status="filled",
        )
        self.assertIsNone(dev, "閾値ちょうどは正常")

    # D-3: 異常ケース (45秒遅延 = ブローカー問題)
    def test_d3_anomaly_slow_fill(self):
        dev = self.d.check_execution_anomaly(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD_D3",
            submitted_at=self.now,
            filled_at=self.now + 45.0,
            submitted_price=2.50,
            filled_price=2.50,
            status="filled",
        )
        self.assertIsNotNone(dev)
        self.assertEqual(dev.perspective, "D")
        self.assertIn("約定遅延", "\n".join(dev.details.get("violations", [])))

    # D-4: リジェクト = CRITICAL
    def test_d4_anomaly_rejected(self):
        dev = self.d.check_execution_anomaly(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD_D4",
            submitted_at=self.now,
            filled_at=None,
            submitted_price=2.50,
            filled_price=None,
            status="rejected",
        )
        self.assertIsNotNone(dev)
        self.assertEqual(dev.severity, "CRITICAL")

    # D-5: スリッページ超過 (3% > 2% 閾値)
    def test_d5_anomaly_slippage(self):
        dev = self.d.check_execution_anomaly(
            bot_name="atlas",
            tactic="cs_sell",
            order_id="ORD_D5",
            submitted_at=self.now,
            filled_at=self.now + 1.0,
            submitted_price=2.00,
            filled_price=2.06,  # 3% スリッページ
            status="filled",
        )
        self.assertIsNotNone(dev)
        self.assertIn("スリッページ超過", "\n".join(dev.details.get("violations", [])))


# ─────────────────────────────────────────────────────────────────────────────
# アラート・エスカレーション
# ─────────────────────────────────────────────────────────────────────────────

class TestAlertAndEscalation(unittest.TestCase):

    def setUp(self):
        self.d = _fresh_detector()
        # Pushover を mock にして実送信しない
        self.pc_mock = MagicMock()
        self.pc_mock.send.return_value = True
        # _mod._PC_AVAILABLE と _mod._pc を差し替え
        self._orig_pc = getattr(_mod, "_pc", None)
        self._orig_available = _mod._PC_AVAILABLE
        _mod._pc = self.pc_mock
        _mod._PC_AVAILABLE = True

    def tearDown(self):
        _mod._pc = self._orig_pc
        _mod._PC_AVAILABLE = self._orig_available

    def _make_deviation(self, tactic="cs_sell") -> Deviation:
        return Deviation(
            perspective="A",
            bot_name="atlas",
            tactic=tactic,
            severity="WARNING",
            title="[ATLAS/DEV-A] テスト乖離",
            message="テストメッセージ",
        )

    # アラート1回目 → priority=1 (通常)
    def test_alert_first_occurrence(self):
        _mod.ESCALATE_THRESHOLD = 5  # ペーパー想定
        dev = self._make_deviation()
        self.d.alert(dev)
        call_args = self.pc_mock.send.call_args
        self.assertEqual(call_args.kwargs.get("priority", call_args[1].get("priority", 1)), 1)

    # 連続 ESCALATE_THRESHOLD 回でエスカレーション
    def test_escalation_on_repeated_deviation(self):
        _mod.ESCALATE_THRESHOLD = 3  # テスト用に低く設定
        dev = self._make_deviation("ic_sell")
        # 2回まで通常
        self.d.alert(dev)
        self.d.alert(dev)
        # 3回目でエスカレーション
        self.d.alert(dev)
        last_call = self.pc_mock.send.call_args
        priority = last_call.kwargs.get("priority", last_call[1].get("priority", 0))
        self.assertEqual(priority, 2, "3回連続でpriority=2になる")

    # エスカレーション後 halt_flag が True
    def test_halt_flag_set_on_escalation(self):
        _mod.ESCALATE_THRESHOLD = 2
        dev = self._make_deviation("cs_sell")
        self.d.alert(dev)
        self.d.alert(dev)
        self.assertTrue(DeviationDetector.is_halt_flagged())

    # halt_flag クリア
    def test_clear_halt_flag(self):
        _mod.ESCALATE_THRESHOLD = 2
        dev = self._make_deviation("cs_sell")
        self.d.alert(dev)
        self.d.alert(dev)
        self.assertTrue(DeviationDetector.is_halt_flagged())
        DeviationDetector.clear_halt_flag()
        self.assertFalse(DeviationDetector.is_halt_flagged())

    # decision_log に書き込まれる
    def test_decision_log_written(self):
        _TMP_DECISION_LOG.unlink(missing_ok=True)
        dev = self._make_deviation()
        self.d.alert(dev)
        self.assertTrue(_TMP_DECISION_LOG.exists())
        lines = [l for l in _TMP_DECISION_LOG.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["perspective"], "A")
        self.assertIn("ts_jst", entry)

    # 複数 tactic のカウンタは独立
    def test_independent_counters_per_tactic(self):
        _mod.ESCALATE_THRESHOLD = 3
        dev_cs = self._make_deviation("cs_sell")
        dev_ic = self._make_deviation("ic_sell")
        self.d.alert(dev_cs)
        self.d.alert(dev_cs)
        self.d.alert(dev_ic)  # ic_sell は1回目 → escalation なし
        state = _load_state()
        self.assertFalse(state.get("bot_halt_flag", False),
                         "ic_sell 1回だけでは halt_flag はまだ False")


# ─────────────────────────────────────────────────────────────────────────────
# Chronos 戦術 (chronos セクション)
# ─────────────────────────────────────────────────────────────────────────────

class TestChronosTactics(unittest.TestCase):

    def setUp(self):
        # chronos セクション含む expectations を書き出す
        exp = {
            "chronos": {
                "futures_orb": {
                    "bt_monthly_return_pct": 10.0,
                    "tolerance_pct": 50,
                    "max_fill_latency_sec": 5,
                    "max_slippage_pct": 0.1,
                }
            }
        }
        _TMP_EXPECTATIONS.write_text(json.dumps(exp), encoding="utf-8")
        _TMP_STATE.unlink(missing_ok=True)
        self.d = DeviationDetector()
        self.d.reload_expectations()

    # Chronos 戦術の期待値が正しくロードされる
    def test_chronos_tactic_expectation_loaded(self):
        exp = self.d._get_tactic_expectation("futures_orb")
        self.assertEqual(exp.get("bt_monthly_return_pct"), 10.0)

    # Chronos 戦術でのスリッページ検知
    def test_chronos_slippage_detection(self):
        now = time.time()
        dev = self.d.check_execution_anomaly(
            bot_name="chronos",
            tactic="futures_orb",
            order_id="CORD001",
            submitted_at=now,
            filled_at=now + 1.0,
            submitted_price=5000.0,
            filled_price=5006.0,  # 0.12% > 0.1% 閾値
            status="filled",
        )
        self.assertIsNotNone(dev)
        self.assertIn("chronos", dev.bot_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
