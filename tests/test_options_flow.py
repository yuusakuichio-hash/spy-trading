"""
tests/test_options_flow.py — OptionsFlow モジュール テスト (12テスト)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.options_flow import (
    OptionFlowRecord, FlowSignal,
    analyze_flow, get_flow_signal,
    _compute_uoa_threshold, BLOCK_TRADE_MIN_CONTRACTS,
)


def _make_record(right: str, volume: int, oi: int = 100,
                 mid: float = 1.0, strike: float = 400.0) -> OptionFlowRecord:
    """テスト用OptionFlowRecordを生成。"""
    return OptionFlowRecord(
        symbol="SPY", expiry="20260418", strike=strike,
        right=right, volume=volume, open_interest=oi,
        mid_price=mid,
        premium_total=volume * mid * 100,
        is_block=(volume >= BLOCK_TRADE_MIN_CONTRACTS),
    )


def test_analyze_flow_empty_records():
    """空のレコードで FlowSignal が返り、data_available=False。"""
    sig = analyze_flow([], "SPY")
    assert sig.flow_bias == 0.0
    assert sig.confidence == 0.0
    assert sig.data_available is False


def test_analyze_flow_bullish():
    """コール優勢の場合、flow_bias > 0。"""
    records = (
        [_make_record("C", 1000, mid=2.0)] * 10 +
        [_make_record("P", 100,  mid=1.0)] * 2
    )
    sig = analyze_flow(records, "SPY")
    assert sig.flow_bias > 0.0
    assert sig.direction() == "bullish"


def test_analyze_flow_bearish():
    """プット優勢の場合、flow_bias < 0。"""
    records = (
        [_make_record("P", 1000, mid=2.0)] * 10 +
        [_make_record("C", 100,  mid=1.0)] * 2
    )
    sig = analyze_flow(records, "SPY")
    assert sig.flow_bias < 0.0
    assert sig.direction() == "bearish"


def test_analyze_flow_neutral():
    """コール・プット均等の場合、neutral に近い。"""
    records = (
        [_make_record("C", 500, mid=1.0)] * 5 +
        [_make_record("P", 500, mid=1.0)] * 5
    )
    sig = analyze_flow(records, "SPY")
    assert -0.5 <= sig.flow_bias <= 0.5


def test_block_trade_detection():
    """500枚以上の取引が is_block=True になる。"""
    rec_block  = _make_record("C", BLOCK_TRADE_MIN_CONTRACTS)
    rec_normal = _make_record("C", BLOCK_TRADE_MIN_CONTRACTS - 1)
    assert rec_block.is_block is True
    assert rec_normal.is_block is False


def test_block_trade_impact_on_signal():
    """大口コールブロックが flow_bias を押し上げる。"""
    # コールのブロックトレードのみ
    records = [_make_record("C", 1000, mid=2.0)]
    sig = analyze_flow(records, "SPY")
    assert sig.block_calls >= 1
    assert sig.block_puts == 0


def test_uoa_threshold_dynamic():
    """UOA閾値がデータ量から動的に算出される。"""
    volumes = list(range(1, 101))  # 1〜100の一様分布
    threshold = _compute_uoa_threshold(volumes)
    # P90 ≈ 90
    assert 85.0 <= threshold <= 95.0


def test_uoa_threshold_insufficient_data():
    """データ不足時のフォールバック閾値が返る。"""
    volumes = [10, 20, 30]  # 10件未満
    threshold = _compute_uoa_threshold(volumes)
    assert threshold == 100.0  # フォールバック値


def test_uoa_count_in_signal():
    """UOA閾値を超えたレコード数が正しくカウントされる。"""
    # 小さいボリュームが多い中、大きなものが1件
    records = [_make_record("C", 10)] * 20 + [_make_record("C", 5000)]
    sig = analyze_flow(records, "SPY")
    assert sig.uoa_count >= 1


def test_confidence_proportional_to_volume():
    """信頼度がボリューム量に比例する。"""
    records_small = [_make_record("C", 10)] * 5
    records_large = [_make_record("C", 500)] * 5

    sig_small = analyze_flow(records_small, "SPY")
    sig_large = analyze_flow(records_large, "SPY")
    assert sig_large.confidence >= sig_small.confidence


def test_flow_bias_bounded():
    """flow_bias が常に -1.0〜+1.0 の範囲に収まる。"""
    # 極端なコール優勢
    records = [_make_record("C", 10000, mid=10.0)] * 100
    sig = analyze_flow(records, "SPY")
    assert -1.0 <= sig.flow_bias <= 1.0


def test_get_flow_signal_no_data():
    """データなし時のフォールバック動作（ThetaDataなし環境）。"""
    # ThetaDataサーバーがない環境でのテスト
    sig = get_flow_signal("SPY", date_str="19000101", records=[])
    assert isinstance(sig, FlowSignal)
    assert sig.flow_bias == 0.0
    assert sig.data_available is False


def test_net_premium_calculation():
    """Net premiumが正しく計算される (コール優勢時は正)。"""
    records = (
        [_make_record("C", 100, mid=3.0)] +  # premium = 100*3*100 = 30000
        [_make_record("P", 100, mid=1.0)]    # premium = 100*1*100 = 10000
    )
    sig = analyze_flow(records, "SPY")
    assert sig.net_premium > 0  # コールプレミアムが多い


if __name__ == "__main__":
    tests = [
        test_analyze_flow_empty_records,
        test_analyze_flow_bullish,
        test_analyze_flow_bearish,
        test_analyze_flow_neutral,
        test_block_trade_detection,
        test_block_trade_impact_on_signal,
        test_uoa_threshold_dynamic,
        test_uoa_threshold_insufficient_data,
        test_uoa_count_in_signal,
        test_confidence_proportional_to_volume,
        test_flow_bias_bounded,
        test_get_flow_signal_no_data,
        test_net_premium_calculation,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
