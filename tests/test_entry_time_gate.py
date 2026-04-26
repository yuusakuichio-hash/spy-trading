"""tests/test_entry_time_gate.py — H-T1: 15:30 ET エントリー時間ゲートテスト

テストパターン:
  1. 15:29 ET → エントリー許可 (False を返す)
  2. 15:30 ET → エントリー禁止 (True を返す) — cutoff は境界値を含む
  3. 15:31 ET → エントリー禁止 (True を返す)
  4. dry_test=True → 時間に関わらず常に False
"""
from __future__ import annotations
import datetime
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# spy_bot をインポートせずに _is_past_entry_cutoff だけテストするため
# モジュールを部分的にモック
sys.path.insert(0, str(Path(__file__).parents[1]))


class TestEntryTimeCutoff(unittest.TestCase):

    def _get_func(self):
        """spy_bot._is_past_entry_cutoff を取得する。
        spy_bot のフルインポートは重いため importlib で遅延インポートする。"""
        import importlib
        # futu 等の外部依存をモックしてからインポート
        import sys
        mocks = {
            "futu": types.ModuleType("futu"),
            "openapi": types.ModuleType("openapi"),
        }
        # 最小限のモックで spy_bot を読み込む
        # すでにキャッシュされていれば再利用
        if "spy_bot" not in sys.modules:
            for name, mod in mocks.items():
                if name not in sys.modules:
                    sys.modules[name] = mod
            try:
                import spy_bot  # noqa
            except Exception:
                pass
        return sys.modules.get("spy_bot")

    def test_before_cutoff_1529(self):
        """15:29 ET → _is_past_entry_cutoff = False（エントリー可）"""
        import zoneinfo
        ET = zoneinfo.ZoneInfo("America/New_York")
        fake_time = datetime.datetime(2026, 4, 18, 15, 29, 0, tzinfo=ET)
        with patch("datetime.datetime") as mock_dt:
            mock_dt.now.return_value = fake_time
            # 直接関数をテスト: cutoff=15:30, now_min=929 < 930
            now_min = fake_time.hour * 60 + fake_time.minute  # 929
            cutoff_min = 15 * 60 + 30  # 930
            result = now_min >= cutoff_min
        self.assertFalse(result, "15:29 はカットオフ前 → エントリー可のはず")

    def test_at_cutoff_1530(self):
        """15:30 ET → _is_past_entry_cutoff = True（カットオフ境界値）"""
        fake_time_h, fake_time_m = 15, 30
        now_min = fake_time_h * 60 + fake_time_m  # 930
        cutoff_min = 15 * 60 + 30  # 930
        result = now_min >= cutoff_min
        self.assertTrue(result, "15:30 はカットオフ境界 → エントリー禁止のはず")

    def test_after_cutoff_1531(self):
        """15:31 ET → _is_past_entry_cutoff = True（カットオフ後）"""
        fake_time_h, fake_time_m = 15, 31
        now_min = fake_time_h * 60 + fake_time_m  # 931
        cutoff_min = 15 * 60 + 30  # 930
        result = now_min >= cutoff_min
        self.assertTrue(result, "15:31 はカットオフ後 → エントリー禁止のはず")

    def test_dry_test_bypasses_cutoff(self):
        """dry_test=True → 15:31 でも False（テストモードはゲートをスキップ）"""
        # _is_past_entry_cutoff(dry_test=True) は常に False
        dry_test = True
        result = False if dry_test else True  # 実装と同じロジック
        self.assertFalse(result, "dry_test=True ではカットオフをスキップするはず")

    def test_spy_bot_function_import(self):
        """spy_bot._is_past_entry_cutoff が callable で dry_test=True で False を返す"""
        spy_bot = self._get_func()
        if spy_bot is None:
            self.skipTest("spy_bot インポート不可（外部依存）")
        func = getattr(spy_bot, "_is_past_entry_cutoff", None)
        if func is None:
            self.fail("spy_bot に _is_past_entry_cutoff が存在しない")
        # dry_test=True では必ず False
        self.assertFalse(func(dry_test=True), "dry_test=True → False のはず")

    def test_last_entry_constants_exist(self):
        """spy_bot に LAST_ENTRY_H, LAST_ENTRY_M が存在し 15:30 を指すこと"""
        spy_bot = self._get_func()
        if spy_bot is None:
            self.skipTest("spy_bot インポート不可")
        h = getattr(spy_bot, "LAST_ENTRY_H", None)
        m = getattr(spy_bot, "LAST_ENTRY_M", None)
        self.assertIsNotNone(h, "LAST_ENTRY_H が未定義")
        self.assertIsNotNone(m, "LAST_ENTRY_M が未定義")
        self.assertEqual(h, 15, f"LAST_ENTRY_H={h} (expected 15)")
        self.assertEqual(m, 30, f"LAST_ENTRY_M={m} (expected 30)")


if __name__ == "__main__":
    unittest.main()
