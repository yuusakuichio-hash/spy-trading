"""atlas_v3/ops/symbol_aware_price.py — Symbol-aware current price wrapper

問題背景 (2026-04-25):
  spy_bot.py 内の ORB / Straddle / IVCrush / GammaScalp Engine が
  underlying_code に無関係に `mkt.get_spy_current()` を呼び出し、
  SPX 処理中に SPY の price を使ってしまう symbol 取り違えが存在する。

  加えて ORBEngine._get_fallback_price() は
    `_FALLBACK_PRICE_DEFAULTS.get(ticker, 300.0)`
  で SPX ("SPX" は dict 未登録) → 300.0 を返す H=300 バグがある。

本モジュールの役割:
  `get_current_price(underlying_code, market_data) -> float` の 1 関数を
  提供し、ChainGuard wrapper と同設計の "symbol 自身の last_price を動的取得"
  を実現する。spy_bot.py は schg 中のため直接書換不可。本 wrapper を経由して
  月曜 sudo unlock 後に get_spy_current() 呼出を 1 行差替する。

設計方針:
  1. underlying_code から ticker を正規化して price を返す
  2. プロバイダ優先順: MarketDataProtocol → dict-like → raise
  3. zero / 負値は MissingPriceError を raise（silent 300.0 fallback 禁止）
  4. ChainGuard の StalePriceError / MissingPriceError と同一例外体系を共有
  5. 銘柄ごとの price_range_guard でありえない価格を早期検知

Interface 契約:
    MarketDataProtocol:
        def get_last_price(symbol: str) -> float | None

Usage (spy_bot.py 統合後):
    from atlas_v3.ops.symbol_aware_price import get_current_price
    # 旧: spy_price = self.mkt.get_spy_current()
    # 新: spy_price = get_current_price(self.mkt.underlying_code, self.mkt)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# ── 公開例外（ChainGuard と同一体系） ────────────────────────────────────────

class SymbolPriceError(RuntimeError):
    """symbol_aware_price 全般エラーの基底"""


class MissingPriceError(SymbolPriceError):
    """market_data から price を取得できなかった（None / キー欠落 / 不正な symbol）"""


class StalePriceError(SymbolPriceError):
    """キャッシュが stale_threshold_secs を超えて更新されていない"""


class OutOfRangePriceError(SymbolPriceError):
    """取得した price が銘柄の正常範囲外（例: SPX=300 などありえない値）"""


# ── MarketData Protocol ───────────────────────────────────────────────────────

@runtime_checkable
class MarketDataProtocol(Protocol):
    """get_last_price を持つ任意のプロバイダ (futu / yfinance / mock 共通)"""

    def get_last_price(self, symbol: str) -> float | None:  # noqa: D102
        ...


# ── 銘柄コード正規化 ─────────────────────────────────────────────────────────

def normalize_symbol(underlying_code: str) -> str:
    """futu underlying_code を Finnhub/内部 ticker に正規化する。

    spy_bot.py の `symbol.replace("US.", "").replace(".", "")` と同一ロジックだが、
    結果が空文字になるケースは MissingPriceError を raise する。

    "US.SPY"  -> "SPY"
    "US.QQQ"  -> "QQQ"
    "US..SPX" -> "SPX"   (CBOE インデックス: ドット 2 つ)
    "US.TSLA" -> "TSLA"
    """
    if not underlying_code or not isinstance(underlying_code, str):
        raise MissingPriceError(
            f"[SymbolAware] underlying_code が空または非文字列: {underlying_code!r}"
        )
    code = underlying_code.strip()
    if code.startswith("US.."):
        # CBOE インデックス (US..SPX → "SPX")
        ticker = code[4:]
    elif code.startswith("US."):
        ticker = code[3:]
    else:
        ticker = code
    ticker = ticker.strip()
    if not ticker:
        raise MissingPriceError(
            f"[SymbolAware] normalize_symbol: {underlying_code!r} → ticker が空文字"
        )
    return ticker


# ── 銘柄価格レンジガード ─────────────────────────────────────────────────────
# (low, high) — この範囲外は OutOfRangePriceError を raise する
# 値は 2025-2026 年時点の合理的な上下限（市場クラッシュ想定込み）
_PRICE_RANGE: dict[str, tuple[float, float]] = {
    "SPY":   (50.0,    1_500.0),
    "SPX":   (500.0,  15_000.0),
    "SPXW":  (500.0,  15_000.0),
    "QQQ":   (50.0,    1_000.0),
    "IWM":   (50.0,      500.0),
    "TSLA":  (5.0,     2_000.0),
    "NVDA":  (5.0,     5_000.0),
    "AAPL":  (5.0,     1_000.0),
    "MSFT":  (5.0,     1_500.0),
    "AMZN":  (5.0,     1_000.0),
    "META":  (5.0,     2_000.0),
    "GOOGL": (5.0,     1_000.0),
}
_GENERIC_RANGE: tuple[float, float] = (0.01, 100_000.0)


def _check_price_range(ticker: str, price: float) -> None:
    """price が銘柄の正常レンジ外なら OutOfRangePriceError を raise する。"""
    lo, hi = _PRICE_RANGE.get(ticker, _GENERIC_RANGE)
    if not (lo <= price <= hi):
        raise OutOfRangePriceError(
            f"[SymbolAware] {ticker} price={price:.4f} は正常レンジ外 "
            f"[{lo}, {hi}]。SPX 処理中に SPY 価格 (~560) や "
            f"H=300 fallback が混入した可能性あり。"
        )


# ── プロセス内 price キャッシュ ───────────────────────────────────────────────

_price_cache: dict[str, tuple[float, float]] = {}   # symbol → (price, monotonic_ts)
_DEFAULT_STALE_SECS: float = 30.0


def _cache_get(symbol: str, stale_secs: float) -> float | None:
    entry = _price_cache.get(symbol)
    if entry is None:
        return None
    price, ts = entry
    age = time.monotonic() - ts
    if age > stale_secs:
        log.warning(
            "[SymbolAware] cache stale: symbol=%s age=%.1fs threshold=%.1fs",
            symbol, age, stale_secs,
        )
        return None
    return price


def _cache_set(symbol: str, price: float) -> None:
    _price_cache[symbol] = (price, time.monotonic())


def clear_cache(symbol: str | None = None) -> None:
    """テスト・日次リセット用。symbol=None で全クリア。"""
    if symbol is None:
        _price_cache.clear()
    else:
        _price_cache.pop(symbol, None)


# ── dict から price を抽出 ────────────────────────────────────────────────────

_PRICE_KEYS = ("last_price", "close", "price", "last", "mark", "c")


def _extract_from_dict(data: dict[str, Any], symbol: str) -> float | None:
    """dict-like から price を優先キー順に抽出する（ChainGuard と同設計）。"""
    # {"US.SPY": {"last_price": 595.1}} 形式
    if symbol in data and isinstance(data[symbol], dict):
        sub = data[symbol]
        for k in _PRICE_KEYS:
            v = sub.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass

    # flat dict {"last_price": 595.1} 形式
    for k in _PRICE_KEYS:
        v = data.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass

    return None


# ── 公開 API ─────────────────────────────────────────────────────────────────

def get_current_price(
    underlying_code: str,
    market_data: Any,
    *,
    stale_threshold_secs: float = _DEFAULT_STALE_SECS,
    skip_range_check: bool = False,
) -> float:
    """underlying_code 自身の現在価格を動的取得して返す。

    spy_bot.py 内で `mkt.get_spy_current()` を呼び出している全箇所の
    drop-in 置換として設計されている。

    Args:
        underlying_code:       futu 銘柄コード (例: "US.SPY", "US..SPX", "US.TSLA")
        market_data:           MarketDataProtocol または dict-like オブジェクト
                               （spy_bot.py の MarketData インスタンスを渡す想定）
        stale_threshold_secs:  キャッシュ有効期限 (秒)。デフォルト 30s。
        skip_range_check:      True なら OutOfRangePriceError を skip（テスト用）

    Returns:
        float: 最新の underlying price

    Raises:
        MissingPriceError:    price を取得できなかった（None / zero / 不正コード）
        StalePriceError:      stale キャッシュのみ残存
        OutOfRangePriceError: price が銘柄の正常レンジ外（SPX=300 等の混入検知）
    """
    ticker = normalize_symbol(underlying_code)

    # ── (1) MarketDataProtocol 経由 (duck-typing) ────────────────────────────
    # NOTE: isinstance(market_data, MarketDataProtocol) は Python 3.14 で
    # MagicMock を正しく認識しない場合がある。hasattr duck-typing に統一する。
    raw: float | None = None
    fetch_error: Exception | None = None

    if hasattr(market_data, "get_last_price") and callable(
        getattr(market_data, "get_last_price", None)
    ):
        try:
            raw = market_data.get_last_price(underlying_code)
        except Exception as exc:  # noqa: BLE001
            fetch_error = exc
            log.warning("[SymbolAware] get_last_price raised: %s", exc)

    # ── (2) dict fallback ─────────────────────────────────────────────────────
    elif isinstance(market_data, dict):
        raw = _extract_from_dict(market_data, underlying_code)
        if raw is None:
            # ticker キーでも試みる（{"SPY": {"last_price": 595.1}} 形式対応）
            raw = _extract_from_dict(market_data, ticker)
        if raw is None:
            fetch_error = MissingPriceError(
                f"[SymbolAware] dict に {underlying_code!r} / {ticker!r} の price キーなし. "
                f"Tried: {_PRICE_KEYS}"
            )

    else:
        raise MissingPriceError(
            f"[SymbolAware] market_data 型が非対応: {type(market_data)!r}. "
            "MarketDataProtocol.get_last_price() を実装するか dict を渡してください。"
        )

    # ── (3) 価格の有効性チェック ──────────────────────────────────────────────
    if raw is not None:
        try:
            price = float(raw)
        except (TypeError, ValueError) as exc:
            raise MissingPriceError(
                f"[SymbolAware] price を float 変換できない: {raw!r} ({exc})"
            ) from exc

        if price <= 0:
            raise MissingPriceError(
                f"[SymbolAware] {underlying_code!r} から非正値 price={price} を受信。"
                "データソースがプレースホルダーを返している可能性あり。"
            )

        if not skip_range_check:
            _check_price_range(ticker, price)

        _cache_set(underlying_code, price)
        log.debug(
            "[SymbolAware] price=%s symbol=%s ticker=%s (fresh)",
            price, underlying_code, ticker,
        )
        return price

    # ── (4) 取得失敗 → stale cache は使わず raise ─────────────────────────────
    # zero-fallback / 300.0 fallback を根治するため stale cache は使わない設計。
    # （allow_cache_on_error が必要なら get_current_price_with_fallback を使う）
    stale_entry = _price_cache.get(underlying_code)
    if stale_entry is not None:
        raise StalePriceError(
            f"[SymbolAware] {underlying_code!r} の price 取得失敗 + stale cache のみ残存。"
            f"stale_price={stale_entry[0]}, age={time.monotonic() - stale_entry[1]:.1f}s. "
            "allow_cache_on_error=True が必要なら get_current_price_with_fallback を使用。"
        )

    if fetch_error is not None:
        raise MissingPriceError(
            f"[SymbolAware] {underlying_code!r} price 取得完全失敗: {fetch_error}"
        ) from fetch_error

    raise MissingPriceError(
        f"[SymbolAware] {underlying_code!r} price 取得完全失敗: "
        "market_data が None を返し stale cache もなし。"
    )


def get_current_price_with_fallback(
    underlying_code: str,
    market_data: Any,
    fallback_price: float,
    *,
    stale_threshold_secs: float = _DEFAULT_STALE_SECS,
) -> tuple[float, str]:
    """get_current_price のフォールバック付きラッパー。

    spy_bot.py 統合時に `or 562.5` などのハードコード fallback を
    置き換えるために使用する（schg unlock 後に即全適用）。

    Returns:
        (price, source):
            source = "live"      新鮮なデータ取得成功
            source = "cache"     stale キャッシュを使用
            source = "fallback"  全て失敗して fallback_price を使用
    """
    try:
        price = get_current_price(
            underlying_code, market_data,
            stale_threshold_secs=stale_threshold_secs,
        )
        return price, "live"
    except SymbolPriceError:
        pass

    # stale cache を 10x 許容で試行
    cached = _cache_get(underlying_code, stale_threshold_secs * 10)
    if cached is not None:
        return cached, "cache"

    log.error(
        "[SymbolAware] All sources failed for %s. Using fallback=%s",
        underlying_code, fallback_price,
    )
    return fallback_price, "fallback"
