"""atlas_v3/bots/engines/earnings_calendar_check.py — 決算近接日チェックモジュール

NVDA 2024-02-21 型の問題: 決算発表直前にプレミアム売りを建てると、
発表直前の IV スパイク / ギャップリスクにさらされ損失が急拡大する。
本モジュールはその再発を防ぐ。

概要:
    - 指定 symbol の直近決算日（next earnings date）を Finnhub API / キャッシュから取得
    - 今日から決算日までの営業日差（business days）を算出
    - proximity_days 以内なら True（ブロック）を返す

設計原則:
    - spy_bot.py / chronos_bot.py への import 禁止
    - 固定銘柄リスト禁止: 全銘柄動的に Finnhub から取得
    - キャッシュ TTL: 6 時間（過度な API 呼び出し防止）
    - earnings_date_fn DI: テスト用 stub 注入を完全サポート
    - pandas.bdate_range で営業日差を算出（米国祝日は近似・精度優先）
    - earnings_date_fn が None を返した場合 → safe_default=True でブロック

CC <= 10 規律準拠
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# キャッシュ設定
# ---------------------------------------------------------------------------

_CACHE_DIR = Path(os.environ.get("SPY_DATA_DIR", Path(__file__).parent.parent.parent.parent / "data"))
_EARNINGS_PROXIMITY_CACHE_FILE = _CACHE_DIR / "earnings_proximity_cache.json"
_CACHE_TTL_SEC: int = 6 * 3600  # 6 時間


# ---------------------------------------------------------------------------
# 営業日差計算
# ---------------------------------------------------------------------------

def _business_days_until(today: datetime.date, target: datetime.date) -> int:
    """today から target（exclusive today, inclusive target）までの営業日数を返す。

    pd.bdate_range(today, target) の長さ - 1 で算出する。
    today == target なら 0 を返す。
    target < today なら 0 を返す（過去の決算はブロックしない）。

    Args:
        today:  起点日（当日）
        target: 終点日（決算日）

    Returns:
        0 以上の整数
    """
    if target <= today:
        return 0
    try:
        import pandas as pd  # type: ignore
        rng = pd.bdate_range(pd.Timestamp(today), pd.Timestamp(target))
        return max(0, len(rng) - 1)
    except ImportError:
        # pandas 未インストール時はカレンダー日差で近似（営業日は 71% と想定）
        delta = (target - today).days
        return max(0, round(delta * 0.714))


# ---------------------------------------------------------------------------
# Finnhub 経由の次回決算日取得（キャッシュあり）
# ---------------------------------------------------------------------------

def _fetch_next_earnings_date_finnhub(symbol: str, api_key: str) -> Optional[datetime.date]:
    """Finnhub の Earnings Calendar API から symbol の直近将来決算日を取得する。

    エンドポイント: GET /calendar/earnings?from=today&to=today+90days&symbol={symbol}

    Args:
        symbol:  銘柄コード（例: "NVDA"）
        api_key: Finnhub API key

    Returns:
        直近将来決算日 / None（取得失敗・該当なし）
    """
    if not api_key:
        log.debug("[EarningsProximity] FINNHUB_API_KEY 未設定 → None")
        return None

    import requests  # type: ignore

    today = datetime.date.today()
    to_date = (today + datetime.timedelta(days=90)).isoformat()
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings"
        f"?from={today.isoformat()}&to={to_date}"
        f"&symbol={symbol}&token={api_key}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("earningsCalendar", [])
        dates = []
        for item in items:
            d = _parse_date(item.get("date", ""))
            if d and d >= today:
                dates.append(d)
        if not dates:
            return None
        return min(dates)
    except Exception as e:
        log.warning("[EarningsProximity] Finnhub fetch error: symbol=%s %s", symbol, e)
        return None


def _parse_date(date_str: str) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(date_str[:10])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# キャッシュ付き決算日取得
# ---------------------------------------------------------------------------

def _load_proximity_cache() -> dict:
    if not _EARNINGS_PROXIMITY_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_EARNINGS_PROXIMITY_CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_proximity_cache(cache: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _EARNINGS_PROXIMITY_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.warning("[EarningsProximity] cache save error: %s", e)


def get_next_earnings_date(
    symbol: str,
    api_key: str = "",
    cache_ttl_sec: int = _CACHE_TTL_SEC,
) -> Optional[datetime.date]:
    """symbol の直近将来決算日を返す（キャッシュあり・TTL=6h）。

    1. キャッシュ内に当日有効なエントリーがあればそれを返す
    2. Finnhub API から取得してキャッシュに保存
    3. 取得失敗時は None を返す

    Args:
        symbol:        銘柄コード
        api_key:       Finnhub API key（省略時は FINNHUB_API_KEY env から取得）
        cache_ttl_sec: キャッシュ有効期間（秒）

    Returns:
        datetime.date または None
    """
    resolved_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
    cache = _load_proximity_cache()
    entry = cache.get(symbol, {})

    if entry:
        cached_date_str = entry.get("date", "")
        cached_ts = entry.get("ts", 0)
        cached_today = entry.get("fetched_on", "")
        today_str = datetime.date.today().isoformat()
        age = time.time() - cached_ts
        if cached_today == today_str and age < cache_ttl_sec:
            result = _parse_date(cached_date_str) if cached_date_str else None
            log.debug(
                "[EarningsProximity] cache hit: symbol=%s next_earnings=%s (age=%.0fs)",
                symbol, result, age,
            )
            return result

    # API 取得
    next_date = _fetch_next_earnings_date_finnhub(symbol, resolved_key)

    # キャッシュ更新（取得失敗も null として保存）
    cache[symbol] = {
        "date": next_date.isoformat() if next_date else "",
        "ts": time.time(),
        "fetched_on": datetime.date.today().isoformat(),
    }
    _save_proximity_cache(cache)

    log.info(
        "[EarningsProximity] fetched: symbol=%s next_earnings=%s",
        symbol, next_date,
    )
    return next_date


# ---------------------------------------------------------------------------
# メイン公開 API
# ---------------------------------------------------------------------------

def is_near_earnings(
    symbol: str,
    proximity_days: int,
    today: Optional[datetime.date] = None,
    earnings_date_fn: Optional[Callable[[str], Optional[datetime.date]]] = None,
    safe_default: bool = True,
) -> tuple[bool, str]:
    """決算発表まで proximity_days 営業日以内かを判定する。

    Args:
        symbol:           銘柄コード
        proximity_days:   ブロック閾値（この営業日数以内ならブロック）
        today:            基準日（None なら datetime.date.today()）
        earnings_date_fn: 決算日取得関数 DI（テスト用 stub 注入・None なら実 API）
                          シグネチャ: (symbol: str) -> Optional[datetime.date]
        safe_default:     True の場合、決算日取得失敗時はブロック（安全側）

    Returns:
        (blocked: bool, reason: str)
        blocked=True   → エントリー禁止（proximity_days 以内 or 取得失敗+safe_default）
        blocked=False  → エントリー許可

    使用例:
        blocked, reason = is_near_earnings("NVDA", proximity_days=5)
        if blocked:
            return Decision(should_enter=False, reason=reason)
    """
    if proximity_days is None or proximity_days <= 0:
        return False, "earnings_proximity_check: skipped (proximity_days=None or <=0)"

    base_date = today or datetime.date.today()

    if earnings_date_fn is not None:
        next_date = earnings_date_fn(symbol)
        # earnings_date_fn が datetime.date を返す場合はそのまま使う
        # date 以外（例: datetime.datetime）が返されたら .date() で変換
        if next_date is not None and hasattr(next_date, "date") and callable(next_date.date):
            next_date = next_date.date()
    else:
        next_date = get_next_earnings_date(symbol)

    if next_date is None:
        if safe_default:
            reason = (
                f"earnings_proximity_block: "
                f"symbol={symbol} next_earnings=unknown safe_default=True"
            )
            log.warning(
                "[EarningsProximity] %s: 決算日取得失敗 → safe_default でブロック",
                symbol,
            )
            return True, reason
        return False, f"earnings_proximity_check: symbol={symbol} next_earnings=unknown safe_default=False"

    bdays = _business_days_until(base_date, next_date)

    if bdays <= proximity_days:
        reason = (
            f"earnings_proximity_block: "
            f"symbol={symbol} next_earnings={next_date} "
            f"bdays_until={bdays} <= proximity_days={proximity_days}"
        )
        log.info(
            "[EarningsProximity] %s: 決算 %d 営業日前 → ブロック (proximity_days=%d)",
            symbol, bdays, proximity_days,
        )
        return True, reason

    reason = (
        f"earnings_proximity_ok: "
        f"symbol={symbol} next_earnings={next_date} "
        f"bdays_until={bdays} > proximity_days={proximity_days}"
    )
    log.debug(
        "[EarningsProximity] %s: 決算 %d 営業日前 → 許可 (proximity_days=%d)",
        symbol, bdays, proximity_days,
    )
    return False, reason
