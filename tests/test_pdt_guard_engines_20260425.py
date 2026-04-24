"""tests/test_pdt_guard_engines_20260425.py — PDTGuard 単体テスト（22 件）

テスト対象:
  atlas_v3/bots/engines/pdt_guard.py

カバー範囲:
  [group A] paper_mode: PDT 非対象で常に allowed=True
  [group B] live_mode + $25K 以上: 常に allowed=True
  [group C] live_mode + $25K 未満 + rolling < 3: True
  [group D] live_mode + $25K 未満 + rolling >= 3: False + reason 文字列
  [group E] check_can_trade_with_count: predicted_count 込み判定
  [group F] earnings straddle 翌日クローズ → 同日 count 対象
  [group G] 境界値・エッジケース

各テストは独立 tmp PDTTracker を使用しファイルシステムを汚染しない。
"""
from __future__ import annotations

import datetime
import sys
import tempfile
from pathlib import Path

import pytest

# プロジェクトルートを sys.path に追加
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zoneinfo
ET = zoneinfo.ZoneInfo("America/New_York")

from common.pdt_tracker import PDTTracker, PDT_LIMIT, PDT_THRESHOLD_USD
from atlas_v3.bots.engines.pdt_guard import PDTGuard, PDTCheckResult


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _et(year: int, month: int, day: int, hour: int = 10, minute: int = 30) -> datetime.datetime:
    """ET aware datetime を生成する。"""
    return datetime.datetime(year, month, day, hour, minute, 0, tzinfo=ET)


def _make_tracker_with_trades(n_trades: int, base_date: datetime.date) -> PDTTracker:
    """一時 JSONL に n_trades 件の day_trade を記録した PDTTracker を返す。

    全取引は base_date の ET 同日 open+close (manual_close) として記録する。
    """
    with tempfile.TemporaryDirectory() as tmp:
        tracker = PDTTracker(data_file=Path(tmp) / "pdt_day_trades.jsonl")
        entry = _et(base_date.year, base_date.month, base_date.day, 10, 0)
        exit_ = _et(base_date.year, base_date.month, base_date.day, 14, 0)
        for _ in range(n_trades):
            tracker.record_round_trip(
                symbol="US.SPY",
                entry_time=entry,
                exit_time=exit_,
                strategy="TEST",
                exit_type="manual_close",
            )
        # パスを保持したままディレクトリが消えてしまうため、
        # 別の永続的 tempdir を使う実装に変更
        raise RuntimeError("use _make_tracker_persistent instead")


def _make_tracker_persistent(n_trades: int, base_date: datetime.date, tmp_path: Path) -> PDTTracker:
    """pytest tmp_path を使って n_trades 件を記録した PDTTracker を返す。"""
    tracker = PDTTracker(data_file=tmp_path / "pdt_day_trades.jsonl")
    entry = _et(base_date.year, base_date.month, base_date.day, 10, 0)
    exit_ = _et(base_date.year, base_date.month, base_date.day, 14, 0)
    for _ in range(n_trades):
        tracker.record_round_trip(
            symbol="US.SPY",
            entry_time=entry,
            exit_time=exit_,
            strategy="TEST",
            exit_type="manual_close",
        )
    return tracker


def _empty_tracker(tmp_path: Path) -> PDTTracker:
    """取引記録のない空 PDTTracker を返す。"""
    return PDTTracker(data_file=tmp_path / "pdt_day_trades.jsonl")


# 基準日（月曜日 = 平日営業日）
_BASE_DATE = datetime.date(2026, 4, 21)  # 月曜日


# ---------------------------------------------------------------------------
# [group A] paper_mode=True — PDT チェックスキップ
# ---------------------------------------------------------------------------

class TestPaperMode:
    """paper_mode=True の場合は rolling 件数・資金量に関わらず常に allowed=True。"""

    def test_paper_mode_empty_tracker(self, tmp_path: Path) -> None:
        """A-01: paper_mode=True + 取引なし → allowed=True"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=True, capital_usd=5000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True

    def test_paper_mode_2_trades(self, tmp_path: Path) -> None:
        """A-02: paper_mode=True + rolling 2 件 → allowed=True"""
        tracker = _make_tracker_persistent(2, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=True, capital_usd=5000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True

    def test_paper_mode_3_trades(self, tmp_path: Path) -> None:
        """A-03: paper_mode=True + rolling 3 件（上限）→ allowed=True（paper は無視）"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=True, capital_usd=5000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True

    def test_paper_mode_result_fields(self, tmp_path: Path) -> None:
        """A-04: paper_mode=True の result は paper_mode=True、rolling5_count=0 を返す"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=True, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.QQQ", _BASE_DATE)
        assert result.paper_mode is True
        assert result.rolling5_count == 0
        assert result.pdt_remaining == float("inf")

    def test_paper_mode_high_capital(self, tmp_path: Path) -> None:
        """A-05: paper_mode=True + capital >= 25000 → allowed=True（どちらの条件も True）"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=True, capital_usd=30000.0, tracker=tracker)
        result = guard.check_can_trade("US.IWM", _BASE_DATE)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# [group B] live_mode + capital >= $25K — PDT 非対象
# ---------------------------------------------------------------------------

class TestLiveModeHighCapital:
    """live_mode + capital >= $25K は rolling 件数に関わらず allowed=True。"""

    def test_live_25k_exact_boundary(self, tmp_path: Path) -> None:
        """B-01: capital=25000 丁度 → allowed=True（境界値）"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=PDT_THRESHOLD_USD, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True
        assert result.pdt_remaining == float("inf")

    def test_live_50k_3_trades(self, tmp_path: Path) -> None:
        """B-02: capital=50000 + rolling 3 件 → allowed=True"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=50000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True

    def test_live_high_capital_reason_contains_unlimited(self, tmp_path: Path) -> None:
        """B-03: $25K 超 → reason に PDT 非対象の文言を含む"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=100000.0, tracker=tracker)
        result = guard.check_can_trade("US.AAPL", _BASE_DATE)
        assert "非対象" in result.reason or "unlimited" in result.reason.lower()


# ---------------------------------------------------------------------------
# [group C] live_mode + $25K 未満 + rolling < 3 → allowed=True
# ---------------------------------------------------------------------------

class TestLiveModeLowCapitalAllowed:
    """live_mode + capital < $25K で PDT 残枠あり → allowed=True。"""

    def test_live_zero_trades(self, tmp_path: Path) -> None:
        """C-01: rolling=0 → allowed=True remaining=3"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True
        assert result.pdt_remaining == 3

    def test_live_1_trade(self, tmp_path: Path) -> None:
        """C-02: rolling=1 → allowed=True remaining=2"""
        tracker = _make_tracker_persistent(1, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True
        assert result.pdt_remaining == 2

    def test_live_2_trades(self, tmp_path: Path) -> None:
        """C-03: rolling=2 → allowed=True remaining=1（最後の 1 枠）"""
        tracker = _make_tracker_persistent(2, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is True
        assert result.pdt_remaining == 1

    def test_live_result_fields_populated(self, tmp_path: Path) -> None:
        """C-04: result の rolling5_count と capital_usd が正しく設定される"""
        tracker = _make_tracker_persistent(1, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=9999.0, tracker=tracker)
        result = guard.check_can_trade("US.QQQ", _BASE_DATE)
        assert result.rolling5_count == 1
        assert result.capital_usd == 9999.0
        assert result.paper_mode is False


# ---------------------------------------------------------------------------
# [group D] live_mode + $25K 未満 + rolling >= 3 → allowed=False
# ---------------------------------------------------------------------------

class TestLiveModePDTBlocked:
    """rolling >= 3 は発注を物理ブロックする。"""

    def test_live_3_trades_blocked(self, tmp_path: Path) -> None:
        """D-01: rolling=3 → allowed=False（4 回目の day_trade 阻止）"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is False

    def test_live_3_trades_reason_contains_count(self, tmp_path: Path) -> None:
        """D-02: reason に rolling5 件数を含む"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert "3" in result.reason
        assert result.rolling5_count == 3

    def test_live_4_trades_also_blocked(self, tmp_path: Path) -> None:
        """D-03: rolling=4（既に上限超）→ allowed=False"""
        tracker = _make_tracker_persistent(4, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is False

    def test_live_blocked_pdt_remaining_is_zero(self, tmp_path: Path) -> None:
        """D-04: ブロック時 pdt_remaining=0"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=5000.0, tracker=tracker)
        result = guard.check_can_trade("US.IWM", _BASE_DATE)
        assert result.pdt_remaining == 0


# ---------------------------------------------------------------------------
# [group E] check_can_trade_with_count — predicted_count 込み判定
# ---------------------------------------------------------------------------

class TestCheckWithPredictedCount:
    """predicted_count を使った事前予測判定。"""

    def test_predicted_1_from_zero_allowed(self, tmp_path: Path) -> None:
        """E-01: current=0 + predicted=1 = 1 <= 3 → allowed=True"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade_with_count("US.SPY", predicted_count=1, trade_date=_BASE_DATE)
        assert result.allowed is True

    def test_predicted_3_from_zero_exactly_allowed(self, tmp_path: Path) -> None:
        """E-02: current=0 + predicted=3 = 3 <= 3 → allowed=True（上限ちょうど）"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade_with_count("US.SPY", predicted_count=3, trade_date=_BASE_DATE)
        assert result.allowed is True

    def test_predicted_1_from_3_blocked(self, tmp_path: Path) -> None:
        """E-03: current=3 + predicted=1 = 4 > 3 → allowed=False"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade_with_count("US.SPY", predicted_count=1, trade_date=_BASE_DATE)
        assert result.allowed is False

    def test_predicted_2_from_2_blocked(self, tmp_path: Path) -> None:
        """E-04: current=2 + predicted=2 = 4 > 3 → allowed=False"""
        tracker = _make_tracker_persistent(2, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade_with_count("US.SPY", predicted_count=2, trade_date=_BASE_DATE)
        assert result.allowed is False

    def test_predicted_paper_mode_always_true(self, tmp_path: Path) -> None:
        """E-05: paper_mode=True + predicted=10 → allowed=True"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=True, capital_usd=5000.0, tracker=tracker)
        result = guard.check_can_trade_with_count("US.SPY", predicted_count=10, trade_date=_BASE_DATE)
        assert result.allowed is True


# ---------------------------------------------------------------------------
# [group F] earnings straddle 翌日クローズ → 同日 count 対象
# ---------------------------------------------------------------------------

class TestEarningsStraddleSameDayCount:
    """EarningsStraddleBuy: 前日仕込み翌日クローズは日跨ぎのため PDT 非対象。
    当日仕込み当日クローズは PDT 計上対象（manual_close）。
    guard は「このエントリーが同日 round-trip になるか」の判断を caller に委ねる。
    check_can_trade_with_count で predicted=1 を渡すことで対象として計上する。
    """

    def test_earnings_same_day_roundtrip_predicted(self, tmp_path: Path) -> None:
        """F-01: 当日仕込み当日クローズ予定 → predicted=1 込みで判定"""
        # current=2 + predicted=1 = 3 <= limit → allowed=True（ぎりぎり通過）
        tracker = _make_tracker_persistent(2, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade_with_count(
            "US.NVDA", predicted_count=1, trade_date=_BASE_DATE
        )
        assert result.allowed is True

    def test_earnings_same_day_roundtrip_blocked(self, tmp_path: Path) -> None:
        """F-02: 当日仕込み当日クローズ予定 + current=3 → blocked"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade_with_count(
            "US.NVDA", predicted_count=1, trade_date=_BASE_DATE
        )
        assert result.allowed is False

    def test_earnings_cross_day_not_counted(self, tmp_path: Path) -> None:
        """F-03: 前日仕込み翌日クローズ予定 → predicted=0 で判定（PDT 非対象）"""
        # 前日エントリー・翌日クローズは record_round_trip で計上されない
        # guard 側は predicted=0 を渡すことで「day_trade にならない」ことを表現
        tracker = _make_tracker_persistent(2, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade_with_count(
            "US.NVDA", predicted_count=0, trade_date=_BASE_DATE
        )
        # predicted=0: 2+0=2 <= 3 → allowed
        assert result.allowed is True


# ---------------------------------------------------------------------------
# [group G] 境界値・エッジケース
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """境界値と特殊ケース。"""

    def test_capital_just_below_threshold_blocked(self, tmp_path: Path) -> None:
        """G-01: capital=24999.99（$25K 未満境界値）+ rolling=3 → blocked"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=24999.99, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is False

    def test_capital_zero_rolling_3_blocked(self, tmp_path: Path) -> None:
        """G-02: capital=0 + rolling=3 → blocked（最低資金ケース）"""
        tracker = _make_tracker_persistent(3, _BASE_DATE, tmp_path)
        guard = PDTGuard(paper_mode=False, capital_usd=0.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        assert result.allowed is False

    def test_different_symbols_share_global_count(self, tmp_path: Path) -> None:
        """G-03: PDT カウントは銘柄合算（SPY 2回 + QQQ 1回 = rolling 3）→ blocked"""
        tracker = PDTTracker(data_file=tmp_path / "pdt.jsonl")
        date_ = _BASE_DATE
        entry = _et(date_.year, date_.month, date_.day, 10, 0)
        exit_ = _et(date_.year, date_.month, date_.day, 14, 0)
        tracker.record_round_trip("US.SPY", entry, exit_, "CS", "manual_close")
        tracker.record_round_trip("US.SPY", entry, exit_, "CS", "manual_close")
        tracker.record_round_trip("US.QQQ", entry, exit_, "ORB", "manual_close")

        guard = PDTGuard(paper_mode=False, capital_usd=8000.0, tracker=tracker)
        result = guard.check_can_trade("US.IWM", date_)
        assert result.allowed is False
        assert result.rolling5_count == 3

    def test_pdt_check_result_is_dataclass(self, tmp_path: Path) -> None:
        """G-04: PDTCheckResult が frozen dataclass で不変である"""
        tracker = _empty_tracker(tmp_path)
        guard = PDTGuard(paper_mode=True, capital_usd=0.0, tracker=tracker)
        result = guard.check_can_trade("US.SPY", _BASE_DATE)
        with pytest.raises((AttributeError, TypeError)):
            result.allowed = False  # type: ignore[misc]
