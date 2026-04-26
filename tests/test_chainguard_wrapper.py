"""tests/test_chainguard_wrapper.py — ChainGuard wrapper テスト (12 件)

カバー範囲:
  - get_chain_center_price: Protocol / dict / stale / エラー系
  - get_chain_center_price_with_fallback: live / cache / fallback パス
  - キャッシュ更新・stale 検出
  - 入力バリデーション
"""
from __future__ import annotations

import sys
import os
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atlas_v3.ops.chainguard_wrapper import (
    ChainGuardError,
    MissingPriceError,
    StalePriceError,
    _clear_cache,
    _price_cache,
    _store_cache,
    get_chain_center_price,
    get_chain_center_price_with_fallback,
)


# ── フィクスチャ ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_price_cache():
    """各テスト前後にキャッシュを全クリア。"""
    _clear_cache()
    yield
    _clear_cache()


class _MockMarketData:
    """MarketDataProtocol を実装したモック。"""
    def __init__(self, prices: dict[str, float | None]):
        self._prices = prices

    def get_last_price(self, symbol: str) -> float | None:
        return self._prices.get(symbol)


class _RaisingMarketData:
    """get_last_price が常に例外を投げるモック。"""
    def get_last_price(self, symbol: str) -> float | None:  # noqa: D102
        raise RuntimeError("OpenD connection refused")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Protocol オブジェクトから正常取得できる
# ─────────────────────────────────────────────────────────────────────────────

def test_get_price_via_protocol_success():
    md = _MockMarketData({"US.SPY": 595.1})
    price = get_chain_center_price("US.SPY", md)
    assert price == pytest.approx(595.1)


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: dict (symbol キー配下) から正常取得できる
# ─────────────────────────────────────────────────────────────────────────────

def test_get_price_from_nested_dict():
    md = {"US.SPY": {"last_price": 597.5}}
    price = get_chain_center_price("US.SPY", md)
    assert price == pytest.approx(597.5)


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: dict (flat) から close キーで取得できる
# ─────────────────────────────────────────────────────────────────────────────

def test_get_price_from_flat_dict_close_key():
    md = {"close": 600.0}
    price = get_chain_center_price("US.QQQ", md)
    assert price == pytest.approx(600.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: 取得成功後にキャッシュが更新される
# ─────────────────────────────────────────────────────────────────────────────

def test_cache_updated_after_success():
    md = _MockMarketData({"US.SPY": 590.0})
    get_chain_center_price("US.SPY", md)
    assert "US.SPY" in _price_cache
    assert _price_cache["US.SPY"][0] == pytest.approx(590.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: market_data が None を返すと MissingPriceError
# ─────────────────────────────────────────────────────────────────────────────

def test_none_price_raises_missing_price_error():
    md = _MockMarketData({"US.SPY": None})
    with pytest.raises(MissingPriceError):
        get_chain_center_price("US.SPY", md)


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: 非正値 price は MissingPriceError
# ─────────────────────────────────────────────────────────────────────────────

def test_nonpositive_price_raises_missing_price_error():
    md = _MockMarketData({"US.SPY": 0.0})
    with pytest.raises(MissingPriceError, match="non-positive"):
        get_chain_center_price("US.SPY", md)


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: get_last_price が例外を投げると MissingPriceError に包まれる
# ─────────────────────────────────────────────────────────────────────────────

def test_raising_provider_raises_missing_price_error():
    md = _RaisingMarketData()
    with pytest.raises(MissingPriceError, match="OpenD connection refused"):
        get_chain_center_price("US.SPY", md)


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: 空 symbol は ChainGuardError
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_symbol_raises():
    md = _MockMarketData({})
    with pytest.raises(ChainGuardError):
        get_chain_center_price("", md)


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: 未サポート型の market_data は ChainGuardError
# ─────────────────────────────────────────────────────────────────────────────

def test_unsupported_market_data_type_raises():
    with pytest.raises(ChainGuardError, match="unsupported"):
        get_chain_center_price("US.SPY", 12345)  # int は非対応


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: stale キャッシュ + allow_cache_on_error=True でフォールバック返却
# ─────────────────────────────────────────────────────────────────────────────

def test_stale_cache_fallback_with_allow_flag():
    # キャッシュに古い価格を書き込む
    _store_cache("US.SPY", 588.0)
    # fetch 失敗するプロバイダ
    md = _MockMarketData({"US.SPY": None})
    price = get_chain_center_price(
        "US.SPY", md, stale_threshold_secs=0.001, allow_cache_on_error=True
    )
    # stale キャッシュ値が返る（0.001s は即 stale だが allow_cache_on_error=True で許容 × 10）
    assert price == pytest.approx(588.0)


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: get_chain_center_price_with_fallback — live パス
# ─────────────────────────────────────────────────────────────────────────────

def test_with_fallback_returns_live():
    md = _MockMarketData({"US.SPY": 601.0})
    price, source = get_chain_center_price_with_fallback("US.SPY", md, fallback_price=500.0)
    assert price == pytest.approx(601.0)
    assert source == "live"


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: get_chain_center_price_with_fallback — 全失敗で fallback_price を返す
# ─────────────────────────────────────────────────────────────────────────────

def test_with_fallback_returns_fallback_on_total_failure():
    md = _MockMarketData({"US.SPY": None})
    price, source = get_chain_center_price_with_fallback("US.SPY", md, fallback_price=555.0)
    assert price == pytest.approx(555.0)
    assert source == "fallback"
