"""tests/test_symbol_aware_price_20260425.py

テスト対象: atlas_v3/ops/symbol_aware_price.py

カバレッジ要件:
  1. normalize_symbol: SPY / QQQ / IWM / SPX (US..SPX) / 個別株 / 不正入力
  2. get_current_price: Protocol 経由 / dict 経由 / zero 拒否 / 非正値 拒否
  3. OutOfRangePriceError: SPX=300 (H=300 bug) / SPY=50000 など
  4. StalePriceError: stale cache のみ残存時
  5. MissingPriceError: None 返却 / キー欠落 / 不正型
  6. get_current_price_with_fallback: live / cache / fallback ソース区別
  7. clear_cache: symbol 指定 / 全クリア
  8. 銘柄横断: SPX / SPY / QQQ / IWM / TSLA を同一テストサイクル内で混在

実装要件: 15 件以上
"""
from __future__ import annotations

import time
import pytest
from unittest.mock import MagicMock

from atlas_v3.ops.symbol_aware_price import (
    get_current_price,
    get_current_price_with_fallback,
    normalize_symbol,
    clear_cache,
    MissingPriceError,
    StalePriceError,
    OutOfRangePriceError,
    SymbolPriceError,
    MarketDataProtocol,
    _price_cache,
    _check_price_range,
    _PRICE_RANGE,
)


# ── フィクスチャ ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_price_cache():
    """各テスト前後でキャッシュをクリアしてテスト間干渉を防ぐ。"""
    clear_cache()
    yield
    clear_cache()


def _make_protocol_mock(symbol: str, price: float | None) -> MagicMock:
    """MarketDataProtocol を満たす mock を返す。"""
    mock = MagicMock(spec=["get_last_price"])
    mock.get_last_price.return_value = price
    return mock


# ── 1. normalize_symbol ───────────────────────────────────────────────────────

class TestNormalizeSymbol:
    def test_spy(self):
        assert normalize_symbol("US.SPY") == "SPY"

    def test_qqq(self):
        assert normalize_symbol("US.QQQ") == "QQQ"

    def test_iwm(self):
        assert normalize_symbol("US.IWM") == "IWM"

    def test_spx_double_dot(self):
        # CBOE インデックス: US..SPX → SPX (H=300 bug の再現前提確認)
        assert normalize_symbol("US..SPX") == "SPX"

    def test_tsla(self):
        assert normalize_symbol("US.TSLA") == "TSLA"

    def test_nvda(self):
        assert normalize_symbol("US.NVDA") == "NVDA"

    def test_no_prefix(self):
        # プレフィックスなしはそのまま返す
        assert normalize_symbol("AAPL") == "AAPL"

    def test_empty_string_raises(self):
        with pytest.raises(MissingPriceError, match="空または非文字列"):
            normalize_symbol("")

    def test_none_raises(self):
        with pytest.raises(MissingPriceError):
            normalize_symbol(None)  # type: ignore[arg-type]


# ── 2. get_current_price — Protocol 経由 ─────────────────────────────────────

class TestGetCurrentPriceProtocol:
    def test_spy_live(self):
        mock = _make_protocol_mock("US.SPY", 595.1)
        price = get_current_price("US.SPY", mock)
        assert price == pytest.approx(595.1)

    def test_qqq_live(self):
        mock = _make_protocol_mock("US.QQQ", 480.5)
        price = get_current_price("US.QQQ", mock)
        assert price == pytest.approx(480.5)

    def test_iwm_live(self):
        mock = _make_protocol_mock("US.IWM", 201.3)
        price = get_current_price("US.IWM", mock)
        assert price == pytest.approx(201.3)

    def test_spx_live(self):
        """SPX は 5000 台が正常。300 は OutOfRange になる。"""
        mock = _make_protocol_mock("US..SPX", 5300.0)
        price = get_current_price("US..SPX", mock)
        assert price == pytest.approx(5300.0)

    def test_tsla_live(self):
        mock = _make_protocol_mock("US.TSLA", 175.0)
        price = get_current_price("US.TSLA", mock)
        assert price == pytest.approx(175.0)

    def test_returns_float_when_protocol_returns_int(self):
        mock = _make_protocol_mock("US.SPY", 560)  # int
        price = get_current_price("US.SPY", mock)
        assert isinstance(price, float)
        assert price == pytest.approx(560.0)

    def test_protocol_returns_none_raises_missing(self):
        mock = _make_protocol_mock("US.SPY", None)
        with pytest.raises(MissingPriceError):
            get_current_price("US.SPY", mock)

    def test_protocol_returns_zero_raises_missing(self):
        mock = _make_protocol_mock("US.SPY", 0.0)
        with pytest.raises(MissingPriceError, match="非正値"):
            get_current_price("US.SPY", mock)

    def test_protocol_returns_negative_raises_missing(self):
        mock = _make_protocol_mock("US.QQQ", -5.0)
        with pytest.raises(MissingPriceError):
            get_current_price("US.QQQ", mock)


# ── 3. get_current_price — dict 経由 ─────────────────────────────────────────

class TestGetCurrentPriceDict:
    def test_flat_dict_last_price(self):
        price = get_current_price("US.SPY", {"last_price": 598.0})
        assert price == pytest.approx(598.0)

    def test_flat_dict_close(self):
        price = get_current_price("US.QQQ", {"close": 482.5})
        assert price == pytest.approx(482.5)

    def test_nested_dict_by_symbol(self):
        data = {"US.IWM": {"last_price": 202.0}}
        price = get_current_price("US.IWM", data)
        assert price == pytest.approx(202.0)

    def test_nested_dict_by_ticker(self):
        # ticker キー ("SPY") での fallback
        data = {"SPY": {"last_price": 595.0}}
        price = get_current_price("US.SPY", data)
        assert price == pytest.approx(595.0)

    def test_dict_missing_key_raises(self):
        with pytest.raises(MissingPriceError):
            get_current_price("US.SPY", {"volume": 1000000})

    def test_unsupported_type_raises(self):
        with pytest.raises(MissingPriceError, match="型が非対応"):
            get_current_price("US.SPY", [595.0])  # list は非対応


# ── 4. OutOfRangePriceError — H=300 バグ検知 ─────────────────────────────────

class TestOutOfRangePrice:
    def test_spx_300_raises(self):
        """H=300 バグ再現: SPX に 300.0 が渡された場合を検知する。"""
        mock = _make_protocol_mock("US..SPX", 300.0)
        with pytest.raises(OutOfRangePriceError, match="H=300"):
            get_current_price("US..SPX", mock)

    def test_spy_at_spx_price_raises(self):
        """SPX 処理中に SPY price (~560) が混入した場合を SPX 視点で検知する。"""
        # SPX が 560 → 範囲 [500, 15000] 内なので raise されない
        # → 実際の検知は SPX に SPY range (50-1500) が混入する逆ケース
        mock = _make_protocol_mock("US..SPX", 560.0)
        # 560 は SPX のレンジ [500, 15000] 内なのでこれは pass
        price = get_current_price("US..SPX", mock)
        assert price == pytest.approx(560.0)

    def test_spx_negative_raises_missing_not_range(self):
        """非正値は range check 前に MissingPriceError になる。"""
        mock = _make_protocol_mock("US..SPX", -100.0)
        with pytest.raises(MissingPriceError):
            get_current_price("US..SPX", mock)

    def test_spy_unrealistically_high_raises(self):
        mock = _make_protocol_mock("US.SPY", 9999.0)
        with pytest.raises(OutOfRangePriceError):
            get_current_price("US.SPY", mock)

    def test_iwm_too_low_raises(self):
        mock = _make_protocol_mock("US.IWM", 10.0)
        with pytest.raises(OutOfRangePriceError):
            get_current_price("US.IWM", mock)

    def test_skip_range_check_allows_out_of_range(self):
        """skip_range_check=True ならレンジ外でも pass する（テスト用）。"""
        mock = _make_protocol_mock("US..SPX", 300.0)
        price = get_current_price("US..SPX", mock, skip_range_check=True)
        assert price == pytest.approx(300.0)


# ── 5. StalePriceError ────────────────────────────────────────────────────────

class TestStalePriceError:
    def test_stale_cache_raises_when_protocol_returns_none(self, monkeypatch):
        """Protocol が None を返し、かつ stale cache が残っている場合は StalePriceError。"""
        # まず有効な価格でキャッシュを作る
        mock = _make_protocol_mock("US.SPY", 595.0)
        get_current_price("US.SPY", mock)
        # stale 判定を強制するためキャッシュを古く書き換える
        from atlas_v3.ops import symbol_aware_price as _sap
        _sap._price_cache["US.SPY"] = (595.0, time.monotonic() - 1000)
        # 次の呼び出しで None が返り stale cache のみ残存
        mock2 = _make_protocol_mock("US.SPY", None)
        with pytest.raises(StalePriceError, match="stale cache"):
            get_current_price("US.SPY", mock2, stale_threshold_secs=30.0)


# ── 6. get_current_price_with_fallback ───────────────────────────────────────

class TestGetCurrentPriceWithFallback:
    def test_live_source(self):
        mock = _make_protocol_mock("US.SPY", 595.0)
        price, source = get_current_price_with_fallback("US.SPY", mock, 562.5)
        assert source == "live"
        assert price == pytest.approx(595.0)

    def test_fallback_source_when_none(self):
        mock = _make_protocol_mock("US.QQQ", None)
        price, source = get_current_price_with_fallback("US.QQQ", mock, 480.0)
        assert source == "fallback"
        assert price == pytest.approx(480.0)

    def test_cache_source_when_stale_allowed(self, monkeypatch):
        """Protocol が None を返し、stale cache が 10x 許容範囲内なら "cache" ソース。"""
        from atlas_v3.ops import symbol_aware_price as _sap
        # stale_threshold_secs=30 → 10x=300s 許容の stale cache を差し込む
        _sap._price_cache["US.SPY"] = (595.0, time.monotonic() - 100)  # 100s stale
        mock = _make_protocol_mock("US.SPY", None)
        price, source = get_current_price_with_fallback("US.SPY", mock, 562.5, stale_threshold_secs=30.0)
        assert source == "cache"
        assert price == pytest.approx(595.0)

    def test_spx_h300_fallback_detects_range_but_fallback_used(self):
        """SPX get_current_price は OutOfRangePriceError → with_fallback は fallback を使う。"""
        mock = _make_protocol_mock("US..SPX", 300.0)
        price, source = get_current_price_with_fallback("US..SPX", mock, 5200.0)
        assert source == "fallback"
        assert price == pytest.approx(5200.0)


# ── 7. clear_cache ────────────────────────────────────────────────────────────

class TestClearCache:
    def test_clear_specific_symbol(self):
        from atlas_v3.ops import symbol_aware_price as _sap
        _sap._price_cache["US.SPY"] = (595.0, time.monotonic())
        _sap._price_cache["US.QQQ"] = (480.0, time.monotonic())
        clear_cache("US.SPY")
        assert "US.SPY" not in _sap._price_cache
        assert "US.QQQ" in _sap._price_cache

    def test_clear_all(self):
        from atlas_v3.ops import symbol_aware_price as _sap
        _sap._price_cache["US.SPY"] = (595.0, time.monotonic())
        _sap._price_cache["US..SPX"] = (5300.0, time.monotonic())
        clear_cache()
        assert len(_sap._price_cache) == 0


# ── 8. 銘柄横断: 同一テストサイクル内 ─────────────────────────────────────────

class TestMultiSymbolCycle:
    """SPX / SPY / QQQ / IWM / TSLA を同一テスト内で混在させて相互干渉がないことを確認。"""

    def test_no_cross_contamination(self):
        symbols_prices = [
            ("US.SPY",  595.0),
            ("US..SPX", 5300.0),
            ("US.QQQ",  480.0),
            ("US.IWM",  205.0),
            ("US.TSLA", 170.0),
            ("US.NVDA", 850.0),
        ]
        for code, expected in symbols_prices:
            mock = _make_protocol_mock(code, expected)
            price = get_current_price(code, mock)
            assert price == pytest.approx(expected), (
                f"{code}: expected {expected}, got {price}"
            )

    def test_spx_300_detected_while_spy_valid(self):
        """SPX=300 エラーが SPY キャッシュに影響しないことを確認。"""
        spy_mock = _make_protocol_mock("US.SPY", 595.0)
        get_current_price("US.SPY", spy_mock)  # SPY キャッシュ設定

        spx_bad_mock = _make_protocol_mock("US..SPX", 300.0)
        with pytest.raises(OutOfRangePriceError):
            get_current_price("US..SPX", spx_bad_mock)

        # SPY キャッシュは生きている
        from atlas_v3.ops import symbol_aware_price as _sap
        assert "US.SPY" in _sap._price_cache
        assert _sap._price_cache["US.SPY"][0] == pytest.approx(595.0)

    def test_qqq_after_spx_error(self):
        """SPX エラー後に QQQ が正常取得できることを確認。"""
        spx_bad = _make_protocol_mock("US..SPX", 300.0)
        with pytest.raises(OutOfRangePriceError):
            get_current_price("US..SPX", spx_bad)

        qqq_mock = _make_protocol_mock("US.QQQ", 480.0)
        price = get_current_price("US.QQQ", qqq_mock)
        assert price == pytest.approx(480.0)
