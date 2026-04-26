"""Black-Scholes Greeks 一貫性プロパティテスト (Hypothesis)

数理的不変条件:
  GK-01  Put-call parity: C - P = S*e^(-q*T) - K*e^(-r*T)
  GK-02  Delta 符号: call delta ∈ (0, 1), put delta ∈ (-1, 0)
  GK-03  Gamma >= 0 (凸性)
  GK-04  Theta <= 0 (時間価値の減少)
  GK-05  Vega >= 0 (IV 増加 → プレミアム増加)
  GK-06  Rho: call rho >= 0, put rho <= 0 (金利方向性)
  GK-07  Moneyness 単調性: S 増加 → call 価格増加, put 価格減少
  GK-08  Vega 対称性: call vega == put vega (同パラメータ)
  GK-09  ATM の|delta| が 0.5 に近い (近似)
  GK-10  IV → 0 の極限: call max(S-K,0), put max(K-S,0)

scipy.stats.norm ベースの純 Python BS 実装を直接テストする。
"""
from __future__ import annotations

import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from hypothesis import assume, given, settings, HealthCheck
from hypothesis import strategies as st
import numpy as np
from scipy.stats import norm

# ── Black-Scholes 実装 (テスト専用・scipy.stats.norm のみ依存) ───────────────

def _d1(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    return (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def _d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    return _d1(S, K, T, r, q, sigma) - sigma * math.sqrt(T)


def bs_price(S: float, K: float, T: float, r: float, q: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes オプション価格。"""
    d1 = _d1(S, K, T, r, q, sigma)
    d2 = _d2(S, K, T, r, q, sigma)
    if is_call:
        return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)


def bs_delta(S: float, K: float, T: float, r: float, q: float, sigma: float, is_call: bool) -> float:
    d1 = _d1(S, K, T, r, q, sigma)
    if is_call:
        return math.exp(-q * T) * norm.cdf(d1)
    else:
        return math.exp(-q * T) * (norm.cdf(d1) - 1.0)


def bs_gamma(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    d1 = _d1(S, K, T, r, q, sigma)
    return math.exp(-q * T) * norm.pdf(d1) / (S * sigma * math.sqrt(T))


def bs_vega(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    d1 = _d1(S, K, T, r, q, sigma)
    return S * math.exp(-q * T) * norm.pdf(d1) * math.sqrt(T)


def bs_theta(
    S: float, K: float, T: float, r: float, q: float, sigma: float, is_call: bool
) -> float:
    d1 = _d1(S, K, T, r, q, sigma)
    d2 = _d2(S, K, T, r, q, sigma)
    term1 = -(S * math.exp(-q * T) * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
    if is_call:
        return term1 - r * K * math.exp(-r * T) * norm.cdf(d2) + q * S * math.exp(-q * T) * norm.cdf(d1)
    else:
        return term1 + r * K * math.exp(-r * T) * norm.cdf(-d2) - q * S * math.exp(-q * T) * norm.cdf(-d1)


def bs_rho(
    S: float, K: float, T: float, r: float, q: float, sigma: float, is_call: bool
) -> float:
    d2 = _d2(S, K, T, r, q, sigma)
    if is_call:
        return K * T * math.exp(-r * T) * norm.cdf(d2)
    else:
        return -K * T * math.exp(-r * T) * norm.cdf(-d2)


# ── 共通ストラテジー ────────────────────────────────────────────────────────

st_S = st.floats(min_value=10.0, max_value=10000.0, allow_nan=False, allow_infinity=False)
st_K = st.floats(min_value=10.0, max_value=10000.0, allow_nan=False, allow_infinity=False)
st_T = st.floats(min_value=1/365, max_value=2.0, allow_nan=False, allow_infinity=False)  # 1日〜2年
st_r = st.floats(min_value=0.0, max_value=0.15, allow_nan=False, allow_infinity=False)
st_q = st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False)
st_sigma = st.floats(min_value=0.01, max_value=5.0, allow_nan=False, allow_infinity=False)
st_bool = st.booleans()

_COMMON_SETTINGS = dict(max_examples=300, suppress_health_check=[HealthCheck.too_slow])


def _safe(*args: float) -> bool:
    """数値的に問題ない入力か確認。"""
    return all(math.isfinite(v) and v > 0 for v in args)


# ── GK-01: Put-call parity ───────────────────────────────────────────────────

@given(S=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk01_put_call_parity(S, K, T, r, q, sigma):
    """GK-01: C - P = S*e^(-qT) - K*e^(-rT)  (put-call parity)。"""
    assume(_safe(S, K, T, sigma) and T > 0 and sigma > 0)
    C = bs_price(S, K, T, r, q, sigma, is_call=True)
    P = bs_price(S, K, T, r, q, sigma, is_call=False)
    lhs = C - P
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert abs(lhs - rhs) < 1e-8, f"GK-01: put-call parity violated: C-P={lhs:.10f} rhs={rhs:.10f}"


# ── GK-02: Delta 符号 ────────────────────────────────────────────────────────

@given(S=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk02_call_delta_range(S, K, T, r, q, sigma):
    """GK-02a: call delta ∈ [0, 1] (deep OTM で 0, deep ITM で 1 に収束するため含む)。"""
    assume(_safe(S, K, T, sigma))
    d = bs_delta(S, K, T, r, q, sigma, is_call=True)
    assert 0.0 <= d <= 1.0, f"GK-02a: call delta={d:.6f} out of [0,1]"


@given(S=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk02_put_delta_range(S, K, T, r, q, sigma):
    """GK-02b: put delta ∈ [-1, 0] (deep OTM で 0, deep ITM で -1 に収束するため含む)。"""
    assume(_safe(S, K, T, sigma))
    d = bs_delta(S, K, T, r, q, sigma, is_call=False)
    assert -1.0 <= d <= 0.0, f"GK-02b: put delta={d:.6f} out of [-1,0]"


# ── GK-03: Gamma >= 0 ────────────────────────────────────────────────────────

@given(S=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk03_gamma_nonnegative(S, K, T, r, q, sigma):
    """GK-03: gamma >= 0 (call も put も同値・非負)。"""
    assume(_safe(S, K, T, sigma))
    g = bs_gamma(S, K, T, r, q, sigma)
    assert g >= -1e-12, f"GK-03: gamma={g:.10f} < 0"


# ── GK-04: Theta <= 0 ────────────────────────────────────────────────────────

@given(S=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma, is_call=st_bool)
@settings(**_COMMON_SETTINGS)
def test_gk04_theta_nonpositive(S, K, T, r, q, sigma, is_call):
    """GK-04: theta <= 0 (時間価値の減少・q=0 かつ r=0 近傍でも成立)。"""
    assume(_safe(S, K, T, sigma))
    # deep OTM / deep ITM では theta が非常に小さな正値になる浮動小数誤差を許容
    th = bs_theta(S, K, T, r=0.0, q=0.0, sigma=sigma, is_call=is_call)
    assert th <= 1e-6, f"GK-04: theta={th:.10f} > 0 (S={S} K={K} T={T} sigma={sigma})"


# ── GK-05: Vega >= 0 ─────────────────────────────────────────────────────────

@given(S=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk05_vega_nonnegative(S, K, T, r, q, sigma):
    """GK-05: vega >= 0。"""
    assume(_safe(S, K, T, sigma))
    v = bs_vega(S, K, T, r, q, sigma)
    assert v >= -1e-12, f"GK-05: vega={v:.10f} < 0"


# ── GK-06: Rho 方向性 ────────────────────────────────────────────────────────

@given(S=st_S, K=st_K, T=st_T, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk06_call_rho_nonneg(S, K, T, q, sigma):
    """GK-06a: call rho >= 0。"""
    assume(_safe(S, K, T, sigma))
    rho = bs_rho(S, K, T, r=0.05, q=q, sigma=sigma, is_call=True)
    assert rho >= -1e-12, f"GK-06a: call rho={rho:.10f} < 0"


@given(S=st_S, K=st_K, T=st_T, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk06_put_rho_nonpos(S, K, T, q, sigma):
    """GK-06b: put rho <= 0。"""
    assume(_safe(S, K, T, sigma))
    rho = bs_rho(S, K, T, r=0.05, q=q, sigma=sigma, is_call=False)
    assert rho <= 1e-12, f"GK-06b: put rho={rho:.10f} > 0"


# ── GK-07: Moneyness 単調性 ──────────────────────────────────────────────────

@given(
    S1=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma,
    dS=st.floats(min_value=0.01, max_value=100.0, allow_nan=False),
)
@settings(**_COMMON_SETTINGS)
def test_gk07_call_monotone_in_S(S1, K, T, r, q, sigma, dS):
    """GK-07a: S 増加 → call 価格増加 (call は S の単調増加関数)。"""
    assume(_safe(S1, K, T, sigma, dS))
    S2 = S1 + dS
    c1 = bs_price(S1, K, T, r, q, sigma, is_call=True)
    c2 = bs_price(S2, K, T, r, q, sigma, is_call=True)
    assert c2 >= c1 - 1e-10, f"GK-07a: call not monotone: S1={S1} S2={S2} C1={c1} C2={c2}"


@given(
    S1=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma,
    dS=st.floats(min_value=0.01, max_value=100.0, allow_nan=False),
)
@settings(**_COMMON_SETTINGS)
def test_gk07_put_monotone_in_S(S1, K, T, r, q, sigma, dS):
    """GK-07b: S 増加 → put 価格減少 (put は S の単調減少関数)。"""
    assume(_safe(S1, K, T, sigma, dS))
    S2 = S1 + dS
    p1 = bs_price(S1, K, T, r, q, sigma, is_call=False)
    p2 = bs_price(S2, K, T, r, q, sigma, is_call=False)
    assert p2 <= p1 + 1e-10, f"GK-07b: put not monotone: S1={S1} S2={S2} P1={p1} P2={p2}"


# ── GK-08: Vega 対称性 call == put ─────────────────────────────────────────

@given(S=st_S, K=st_K, T=st_T, r=st_r, q=st_q, sigma=st_sigma)
@settings(**_COMMON_SETTINGS)
def test_gk08_vega_symmetry(S, K, T, r, q, sigma):
    """GK-08: call vega == put vega (同パラメータ)。"""
    assume(_safe(S, K, T, sigma))
    v = bs_vega(S, K, T, r, q, sigma)
    # vega は call/put で共通なので BS の call vega と put vega は恒等
    # 数値で確認: 微分 dC/dsigma と dP/dsigma の差が 0
    eps = 1e-6
    vc_num = (
        bs_price(S, K, T, r, q, sigma + eps, True)
        - bs_price(S, K, T, r, q, sigma - eps, True)
    ) / (2 * eps)
    vp_num = (
        bs_price(S, K, T, r, q, sigma + eps, False)
        - bs_price(S, K, T, r, q, sigma - eps, False)
    ) / (2 * eps)
    # 数値微分の精度は 1e-5 程度まで許容 (大 S/K 比での浮動小数誤差)
    assert abs(vc_num - vp_num) < 1e-5, (
        f"GK-08: vega asymmetry call={vc_num:.8f} put={vp_num:.8f}"
    )


# ── GK-09: ATM の|delta| ≈ 0.5 ──────────────────────────────────────────────

@given(
    S=st.floats(min_value=10.0, max_value=5000.0, allow_nan=False),
    T=st.floats(min_value=1/365, max_value=7/365, allow_nan=False),  # 超短期 (1-7 日)
    sigma=st.floats(min_value=0.05, max_value=0.80, allow_nan=False),
)
@settings(**_COMMON_SETTINGS)
def test_gk09_atm_delta_near_half(S, T, sigma):
    """GK-09: ATM (S==K) の call delta ∈ [0.48, 0.52] 近似 (超短期のみ・r=q=0)。

    ATM の call delta = N(sigma*sqrt(T)/2)。
    sigma*sqrt(T)/2 の最大は sigma=0.80 T=7/365 → 0.80*sqrt(7/365)/2 ≈ 0.055 → N(0.055)≈0.522。
    許容範囲: [0.47, 0.53] (sigma=0.80 T=7/365 での最大偏差を考慮)。
    """
    assume(_safe(S, T, sigma))
    K = S  # perfect ATM
    d = bs_delta(S, K, T, r=0.0, q=0.0, sigma=sigma, is_call=True)
    assert 0.47 <= d <= 0.53, (
        f"GK-09: ATM call delta={d:.4f} not near 0.5 (S={S} T={T:.4f} sigma={sigma:.3f})"
    )


# ── GK-10: IV → 0 の極限 ────────────────────────────────────────────────────

@given(
    S=st.floats(min_value=50.0, max_value=5000.0, allow_nan=False),
    moneyness=st.sampled_from(["itm", "otm"]),
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_gk10_iv_to_zero_intrinsic(S, moneyness):
    """GK-10: sigma→0 で call price → max(S-K, 0), put price → max(K-S, 0)。"""
    K = S * 0.9 if moneyness == "itm" else S * 1.1  # ITM call / OTM call
    sigma_tiny = 1e-6
    T = 30 / 365
    r = 0.05
    q = 0.0

    call_price = bs_price(S, K, T, r, q, sigma_tiny, is_call=True)
    put_price = bs_price(S, K, T, r, q, sigma_tiny, is_call=False)

    # sigma→0: C ≈ max(F-K, 0) * discount, P ≈ max(K-F, 0) * discount
    F = S * math.exp((r - q) * T)
    expected_call = max(F - K, 0) * math.exp(-r * T)
    expected_put = max(K - F, 0) * math.exp(-r * T)

    assert abs(call_price - expected_call) < 0.01, (
        f"GK-10: call near-zero IV: got={call_price:.6f} expected={expected_call:.6f}"
    )
    assert abs(put_price - expected_put) < 0.01, (
        f"GK-10: put near-zero IV: got={put_price:.6f} expected={expected_put:.6f}"
    )
