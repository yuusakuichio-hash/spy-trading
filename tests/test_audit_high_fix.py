"""tests/test_audit_high_fix.py — Red Team AUDIT HIGH 前半7件 回帰テスト

H-1: _recent_orders ファイル永続化
H-2: pre_trade_check ctx 破壊的変更禁止
H-3: APIRecorder args_hash 照合
H-4: APIRecorder _make_serializable 型保全
H-5: atomic write (_atomic_json_write)
H-6: SymbolSelector ivr=None → 候補除外
H-7: EarningsEngine 信頼性ペナルティ
"""
from __future__ import annotations
import copy
import datetime
import json
import os
import sys
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))


# ── H-1: _recent_orders ファイル永続化 ───────────────────────────────────────

class TestH1RecentOrdersPersist(unittest.TestCase):
    """H-1: 発注頻度トラッキングがファイルに write-through されること"""

    def test_save_and_load_roundtrip(self):
        """_save_recent_orders → _load_recent_orders で復元できる"""
        import importlib
        import common.pre_trade_check as ptc

        with tempfile.TemporaryDirectory() as td:
            test_file = Path(td) / "recent_orders.json"
            # モンキーパッチでファイルパスを差し替え
            original_file = ptc._RECENT_ORDERS_FILE
            ptc._RECENT_ORDERS_FILE = test_file
            try:
                # 現在時刻のエントリーを追加
                now = datetime.datetime.now()
                ptc._recent_orders.clear()
                ptc._recent_orders.append(now)
                ptc._recent_keys.clear()
                ptc._recent_keys.append(("US.SPY", 560.0, "SELL"))

                ptc._save_recent_orders()
                self.assertTrue(test_file.exists(), "recent_orders.json が作成されていない")

                # クリアしてから復元
                ptc._recent_orders.clear()
                ptc._recent_keys.clear()
                ptc._load_recent_orders()

                self.assertGreater(len(ptc._recent_orders), 0, "orders が復元されていない")
            finally:
                ptc._RECENT_ORDERS_FILE = original_file
                ptc._recent_orders.clear()
                ptc._recent_keys.clear()

    def test_expired_entries_excluded(self):
        """TTL 超過エントリーは復元時に除外される"""
        import common.pre_trade_check as ptc

        with tempfile.TemporaryDirectory() as td:
            test_file = Path(td) / "recent_orders.json"
            # 古い timestamp を直接書き込む（TTL=120秒を超える古さ）
            old_ts = datetime.datetime.now().timestamp() - 300  # 5分前
            data = {
                "saved_at": datetime.datetime.now().isoformat(),
                "orders": [{"ts": old_ts, "expiry": "2000-01-01T00:00:00"}],
                "keys": [["US.SPY", 560.0, "SELL"]],
            }
            test_file.write_text(json.dumps(data))

            original_file = ptc._RECENT_ORDERS_FILE
            ptc._RECENT_ORDERS_FILE = test_file
            try:
                ptc._recent_orders.clear()
                ptc._load_recent_orders()
                # 古いエントリーは除外される
                self.assertEqual(len(ptc._recent_orders), 0, "期限切れエントリーが除外されていない")
            finally:
                ptc._RECENT_ORDERS_FILE = original_file


# ── H-2: ctx 破壊的変更禁止 ──────────────────────────────────────────────────

class TestH2CtxImmutability(unittest.TestCase):
    """H-2: check_order が呼び出し元の ctx.est_margin を変更しないこと"""

    def test_ctx_not_mutated(self):
        """check_order 後に元の ctx.est_margin が変わっていないこと"""
        import common.pre_trade_check as ptc
        from common.pre_trade_check import OrderContext

        ctx = OrderContext(
            symbol="US.SPY", strike=560.0, side="SELL",
            qty=1, option_price=0.50,
            bid=0.48, ask=0.52,
            est_margin=5000.0, capital_usd=10000.0,
            open_positions=0, open_margin_total=0.0,
            symbol_margin=0.0, paper=True,
        )
        original_margin = ctx.est_margin

        # QCM をモックして scale=0.5 を返す（est_margin が変わる操作）
        mock_qcm = MagicMock()
        mock_qcm.allow_new_entry.return_value = True
        mock_qcm.margin_scale.return_value = 0.5
        mock_qcm.get_level.return_value = 1

        with patch("common.pre_trade_check._QCM_AVAILABLE", True), \
             patch("common.pre_trade_check._qcm_get", return_value=mock_qcm):
            try:
                ptc.check_order(ctx)
            except Exception:
                pass

        self.assertEqual(
            ctx.est_margin, original_margin,
            f"ctx.est_margin が変更された: {original_margin} → {ctx.est_margin}"
        )


# ── H-3: APIRecorder args_hash 照合 ──────────────────────────────────────────

class TestH3ApiRecorderArgsHash(unittest.TestCase):
    """H-3: REPLAY 時に args_hash が不一致なら ReplayMethodMismatchError を送出"""

    def setUp(self):
        from common.api_recorder import APIRecorder
        self.tmpdir = tempfile.mkdtemp()
        self.recorder = APIRecorder(record_dir=Path(self.tmpdir))

    def tearDown(self):
        self.recorder.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_matching_hash_ok(self):
        """同じ args → replay 成功"""
        from common.api_recorder import ReplayMethodMismatchError
        path = self.recorder.start_record("test_match")
        real_fn = lambda x: x * 2
        result = self.recorder.call("double", real_fn, 42)
        self.assertEqual(result, 84)
        self.recorder.stop()

        self.recorder.start_replay(path)
        result2 = self.recorder.call("double", real_fn, 42)
        self.assertEqual(result2, 84)

    def test_method_mismatch_raises(self):
        """異なるメソッド名 → ReplayMethodMismatchError"""
        from common.api_recorder import ReplayMethodMismatchError
        path = self.recorder.start_record("test_method_mismatch")
        real_fn = lambda x: x
        self.recorder.call("method_a", real_fn, 1)
        self.recorder.stop()

        self.recorder.start_replay(path)
        with self.assertRaises(ReplayMethodMismatchError):
            self.recorder.call("method_b", real_fn, 1)

    def test_args_mismatch_raises(self):
        """同じメソッド名・異なる args → ReplayMethodMismatchError"""
        from common.api_recorder import ReplayMethodMismatchError
        path = self.recorder.start_record("test_args_mismatch")
        real_fn = lambda x: x
        self.recorder.call("get_price", real_fn, "US.SPY")
        self.recorder.stop()

        self.recorder.start_replay(path)
        with self.assertRaises(ReplayMethodMismatchError):
            self.recorder.call("get_price", real_fn, "US.QQQ")  # args が違う


# ── H-4: APIRecorder _make_serializable 型保全 ────────────────────────────────

class TestH4MakeSerializable(unittest.TestCase):
    """H-4: enum/非シリアライズ型が型情報を失わずに記録されること"""

    def setUp(self):
        from common.api_recorder import _make_serializable
        self._fn = _make_serializable

    def test_enum_preserves_type_info(self):
        """Enum → {"__type__": "enum", "qualname": ..., "name": ..., "value": ...}"""
        class Color(Enum):
            RED = 1
        result = self._fn(Color.RED)
        self.assertEqual(result.get("__type__"), "enum")
        self.assertIn("qualname", result)
        self.assertEqual(result["name"], "RED")
        self.assertEqual(result["value"], 1)

    def test_str_not_fallback_for_enum(self):
        """Enum が単純な str に変換されていないこと（型情報が失われていない）"""
        class Status(Enum):
            OPEN = "open"
        result = self._fn(Status.OPEN)
        self.assertIsInstance(result, dict, "Enum が dict ではなく str/他に変換された")
        self.assertEqual(result.get("__type__"), "enum")

    def test_datetime_preserved(self):
        """datetime → {"__type__": "datetime", "iso": ...}"""
        dt = datetime.datetime(2026, 4, 18, 12, 0, 0)
        result = self._fn(dt)
        self.assertEqual(result.get("__type__"), "datetime")
        self.assertIn("2026-04-18", result.get("iso", ""))

    def test_tuple_preserved(self):
        """tuple → {"__type__": "tuple", "items": [...]}"""
        result = self._fn((1, "a", 3.0))
        self.assertEqual(result.get("__type__"), "tuple")
        self.assertEqual(len(result["items"]), 3)

    def test_unknown_type_has_type_tag(self):
        """未知の型 → {"__type__": "unknown", "repr": ...}（str fallback なし）"""
        class Custom:
            def __repr__(self): return "custom_object"
        result = self._fn(Custom())
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("__type__"), "unknown")


# ── H-5: atomic write ────────────────────────────────────────────────────────

class TestH5AtomicWrite(unittest.TestCase):
    """H-5: _atomic_json_write が .tmp + os.replace で書き込むこと"""

    def test_atomic_write_no_tmp_left(self):
        """書き込み後に .tmp ファイルが残らないこと"""
        import spy_bot
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "test.json"
            data = {"trades": [1, 2, 3]}
            spy_bot._atomic_json_write(target, data)
            self.assertTrue(target.exists(), "書き込み先ファイルが存在しない")
            tmp = Path(str(target) + ".tmp")
            self.assertFalse(tmp.exists(), ".tmp ファイルが残存している")

    def test_atomic_write_content(self):
        """書き込み後のファイル内容が正しいこと"""
        import spy_bot
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "pnl.json"
            data = {"trades": [{"pnl": 100}]}
            spy_bot._atomic_json_write(target, data)
            loaded = json.loads(target.read_text())
            self.assertEqual(loaded["trades"][0]["pnl"], 100)

    def test_atomic_write_ensure_ascii_false(self):
        """ensure_ascii=False が正しく動作すること"""
        import spy_bot
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "test_jp.json"
            data = {"msg": "日本語テスト"}
            spy_bot._atomic_json_write(target, data, ensure_ascii=False)
            content = target.read_text(encoding="utf-8")
            self.assertIn("日本語", content)


# ── H-6: SymbolSelector ivr=None → 候補除外 ─────────────────────────────────

class TestH6SymbolSelectorIvrNone(unittest.TestCase):
    """H-6: ivr=None の銘柄が score_symbols で末尾固定（除外扱い）になること"""

    def test_ivr_none_is_excluded(self):
        """ivr=None 銘柄は excluded=True で末尾に来ること"""
        from common.symbol_selector import SymbolMetrics, score_symbols

        metrics = [
            SymbolMetrics("US.SPY",  ivr=65.0, volume_spike_ratio=1.2,
                          gap_abs_pct=0.005, bid_ask_spread_pct=0.001, vix_correlation=0.8),
            SymbolMetrics("US.UNKN", ivr=None, volume_spike_ratio=1.5,
                          gap_abs_pct=0.01,  bid_ask_spread_pct=0.002, vix_correlation=0.7),
            SymbolMetrics("US.QQQ",  ivr=55.0, volume_spike_ratio=1.1,
                          gap_abs_pct=0.004, bid_ask_spread_pct=0.001, vix_correlation=0.9),
        ]
        results = score_symbols(metrics, tactic="credit_spread", earnings_exclude=False)

        # ivr=None の銘柄は除外扱い
        unkn = next((r for r in results if r.symbol == "US.UNKN"), None)
        self.assertIsNotNone(unkn, "US.UNKN が結果に存在しない")
        self.assertTrue(unkn.excluded, "US.UNKN が excluded=True になっていない")
        self.assertEqual(unkn.exclude_reason, "ivr_unavailable")

    def test_ivr_none_not_at_top(self):
        """ivr=None 銘柄が上位にこないこと（fail-open 防止）"""
        from common.symbol_selector import SymbolMetrics, score_symbols

        metrics = [
            SymbolMetrics("US.SPY", ivr=50.0, volume_spike_ratio=1.0,
                          gap_abs_pct=0.005, bid_ask_spread_pct=0.001, vix_correlation=0.8),
            SymbolMetrics("US.UNKN", ivr=None, volume_spike_ratio=2.0,
                          gap_abs_pct=0.02, bid_ask_spread_pct=0.0005, vix_correlation=0.9),
        ]
        results = score_symbols(metrics, tactic="credit_spread", earnings_exclude=False)
        # 最上位は ivr=None でないこと
        self.assertNotEqual(results[0].symbol, "US.UNKN",
                            "ivr=None 銘柄が最上位にいる（fail-open 発生）")


# ── H-7: EarningsEngine 信頼性ペナルティ ─────────────────────────────────────

class TestH7EarningsEngineSizePenalty(unittest.TestCase):
    """H-7: 履歴3件未満/dict外symbol の size_factor がペナルティ適用されること"""

    def setUp(self):
        from common.earnings_engine import EarningsEngine
        self.eng = EarningsEngine(api_key="test")

    def test_known_symbol_with_history_no_penalty(self):
        """履歴3件以上の既知銘柄 → ペナルティなし"""
        sym = "NVDA"
        self.eng._history[sym] = [
            {"actual_crush": 0.40, "ts": "2026-01-01"},
            {"actual_crush": 0.38, "ts": "2026-01-02"},
            {"actual_crush": 0.42, "ts": "2026-01-03"},
        ]
        crush = self.eng._get_iv_crush_rate(sym)
        size  = self.eng._calc_size_factor(crush, symbol=sym)
        # ペナルティなし: SIZE_FACTOR_HIGH = 1.2
        from common.earnings_engine import SIZE_FACTOR_HIGH
        self.assertAlmostEqual(size, SIZE_FACTOR_HIGH, places=2)

    def test_unknown_symbol_no_history_penalty(self):
        """履歴なし + dict外symbol → size × 0.5 ペナルティ"""
        sym = "UNKNOWN_SYM_XYZ"
        # 履歴なし・dict外
        self.eng._history.pop(sym, None)
        crush = self.eng._get_iv_crush_rate(sym)
        size  = self.eng._calc_size_factor(crush, symbol=sym)

        # _DEFAULT_CRUSH_RATE(0.28) → base=SIZE_FACTOR_LOW(0.7) × 0.5 = 0.35
        from common.earnings_engine import SIZE_FACTOR_LOW
        expected = round(SIZE_FACTOR_LOW * 0.5, 2)
        self.assertAlmostEqual(size, expected, places=2,
                               msg=f"期待値 {expected}, 実際 {size}")

    def test_insufficient_history_penalty(self):
        """履歴1-2件のみ → size × 0.7 ペナルティ"""
        sym = "NVDA"
        self.eng._history[sym] = [
            {"actual_crush": 0.40, "ts": "2026-01-01"},
            {"actual_crush": 0.38, "ts": "2026-01-02"},
            # 3件未満
        ]
        crush = self.eng._get_iv_crush_rate(sym)
        size  = self.eng._calc_size_factor(crush, symbol=sym)

        # dict に存在するので 0.5 ではなく 0.7 ペナルティ
        from common.earnings_engine import SIZE_FACTOR_HIGH
        expected = round(SIZE_FACTOR_HIGH * 0.7, 2)
        self.assertAlmostEqual(size, expected, places=2,
                               msg=f"期待値 {expected}, 実際 {size}")

    def test_size_never_below_min(self):
        """どんな状況でも size_factor >= 0.1（最低保証）"""
        sym = "XYZ_NOVEL"
        self.eng._history.pop(sym, None)
        crush = 0.10  # 最低crush rate
        size = self.eng._calc_size_factor(crush, symbol=sym)
        self.assertGreaterEqual(size, 0.1, "size_factor が 0.1 を下回っている")


if __name__ == "__main__":
    unittest.main(verbosity=2)
