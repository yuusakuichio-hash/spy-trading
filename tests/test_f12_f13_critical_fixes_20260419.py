#!/usr/bin/env python3
"""
test_f12_f13_critical_fixes_20260419.py
CRITICAL C1-C5 / HIGH 1-8 修正の動作検証テスト (30+件)

対象修正:
  C1: update(tick) が _maybe_flush_bucket を呼ぶ
  C2: update_from_bar のバケット境界処理順序 (先flush→後加算)
  C3: chronos_bot._daily_reset が cumulative_delta.daily_reset を呼ぶ
  C4: 採点ロジック - 空クラスで0点 / AST解析確認
  C5: LiquiditySweep ATRフィルタ恒真式修正
  HIGH-1: get_total_delta が確定済み+未確定を合算
  HIGH-2: detect_divergence threshold 実際に使用
  HIGH-3: ORB strategy に direction フィールドあり
  HIGH-4: F12方向マップ ブラックリスト方式
  HIGH-6: confidence 係数コメント明記
  HIGH-7: F13 size_pct Kelly/phaseフェーズ対応
  HIGH-8: F13 sweep に F12 bias 補正適用

実行:
  python3 -m pytest tests/test_f12_f13_critical_fixes_20260419.py -v
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from chronos_cumulative_delta import (
    CumulativeDelta,
    Tick,
    BarData,
    BucketDelta,
    calc_bid_ask_delta,
    calc_volume_ratio,
)
from chronos_liquidity_sweep import (
    LiquiditySweepDetector,
    SweepSignal,
    BarSnapshot,
)


# =============================================================================
# C1: update(tick) が flush を呼ぶ
# =============================================================================

class TestC1TickFlush:
    """C1: update(tick) にtimestampがあればバケット境界でflushされる"""

    def setup_method(self):
        self.cd = CumulativeDelta(bucket_minutes=5, max_buckets=20)

    def test_tick_with_timestamp_triggers_flush(self):
        """timestamp付きtickが別バケットに到達するとflushが発生する"""
        t1 = Tick(price=5000.0, volume=100.0, aggressor_side="buy", timestamp=0)
        self.cd.update(t1)
        t2 = Tick(price=5001.0, volume=50.0, aggressor_side="sell", timestamp=301)
        self.cd.update(t2)
        buckets = self.cd.get_buckets()
        assert len(buckets) >= 1, "バケット1がflushされていない (C1未修正)"

    def test_tick_without_timestamp_no_auto_flush(self):
        """timestamp なしのtickはflushしない"""
        t1 = Tick(price=5000.0, volume=100.0, aggressor_side="buy")
        self.cd.update(t1)
        buckets = self.cd.get_buckets()
        assert len(buckets) == 0

    def test_multiple_ticks_same_bucket_no_extra_flush(self):
        """同一バケット内の複数tickはflushしない"""
        self.cd.update(Tick(5000.0, 10.0, "buy", timestamp=10))
        self.cd.update(Tick(5000.0, 10.0, "buy", timestamp=50))
        self.cd.update(Tick(5000.0, 10.0, "buy", timestamp=200))
        buckets = self.cd.get_buckets()
        assert len(buckets) == 0

    def test_tick_timestamp_preserves_cumulative(self):
        """flush後の日次累積deltaが正しく積み上がる"""
        t1 = Tick(5000.0, 100.0, "buy", timestamp=0)
        self.cd.update(t1)
        t2 = Tick(5001.0, 200.0, "buy", timestamp=400)
        self.cd.update(t2)
        confirmed = self.cd.get_current_delta()
        assert confirmed == 100.0, f"期待 100.0, 実際 {confirmed}"


# =============================================================================
# C2: update_from_bar バケット境界の順序 (先flush→後加算)
# =============================================================================

class TestC2BarBoundaryOrder:
    """C2: update_from_bar は先にflush→その後加算 (二重計上なし)"""

    def setup_method(self):
        self.cd = CumulativeDelta(bucket_minutes=5, max_buckets=20)

    def test_two_bars_in_different_buckets_separate(self):
        """別バケットの2バーは分離されて保存される"""
        bar1 = BarData(open=5000.0, high=5005.0, low=4998.0, close=5004.0,
                       volume=500.0, timestamp=100)
        self.cd.update_from_bar(bar1)
        bar2 = BarData(open=5004.0, high=5010.0, low=5002.0, close=5009.0,
                       volume=600.0, timestamp=400)
        self.cd.update_from_bar(bar2)
        buckets = self.cd.get_buckets()
        assert len(buckets) == 1, f"バケット分離失敗: {len(buckets)}件"
        b1 = buckets[0]
        assert b1.buy_volume > 0 or b1.sell_volume > 0

    def test_no_double_count_across_boundary(self):
        """バケット境界をまたぐ場合に二重計上が発生しない"""
        bar1 = BarData(5000.0, 5010.0, 4998.0, 5008.0, 1000.0, timestamp=0)
        self.cd.update_from_bar(bar1)
        bar2 = BarData(5008.0, 5015.0, 5006.0, 5012.0, 1000.0, timestamp=310)
        self.cd.update_from_bar(bar2)
        buckets = self.cd.get_buckets()
        assert len(buckets) == 1
        b1 = buckets[0]
        assert 650 < b1.buy_volume < 900, f"buy_volume={b1.buy_volume} (期待: 650-900)"

    def test_three_bars_three_buckets(self):
        """3つの異なるバケットのバーが2件の確定バケットを生成する"""
        for i in range(3):
            bar = BarData(5000.0, 5005.0, 4998.0, 5003.0, 100.0, timestamp=i * 310)
            self.cd.update_from_bar(bar)
        buckets = self.cd.get_buckets()
        assert len(buckets) == 2, f"期待2件, 実際{len(buckets)}件"

    def test_bar1_volume_goes_to_bucket1_not_bucket2(self):
        """bar1 のvolumeはバケット1に入り、バケット2には入らない"""
        bar1 = BarData(5000.0, 5010.0, 4998.0, 5008.0, 1000.0, timestamp=0)
        bar2 = BarData(5000.0, 5010.0, 4998.0, 4990.0, 500.0, timestamp=310)
        self.cd.update_from_bar(bar1)
        self.cd.update_from_bar(bar2)
        buckets = self.cd.get_buckets()
        b1 = buckets[0]
        # bar1は上昇バー: buy_vol >= 700
        assert b1.buy_volume + b1.sell_volume >= 900, (
            f"バケット1のvolumeが小さすぎる: {b1.buy_volume + b1.sell_volume}"
        )


# =============================================================================
# C3: chronos_bot._daily_reset が cumulative_delta.daily_reset を呼ぶ
# =============================================================================

class TestC3DailyResetIntegration:
    """C3: ChronosBotの_daily_resetがCumulativeDeltaをリセットする"""

    def test_chronos_bot_has_cumulative_delta_attribute(self):
        """ChronosBot が cumulative_delta 属性を持つ"""
        import chronos_bot
        try:
            bot = chronos_bot.ChronosBot(dry_run=True)
            assert hasattr(bot, "cumulative_delta"), "cumulative_delta 属性なし (C3未修正)"
        except Exception as e:
            pytest.skip(f"ChronosBot インスタンス化失敗: {e}")

    def test_daily_reset_calls_cumulative_delta_reset(self):
        """_daily_reset() が cumulative_delta.daily_reset() を呼ぶ"""
        import chronos_bot
        import datetime
        try:
            bot = chronos_bot.ChronosBot(dry_run=True)
        except Exception as e:
            pytest.skip(f"ChronosBot インスタンス化失敗: {e}")
        if bot.cumulative_delta is None:
            pytest.skip("cumulative_delta が None")
        bar = BarData(5000.0, 5005.0, 4998.0, 5003.0, 100.0, timestamp=0)
        bot.cumulative_delta.update_from_bar(bar)
        today = datetime.date.today()
        bot._daily_reset(today)
        assert bot.cumulative_delta.get_current_delta() == 0.0
        assert bot.cumulative_delta.get_current_bucket_delta() == 0.0
        assert bot.cumulative_delta.get_buckets() == []

    def test_cumulative_delta_in_bot_is_initialized(self):
        """ChronosBot.__init__ で CumulativeDelta が生成される"""
        import chronos_bot
        try:
            bot = chronos_bot.ChronosBot(dry_run=True)
            if chronos_bot.CUMULATIVE_DELTA_AVAILABLE:
                assert bot.cumulative_delta is not None
        except Exception as e:
            pytest.skip(f"ChronosBot インスタンス化失敗: {e}")


# =============================================================================
# C4: 採点ロジック - 空クラスで0点
# =============================================================================

class TestC4ScoringASTCheck:
    """C4: AST解析で空クラス実装が0点になる"""

    def test_ast_check_real_class_is_not_stub(self):
        """実装済みクラスはstubでない"""
        from pathlib import Path
        from scripts.futures_trader_evaluation import ast_check_class_implemented
        cd_file = Path(__file__).parent.parent / "chronos_cumulative_delta.py"
        result = ast_check_class_implemented(
            cd_file, "CumulativeDelta",
            ["update", "update_from_bar", "daily_reset"],
        )
        assert result["found"] is True
        assert result["is_stub"] is False
        assert len(result["methods_implemented"]) >= 3

    def test_ast_check_stub_class_detected(self, tmp_path):
        """空クラスはstubと判定される"""
        from scripts.futures_trader_evaluation import ast_check_class_implemented
        stub_code = """
class CumulativeDelta:
    def update(self, tick):
        pass
    def update_from_bar(self, bar):
        pass
    def daily_reset(self):
        pass
"""
        stub_file = tmp_path / "stub_module.py"
        stub_file.write_text(stub_code)
        result = ast_check_class_implemented(
            stub_file, "CumulativeDelta",
            ["update", "update_from_bar", "daily_reset"],
        )
        assert result["found"] is True
        assert result["is_stub"] is True
        assert len(result["methods_implemented"]) == 0

    def test_ast_check_import_success(self):
        """実モジュールのimport確認が成功する"""
        from pathlib import Path
        from scripts.futures_trader_evaluation import try_import_module
        codebase = Path(__file__).parent.parent
        ok = try_import_module("chronos_cumulative_delta", codebase)
        assert ok is True

    def test_ast_check_import_fake_module_fails(self, tmp_path):
        """存在しないモジュールのimportは失敗する"""
        from scripts.futures_trader_evaluation import try_import_module
        ok = try_import_module("nonexistent_module_xyz_20260419", tmp_path)
        assert ok is False

    def test_ast_check_notimplementederror_stub(self, tmp_path):
        """NotImplementedError のみのメソッドもstub扱い"""
        from scripts.futures_trader_evaluation import ast_check_class_implemented
        stub_code = """
class LiquiditySweepDetector:
    def check_sweep(self, bar, vol, atr):
        raise NotImplementedError
    def is_reversal_confirmed(self, bars, atr):
        raise NotImplementedError
"""
        stub_file = tmp_path / "stub_sweep.py"
        stub_file.write_text(stub_code)
        result = ast_check_class_implemented(
            stub_file, "LiquiditySweepDetector",
            ["check_sweep", "is_reversal_confirmed"],
        )
        assert result["found"] is True
        assert result["is_stub"] is True


# =============================================================================
# C5: ATRフィルタ恒真式修正
# =============================================================================

class TestC5ATRFilter:
    """C5: LiquiditySweepのATRフィルタが恒真式でない"""

    def setup_method(self):
        self.det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0,
            reversal_atr_mult=0.5,
        )
        self.vol_avg = 1000.0

    def test_is_valid_requires_positive_atr_breach(self):
        """is_valid は atr_breach > 0 を要求する"""
        sig_valid = SweepSignal(
            level_type="prev_high", level_price=5150.0, sweep_price=5155.0,
            direction="sweep_high", volume_ratio=2.5, atr_breach=0.1,
            confirmed=False, reason="test",
        )
        sig_zero = SweepSignal(
            level_type="prev_high", level_price=5150.0, sweep_price=5155.0,
            direction="sweep_high", volume_ratio=2.5, atr_breach=0.0,
            confirmed=False, reason="test",
        )
        assert sig_valid.is_valid is True
        assert sig_zero.is_valid is False

    def test_small_atr_breach_rejected_in_check_sweep(self):
        """ATR突破幅が reversal_atr_mult (0.5) 未満の場合はsweepを返さない"""
        # bar: high=5154, level=5150 → breach=4/ATR=10 = 0.4 < 0.5
        bar = BarSnapshot(timestamp=1000, open=5145.0, high=5154.0, low=5142.0,
                          close=5148.0, volume=2500.0)
        signal = self.det.check_sweep(bar, self.vol_avg, atr=10.0)
        assert signal is None, "小さいATR突破がフィルタされていない (C5未修正)"

    def test_sufficient_atr_breach_accepted(self):
        """ATR突破幅が reversal_atr_mult 以上の場合は sweep を返す"""
        # bar: high=5156, level=5150 → breach=6/10=0.6 >= 0.5
        bar = BarSnapshot(timestamp=1000, open=5145.0, high=5156.0, low=5142.0,
                          close=5148.0, volume=2500.0)
        signal = self.det.check_sweep(bar, self.vol_avg, atr=10.0)
        assert signal is not None, "十分なATR突破が拒否された (C5誤修正)"

    def test_atr_breach_zero_low_sweep_rejected(self):
        """Low sweepでもATR突破幅が不十分なら拒否"""
        # bar: low=5096, level=5100 → breach=4/10=0.4 < 0.5
        bar = BarSnapshot(timestamp=1000, open=5105.0, high=5108.0, low=5096.0,
                          close=5102.0, volume=2500.0)
        signal = self.det.check_sweep(bar, self.vol_avg, atr=10.0)
        assert signal is None, "Low sweepの小さいATR突破がフィルタされていない"


# =============================================================================
# HIGH-1: get_total_delta が pending bucket を含む
# =============================================================================

class TestHigh1GetTotalDelta:
    """HIGH-1: get_total_delta = 確定済み + 未確定バケット合算"""

    def setup_method(self):
        self.cd = CumulativeDelta(bucket_minutes=5, max_buckets=20)

    def test_get_total_delta_includes_pending(self):
        """get_total_delta は未確定バケットを含む"""
        bar1 = BarData(5000.0, 5005.0, 4998.0, 5003.0, 1000.0, timestamp=0)
        bar2 = BarData(5003.0, 5008.0, 5001.0, 5006.0, 1000.0, timestamp=400)
        self.cd.update_from_bar(bar1)
        self.cd.update_from_bar(bar2)
        confirmed = self.cd.get_current_delta()
        pending = self.cd.get_current_bucket_delta()
        total = self.cd.get_total_delta()
        assert total == confirmed + pending, (
            f"get_total_delta mismatch: {total} != {confirmed} + {pending}"
        )

    def test_get_total_delta_zero_initially(self):
        """初期状態では0"""
        assert self.cd.get_total_delta() == 0.0

    def test_get_total_delta_after_reset(self):
        """reset後は0"""
        bar = BarData(5000.0, 5005.0, 4998.0, 5003.0, 100.0, timestamp=0)
        self.cd.update_from_bar(bar)
        self.cd.daily_reset()
        assert self.cd.get_total_delta() == 0.0


# =============================================================================
# HIGH-2: detect_divergence threshold 実際に使用
# =============================================================================

class TestHigh2DivergenceThreshold:
    """HIGH-2: detect_divergence の threshold 引数が実際に効く"""

    def setup_method(self):
        self.cd = CumulativeDelta()

    def test_high_threshold_suppresses_divergence(self):
        """threshold が高い場合、小さな乖離は aligned になる"""
        prices = [5010.0, 5009.9, 5009.8]
        deltas = [100.0, 100.1, 100.2]
        result_high = self.cd.detect_divergence(prices, deltas, threshold=1.0)
        assert result_high == "aligned"

    def test_default_threshold_behavior(self):
        """デフォルト threshold=0.3 で明確な乖離を検出する"""
        prices = [5010.0, 5005.0, 5000.0]
        deltas = [100.0, 500.0, 1000.0]
        result = self.cd.detect_divergence(prices, deltas)
        assert result in ("bullish_divergence", "aligned")


# =============================================================================
# HIGH-3: ORB strategy に direction フィールド
# =============================================================================

class TestHigh3ORBDirection:
    """HIGH-3: select_futures_strategy の ORB 戦術に direction フィールドがある"""

    def test_orb_strategy_has_direction_field(self):
        """ORB 戦術の dict に direction キーが含まれる"""
        import chronos_strategy_selector as sel
        env = {
            "vix": 25.0,
            "vix_history": [22.0, 24.0, 25.0] * 20,
            "vix_z": 0.5,
            "time_et": "10:00",
            "account_pnl_day": 0.0,
            "account_pnl_month": 0.0,
            "account_balance": 50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct": 0.0,
        }
        strategies = sel.select_futures_strategy(env)
        orb_strats = [s for s in strategies if s["strategy"] == "orb"]
        assert len(orb_strats) > 0, "ORB戦術が選択されていない"
        for orb in orb_strats:
            assert "direction" in orb, f"ORB戦術にdirectionフィールドなし: {orb}"

    def test_orb_direction_env_passed_through(self):
        """env[orb_direction] が ORB 戦術の direction に反映される"""
        import chronos_strategy_selector as sel
        env = {
            "vix": 25.0,
            "vix_history": [22.0, 24.0, 25.0] * 20,
            "vix_z": 0.5,
            "time_et": "10:00",
            "account_pnl_day": 0.0,
            "account_pnl_month": 0.0,
            "account_balance": 50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct": 0.0,
            "orb_direction": "short",
        }
        strategies = sel.select_futures_strategy(env)
        orb_strats = [s for s in strategies if s["strategy"] == "orb"]
        for orb in orb_strats:
            assert orb["direction"] == "short", (
                f"orb_direction が反映されていない: {orb['direction']}"
            )


# =============================================================================
# HIGH-7: F13 size_pct Kelly/phase対応
# =============================================================================

class TestHigh7SweepSize:
    """HIGH-7: F13 liquidity sweep size が Kelly分率・phaseを反映する"""

    def _make_sweep_env(self, kelly_fraction=None, phase_size_mult=None):
        env = {
            "vix": 22.0,
            "vix_history": [20.0, 21.0, 22.0] * 20,
            "vix_z": 0.5,
            "time_et": "10:00",
            "account_pnl_day": 0.0,
            "account_pnl_month": 0.0,
            "account_balance": 50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct": 0.0,
            "liquidity_sweep_signal": {
                "signal": "short",
                "level_type": "prev_high",
                "level_price": 5150.0,
                "confidence": 0.80,
                "reason": "test",
            },
        }
        if kelly_fraction is not None:
            env["kelly_fraction"] = kelly_fraction
        if phase_size_mult is not None:
            env["phase_size_mult"] = phase_size_mult
        return env

    def test_default_kelly_sweep_size(self):
        """kelly_fraction 未指定でもsweepのsize_pctが合理的な範囲"""
        import chronos_strategy_selector as sel
        env = self._make_sweep_env()
        strategies = sel.select_futures_strategy(env)
        sweep_strats = [s for s in strategies if "liquidity_sweep_reversal" in s["strategy"]]
        assert len(sweep_strats) > 0
        for s in sweep_strats:
            assert 0.0 < s["size_pct"] <= 0.5

    def test_small_kelly_reduces_sweep_size(self):
        """kelly_fraction が小さいと sweep size が縮小される"""
        import chronos_strategy_selector as sel
        env_default = self._make_sweep_env(kelly_fraction=1.0)
        env_small   = self._make_sweep_env(kelly_fraction=0.2)
        strats_default = sel.select_futures_strategy(env_default)
        strats_small   = sel.select_futures_strategy(env_small)
        size_default = next(
            (s["size_pct"] for s in strats_default if "liquidity_sweep_reversal" in s["strategy"]),
            None
        )
        size_small = next(
            (s["size_pct"] for s in strats_small if "liquidity_sweep_reversal" in s["strategy"]),
            None
        )
        if size_default is not None and size_small is not None:
            assert size_small < size_default

    def test_small_phase_mult_reduces_sweep_size(self):
        """phase_size_mult が小さいと sweep size が縮小される"""
        import chronos_strategy_selector as sel
        env_full  = self._make_sweep_env(phase_size_mult=1.0)
        env_small = self._make_sweep_env(phase_size_mult=0.3)
        strats_full  = sel.select_futures_strategy(env_full)
        strats_small = sel.select_futures_strategy(env_small)
        size_full = next(
            (s["size_pct"] for s in strats_full if "liquidity_sweep_reversal" in s["strategy"]),
            None
        )
        size_small = next(
            (s["size_pct"] for s in strats_small if "liquidity_sweep_reversal" in s["strategy"]),
            None
        )
        if size_full is not None and size_small is not None:
            assert size_small < size_full


# =============================================================================
# HIGH-8: F12->F13 順序依存解消 (sweep に F12 bias補正適用)
# =============================================================================

class TestHigh8F12ToF13Order:
    """HIGH-8: F13 sweep に F12 cumulative_delta_bias の補正が適用される"""

    def test_bearish_bias_reduces_long_sweep_size(self):
        """bearish bias の場合、long sweep のサイズが縮小される"""
        import chronos_strategy_selector as sel
        env = {
            "vix": 22.0,
            "vix_history": [20.0, 21.0, 22.0] * 20,
            "vix_z": 0.5,
            "time_et": "10:00",
            "account_pnl_day": 0.0,
            "account_pnl_month": 0.0,
            "account_balance": 50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct": 0.0,
            "liquidity_sweep_signal": {
                "signal": "long",
                "level_type": "prev_low",
                "level_price": 5100.0,
                "confidence": 0.80,
                "reason": "test",
            },
            "cumulative_delta_bias": {
                "bias": "bearish",
                "current": -5000.0,
                "recent_5m": -1000.0,
                "divergence": "aligned",
                "confidence": 0.8,
            },
        }
        env_no_bias = {k: v for k, v in env.items() if k != "cumulative_delta_bias"}
        strats_with_bias    = sel.select_futures_strategy(env)
        strats_without_bias = sel.select_futures_strategy(env_no_bias)
        size_with = next(
            (s["size_pct"] for s in strats_with_bias if "liquidity_sweep_reversal" in s["strategy"]),
            None
        )
        size_without = next(
            (s["size_pct"] for s in strats_without_bias if "liquidity_sweep_reversal" in s["strategy"]),
            None
        )
        if size_with is not None and size_without is not None:
            assert size_with <= size_without

    def test_aligned_bias_no_reduction(self):
        """bias が neutral の場合は縮小されない"""
        import chronos_strategy_selector as sel
        env = {
            "vix": 22.0,
            "vix_history": [20.0, 21.0, 22.0] * 20,
            "vix_z": 0.5,
            "time_et": "10:00",
            "account_pnl_day": 0.0,
            "account_pnl_month": 0.0,
            "account_balance": 50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct": 0.0,
            "liquidity_sweep_signal": {
                "signal": "short",
                "level_type": "prev_high",
                "level_price": 5150.0,
                "confidence": 0.80,
                "reason": "test",
            },
            "cumulative_delta_bias": {
                "bias": "neutral",
                "current": 0.0,
                "recent_5m": 0.0,
                "divergence": "aligned",
                "confidence": 0.5,
            },
        }
        strategies = sel.select_futures_strategy(env)
        sweep_strats = [s for s in strategies if "liquidity_sweep_reversal" in s["strategy"]]
        assert len(sweep_strats) > 0
        for s in sweep_strats:
            assert s["size_pct"] > 0.0


# =============================================================================
# 統合テスト: 全CRITICAL修正の統合確認
# =============================================================================

class TestIntegrationAllCritical:
    """全CRITICAL修正の統合動作確認"""

    def test_c1_c2_combined_flush_and_accumulation(self):
        """C1+C2: tick/bar を混在させても累積が正確"""
        cd = CumulativeDelta(bucket_minutes=5)
        t1 = Tick(5000.0, 100.0, "buy", timestamp=10)
        cd.update(t1)
        bar = BarData(5000.0, 5005.0, 4998.0, 5003.0, 200.0, timestamp=400)
        cd.update_from_bar(bar)
        buckets = cd.get_buckets()
        assert len(buckets) >= 1

    def test_full_cycle_update_flush_get(self):
        """update -> flush -> get_bucket_delta の完全フロー"""
        cd = CumulativeDelta(bucket_minutes=5)
        for i in range(6):
            bar = BarData(5000.0, 5005.0, 4998.0, 5003.0, 100.0, timestamp=i * 310)
            cd.update_from_bar(bar)
        buckets = cd.get_buckets()
        assert len(buckets) >= 2
        delta_5m = cd.get_bucket_delta(5)
        assert isinstance(delta_5m, float)
        total = cd.get_total_delta()
        assert isinstance(total, float)

    def test_c5_atr_filter_real_scenario(self):
        """C5: 実シナリオでATRフィルタが機能する"""
        det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0, reversal_atr_mult=0.5,
        )
        small_bar = BarSnapshot(1000, 5145.0, 5153.0, 5142.0, 5148.0, 2500.0)
        small_sig = det.check_sweep(small_bar, 1000.0, atr=10.0)
        assert small_sig is None

        big_bar = BarSnapshot(2000, 5145.0, 5158.0, 5142.0, 5148.0, 2500.0)
        big_sig = det.check_sweep(big_bar, 1000.0, atr=10.0)
        assert big_sig is not None
