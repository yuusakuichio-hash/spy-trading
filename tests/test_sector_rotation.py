"""
tests/test_sector_rotation.py — SectorRotation モジュール テスト (12テスト)
"""
import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.sector_rotation import (
    get_sector_scores, get_leading_sectors, get_lagging_sectors,
    sector_signal_for_symbol, SECTOR_ETFS, SectorScore,
    _normalize_universe, _compute_return, _assign_regime,
)


def _make_prices(start: float, daily_change: float, n: int = 22) -> list[float]:
    """テスト用価格系列を生成。"""
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + daily_change))
    return prices


def test_normalize_universe_basic():
    """基本的なノーマライズが 0〜1 の範囲に収まる。"""
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = _normalize_universe(values)
    assert len(result) == 5
    assert result[0] == 0.0
    assert result[-1] == 1.0
    for v in result:
        assert 0.0 <= v <= 1.0


def test_normalize_universe_with_none():
    """None値を含む場合に 0.5 で補完される。"""
    values = [None, 2.0, 4.0, None]
    result = _normalize_universe(values)
    assert result[0] == 0.5
    assert result[3] == 0.5
    assert 0.0 <= result[1] <= 1.0


def test_normalize_universe_all_none():
    """全てNoneの場合、全て0.5。"""
    values = [None, None, None]
    result = _normalize_universe(values)
    assert all(v == 0.5 for v in result)


def test_normalize_universe_constant():
    """全て同じ値の場合、全て0.5。"""
    values = [3.0, 3.0, 3.0]
    result = _normalize_universe(values)
    assert all(v == 0.5 for v in result)


def test_compute_return_5d():
    """5日リターンが正しく計算される。"""
    prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    ret = _compute_return(prices, 5)
    assert ret is not None
    assert abs(ret - 0.05) < 1e-9  # 5% gain


def test_compute_return_insufficient_data():
    """データ不足時はNoneを返す。"""
    prices = [100.0, 105.0]
    ret = _compute_return(prices, 5)
    assert ret is None


def test_assign_regime_basic():
    """レジーム割り当てが正しく動作する。"""
    scores = [0.1, 0.5, 0.9]
    regimes = _assign_regime(scores, 0.7, 0.3)
    assert regimes[0] == "lagging"
    assert regimes[1] == "neutral"
    assert regimes[2] == "leading"


def test_get_sector_scores_with_injected_data():
    """外部注入データでセクタースコアが正しく算出される。"""
    # XLK が強い、XLE が弱いシナリオ
    price_data = {
        "XLK":  _make_prices(100.0, +0.01, 22),   # +10% trend
        "XLF":  _make_prices(100.0,  0.0,  22),   # flat
        "XLE":  _make_prices(100.0, -0.005, 22),  # -5% trend
        "XLV":  _make_prices(100.0, +0.002, 22),  # slight up
        "XLY":  _make_prices(100.0, +0.003, 22),
        "XLP":  _make_prices(100.0,  0.001, 22),
        "XLI":  _make_prices(100.0, +0.004, 22),
        "XLU":  _make_prices(100.0, -0.002, 22),
        "XLRE": _make_prices(100.0,  0.0,   22),
        "XLB":  _make_prices(100.0, +0.001, 22),
        "XLC":  _make_prices(100.0, +0.005, 22),
    }
    scores = get_sector_scores(price_data=price_data)

    assert "XLK" in scores
    assert "XLE" in scores
    # XLK は最高スコアであるべき
    assert scores["XLK"].composite_score > scores["XLE"].composite_score


def test_get_leading_lagging():
    """leading/lagging が正しく分類される。"""
    price_data = {
        "XLK":  _make_prices(100.0, +0.01, 22),
        "XLF":  _make_prices(100.0,  0.0,  22),
        "XLE":  _make_prices(100.0, -0.01, 22),
    }
    scores = get_sector_scores(symbols=["XLK", "XLF", "XLE"], price_data=price_data)
    leading = get_leading_sectors(scores)
    lagging = get_lagging_sectors(scores)

    assert "XLK" in leading or scores["XLK"].composite_score > scores["XLE"].composite_score
    assert len(leading) + len(lagging) <= 3


def test_sector_signal_for_known_symbol():
    """既知銘柄のセクターシグナルが返る。"""
    price_data = {
        "XLK":  _make_prices(100.0, +0.01, 22),
        "XLY":  _make_prices(100.0, -0.01, 22),
    }
    scores = get_sector_scores(symbols=["XLK", "XLY"], price_data=price_data)
    # AAPL は XLK に属する
    signal = sector_signal_for_symbol("AAPL", scores, sector_map={"AAPL": "XLK"})
    assert signal in ("leading", "neutral", "lagging")


def test_sector_signal_unknown_symbol():
    """未知銘柄は "unknown" を返す。"""
    price_data = {"XLK": _make_prices(100.0, 0.01, 22)}
    scores = get_sector_scores(symbols=["XLK"], price_data=price_data)
    signal = sector_signal_for_symbol("UNKNOWN_XYZ", scores)
    assert signal == "unknown"


def test_graceful_degradation_empty_price_data():
    """空の価格データでもエラーにならない。"""
    scores = get_sector_scores(price_data={})
    # 全てneutralで返る
    assert isinstance(scores, dict)
    for sym in SECTOR_ETFS:
        assert sym in scores
        assert scores[sym].composite_score == 0.5


def test_data_available_flag():
    """price_dataがある銘柄はdata_available=True。"""
    price_data = {"XLK": _make_prices(100.0, 0.01, 22)}
    scores = get_sector_scores(symbols=["XLK", "XLF"], price_data=price_data)
    assert scores["XLK"].data_available is True
    assert scores["XLF"].data_available is False


if __name__ == "__main__":
    tests = [
        test_normalize_universe_basic,
        test_normalize_universe_with_none,
        test_normalize_universe_all_none,
        test_normalize_universe_constant,
        test_compute_return_5d,
        test_compute_return_insufficient_data,
        test_assign_regime_basic,
        test_get_sector_scores_with_injected_data,
        test_get_leading_lagging,
        test_sector_signal_for_known_symbol,
        test_sector_signal_unknown_symbol,
        test_graceful_degradation_empty_price_data,
        test_data_available_flag,
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
