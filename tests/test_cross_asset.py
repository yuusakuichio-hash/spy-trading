"""
tests/test_cross_asset.py — CrossAsset モジュール テスト (12テスト)
"""
import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.cross_asset import (
    AssetPrice, CrossAssetSignal,
    get_cross_asset_signal,
    _pearson_corr, _returns, _rolling_corr_history, _anomaly_zscore,
)


def _prices_trending(n: int = 35, start: float = 100.0,
                      daily: float = 0.001) -> list[float]:
    """単調増加または減少の価格系列。"""
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + daily))
    return prices


def _make_asset(key: str, prices: list[float], source: str = "test") -> AssetPrice:
    return AssetPrice(symbol=key, prices=prices, source=source, available=bool(prices))


def test_pearson_corr_perfect_positive():
    """完全正相関は 1.0 を返す。"""
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [2.0, 4.0, 6.0, 8.0, 10.0]
    corr = _pearson_corr(x, y)
    assert corr is not None
    assert abs(corr - 1.0) < 1e-9


def test_pearson_corr_perfect_negative():
    """完全負相関は -1.0 を返す。"""
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [5.0, 4.0, 3.0, 2.0, 1.0]
    corr = _pearson_corr(x, y)
    assert corr is not None
    assert abs(corr - (-1.0)) < 1e-9


def test_pearson_corr_insufficient_data():
    """5件未満のデータは None を返す。"""
    x = [1.0, 2.0, 3.0]
    y = [1.0, 2.0, 3.0]
    corr = _pearson_corr(x, y)
    assert corr is None


def test_pearson_corr_constant():
    """定数系列は None を返す (std=0)。"""
    x = [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    corr = _pearson_corr(x, y)
    assert corr is None


def test_returns_basic():
    """リターン計算が正しい。"""
    prices = [100.0, 105.0, 110.0]
    rets   = _returns(prices)
    assert len(rets) == 2
    assert abs(rets[0] - 0.05) < 1e-9  # 5%
    assert abs(rets[1] - (110.0 / 105.0 - 1.0)) < 1e-9


def test_returns_single_price():
    """1価格は空のリターン系列。"""
    assert _returns([100.0]) == []


def test_anomaly_zscore_normal():
    """通常の相関係数は z スコアが小さい。"""
    history = [0.78 + 0.02 * (i % 5 - 2) for i in range(30)]
    z = _anomaly_zscore(0.80, history)
    assert abs(z) < 2.0


def test_anomaly_zscore_extreme():
    """異常な相関係数は z スコアの絶対値が大きい。

    sigma>0 になるように history に変動を持たせる。
    mean≈0.80, sigma≈0.01 の場合、-0.5 は extreme 外れ値。
    """
    # mean=0.80, 小さなjitterで sigma>0 を確保
    import random
    random.seed(42)
    history = [0.80 + random.gauss(0, 0.01) for _ in range(30)]
    z = _anomaly_zscore(-0.5, history)
    assert abs(z) >= 2.0


def test_get_signal_risk_on():
    """ES正相関・GC負相関のデータでシグナルが返る。

    リターン系列の相関を直接制御するため、
    同じリターンパターンを持つ価格系列を構築する。
    """
    import random
    random.seed(1234)
    # ランダムリターンの共通部分 (相関の源泉)
    common_rets = [random.gauss(0.001, 0.01) for _ in range(34)]
    noise = lambda scale=0.001: [random.gauss(0, scale) for _ in range(34)]

    def build(start, rets):
        prices = [start]
        for r in rets:
            prices.append(prices[-1] * (1 + r))
        return prices

    spy_rets = common_rets
    es_rets  = [r + n for r, n in zip(common_rets, noise(0.0005))]  # 高相関
    gc_rets  = [-r + n for r, n in zip(common_rets, noise(0.0005))] # 負相関

    price_data = {
        "SPY": _make_asset("SPY", build(100.0, spy_rets)),
        "ES":  _make_asset("ES",  build(4000.0, es_rets)),
        "NQ":  _make_asset("NQ",  build(14000.0, es_rets)),
        "GC":  _make_asset("GC",  build(1800.0, gc_rets)),
        "CL":  _make_asset("CL",  build(70.0, es_rets)),
        "BTC": _make_asset("BTC", build(60000.0, es_rets)),
    }
    sig = get_cross_asset_signal(price_data=price_data)
    assert sig.data_available is True
    # ES 正相関、GC 負相関なので regime_score は正方向になるはず
    assert sig.corr_es_spy is not None and sig.corr_es_spy > 0.5
    assert sig.regime_score >= 0.0


def test_get_signal_no_spy_data():
    """SPYデータなし → neutral + data_available=False。"""
    price_data = {
        "ES": _make_asset("ES", _prices_trending(35, 4000.0)),
    }
    sig = get_cross_asset_signal(price_data=price_data)
    assert sig.regime == "neutral"


def test_corr_anomaly_detection():
    """ES と SPY が逆方向に動いた場合、負相関が検知される。"""
    import random
    random.seed(9999)
    # SPY が上昇、ES が下降するリターン系列
    spy_prices = [100.0]
    es_prices  = [4000.0]
    for _ in range(64):
        daily_spy = random.gauss(0.002, 0.008)
        daily_es  = -daily_spy + random.gauss(0, 0.001)  # 逆方向
        spy_prices.append(spy_prices[-1] * (1 + daily_spy))
        es_prices.append(es_prices[-1]  * (1 + daily_es))

    price_data = {
        "SPY": _make_asset("SPY", spy_prices),
        "ES":  _make_asset("ES",  es_prices),
        "GC":  _make_asset("GC",  _prices_trending(65, 1800.0, 0.001)),
        "CL":  _make_asset("CL",  _prices_trending(65, 70.0, 0.001)),
        "BTC": _make_asset("BTC", _prices_trending(65, 60000.0, 0.001)),
        "NQ":  _make_asset("NQ",  _prices_trending(65, 14000.0, -0.001)),
    }
    sig = get_cross_asset_signal(price_data=price_data, corr_window=30)
    # 逆方向に動いているので corr_es_spy < 0 になるはず
    assert sig.corr_es_spy is not None
    assert sig.corr_es_spy < 0


def test_regime_score_bounded():
    """regime_score は常に -1.0〜+1.0 の範囲。"""
    spy_prices = _prices_trending(35)
    price_data = {
        "SPY": _make_asset("SPY", spy_prices),
        "ES":  _make_asset("ES",  _prices_trending(35, 4000.0, 0.01)),
        "GC":  _make_asset("GC",  _prices_trending(35, 1800.0, -0.01)),
        "CL":  _make_asset("CL",  _prices_trending(35, 70.0, 0.01)),
        "BTC": _make_asset("BTC", _prices_trending(35, 60000.0, 0.01)),
        "NQ":  _make_asset("NQ",  _prices_trending(35, 14000.0, 0.01)),
    }
    sig = get_cross_asset_signal(price_data=price_data)
    assert -1.0 <= sig.regime_score <= 1.0


def test_corr_values_bounded():
    """各相関係数は -1.0〜+1.0 の範囲。"""
    spy = _prices_trending(35, 100.0, 0.002)
    price_data = {
        "SPY": _make_asset("SPY", spy),
        "ES":  _make_asset("ES",  _prices_trending(35, 4000.0, 0.003)),
        "GC":  _make_asset("GC",  _prices_trending(35, 1800.0, -0.001)),
        "CL":  _make_asset("CL",  _prices_trending(35, 70.0, 0.002)),
        "BTC": _make_asset("BTC", _prices_trending(35, 60000.0, 0.001)),
        "NQ":  _make_asset("NQ",  _prices_trending(35, 14000.0, 0.002)),
    }
    sig = get_cross_asset_signal(price_data=price_data)
    for corr in [sig.corr_es_spy, sig.corr_cl_spy, sig.corr_gc_spy, sig.corr_btc_spy]:
        if corr is not None:
            assert -1.0 <= corr <= 1.0


def test_data_sources_populated():
    """データが存在するアセットは data_sources に記録される。"""
    spy = _prices_trending(35)
    avail_asset = AssetPrice(symbol="ES", prices=_prices_trending(35, 4000.0),
                             source="yahoo", available=True)
    empty_asset  = AssetPrice(symbol="GC", prices=[], source="yahoo", available=False)
    spy_asset    = AssetPrice(symbol="SPY", prices=spy, source="yahoo", available=True)

    price_data = {
        "SPY": spy_asset,
        "ES":  avail_asset,
        "GC":  empty_asset,
    }
    sig = get_cross_asset_signal(price_data=price_data)
    assert "SPY" in sig.data_sources
    assert "ES" in sig.data_sources


if __name__ == "__main__":
    tests = [
        test_pearson_corr_perfect_positive,
        test_pearson_corr_perfect_negative,
        test_pearson_corr_insufficient_data,
        test_pearson_corr_constant,
        test_returns_basic,
        test_returns_single_price,
        test_anomaly_zscore_normal,
        test_anomaly_zscore_extreme,
        test_get_signal_risk_on,
        test_get_signal_no_spy_data,
        test_corr_anomaly_detection,
        test_regime_score_bounded,
        test_corr_values_bounded,
        test_data_sources_populated,
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
