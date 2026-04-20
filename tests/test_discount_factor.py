"""割引率フィードバック機構 テスト

テスト対象:
  - record_discount_factor()   : spy_bot.py の exit フック
  - load_records() / calc_stats(): scripts/calc_discount_factor.py
  - マイルストーン閾値判定ロジック
  - append_pnl_entry から record_discount_factor が呼ばれること（統合）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

import pytest

# プロジェクトルートを sys.path に追加
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_exit_record(
    trade_id: str = "tid-001",
    tactic: str = "cs_sell",
    bt_expected_pnl: float = 100.0,
    actual_pnl: float = 60.0,
    entry_ts: str = "2026-04-20T09:30:00-04:00",
    exit_ts: str  = "2026-04-20T15:50:00-04:00",
) -> dict:
    return {
        "event": "exit",
        "trade_id": trade_id,
        "tactic": tactic,
        "bt_expected_pnl": bt_expected_pnl,
        "pnl_usd": actual_pnl,
        "entry_ts": entry_ts,
        "ts": exit_ts,
    }


# ---------------------------------------------------------------------------
# record_discount_factor のテスト
# ---------------------------------------------------------------------------

class TestRecordDiscountFactor:
    """spy_bot.record_discount_factor() の単体テスト。"""

    def _import_func(self, tmp_path: Path):
        """DISCOUNT_LOG_FILE を tmp_path に差し替えてインポート。"""
        import importlib
        import spy_bot as sb
        orig = sb.DISCOUNT_LOG_FILE
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            yield sb.record_discount_factor, sb.DISCOUNT_LOG_FILE
        finally:
            sb.DISCOUNT_LOG_FILE = orig

    def test_writes_correct_schema(self, tmp_path):
        """正常ケース: 必須フィールドが JSONL に書き込まれること。"""
        import spy_bot as sb
        orig_path = sb.DISCOUNT_LOG_FILE
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = _make_exit_record(bt_expected_pnl=100.0, actual_pnl=60.0)
            sb.record_discount_factor(rec)
            lines = sb.DISCOUNT_LOG_FILE.read_text().strip().splitlines()
            assert len(lines) == 1
            row = json.loads(lines[0])
            assert row["trade_id"]         == "tid-001"
            assert row["strategy"]         == "cs_sell"
            assert row["bt_expected_pnl"]  == pytest.approx(100.0)
            assert row["actual_pnl"]       == pytest.approx(60.0)
            assert row["ratio"]            == pytest.approx(0.6, abs=0.0001)
            assert "entry_ts" in row
            assert "exit_ts"  in row
        finally:
            sb.DISCOUNT_LOG_FILE = orig_path

    def test_ratio_calculation(self, tmp_path):
        """ratio = actual / bt_expected で計算されること。"""
        import spy_bot as sb
        orig_path = sb.DISCOUNT_LOG_FILE
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = _make_exit_record(bt_expected_pnl=200.0, actual_pnl=110.0)
            sb.record_discount_factor(rec)
            row = json.loads(sb.DISCOUNT_LOG_FILE.read_text().strip())
            assert row["ratio"] == pytest.approx(0.55, abs=0.0001)
        finally:
            sb.DISCOUNT_LOG_FILE = orig_path

    def test_skips_when_bt_expected_zero(self, tmp_path):
        """bt_expected_pnl が 0 の場合はゼロ除算を回避して記録しないこと。"""
        import spy_bot as sb
        orig_path = sb.DISCOUNT_LOG_FILE
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = _make_exit_record(bt_expected_pnl=0.0, actual_pnl=50.0)
            sb.record_discount_factor(rec)
            assert not sb.DISCOUNT_LOG_FILE.exists()
        finally:
            sb.DISCOUNT_LOG_FILE = orig_path

    def test_skips_when_bt_expected_missing(self, tmp_path):
        """bt_expected_pnl キーが存在しない場合は記録しないこと。"""
        import spy_bot as sb
        orig_path = sb.DISCOUNT_LOG_FILE
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = {"event": "exit", "trade_id": "x", "pnl_usd": 50.0, "ts": "2026-04-20T15:50:00"}
            sb.record_discount_factor(rec)
            assert not sb.DISCOUNT_LOG_FILE.exists()
        finally:
            sb.DISCOUNT_LOG_FILE = orig_path

    def test_appends_multiple_records(self, tmp_path):
        """複数回呼び出しで行が追記されること（上書きではない）。"""
        import spy_bot as sb
        orig_path = sb.DISCOUNT_LOG_FILE
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            for i in range(3):
                rec = _make_exit_record(trade_id=f"tid-{i:03d}", bt_expected_pnl=100.0, actual_pnl=50.0 + i * 10)
                sb.record_discount_factor(rec)
            lines = sb.DISCOUNT_LOG_FILE.read_text().strip().splitlines()
            assert len(lines) == 3
        finally:
            sb.DISCOUNT_LOG_FILE = orig_path

    def test_uses_strategy_field_as_fallback(self, tmp_path):
        """tactic がない場合は strategy フィールドを使うこと。"""
        import spy_bot as sb
        orig_path = sb.DISCOUNT_LOG_FILE
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = {
                "event": "exit",
                "trade_id": "tid-x",
                "strategy": "ic_sell",
                "bt_expected_pnl": 100.0,
                "pnl_usd": 80.0,
                "ts": "2026-04-20T15:50:00",
            }
            sb.record_discount_factor(rec)
            row = json.loads(sb.DISCOUNT_LOG_FILE.read_text().strip())
            assert row["strategy"] == "ic_sell"
        finally:
            sb.DISCOUNT_LOG_FILE = orig_path

    def test_does_not_raise_on_io_error(self, tmp_path):
        """ファイル書き込みエラーが発生しても例外を上げないこと（安全失敗）。"""
        import spy_bot as sb
        orig_path = sb.DISCOUNT_LOG_FILE
        # 存在しない深いパスに設定（親ディレクトリ作成は内部でやるが、open失敗をモック）
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = _make_exit_record()
            with mock.patch("builtins.open", side_effect=OSError("disk full")):
                sb.record_discount_factor(rec)  # 例外が上がらないことを確認
        finally:
            sb.DISCOUNT_LOG_FILE = orig_path


# ---------------------------------------------------------------------------
# calc_discount_factor.py のテスト
# ---------------------------------------------------------------------------

class TestCalcDiscountFactor:
    """scripts/calc_discount_factor.py の load_records / calc_stats テスト。"""

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_load_records_empty(self, tmp_path):
        from calc_discount_factor import load_records, DISCOUNT_LOG_FILE
        import calc_discount_factor as cdf
        orig = cdf.DISCOUNT_LOG_FILE
        cdf.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rows = load_records()
            assert rows == []
        finally:
            cdf.DISCOUNT_LOG_FILE = orig

    def test_load_records_ignores_bad_json(self, tmp_path):
        import calc_discount_factor as cdf
        orig = cdf.DISCOUNT_LOG_FILE
        f = tmp_path / "discount_factor_log.jsonl"
        f.write_text('{"ratio": 0.6}\n{BAD JSON}\n{"ratio": 0.7}\n')
        cdf.DISCOUNT_LOG_FILE = f
        try:
            rows = cdf.load_records()
            assert len(rows) == 2
        finally:
            cdf.DISCOUNT_LOG_FILE = orig

    def test_calc_stats_empty(self):
        from calc_discount_factor import calc_stats
        stats = calc_stats([])
        assert stats["total_count"] == 0
        assert stats["overall"] is None

    def test_calc_stats_single_row(self):
        from calc_discount_factor import calc_stats
        rows = [{"ratio": 0.6, "strategy": "cs_sell"}]
        stats = calc_stats(rows)
        assert stats["total_count"] == 1
        assert stats["overall"]["median"] == pytest.approx(0.6)
        assert stats["overall"]["mean"]   == pytest.approx(0.6)
        assert "cs_sell" in stats["by_strategy"]

    def test_calc_stats_multiple_rows(self):
        from calc_discount_factor import calc_stats
        rows = [
            {"ratio": 0.5, "strategy": "cs_sell"},
            {"ratio": 0.6, "strategy": "cs_sell"},
            {"ratio": 0.7, "strategy": "ic_sell"},
        ]
        stats = calc_stats(rows)
        assert stats["total_count"] == 3
        # 全体中央値は [0.5, 0.6, 0.7] の中央 = 0.6
        assert stats["overall"]["median"] == pytest.approx(0.6)
        assert "cs_sell" in stats["by_strategy"]
        assert "ic_sell" in stats["by_strategy"]
        assert stats["by_strategy"]["cs_sell"]["count"] == 2
        assert stats["by_strategy"]["ic_sell"]["count"] == 1

    def test_calc_stats_skips_none_ratio(self):
        from calc_discount_factor import calc_stats
        rows = [
            {"ratio": 0.5, "strategy": "cs_sell"},
            {"ratio": None, "strategy": "cs_sell"},
            {"strategy": "cs_sell"},  # ratio キーなし
        ]
        stats = calc_stats(rows)
        assert stats["total_count"] == 1

    def test_threshold_upper_triggers_notify(self):
        """割引率 >= 0.55 で priority=0 通知が発火すること。"""
        import calc_discount_factor as cdf
        with mock.patch.object(cdf, "send_pushover") as mock_push:
            cdf.check_milestones(total=15, overall_median=0.60)
            calls = [c.args for c in mock_push.call_args_list]
            priorities = [c[2] if len(c) > 2 else mock_push.call_args_list[i].kwargs.get("priority", 0)
                          for i, c in enumerate(calls)]
            # 上方修正通知が含まれること
            titles = [c.args[0] for c in mock_push.call_args_list]
            assert any("上方修正" in t for t in titles)

    def test_threshold_lower_triggers_priority1(self):
        """割引率 <= 0.35 で priority=1 通知が発火すること。"""
        import calc_discount_factor as cdf
        with mock.patch.object(cdf, "send_pushover") as mock_push:
            cdf.check_milestones(total=15, overall_median=0.30)
            titles = [c.args[0] for c in mock_push.call_args_list]
            assert any("下方修正" in t for t in titles)
            # priority=1 で呼ばれた呼び出しがあること
            all_calls = mock_push.call_args_list
            priorities = []
            for c in all_calls:
                p = c.kwargs.get("priority")
                if p is None and len(c.args) >= 3:
                    p = c.args[2]
                if p is not None:
                    priorities.append(p)
            assert 1 in priorities

    def test_milestone_10_triggers_notify(self):
        """10件マイルストーンで通知が発火すること。"""
        import calc_discount_factor as cdf
        with mock.patch.object(cdf, "send_pushover") as mock_push:
            cdf.check_milestones(total=10, overall_median=0.50)
            assert mock_push.called


# ---------------------------------------------------------------------------
# 統合テスト: append_pnl_entry -> record_discount_factor フック
# ---------------------------------------------------------------------------

class TestAppendPnlEntryHook:
    """append_pnl_entry から record_discount_factor が呼ばれる統合テスト。"""

    def test_hook_called_on_exit_with_bt_expected(self, tmp_path):
        """exit イベントに bt_expected_pnl があれば record_discount_factor が呼ばれること。"""
        import spy_bot as sb
        orig_pnl  = sb.PNL_FILE
        orig_disc = sb.DISCOUNT_LOG_FILE
        sb.PNL_FILE          = tmp_path / "condor_pnl.json"
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = _make_exit_record(bt_expected_pnl=100.0, actual_pnl=55.0)
            with mock.patch.object(sb, "record_discount_factor", wraps=sb.record_discount_factor) as mock_df:
                sb.append_pnl_entry(rec)
                mock_df.assert_called_once()
        finally:
            sb.PNL_FILE          = orig_pnl
            sb.DISCOUNT_LOG_FILE = orig_disc

    def test_hook_not_called_without_bt_expected(self, tmp_path):
        """bt_expected_pnl がない exit では record_discount_factor が呼ばれないこと。"""
        import spy_bot as sb
        orig_pnl  = sb.PNL_FILE
        orig_disc = sb.DISCOUNT_LOG_FILE
        sb.PNL_FILE          = tmp_path / "condor_pnl.json"
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = {"event": "exit", "trade_id": "tid-x", "pnl_usd": 50.0}
            with mock.patch.object(sb, "record_discount_factor", wraps=sb.record_discount_factor) as mock_df:
                sb.append_pnl_entry(rec)
                mock_df.assert_not_called()
        finally:
            sb.PNL_FILE          = orig_pnl
            sb.DISCOUNT_LOG_FILE = orig_disc

    def test_hook_not_called_for_entry_event(self, tmp_path):
        """entry イベントでは record_discount_factor が呼ばれないこと。"""
        import spy_bot as sb
        orig_pnl  = sb.PNL_FILE
        orig_disc = sb.DISCOUNT_LOG_FILE
        sb.PNL_FILE          = tmp_path / "condor_pnl.json"
        sb.DISCOUNT_LOG_FILE = tmp_path / "discount_factor_log.jsonl"
        try:
            rec = {
                "event": "entry",
                "trade_id": "tid-x",
                "bt_expected_pnl": 100.0,
                "net_credit": 1.0,
            }
            with mock.patch.object(sb, "record_discount_factor", wraps=sb.record_discount_factor) as mock_df:
                sb.append_pnl_entry(rec)
                mock_df.assert_not_called()
        finally:
            sb.PNL_FILE          = orig_pnl
            sb.DISCOUNT_LOG_FILE = orig_disc
