"""atlas_v3/ops/vix_estimator.py — SPY ATM straddle IV から VIX 推定

Why
---
moomoo OpenD は US stock index series (VIX / SPX 等) を配信しないため
(出典: futu-api error "Do not support US stock index series")、VIX 値を
直接取得する代わりに SPY 0DTE ATM Call/Put の IV 平均から VIX 近似値を算出する。

設計 (spy_bot.py:3254-3383 _get_vix_from_atm_straddle() の atlas_v3 移植版):

1. SPY 現在値を get_market_snapshot で取得
2. ET 現在日付の expiry を取得 (0DTE)
3. get_option_chain で ATM 付近の Call / Put コードを取得
4. get_market_snapshot で option_implied_volatility を直接取得
5. IV が 0 の場合は Black-Scholes (brentq) で mid-price から逆算
6. Call/Put IV 平均 × 100 = VIX 近似値

精度
----
- SPY ATM straddle IV ≈ 公式 VIX (誤差 0.5-1.0 ポイント程度)
- リアルタイム (moomoo 既接続経路)
- paper / live で同一 feed (移行時の挙動変化なし)

公式 VIX (CBOE) との差異要因:
- VIX 公式は SPX (cash settled) option の広範な strike から計算
- SPY は ETF (配当・税制 1099)・1 strike のみ評価
- 結果: SPY straddle IV は VIX より若干高め (0.5-1.0 ポイント) に出る傾向

VIX 直接取得との比較
--------------------
| 方法                    | 値の正確さ | タイムリー性 | コスト  |
|-------------------------|----------|-------------|--------|
| CBOE 直接 feed          | 公式値    | 1秒以内       | 月額数百ドル~ |
| yfinance ^VIX           | 公式値    | 15分遅延      | 無料   |
| moomoo SPY straddle IV  | 近似      | リアルタイム  | 無料   |  ← 本実装
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Optional

log = logging.getLogger(__name__)

# ET timezone (零依存・標準ライブラリのみ)
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    _ET = datetime.timezone(datetime.timedelta(hours=-5))  # 簡易 fallback

# Black-Scholes IV 範囲制限 (合理性チェック)
_IV_MIN = 0.01  # 1%
_IV_MAX = 5.0   # 500%


def _get_expiry_today_et(now_et: Optional[datetime.datetime] = None) -> str:
    """ET 現在日付 (YYYY-MM-DD) を返す。0DTE option expiry に使用。

    ET 平日のみ・週末は次月曜・祝日処理は半日 guard 側に委譲。
    """
    if now_et is None:
        now_et = datetime.datetime.now(_ET)
    candidate = now_et.date()
    # 週末は次月曜
    while candidate.weekday() >= 5:  # 5=Saturday, 6=Sunday
        candidate += datetime.timedelta(days=1)
    return candidate.strftime("%Y-%m-%d")


def _bs_price(spy_price: float, atm_strike: float, T: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes 公式で option price を計算 (金利・配当ゼロ近似)。

    brentq で IV 逆算する際の price 計算に使用。
    """
    if sigma <= 0 or T <= 0:
        return 0.0
    d1 = (math.log(spy_price / atm_strike) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))

    if is_call:
        return spy_price * _norm_cdf(d1) - atm_strike * _norm_cdf(d2)
    else:
        return atm_strike * _norm_cdf(-d2) - spy_price * _norm_cdf(-d1)


def estimate_vix_from_spy_atm(
    quote_ctx,
    underlying_code: str = "US.SPY",
    now_et: Optional[datetime.datetime] = None,
) -> Optional[float]:
    """SPY 0DTE ATM straddle IV から VIX 近似値を算出する。

    Args:
        quote_ctx: futu.OpenQuoteContext インスタンス (None なら None 返却)
        underlying_code: "US.SPY" 等の futu code
        now_et: ET 現在時刻 (None なら datetime.now)

    Returns:
        VIX 近似値 (float) または None (取得失敗時)
    """
    if quote_ctx is None:
        return None

    try:
        import futu as ft
    except ImportError:
        log.warning("[VIX-ATM-IV] futu module not available")
        return None

    if now_et is None:
        now_et = datetime.datetime.now(_ET)

    try:
        # 1. SPY 現在値
        ret, spy_snap = quote_ctx.get_market_snapshot([underlying_code])
        if ret != ft.RET_OK or spy_snap is None or len(spy_snap) == 0:
            log.debug("[VIX-ATM-IV] SPY snapshot 取得失敗: ret=%s", ret)
            return None
        spy_price = float(spy_snap.iloc[0].get("last_price", 0) or 0)
        if spy_price <= 0:
            log.debug("[VIX-ATM-IV] SPY price <= 0")
            return None

        # 2. 0DTE expiry
        expiry = _get_expiry_today_et(now_et)

        # 3-5. ATM Call/Put の IV を取得
        iv_values: list[float] = []
        for opt_type_ft, opt_label in [
            (ft.OptionType.CALL, "CALL"),
            (ft.OptionType.PUT, "PUT"),
        ]:
            ret_c, chain_df = quote_ctx.get_option_chain(
                underlying_code, start=expiry, end=expiry, option_type=opt_type_ft,
            )
            if ret_c != ft.RET_OK or chain_df is None or len(chain_df) == 0:
                log.debug("[VIX-ATM-IV] chain 取得失敗: %s %s", opt_label, expiry)
                continue

            # ATM (現在値に最も近い strike)
            chain_df = chain_df.copy()
            chain_df["strike_price"] = chain_df["strike_price"].astype(float)
            chain_df["dist"] = (chain_df["strike_price"] - spy_price).abs()
            atm_row = chain_df.loc[chain_df["dist"].idxmin()]
            atm_code = atm_row["code"]
            atm_strike = float(atm_row["strike_price"])

            # 4. snapshot で option_implied_volatility 取得
            ret_s, opt_snap = quote_ctx.get_market_snapshot([atm_code])
            if ret_s != ft.RET_OK or opt_snap is None or len(opt_snap) == 0:
                log.debug("[VIX-ATM-IV] opt snapshot 失敗: %s", atm_code)
                continue

            srow = opt_snap.iloc[0]
            iv_direct = float(srow.get("option_implied_volatility", 0) or 0)

            if iv_direct > 0:
                # moomoo OpenD は option_implied_volatility を % 値で配信 (例: 11.87 = 11.87%)
                # 一方 BS 逆算 (後段) は小数値 (0.1187) で返るため、ここで両者の単位を合わせる
                # 0.0 < x < 1.0 → 小数形式と判定して × 100、それ以上 → 既に % 形式
                if iv_direct < 1.0:
                    iv_pct = iv_direct * 100.0
                else:
                    iv_pct = iv_direct
                iv_values.append(iv_pct)
                log.debug(
                    "[VIX-ATM-IV] %s K=%.1f IV(direct)=%.1f%% (raw=%.4f)",
                    opt_label, atm_strike, iv_pct, iv_direct,
                )
                continue

            # 5. IV=0 → Black-Scholes (brentq) で mid-price から逆算
            bid = float(srow.get("bid_price", 0) or 0)
            ask = float(srow.get("ask_price", 0) or 0)
            mid = (bid + ask) / 2.0
            if mid <= 0:
                log.debug("[VIX-ATM-IV] %s mid=0 skip BS", opt_label)
                continue

            # 残存時間 T (年率換算): 当日 16:00 ET まで
            close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
            t_sec = (close_et - now_et).total_seconds()
            if t_sec <= 60:  # 市場終了直前は計算不安定
                log.debug("[VIX-ATM-IV] t_sec=%.0f < 60s skip BS", t_sec)
                continue
            T = max(t_sec / (365.0 * 24.0 * 3600.0), 1e-6)

            try:
                from scipy.optimize import brentq
                is_call = opt_label == "CALL"
                iv_bs = brentq(
                    lambda s: _bs_price(spy_price, atm_strike, T, s, is_call) - mid,
                    1e-4, 10.0, xtol=1e-4, maxiter=100,
                )
                if _IV_MIN < iv_bs < _IV_MAX:
                    # BS 逆算は小数形式 (0.1187) → % に変換して追加
                    iv_pct = iv_bs * 100.0
                    iv_values.append(iv_pct)
                    log.debug(
                        "[VIX-ATM-IV] %s K=%.1f mid=%.3f IV(BS)=%.1f%%",
                        opt_label, atm_strike, mid, iv_pct,
                    )
            except Exception as e:
                log.debug("[VIX-ATM-IV] brentq 失敗 %s: %s", opt_label, e)

        if not iv_values:
            return None

        # iv_values は既に % 単位 (上記 direct / BS 経路で統一済) なので avg のみ
        vix_approx = sum(iv_values) / len(iv_values)
        log.info("[VIX-ATM-IV] ATMストラドルIVからVIX算出: %.1f", vix_approx)
        return vix_approx

    except Exception as e:
        log.warning("[VIX-ATM-IV] 計算失敗: %s", e)
        return None
