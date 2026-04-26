"""atlas_v3/ops/chainguard_wrapper.py — ChainGuard center price 動的取得 wrapper

Redteam aa60 CRITICAL #1:
  spy_bot.py:5525 で `_cached_spy_price` への代入が欠落し ChainGuard が
  stale/None 値をセンター価格として使用し続けるバグ。
  spy_bot.py への直接書換は schg lock 解除待ちのため、本 wrapper で
  呼出側が正しい center 価格を動的取得できる interface を提供する。

設計方針:
- `get_chain_center_price(symbol, market_data)` が唯一の公開 API
- market_data は dict-like protocol（futu DataFrame / yfinance dict 両対応）
- None / stale 値は明示的に ChainGuardError を raise（zero-fallback 禁止）
- spy_bot.py 側の呼出差替で有効化可能（月曜 sudo unlock 後に 1 行差替）

Interface 契約:
    MarketDataProtocol:
        def get_last_price(symbol: str) -> float | None
    または
        dict キー: "last_price" | "close" | "price" (優先順)

Usage (将来統合時):
    from atlas_v3.ops.chainguard_wrapper import get_chain_center_price
    center = get_chain_center_price("US.SPY", market_data)
    # spy_bot.py:5525 の _cached_spy_price 代入箇所をこれに差替
"""
from __future__ import annotations

import logging
import time
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# ── 公開例外 ──────────────────────────────────────────────────────────────────

class ChainGuardError(RuntimeError):
    """ChainGuard center price 取得失敗（stale / None / fetch エラー）"""


class StalePriceError(ChainGuardError):
    """キャッシュが stale_threshold_secs を超えて更新されていない"""


class MissingPriceError(ChainGuardError):
    """market_data から price を取得できなかった（None / キー欠落）"""


# ── MarketData Protocol（duck-typing 契約） ───────────────────────────────────

@runtime_checkable
class MarketDataProtocol(Protocol):
    """get_last_price を持つ任意のプロバイダ (futu / yfinance / mock 共通)"""
    def get_last_price(self, symbol: str) -> float | None:  # noqa: D102
        ...


# ── 内部: dict から price を抽出する優先キー順 ──────────────────────────────────

_PRICE_KEYS = ("last_price", "close", "price", "last", "mark")


def _extract_price_from_dict(data: dict[str, Any], symbol: str) -> float | None:
    """dict-like market_data から price を優先順に抽出する。"""
    # symbol キー配下に dict がある場合（{"US.SPY": {"last_price": 595.1}} 形式）
    if symbol in data and isinstance(data[symbol], dict):
        sub = data[symbol]
        for k in _PRICE_KEYS:
            v = sub.get(k)
            if v is not None:
                return float(v)

    # flat dict の場合（{"last_price": 595.1} 形式）
    for k in _PRICE_KEYS:
        v = data.get(k)
        if v is not None:
            return float(v)

    return None


# ── キャッシュ管理（プロセス内シングルトン） ──────────────────────────────────

_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
_DEFAULT_STALE_THRESHOLD_SECS = 30.0


def _get_cached(symbol: str, stale_threshold_secs: float) -> float | None:
    """キャッシュが新鮮なら price を返す。stale なら None を返す（raise しない）。"""
    entry = _price_cache.get(symbol)
    if entry is None:
        return None
    price, ts = entry
    age = time.monotonic() - ts
    if age > stale_threshold_secs:
        log.warning(
            "[ChainGuard] cache stale: symbol=%s age=%.1fs threshold=%.1fs",
            symbol, age, stale_threshold_secs,
        )
        return None
    return price


def _store_cache(symbol: str, price: float) -> None:
    _price_cache[symbol] = (price, time.monotonic())


def _clear_cache(symbol: str | None = None) -> None:
    """テスト・リセット用。symbol=None で全クリア。"""
    if symbol is None:
        _price_cache.clear()
    else:
        _price_cache.pop(symbol, None)


# ── 公開 API ─────────────────────────────────────────────────────────────────

def get_chain_center_price(
    symbol: str,
    market_data: Any,
    *,
    stale_threshold_secs: float = _DEFAULT_STALE_THRESHOLD_SECS,
    allow_cache_on_error: bool = False,
) -> float:
    """ChainGuard 向け center price を動的取得して返す。

    spy_bot.py:5525 の `_cached_spy_price` 代入欠落を根治する wrapper。

    Args:
        symbol:               銘柄コード (例: "US.SPY")
        market_data:          MarketDataProtocol または dict-like オブジェクト
        stale_threshold_secs: キャッシュ有効期限 (秒)。デフォルト 30s。
        allow_cache_on_error: True なら fetch 失敗時に stale キャッシュを
                              フォールバックとして使用する（非推奨・テスト用）。

    Returns:
        float: 最新の center 価格 (underlying last price)

    Raises:
        MissingPriceError:  market_data から price を取得できなかった
        StalePriceError:    stale キャッシュのみ残存 + allow_cache_on_error=False
        ChainGuardError:    上記以外の取得失敗
    """
    if not symbol:
        raise ChainGuardError("symbol must be a non-empty string")

    # ── (1) Protocol オブジェクト経由で取得 ──────────────────────────────────
    raw_price: float | None = None
    fetch_error: Exception | None = None

    if isinstance(market_data, MarketDataProtocol):
        try:
            raw_price = market_data.get_last_price(symbol)
        except Exception as exc:  # noqa: BLE001
            fetch_error = exc
            log.warning("[ChainGuard] get_last_price raised: %s", exc)

    # ── (2) dict fallback ─────────────────────────────────────────────────────
    elif isinstance(market_data, dict):
        raw_price = _extract_price_from_dict(market_data, symbol)
        if raw_price is None:
            fetch_error = MissingPriceError(
                f"[ChainGuard] No price key in dict for symbol={symbol!r}. "
                f"Tried keys: {_PRICE_KEYS}"
            )

    else:
        raise ChainGuardError(
            f"[ChainGuard] market_data type unsupported: {type(market_data)!r}. "
            "Implement MarketDataProtocol.get_last_price() or pass dict."
        )

    # ── (3) 価格の有効性チェック ───────────────────────────────────────────────
    if raw_price is not None:
        if raw_price <= 0:
            raise MissingPriceError(
                f"[ChainGuard] Received non-positive price={raw_price} for {symbol!r}. "
                "Data source may be returning placeholder."
            )
        # 有効価格 → キャッシュ更新して返却
        _store_cache(symbol, raw_price)
        log.debug("[ChainGuard] center_price=%s symbol=%s (fresh)", raw_price, symbol)
        return raw_price

    # ── (4) 取得失敗 → stale cache フォールバック判定 ─────────────────────────
    stale_price = _price_cache.get(symbol)
    if stale_price is not None and allow_cache_on_error:
        log.warning(
            "[ChainGuard] Using stale cache fallback: symbol=%s price=%s",
            symbol, stale_price[0],
        )
        return stale_price[0]

    # ── (5) 取得完全失敗 → raise ──────────────────────────────────────────────
    if fetch_error is not None:
        raise MissingPriceError(
            f"[ChainGuard] Cannot obtain center price for {symbol!r}: {fetch_error}"
        ) from fetch_error

    raise MissingPriceError(
        f"[ChainGuard] Cannot obtain center price for {symbol!r}: "
        "market_data returned None and no stale cache available."
    )


def get_chain_center_price_with_fallback(
    symbol: str,
    market_data: Any,
    fallback_price: float,
    *,
    stale_threshold_secs: float = _DEFAULT_STALE_THRESHOLD_SECS,
) -> tuple[float, str]:
    """ChainGuard center price を取得し、失敗時は fallback_price を使用する。

    spy_bot.py の既存ロジックを壊さずに段階的移行したい場合向け。

    Returns:
        (price, source):
            source = "live"     新鮮なデータ取得成功
            source = "cache"    stale キャッシュを使用
            source = "fallback" 全て失敗して fallback_price を使用
    """
    try:
        price = get_chain_center_price(
            symbol, market_data, stale_threshold_secs=stale_threshold_secs
        )
        return price, "live"
    except ChainGuardError:
        pass

    # stale cache 試行
    cached = _get_cached(symbol, stale_threshold_secs * 10)  # fallback 時は 10x 許容
    if cached is not None:
        return cached, "cache"

    log.error(
        "[ChainGuard] All price sources failed for %s. Using fallback=%s",
        symbol, fallback_price,
    )
    return fallback_price, "fallback"
