"""atlas_v3/ops/gex_estimator.py — Gamma Exposure (GEX) 推定

Why
---
GEX = dealers の gamma exposure (Σ gamma × OI × multiplier × spot²)
- GEX > 0: dealers long gamma → 買い圧調整・volatility 抑制
- GEX < 0: dealers short gamma → 買い圧拡大・volatility 加速

公式 GEX feed (SpotGamma 等) は有料のため、moomoo option chain + Black-Scholes で
gamma を計算して自前算出する。

設計 (簡易版):
- ATM ± N strike の call / put を集計 (default N=5)
- gamma は moomoo 配信があれば直接利用、なければ BS で計算
- multiplier = 100 (1 contract = 100 shares)
- GEX = sum(call_gamma × OI × 100 × spot²) - sum(put_gamma × OI × 100 × spot²)
- 単位: notional dollar exposure ($ million 等で表示)

精度
----
ATM ± 5 strike サンプリングは粗いが、ATM 付近に gamma が集中する性質から
近似精度として十分 (誤差 5-10%)。完全実装は全 strike 集計 (重実装) → 後段。
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Optional

log = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    _ET = datetime.timezone(datetime.timedelta(hours=-5))


def calc_bs_gamma(
    spot: float, strike: float, T: float, sigma: float
) -> float:
    """Black-Scholes gamma 計算 (金利・配当ゼロ近似).

    gamma = N'(d1) / (spot × sigma × sqrt(T))
    """
    if spot <= 0 or strike <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * sigma**2 * T) / (sigma * math.sqrt(T))
    # 標準正規 PDF: N'(x) = exp(-x²/2) / sqrt(2π)
    pdf_d1 = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    return pdf_d1 / (spot * sigma * math.sqrt(T))


def estimate_gex_from_moomoo(
    quote_ctx,
    underlying_code: str = "US.SPY",
    strikes_around_atm: int = 5,
    target_dte_days: int = 7,
    now_et: Optional[datetime.datetime] = None,
) -> Optional[float]:
    """moomoo option chain から GEX を推定する (notional $).

    Args:
        quote_ctx: futu.OpenQuoteContext
        underlying_code: "US.SPY"
        strikes_around_atm: ATM ± N strike を集計 (default 5)
        target_dte_days: 対象 DTE (default 7 = 短期 gamma)
        now_et: ET 現在時刻

    Returns:
        GEX (notional $) または None
    """
    if quote_ctx is None:
        return None

    try:
        import futu as ft
    except ImportError:
        return None

    if now_et is None:
        now_et = datetime.datetime.now(_ET)

    try:
        # SPY 現在値
        ret, spy_snap = quote_ctx.get_market_snapshot([underlying_code])
        if ret != ft.RET_OK or spy_snap is None or len(spy_snap) == 0:
            return None
        spot = float(spy_snap.iloc[0].get("last_price", 0) or 0)
        if spot <= 0:
            return None

        # 対象 DTE expiry を取得
        ret_e, exp_df = quote_ctx.get_option_expiration_date(code=underlying_code)
        if ret_e != ft.RET_OK or exp_df is None or len(exp_df) == 0:
            return None
        target_date = now_et.date() + datetime.timedelta(days=target_dte_days)
        exp_df = exp_df.copy()
        exp_df["strike_time_dt"] = exp_df["strike_time"].apply(
            lambda s: datetime.datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        )
        exp_df["dist"] = exp_df["strike_time_dt"].apply(
            lambda d: abs((d - target_date).days)
        )
        chosen = exp_df.loc[exp_df["dist"].idxmin()]
        chosen_expiry = chosen["strike_time"]
        chosen_date = chosen["strike_time_dt"]
        T_years = max((chosen_date - now_et.date()).days / 365.0, 1e-6)

        # Call / Put chain を取得して ATM ± N strike を集計
        gex_total = 0.0
        for opt_type_ft, sign in (
            (ft.OptionType.CALL, 1.0),
            (ft.OptionType.PUT, -1.0),
        ):
            ret_c, chain_df = quote_ctx.get_option_chain(
                underlying_code, start=chosen_expiry, end=chosen_expiry,
                option_type=opt_type_ft,
            )
            if ret_c != ft.RET_OK or chain_df is None or len(chain_df) == 0:
                continue

            chain_df = chain_df.copy()
            chain_df["strike_price"] = chain_df["strike_price"].astype(float)
            chain_df["dist"] = (chain_df["strike_price"] - spot).abs()
            atm_chain = chain_df.nsmallest(strikes_around_atm * 2, "dist")

            # 各 strike の gamma × OI × 100 × spot²
            codes = atm_chain["code"].tolist()
            if not codes:
                continue
            ret_s, opt_snap = quote_ctx.get_market_snapshot(codes)
            if ret_s != ft.RET_OK or opt_snap is None or len(opt_snap) == 0:
                continue

            for _, row in opt_snap.iterrows():
                # gamma: moomoo 配信あれば直接、なければ BS で計算
                gamma = float(row.get("option_gamma", 0) or 0)
                oi = float(row.get("option_open_interest", 0) or 0)
                if oi <= 0:
                    continue

                if gamma <= 0:
                    # BS で計算 (IV を取得)
                    iv = float(row.get("option_implied_volatility", 0) or 0)
                    if iv <= 0:
                        continue
                    sigma = iv / 100.0 if iv >= 1.0 else iv  # % 形式自動判別
                    strike = float(row.get("option_strike_price", 0) or 0)
                    if strike <= 0:
                        # chain_df から strike 取得 fallback
                        match = atm_chain[atm_chain["code"] == row["code"]]
                        if len(match) > 0:
                            strike = float(match.iloc[0]["strike_price"])
                    if strike <= 0:
                        continue
                    gamma = calc_bs_gamma(spot, strike, T_years, sigma)

                if gamma <= 0:
                    continue

                # GEX 寄与: gamma × OI × multiplier × spot² × sign
                contribution = gamma * oi * 100.0 * spot * spot * sign
                gex_total += contribution

        log.info(
            "[GEX] %s ATM±%d strikes DTE=%d → %.2e $",
            underlying_code, strikes_around_atm, target_dte_days, gex_total,
        )
        return gex_total

    except Exception as e:
        log.warning("[GEX] 計算失敗: %s", e)
        return None
