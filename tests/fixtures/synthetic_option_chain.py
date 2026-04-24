"""tests/fixtures/synthetic_option_chain.py — 合成 option chain fixture
(Sprint 2 C-017 補完 / 2026-04-25 新規)

目的:
  moomoo paper API 接続前の dry-run / 回帰テストで使う option chain を
  Black-Scholes + GBM で合成生成する。実 API 不要でオフラインで Greeks /
  IV / OI / volume まで揃った DataFrame を返す。

設計方針:
  1. 既存コード非影響: tests/ 配下のみで完結・本番 import path に混ざらない
  2. moomoo get_option_chain 互換 column 構成
     (code / strike_price / option_type / delta / gamma / theta / vega /
      implied_volatility / open_interest / volume)
  3. option code 形式は common/option_code.py の build_option_code と整合
     (US.SPXW260425C05400000 等) — ただし common は参照のみで依存なし
     (fixture が壊れて code.py も壊すリスクを回避)
  4. numpy + pandas のみ使用 — scipy なし (N(x) は erf で自前実装)
  5. 極端シナリオ 5 パターン:
     normal / vix_spike_30 / gap_up_5 / crash_10 / iv_crush

Greeks self-consistency 担保:
  - Put-Call parity: C - P = S - K*exp(-rT)  (配当ゼロ前提)
  - Delta の符号規約: CALL は +, PUT は -
  - abs(delta_call) + abs(delta_put) = 1.0 (同一 strike・q=0・同一 IV 時)
  - Gamma_call == Gamma_put (同一 strike・同一 IV)
  - Vega_call == Vega_put
  - Theta < 0 (long option 保有側)

Non-goals:
  - 実データの再現精度は追わない (近似で OK)
  - skew / term structure は単純化 (一律 IV or moneyness-linear)
  - Greeks は analytic BSM のみ (monte carlo は扱わない)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd


# ────────────────────────────────────────────────────────────
# 1. Black-Scholes 計算 (scipy 不要・erf 直接)
# ────────────────────────────────────────────────────────────


def _norm_cdf(x: float) -> float:
    """標準正規分布の CDF. math.erf で実装 (scipy 不要)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """標準正規分布の PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(
    S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0
) -> tuple[float, float]:
    """Black-Scholes の d1, d2.

    Args:
        S: 原資産価格
        K: 行使価格
        T: 満期までの年数 (days/365)
        r: リスクフリーレート
        sigma: 年率ボラ (IV)
        q: 配当利回り

    Returns:
        (d1, d2)
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # 退化ケース: intrinsic value のみ
        return (float("nan"), float("nan"))
    vsqrt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vsqrt
    d2 = d1 - vsqrt
    return (d1, d2)


def bs_price_and_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: Literal["CALL", "PUT"],
    q: float = 0.0,
) -> dict:
    """BSM の price と Greeks (delta / gamma / theta / vega).

    Theta は 1 日あたりの値 (annual / 365) で返す。
    Vega は sigma が 0.01 (1%) 変動した時の価格変化 (per 1% IV)。

    退化ケース (T<=0 or sigma<=0) は intrinsic value + delta=±1/0, その他 0.
    """
    is_call = option_type.upper() == "CALL"

    # 退化ケース
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if is_call:
            price = max(S - K, 0.0)
            delta = 1.0 if S > K else (0.5 if S == K else 0.0)
        else:
            price = max(K - S, 0.0)
            delta = -1.0 if S < K else (-0.5 if S == K else 0.0)
        return {
            "price": price,
            "delta": delta,
            "gamma": 0.0,
            "theta": 0.0,
            "vega": 0.0,
        }

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    pdf_d1 = _norm_pdf(d1)

    if is_call:
        price = S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
        delta = disc_q * _norm_cdf(d1)
        theta_annual = (
            -(S * disc_q * pdf_d1 * sigma) / (2.0 * math.sqrt(T))
            - r * K * disc_r * _norm_cdf(d2)
            + q * S * disc_q * _norm_cdf(d1)
        )
    else:
        price = K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)
        delta = -disc_q * _norm_cdf(-d1)
        theta_annual = (
            -(S * disc_q * pdf_d1 * sigma) / (2.0 * math.sqrt(T))
            + r * K * disc_r * _norm_cdf(-d2)
            - q * S * disc_q * _norm_cdf(-d1)
        )

    gamma = (disc_q * pdf_d1) / (S * sigma * math.sqrt(T))
    vega_per_1pct = S * disc_q * pdf_d1 * math.sqrt(T) * 0.01
    theta_per_day = theta_annual / 365.0

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "theta": theta_per_day,
        "vega": vega_per_1pct,
    }


# ────────────────────────────────────────────────────────────
# 2. Symbol metadata (外部依存せず fixture 内で固定化)
# ────────────────────────────────────────────────────────────

# common/symbol_meta.py と同値に揃えるが、依存は避ける (fixture が壊れたら
# 本体も壊れる循環を防ぐ)
_SYMBOL_SPEC: dict[str, dict] = {
    "US..SPX": {
        "option_root":     "SPXW",   # 0DTE は SPXW
        "strike_interval": 5.0,
        "typical_price":   5400.0,
        "typical_iv":      0.15,
    },
    "US.SPY": {
        "option_root":     "SPY",
        "strike_interval": 1.0,
        "typical_price":   540.0,
        "typical_iv":      0.17,
    },
    "US.QQQ": {
        "option_root":     "QQQ",
        "strike_interval": 1.0,
        "typical_price":   470.0,
        "typical_iv":      0.20,
    },
    "US.IWM": {
        "option_root":     "IWM",
        "strike_interval": 0.5,
        "typical_price":   210.0,
        "typical_iv":      0.22,
    },
}


def get_supported_symbols() -> list[str]:
    """fixture がサポートする underlying symbol のリスト."""
    return list(_SYMBOL_SPEC.keys())


def _build_option_code(
    option_root: str, expiry_yyyymmdd: str, opt_type: str, strike: float
) -> str:
    """moomoo 互換 option code 生成.

    Format: US.{ROOT}{YYMMDD}{C|P}{strike*1000:08d}
    expiry_yyyymmdd: "20260425" (YYYYMMDD)
    """
    yymmdd = expiry_yyyymmdd[2:]  # "260425"
    cp = "C" if opt_type.upper() in ("CALL", "C") else "P"
    strike_i = int(round(strike * 1000))
    return f"US.{option_root}{yymmdd}{cp}{strike_i:08d}"


# ────────────────────────────────────────────────────────────
# 3. Scenario parameter
# ────────────────────────────────────────────────────────────

Scenario = Literal["normal", "vix_spike_30", "gap_up_5", "crash_10", "iv_crush"]
SCENARIOS: tuple[Scenario, ...] = (
    "normal",
    "vix_spike_30",
    "gap_up_5",
    "crash_10",
    "iv_crush",
)


@dataclass(frozen=True)
class ChainParams:
    """合成チェーン生成の入力パラメタ."""

    symbol: str                       # "US..SPX" / "US.SPY" / "US.QQQ" / "US.IWM"
    underlying_price: float           # 原資産価格
    iv: float                         # ATM base IV (年率・0.15 = 15%)
    expiry_days: float                # 満期までの日数 (0.0 = 0DTE 寄り付きと同じ)
    strike_range_pct: float = 0.05    # ATM から ±何 % 分を生成するか (0.05 = ±5%)
    risk_free_rate: float = 0.045     # 4.5%
    dividend_yield: float = 0.0
    scenario: Scenario = "normal"
    expiry_date_yyyymmdd: str = "20260425"  # option code に埋め込む
    # IV skew (moneyness=K/S - 1 に対する IV の線形 skew)
    # OTM put (moneyness<0) ほど IV が高い実市場を粗く再現
    iv_skew_per_100pct: float = -0.50
    # OI / volume 分布 (ATM を最大に・遠く離れたら減衰)
    atm_open_interest: int = 5000
    atm_volume: int = 1200
    seed: int = 12345


def apply_scenario(params: ChainParams) -> ChainParams:
    """シナリオに従って params を調整した新 instance を返す.

    - normal: そのまま
    - vix_spike_30: IV を 1.30x
    - gap_up_5: underlying を +5%
    - crash_10: underlying を -10% かつ IV を 1.40x
    - iv_crush: IV を 0.50x
    """
    s = params.scenario
    if s == "normal":
        return params
    if s == "vix_spike_30":
        return _replace(params, iv=params.iv * 1.30)
    if s == "gap_up_5":
        return _replace(params, underlying_price=params.underlying_price * 1.05)
    if s == "crash_10":
        return _replace(
            params,
            underlying_price=params.underlying_price * 0.90,
            iv=params.iv * 1.40,
        )
    if s == "iv_crush":
        return _replace(params, iv=params.iv * 0.50)
    raise ValueError(f"Unknown scenario: {s}")


def _replace(params: ChainParams, **kw) -> ChainParams:
    """dataclass frozen の replace (dataclasses.replace 相当 / 依存最小化)."""
    d = {
        k: getattr(params, k)
        for k in (
            "symbol", "underlying_price", "iv", "expiry_days",
            "strike_range_pct", "risk_free_rate", "dividend_yield",
            "scenario", "expiry_date_yyyymmdd", "iv_skew_per_100pct",
            "atm_open_interest", "atm_volume", "seed",
        )
    }
    d.update(kw)
    return ChainParams(**d)


# ────────────────────────────────────────────────────────────
# 4. チェーン生成本体
# ────────────────────────────────────────────────────────────


def _generate_strike_grid(params: ChainParams) -> np.ndarray:
    """ATM 中心に strike_interval 刻みで strike_range_pct ぶんの grid を返す."""
    spec = _SYMBOL_SPEC[params.symbol]
    interval = float(spec["strike_interval"])
    S = params.underlying_price

    # ATM を interval に丸める
    atm = round(S / interval) * interval
    span = S * params.strike_range_pct
    n_each_side = max(1, int(math.ceil(span / interval)))
    lo = atm - n_each_side * interval
    hi = atm + n_each_side * interval
    grid = np.arange(lo, hi + interval / 2.0, interval)
    # 負の strike は除外
    grid = grid[grid > 0]
    return grid


def generate_option_chain(params: ChainParams) -> pd.DataFrame:
    """Black-Scholes 合成 option chain を moomoo 互換 DataFrame で返す.

    Returns:
        columns = [
          "code", "strike_price", "option_type",
          "delta", "gamma", "theta", "vega",
          "implied_volatility", "open_interest", "volume",
          "last_price", "bid_price", "ask_price",
        ]
        rows = CALL 側全 strike + PUT 側全 strike (connect で longer)
    """
    if params.symbol not in _SYMBOL_SPEC:
        raise ValueError(
            f"Unsupported symbol: {params.symbol}. "
            f"Supported: {list(_SYMBOL_SPEC.keys())}"
        )

    p = apply_scenario(params)
    spec = _SYMBOL_SPEC[p.symbol]
    option_root = str(spec["option_root"])
    S = p.underlying_price
    # 0DTE の場合 T=0 だと BSM が退化するので最小 0.5h = 0.5/24/365 を下限に
    T = max(p.expiry_days / 365.0, 0.5 / 24.0 / 365.0)
    r = p.risk_free_rate
    q = p.dividend_yield

    rng = np.random.default_rng(p.seed)
    strikes = _generate_strike_grid(p)

    rows: list[dict] = []
    for opt_type in ("CALL", "PUT"):
        for K in strikes:
            # IV skew: moneyness に線形 skew を乗せる
            moneyness = (K / S) - 1.0
            iv_local = max(0.01, p.iv + p.iv_skew_per_100pct * moneyness)

            g = bs_price_and_greeks(
                S=S, K=float(K), T=T, r=r, sigma=iv_local,
                option_type=opt_type, q=q,
            )

            # OI / volume: ATM からの距離で減衰 (gaussian kernel)
            dist_pct = abs(K - S) / max(S, 1.0)
            decay = math.exp(-((dist_pct / 0.02) ** 2))  # 2% で 1/e
            # seed 固定 rng で ±10% ランダム
            oi = int(p.atm_open_interest * decay * (0.9 + 0.2 * rng.random()))
            vol = int(p.atm_volume * decay * (0.9 + 0.2 * rng.random()))
            # 最低 1 は保証 (極端に遠い strike でも quote は存在)
            oi = max(oi, 1)
            vol = max(vol, 0)

            code = _build_option_code(
                option_root=option_root,
                expiry_yyyymmdd=p.expiry_date_yyyymmdd,
                opt_type=opt_type,
                strike=float(K),
            )

            price = g["price"]
            # bid/ask は ±0.5% の 簡易スプレッド
            half_spread = max(price * 0.005, 0.01)
            rows.append({
                "code":                code,
                "strike_price":        float(K),
                "option_type":         opt_type,
                "delta":               g["delta"],
                "gamma":               g["gamma"],
                "theta":               g["theta"],
                "vega":                g["vega"],
                "implied_volatility":  iv_local,
                "open_interest":       oi,
                "volume":              vol,
                "last_price":          price,
                "bid_price":           max(0.0, price - half_spread),
                "ask_price":           price + half_spread,
            })

    df = pd.DataFrame(rows)
    # dtype 固定 (moomoo と同等)
    df["strike_price"] = df["strike_price"].astype(float)
    df["open_interest"] = df["open_interest"].astype(int)
    df["volume"] = df["volume"].astype(int)
    return df


# ────────────────────────────────────────────────────────────
# 5. 高レベル API (全シナリオ / 全銘柄を一括)
# ────────────────────────────────────────────────────────────


def generate_all_scenarios(
    symbol: str,
    underlying_price: float | None = None,
    iv: float | None = None,
    expiry_days: float = 0.02,  # 0DTE 想定 (寄り付き〜引けの 0.5h 程度)
) -> dict[str, pd.DataFrame]:
    """指定 symbol について 5 シナリオ全ての DataFrame を dict で返す."""
    spec = _SYMBOL_SPEC[symbol]
    S = underlying_price if underlying_price is not None else float(spec["typical_price"])
    base_iv = iv if iv is not None else float(spec["typical_iv"])

    out: dict[str, pd.DataFrame] = {}
    for sc in SCENARIOS:
        params = ChainParams(
            symbol=symbol,
            underlying_price=S,
            iv=base_iv,
            expiry_days=expiry_days,
            scenario=sc,
        )
        out[sc] = generate_option_chain(params)
    return out


# ────────────────────────────────────────────────────────────
# 6. pytest fixture (他 test から `from tests.fixtures... import *` で使える)
# ────────────────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def synthetic_chain_factory():
        """factory fixture: ChainParams を渡すと DataFrame を返す."""
        def _make(params: ChainParams) -> pd.DataFrame:
            return generate_option_chain(params)
        return _make

    @pytest.fixture
    def synthetic_chain_spx_normal():
        """SPX normal scenario の基準チェーン."""
        return generate_option_chain(ChainParams(
            symbol="US..SPX",
            underlying_price=5400.0,
            iv=0.15,
            expiry_days=0.02,
            scenario="normal",
        ))

    @pytest.fixture(params=list(SCENARIOS))
    def synthetic_chain_spx_all_scenarios(request):
        """SPX の全 5 シナリオを parametrize で展開."""
        return request.param, generate_option_chain(ChainParams(
            symbol="US..SPX",
            underlying_price=5400.0,
            iv=0.15,
            expiry_days=0.02,
            scenario=request.param,
        ))

    @pytest.fixture(params=["US..SPX", "US.SPY", "US.QQQ", "US.IWM"])
    def synthetic_chain_all_symbols(request):
        """4 銘柄 x normal scenario を parametrize で展開."""
        spec = _SYMBOL_SPEC[request.param]
        return request.param, generate_option_chain(ChainParams(
            symbol=request.param,
            underlying_price=float(spec["typical_price"]),
            iv=float(spec["typical_iv"]),
            expiry_days=0.02,
            scenario="normal",
        ))

except ImportError:
    # pytest 非導入環境では fixture 登録をスキップ (import 時の副作用防止)
    pass
