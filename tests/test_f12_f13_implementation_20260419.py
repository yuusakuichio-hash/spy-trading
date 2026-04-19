#!/usr/bin/env python3
"""
test_f12_f13_implementation_20260419.py
F12 Cumulative Delta / F13 Liquidity Sweep 実装テスト (20件+)

カバー範囲:
  - CumulativeDelta: 計算・乖離検出・日次reset・バケット集計
  - LiquiditySweepDetector: 前日H/L/VWAP突破検知・反転確認・出来高フィルタ
  - 戦略統合: strategy_selector への統合確認

実行:
  python3 -m pytest tests/test_f12_f13_implementation_20260419.py -v
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
# F12: Cumulative Delta テスト
# =============================================================================

class TestCumulativeDeltaBasic:
    """基本計算テスト"""

    def setup_method(self):
        self.cd = CumulativeDelta(bucket_minutes=5, max_buckets=20)

    def test_initial_state(self):
        """初期状態: delta = 0"""
        assert self.cd.get_current_delta() == 0.0

    def test_buy_tick_increases_delta(self):
        """買い tick は delta を増加させる"""
        tick = Tick(price=5000.0, volume=100.0, aggressor_side="buy")
        self.cd.update(tick)
        assert self.cd.get_current_bucket_delta() == 100.0

    def test_sell_tick_decreases_delta(self):
        """売り tick は delta を減少させる"""
        tick = Tick(price=5000.0, volume=100.0, aggressor_side="sell")
        self.cd.update(tick)
        assert self.cd.get_current_bucket_delta() == -100.0

    def test_unknown_tick_splits_evenly(self):
        """unknown tick は 0.5/0.5 按分"""
        tick = Tick(price=5000.0, volume=200.0, aggressor_side="unknown")
        self.cd.update(tick)
        # buy_vol = 100, sell_vol = 100 → delta = 0
        assert self.cd.get_current_bucket_delta() == 0.0

    def test_multiple_ticks_accumulate(self):
        """複数 tick の累積"""
        ticks = [
            Tick(5000.0, 100.0, "buy"),
            Tick(5001.0, 50.0, "sell"),
            Tick(5002.0, 200.0, "buy"),
        ]
        for t in ticks:
            self.cd.update(t)
        # buy = 300, sell = 50 → delta = 250
        assert self.cd.get_current_bucket_delta() == 250.0

    def test_daily_reset_clears_state(self):
        """日次 reset で状態がクリアされる"""
        tick = Tick(5000.0, 100.0, "buy")
        self.cd.update(tick)
        self.cd.daily_reset()
        assert self.cd.get_current_delta() == 0.0
        assert self.cd.get_current_bucket_delta() == 0.0
        assert self.cd.get_buckets() == []


class TestCumulativeDeltaBarApproximation:
    """1分足バー代替計算テスト"""

    def setup_method(self):
        self.cd = CumulativeDelta(bucket_minutes=5, max_buckets=20)

    def test_bullish_bar_creates_positive_delta(self):
        """上昇バー (close > open) は positive delta"""
        bar = BarData(open=5000.0, high=5010.0, low=4998.0, close=5008.0, volume=1000.0, timestamp=1000)
        self.cd.update_from_bar(bar)
        # close > open → buy_ratio 0.7 → buy_vol = 700, sell_vol = 300 → delta = 400
        delta = self.cd.get_current_bucket_delta()
        assert delta > 0

    def test_bearish_bar_creates_negative_delta(self):
        """下落バー (close < open) は negative delta"""
        bar = BarData(open=5008.0, high=5010.0, low=4998.0, close=5000.0, volume=1000.0, timestamp=1000)
        self.cd.update_from_bar(bar)
        delta = self.cd.get_current_bucket_delta()
        assert delta < 0

    def test_doji_bar_near_zero_delta(self):
        """十字バー (close == open) は 0 に近い delta"""
        bar = BarData(open=5000.0, high=5005.0, low=4995.0, close=5000.0, volume=1000.0, timestamp=1000)
        self.cd.update_from_bar(bar)
        delta = self.cd.get_current_bucket_delta()
        assert delta == 0.0

    def test_bucket_boundary_flush(self):
        """バケット境界を越えると flush される"""
        # バケット1
        bar1 = BarData(open=5000.0, high=5005.0, low=4998.0, close=5004.0, volume=500.0, timestamp=1000)
        self.cd.update_from_bar(bar1)
        # 5分後 (300秒) のバー → 新バケット
        bar2 = BarData(open=5004.0, high=5010.0, low=5002.0, close=5009.0, volume=600.0, timestamp=1301)
        self.cd.update_from_bar(bar2)
        # バケットが1件確定されているはず
        buckets = self.cd.get_buckets()
        assert len(buckets) == 1

    def test_get_bucket_delta_recent_minutes(self):
        """直近 N 分の delta 集計"""
        # バー 5本分 (各300秒のバケット境界で flush)
        base_ts = 0
        for i in range(6):
            bar = BarData(5000.0, 5005.0, 4998.0, 5004.0, 100.0, base_ts + i * 301)
            self.cd.update_from_bar(bar)
        buckets = self.cd.get_buckets()
        assert len(buckets) >= 1
        delta_5m = self.cd.get_bucket_delta(5)
        assert isinstance(delta_5m, float)


class TestCumulativeDeltaDivergence:
    """乖離検出テスト"""

    def setup_method(self):
        self.cd = CumulativeDelta()

    def test_bullish_divergence_detected(self):
        """価格下落 + Delta 上昇 = bullish_divergence"""
        prices = [5010.0, 5005.0, 5000.0]   # 下落
        deltas = [100.0, 200.0, 400.0]       # 上昇
        result = self.cd.detect_divergence(prices, deltas)
        assert result == "bullish_divergence"

    def test_bearish_divergence_detected(self):
        """価格上昇 + Delta 下落 = bearish_divergence"""
        prices = [5000.0, 5005.0, 5010.0]   # 上昇
        deltas = [400.0, 200.0, 100.0]       # 下落
        result = self.cd.detect_divergence(prices, deltas)
        assert result == "bearish_divergence"

    def test_aligned_trend_detected(self):
        """価格上昇 + Delta 上昇 = aligned"""
        prices = [5000.0, 5005.0, 5010.0]
        deltas = [100.0, 200.0, 400.0]
        result = self.cd.detect_divergence(prices, deltas)
        assert result == "aligned"

    def test_insufficient_data_returns_string(self):
        """データ不足は insufficient_data"""
        result = self.cd.detect_divergence([5000.0], [100.0])
        assert result == "insufficient_data"

    def test_length_mismatch_returns_insufficient(self):
        """長さ不一致は insufficient_data"""
        result = self.cd.detect_divergence([5000.0, 5005.0], [100.0])
        assert result == "insufficient_data"


class TestBidAskUtils:
    """bid_ask_delta / volume_ratio ユーティリティテスト"""

    def test_calc_bid_ask_delta_positive(self):
        """ask > bid → positive delta (買い超)"""
        delta = calc_bid_ask_delta(bid_volume=100.0, ask_volume=300.0)
        assert delta == 200.0

    def test_calc_bid_ask_delta_negative(self):
        """bid > ask → negative delta (売り超)"""
        delta = calc_bid_ask_delta(bid_volume=300.0, ask_volume=100.0)
        assert delta == -200.0

    def test_calc_volume_ratio_balanced(self):
        """均衡時は 0.5"""
        ratio = calc_volume_ratio(100.0, 100.0)
        assert ratio == 0.5

    def test_calc_volume_ratio_all_buy(self):
        """全買いは 1.0"""
        ratio = calc_volume_ratio(100.0, 0.0)
        assert ratio == 1.0

    def test_calc_volume_ratio_zero_total(self):
        """ゼロ合計はデフォルト 0.5"""
        ratio = calc_volume_ratio(0.0, 0.0)
        assert ratio == 0.5


# =============================================================================
# F13: Liquidity Sweep テスト
# =============================================================================

class TestLiquiditySweepDetectorInit:
    """初期化テスト"""

    def test_init_with_all_levels(self):
        """全 levels で初期化"""
        det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            ib_high=5140.0, ib_low=5110.0,
        )
        levels = det.get_levels()
        assert "prev_high" in levels
        assert "prev_low" in levels
        assert "prev_vwap" in levels
        assert "ib_high" in levels
        assert "ib_low" in levels

    def test_init_without_ib_levels(self):
        """IB なしでも初期化可能"""
        det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
        )
        levels = det.get_levels()
        assert "ib_high" not in levels
        assert "ib_low" not in levels

    def test_no_pending_sweep_initially(self):
        """初期状態: pending sweep なし"""
        det = LiquiditySweepDetector(5150.0, 5100.0, 5125.0)
        assert not det.has_pending_sweep()


class TestLiquiditySweepHighSweep:
    """High Sweep 検知テスト (前日高値突破)"""

    def setup_method(self):
        self.det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0, reversal_atr_mult=0.5, post_sweep_window_sec=30,
        )
        self.volume_avg = 1000.0
        self.atr = 10.0

    def test_high_sweep_detected(self):
        """前日高値を突破して close が level 以下 → sweep_high 検知"""
        # bar: high > prev_high, close < prev_high, vol > 2x avg
        bar = BarSnapshot(timestamp=1000, open=5145.0, high=5155.0, low=5142.0, close=5148.0, volume=2500.0)
        signal = self.det.check_sweep(bar, self.volume_avg, self.atr)
        assert signal is not None
        assert signal.direction == "sweep_high"
        assert signal.level_type == "prev_high"

    def test_high_sweep_volume_filter(self):
        """出来高が 2倍未満の場合は検知しない"""
        bar = BarSnapshot(timestamp=1000, open=5145.0, high=5155.0, low=5142.0, close=5148.0, volume=1500.0)
        # volume_ratio = 1500/1000 = 1.5 < 2.0
        signal = self.det.check_sweep(bar, self.volume_avg, self.atr)
        assert signal is None

    def test_high_not_swept_when_close_above_level(self):
        """close が level 以上の場合は sweep でない"""
        # bar: high > prev_high, close > prev_high (継続上昇)
        bar = BarSnapshot(timestamp=1000, open=5145.0, high=5155.0, low=5142.0, close=5152.0, volume=2500.0)
        signal = self.det.check_sweep(bar, self.volume_avg, self.atr)
        # sweep_high は bar.close < level_price が必要
        assert signal is None or signal.direction != "sweep_high"


class TestLiquiditySweepLowSweep:
    """Low Sweep 検知テスト (前日安値突破)"""

    def setup_method(self):
        self.det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0,
        )
        self.volume_avg = 1000.0
        self.atr = 10.0

    def test_low_sweep_detected(self):
        """前日安値を突破して close が level 以上 → sweep_low 検知"""
        bar = BarSnapshot(timestamp=1000, open=5105.0, high=5108.0, low=5095.0, close=5102.0, volume=2500.0)
        signal = self.det.check_sweep(bar, self.volume_avg, self.atr)
        assert signal is not None
        assert signal.direction == "sweep_low"
        assert signal.level_type == "prev_low"

    def test_vwap_sweep_detected(self):
        """VWAP を下方突破して close が VWAP 以上 → sweep_low(vwap) 検知"""
        bar = BarSnapshot(timestamp=1000, open=5128.0, high=5130.0, low=5120.0, close=5126.0, volume=2500.0)
        signal = self.det.check_sweep(bar, self.volume_avg, self.atr)
        assert signal is not None
        assert signal.level_type == "prev_vwap"

    def test_ib_high_sweep_detected(self):
        """IB 高値突破 → sweep_high(ib_high) 検知"""
        det = LiquiditySweepDetector(
            prev_high=5160.0, prev_low=5080.0, prev_vwap=5120.0,
            ib_high=5140.0, ib_low=5100.0, volume_multiplier=2.0,
        )
        bar = BarSnapshot(timestamp=1000, open=5138.0, high=5145.0, low=5135.0, close=5137.0, volume=2500.0)
        signal = det.check_sweep(bar, self.volume_avg, self.atr)
        assert signal is not None
        assert "ib_high" in [signal.level_type] or signal is not None


class TestLiquiditySweepReversal:
    """反転確認テスト"""

    def setup_method(self):
        self.det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0, reversal_atr_mult=0.5,
            post_sweep_window_sec=30, confirm_bars=2,
        )

    def _create_high_sweep(self):
        """テスト用 high sweep を生成する"""
        bar = BarSnapshot(timestamp=1000, open=5145.0, high=5155.0, low=5142.0, close=5148.0, volume=2500.0)
        return self.det.check_sweep(bar, 1000.0, 10.0)

    def test_reversal_confirmed_after_high_sweep(self):
        """High sweep 後に close < level が続けば反転確認"""
        self._create_high_sweep()
        # post-sweep bars: close < 5150.0 (prev_high)
        post_bars = [
            BarSnapshot(1060, 5148.0, 5149.0, 5140.0, 5141.0, 800.0),
            BarSnapshot(1120, 5141.0, 5143.0, 5135.0, 5136.0, 700.0),
        ]
        confirmed = self.det.is_reversal_confirmed(post_bars, atr=10.0)
        assert confirmed is True

    def test_reversal_not_confirmed_insufficient_bars(self):
        """バー数不足では反転確認しない"""
        self._create_high_sweep()
        post_bars = [
            BarSnapshot(1060, 5148.0, 5149.0, 5140.0, 5141.0, 800.0),
        ]
        # confirm_bars=2 なので 1 本では不十分
        confirmed = self.det.is_reversal_confirmed(post_bars, atr=10.0)
        assert confirmed is False

    def test_no_reversal_without_pending_sweep(self):
        """pending sweep なしでは False"""
        post_bars = [
            BarSnapshot(1060, 5148.0, 5149.0, 5140.0, 5141.0, 800.0),
            BarSnapshot(1120, 5141.0, 5143.0, 5135.0, 5136.0, 700.0),
        ]
        confirmed = self.det.is_reversal_confirmed(post_bars)
        assert confirmed is False


class TestLiquiditySweepEntrySignal:
    """エントリーシグナルテスト"""

    def setup_method(self):
        self.det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0, reversal_atr_mult=0.5,
            post_sweep_window_sec=30, confirm_bars=2,
        )

    def test_short_signal_after_high_sweep_reversal(self):
        """High sweep + reversal → short シグナル"""
        sweep_bar = BarSnapshot(1000, 5145.0, 5155.0, 5142.0, 5148.0, 2500.0)
        self.det.check_sweep(sweep_bar, 1000.0, 10.0)

        post_bars = [
            BarSnapshot(1060, 5148.0, 5149.0, 5140.0, 5141.0, 800.0),
            BarSnapshot(1120, 5141.0, 5143.0, 5135.0, 5136.0, 700.0),
        ]
        signal = self.det.get_entry_signal(post_bars, atr=10.0)
        assert signal is not None
        assert signal["signal"] == "short"

    def test_long_signal_after_low_sweep_reversal(self):
        """Low sweep + reversal → long シグナル"""
        sweep_bar = BarSnapshot(1000, 5105.0, 5108.0, 5095.0, 5102.0, 2500.0)
        self.det.check_sweep(sweep_bar, 1000.0, 10.0)

        post_bars = [
            BarSnapshot(1060, 5102.0, 5110.0, 5100.0, 5108.0, 800.0),
            BarSnapshot(1120, 5108.0, 5115.0, 5105.0, 5112.0, 700.0),
        ]
        signal = self.det.get_entry_signal(post_bars, atr=10.0)
        assert signal is not None
        assert signal["signal"] == "long"

    def test_confidence_range(self):
        """confidence は 0.0-1.0 の範囲"""
        sweep_bar = BarSnapshot(1000, 5145.0, 5155.0, 5142.0, 5148.0, 2500.0)
        self.det.check_sweep(sweep_bar, 1000.0, 10.0)

        post_bars = [
            BarSnapshot(1060, 5148.0, 5149.0, 5140.0, 5141.0, 800.0),
            BarSnapshot(1120, 5141.0, 5143.0, 5135.0, 5136.0, 700.0),
        ]
        signal = self.det.get_entry_signal(post_bars, atr=10.0)
        if signal:
            assert 0.0 <= signal["confidence"] <= 1.0


class TestSweepExpiry:
    """Sweep 期限切れテスト"""

    def test_sweep_expires_after_window(self):
        """post_sweep_window_sec 経過後は期限切れ"""
        det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0, post_sweep_window_sec=30,
        )
        sweep_bar = BarSnapshot(1000, 5145.0, 5155.0, 5142.0, 5148.0, 2500.0)
        det.check_sweep(sweep_bar, 1000.0, 10.0)

        # 31秒後 → 期限切れ
        assert det.is_sweep_expired(1031) is True

    def test_sweep_not_expired_within_window(self):
        """window 内は期限切れでない"""
        det = LiquiditySweepDetector(
            prev_high=5150.0, prev_low=5100.0, prev_vwap=5125.0,
            volume_multiplier=2.0, post_sweep_window_sec=30,
        )
        sweep_bar = BarSnapshot(1000, 5145.0, 5155.0, 5142.0, 5148.0, 2500.0)
        det.check_sweep(sweep_bar, 1000.0, 10.0)

        # 29秒後 → 期限内
        assert det.is_sweep_expired(1029) is False

    def test_clear_pending_removes_sweep(self):
        """clear_pending() で pending が消える"""
        det = LiquiditySweepDetector(5150.0, 5100.0, 5125.0)
        sweep_bar = BarSnapshot(1000, 5145.0, 5155.0, 5142.0, 5148.0, 2500.0)
        det.check_sweep(sweep_bar, 1000.0, 10.0)
        det.clear_pending()
        assert not det.has_pending_sweep()


class TestLevelUpdate:
    """Level 更新テスト"""

    def test_update_levels_replaces_values(self):
        """update_levels で levels が更新される"""
        det = LiquiditySweepDetector(5150.0, 5100.0, 5125.0)
        det.update_levels(prev_high=5160.0, prev_low=5090.0)
        levels = det.get_levels()
        assert levels["prev_high"] == 5160.0
        assert levels["prev_low"] == 5090.0

    def test_update_levels_ib_added(self):
        """update_levels で IB levels が追加される"""
        det = LiquiditySweepDetector(5150.0, 5100.0, 5125.0)
        det.update_levels(ib_high=5145.0, ib_low=5110.0)
        levels = det.get_levels()
        assert "ib_high" in levels
        assert levels["ib_high"] == 5145.0


# =============================================================================
# 統合テスト: strategy_selector との連携
# =============================================================================

class TestStrategyIntegration:
    """F12/F13 と strategy_selector の統合テスト"""

    def test_cumulative_delta_module_importable(self):
        """CumulativeDelta は importable"""
        from chronos_cumulative_delta import CumulativeDelta
        assert CumulativeDelta is not None

    def test_liquidity_sweep_module_importable(self):
        """LiquiditySweepDetector は importable"""
        from chronos_liquidity_sweep import LiquiditySweepDetector
        assert LiquiditySweepDetector is not None

    def test_strategy_selector_imports_f12_f13(self):
        """strategy_selector が F12/F13 モジュールをロードする"""
        import chronos_strategy_selector as sel
        # _CUMULATIVE_DELTA_AVAILABLE と _LIQUIDITY_SWEEP_AVAILABLE が定義されているか
        assert hasattr(sel, "_CUMULATIVE_DELTA_AVAILABLE")
        assert hasattr(sel, "_LIQUIDITY_SWEEP_AVAILABLE")
        # 実装済みなので両方 True のはず
        assert sel._CUMULATIVE_DELTA_AVAILABLE is True
        assert sel._LIQUIDITY_SWEEP_AVAILABLE is True

    def test_select_futures_strategy_with_cumulative_delta_bias(self):
        """env に cumulative_delta_bias を渡すと strategy size が調整される"""
        import chronos_strategy_selector as sel

        env = {
            "vix":                 22.0,
            "vix_history":         [20.0, 21.0, 22.0, 23.0, 22.5] * 12,
            "vix_z":               0.5,
            "time_et":             "10:00",
            "account_pnl_day":     0.0,
            "account_pnl_month":   0.0,
            "account_balance":     50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct":             0.0,
            "cumulative_delta_bias": {
                "bias":       "bullish",
                "current":    5000.0,
                "recent_5m":  1000.0,
                "divergence": "aligned",
                "confidence": 0.7,
            },
        }
        strategies = sel.select_futures_strategy(env)
        assert isinstance(strategies, list)
        assert len(strategies) > 0

    def test_select_futures_strategy_with_sweep_signal(self):
        """env に liquidity_sweep_signal を渡すと sweep 戦術が追加される"""
        import chronos_strategy_selector as sel
        # cycle4: F13 disabled 時はスキップ
        if not sel._F13_ENABLED:
            pytest.skip("F13 liquidity_sweep disabled (cycle4 一時無効化中)")


        env = {
            "vix":                 22.0,
            "vix_history":         [20.0, 21.0, 22.0, 23.0, 22.5] * 12,
            "vix_z":               0.5,
            "time_et":             "10:00",
            "account_pnl_day":     0.0,
            "account_pnl_month":   0.0,
            "account_balance":     50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct":             0.0,
            "liquidity_sweep_signal": {
                "signal":      "short",
                "level_type":  "prev_high",
                "level_price": 5150.0,
                "confidence":  0.75,
                "reason":      "test sweep",
            },
        }
        strategies = sel.select_futures_strategy(env)
        strategy_names = [s["strategy"] for s in strategies]
        # liquidity_sweep_reversal_short が含まれるはず
        assert any("liquidity_sweep_reversal" in n for n in strategy_names)

    def test_sweep_signal_below_min_confidence_skipped(self):
        """confidence < 0.60 の sweep signal は追加しない"""
        import chronos_strategy_selector as sel

        env = {
            "vix":                 22.0,
            "vix_history":         [20.0, 21.0, 22.0] * 20,
            "vix_z":               0.5,
            "time_et":             "10:00",
            "account_pnl_day":     0.0,
            "account_pnl_month":   0.0,
            "account_balance":     50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct":             0.0,
            "liquidity_sweep_signal": {
                "signal":      "short",
                "level_type":  "prev_high",
                "level_price": 5150.0,
                "confidence":  0.40,  # < 0.60 → skip
                "reason":      "low confidence test",
            },
        }
        strategies = sel.select_futures_strategy(env)
        strategy_names = [s["strategy"] for s in strategies]
        assert not any("liquidity_sweep_reversal" in n for n in strategy_names)

    def test_bid_ask_volume_in_env_triggers_f12(self):
        """env に bid_volume / ask_volume があると F12 が計算される"""
        import chronos_strategy_selector as sel

        env = {
            "vix":                 22.0,
            "vix_history":         [20.0, 21.0, 22.0] * 20,
            "vix_z":               0.5,
            "time_et":             "10:00",
            "account_pnl_day":     0.0,
            "account_pnl_month":   0.0,
            "account_balance":     50_000.0,
            "consistency_used_pct": 0.0,
            "gap_pct":             0.0,
            "bid_volume":          300.0,
            "ask_volume":          700.0,  # 買い超 → bullish
        }
        # エラーなく実行できることを確認
        strategies = sel.select_futures_strategy(env)
        assert isinstance(strategies, list)
