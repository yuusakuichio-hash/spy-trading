"""atlas_v3/ops/realized_volatility.py — SPY 過去 history から HV 計算

Why
---
VRP (Variance Risk Premium) = Implied Vol - Realized Vol を算出するには
実現ボラ (HV) が必要。公式 IV (VIX) と HV の差が VRP で、戦術の risk-on/off 判定に使う。

設計:
- moomoo OpenD で SPY 日次 history (close 価格) 取得
- 日次 log return = ln(p_t / p_{t-1})
- std × sqrt(252) × 100 = 年率 HV (% 表示)
- 30 日 lookback デフォルト・短期は 10 日・長期は 60 日

精度
----
- 学術論文標準 (Bollerslev・Tauchen・Zhou 2009 等)
- close-to-close で計算 (intraday HV は moomoo の bar data 必要・別途)
- 30 日 → annualized HV % 表示で VIX (%) と直接比較可能
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Optional

log = logging.getLogger(__name__)

_TRADING_DAYS_PER_YEAR = 252
_DEFAULT_LOOKBACK_DAYS = 30


def calc_hv_from_closes(closes: list[float]) -> Optional[float]:
    """日次 close 価格リストから年率 HV (%) を計算する。

    Args:
        closes: 日次 close 価格 (時系列順・古い→新しい)

    Returns:
        年率 HV (%) または None (data 不足時)
    """
    if not closes or len(closes) < 3:
        return None

    # 日次 log return
    log_returns: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            continue
        log_returns.append(math.log(curr / prev))

    if len(log_returns) < 2:
        return None

    # std (sample・unbiased estimator: ddof=1)
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    daily_std = math.sqrt(variance)

    # 年率化: daily_std × sqrt(252) × 100 = annualized HV (%)
    hv_annual_pct = daily_std * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100.0
    return hv_annual_pct


def estimate_hv_from_moomoo(
    quote_ctx,
    underlying_code: str = "US.SPY",
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    end_date: Optional[datetime.date] = None,
) -> Optional[float]:
    """moomoo OpenD 経由で SPY history を取得して HV を計算する。

    Args:
        quote_ctx: futu.OpenQuoteContext (None なら None 返却)
        underlying_code: "US.SPY"
        lookback_days: 過去日数 (default 30 日)
        end_date: 終了日 (None なら今日)

    Returns:
        年率 HV (%) または None (取得失敗時)
    """
    if quote_ctx is None:
        return None

    try:
        import futu as ft
    except ImportError:
        log.warning("[HV] futu module not available")
        return None

    if end_date is None:
        end_date = datetime.date.today()
    # lookback_days 日前を start に (週末考慮で +余裕日)
    start_date = end_date - datetime.timedelta(days=int(lookback_days * 1.5) + 5)

    try:
        ret, kline_df, _ = quote_ctx.request_history_kline(
            underlying_code,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            ktype=ft.KLType.K_DAY,
        )
        if ret != ft.RET_OK or kline_df is None or len(kline_df) == 0:
            log.debug("[HV] kline 取得失敗: ret=%s", ret)
            return None

        # close 価格 (時系列順・古い→新しい)
        closes = kline_df["close"].astype(float).tolist()
        if len(closes) > lookback_days:
            closes = closes[-lookback_days:]  # 直近 lookback_days 日

        hv = calc_hv_from_closes(closes)
        if hv is not None:
            log.info("[HV] %s 過去 %d 日 HV(年率)=%.2f%%", underlying_code, len(closes), hv)
        return hv

    except Exception as e:
        log.warning("[HV] 計算失敗: %s", e)
        return None


def estimate_ivr_proxy_from_hv_history(
    quote_ctx,
    underlying_code: str = "US.SPY",
    rank_lookback_days: int = 252,
    hv_window_days: int = 30,
    end_date: Optional[datetime.date] = None,
) -> Optional[float]:
    """過去 252 日の rolling HV から現在 HV の percentile rank を IVR proxy として返す。

    本来の IVR (Implied Volatility Rank) は ATM IV の 252 日 percentile だが、
    moomoo で過去 252 日の毎日 ATM IV を取得するのは bulk API なし・heavy。
    HV と IV は相関高い (Bollerslev・Tauchen・Zhou 2009) ため HV proxy で代替。

    Args:
        quote_ctx: futu.OpenQuoteContext
        underlying_code: "US.SPY"
        rank_lookback_days: percentile rank の lookback 日数 (default 252 = 1 年)
        hv_window_days: 各時点 rolling HV の window (default 30 日)
        end_date: 終了日 (None なら今日)

    Returns:
        IVR proxy (0-100・% 表示) または None
    """
    if quote_ctx is None:
        return None

    try:
        import futu as ft
    except ImportError:
        return None

    if end_date is None:
        end_date = datetime.date.today()
    # rank_lookback + hv_window + 余裕日
    start_date = end_date - datetime.timedelta(
        days=int((rank_lookback_days + hv_window_days) * 1.5) + 10,
    )

    try:
        ret, kline_df, _ = quote_ctx.request_history_kline(
            underlying_code,
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            ktype=ft.KLType.K_DAY,
        )
        if ret != ft.RET_OK or kline_df is None or len(kline_df) == 0:
            log.debug("[IVR-proxy] kline 取得失敗: ret=%s", ret)
            return None

        closes = kline_df["close"].astype(float).tolist()
        if len(closes) < hv_window_days + 10:
            log.debug("[IVR-proxy] data 不足: %d 日", len(closes))
            return None

        # rolling HV history (各日について過去 hv_window_days の HV)
        rolling_hvs: list[float] = []
        for i in range(hv_window_days, len(closes)):
            window = closes[i - hv_window_days:i]
            hv_i = calc_hv_from_closes(window)
            if hv_i is not None:
                rolling_hvs.append(hv_i)

        if len(rolling_hvs) < 30:  # 最低 30 日 history
            return None

        # 現在 HV (直近 hv_window_days)
        current_hv = calc_hv_from_closes(closes[-hv_window_days:])
        if current_hv is None:
            return None

        # rank_lookback_days 範囲に絞る
        if len(rolling_hvs) > rank_lookback_days:
            rolling_hvs = rolling_hvs[-rank_lookback_days:]

        # percentile rank (0-100)
        below_count = sum(1 for h in rolling_hvs if h < current_hv)
        ivr_proxy = (below_count / len(rolling_hvs)) * 100.0
        log.info(
            "[IVR-proxy] %s 現在 HV=%.2f%% / rolling %d 日中 percentile=%.1f",
            underlying_code, current_hv, len(rolling_hvs), ivr_proxy,
        )
        return ivr_proxy

    except Exception as e:
        log.warning("[IVR-proxy] 計算失敗: %s", e)
        return None
