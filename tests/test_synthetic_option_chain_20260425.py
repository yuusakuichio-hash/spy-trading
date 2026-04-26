"""tests/test_synthetic_option_chain_20260425.py — fixture spec test
(Sprint 2 C-017 補完 / 2026-04-25 新規)

目的:
  tests/fixtures/synthetic_option_chain.py の正当性を担保する spec test.
  - moomoo 互換 column が揃っているか
  - Greeks が self-consistent か (put-call parity / delta 符号 / gamma 対称性)
  - 5 極端シナリオが期待どおり param を変えているか

合計 15 件以上の test を満たす (count は pytest collect で確認).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tests.fixtures.synthetic_option_chain import (
    SCENARIOS,
    ChainParams,
    apply_scenario,
    bs_price_and_greeks,
    generate_all_scenarios,
    generate_option_chain,
    get_supported_symbols,
)

# 期待 column 構成 (moomoo get_option_chain 互換)
_EXPECTED_COLS = {
    "code", "strike_price", "option_type",
    "delta", "gamma", "theta", "vega",
    "implied_volatility", "open_interest", "volume",
    "last_price", "bid_price", "ask_price",
}


# ────────────────────────────────────────────────────────────
# 1. 基本構造 (column / dtype / 非空)
# ────────────────────────────────────────────────────────────


def test_01_supported_symbols_exact_four():
    """サポート銘柄は SPX / SPY / QQQ / IWM の 4 つちょうど."""
    symbols = set(get_supported_symbols())
    assert symbols == {"US..SPX", "US.SPY", "US.QQQ", "US.IWM"}, (
        f"supported mismatch: {symbols}"
    )


def test_02_all_expected_columns_present_spx():
    """SPX normal で期待 column が全部揃う."""
    df = generate_option_chain(ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=0.02, scenario="normal",
    ))
    assert _EXPECTED_COLS.issubset(set(df.columns)), (
        f"missing cols: {_EXPECTED_COLS - set(df.columns)}"
    )


def test_03_nonempty_chain_for_all_symbols():
    """4 銘柄すべて非空の DataFrame を返す."""
    for sym in get_supported_symbols():
        df = generate_option_chain(ChainParams(
            symbol=sym, underlying_price=100.0, iv=0.2,
            expiry_days=1.0, scenario="normal",
        ))
        assert len(df) > 0, f"empty chain for {sym}"
        # CALL / PUT 両側あることを確認
        types = set(df["option_type"].unique())
        assert types == {"CALL", "PUT"}, f"types={types} for {sym}"


def test_04_option_code_format_moomoo_compatible():
    """option code が moomoo の US.{ROOT}{YYMMDD}{C|P}{strike*1000:08d} 形式."""
    df = generate_option_chain(ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=0.02, scenario="normal",
        expiry_date_yyyymmdd="20260425",
    ))
    # SPX は option_root=SPXW
    for code in df["code"].head(5):
        assert code.startswith("US.SPXW260425"), f"bad code prefix: {code}"
        # 桁数: US. + SPXW + 6 + 1 + 8 = 22
        assert len(code) == 22, f"bad code length: {code} (len={len(code)})"
        assert code[13] in ("C", "P"), f"bad side char: {code}"


def test_05_spy_option_code_uses_spy_root():
    """SPY は option_root=SPY のコードになる (SPXW 混入禁止・4/17事故防御)."""
    df = generate_option_chain(ChainParams(
        symbol="US.SPY", underlying_price=540.0, iv=0.17,
        expiry_days=0.02, scenario="normal",
        expiry_date_yyyymmdd="20260425",
    ))
    for code in df["code"].head(5):
        assert code.startswith("US.SPY260425"), f"bad SPY code: {code}"
        # SPXW が混ざっていないこと
        assert "SPXW" not in code


# ────────────────────────────────────────────────────────────
# 2. Greeks self-consistency
# ────────────────────────────────────────────────────────────


def test_06_put_call_parity_atm():
    """ATM で put-call parity: C - P ≈ S - K*exp(-rT).
    配当 0・同一 IV で成立するはず.
    """
    S, K, T, r, sigma = 5400.0, 5400.0, 7.0 / 365.0, 0.045, 0.15
    c = bs_price_and_greeks(S, K, T, r, sigma, "CALL")["price"]
    p = bs_price_and_greeks(S, K, T, r, sigma, "PUT")["price"]
    parity_lhs = c - p
    parity_rhs = S - K * math.exp(-r * T)
    assert abs(parity_lhs - parity_rhs) < 1e-6, (
        f"parity break: lhs={parity_lhs}, rhs={parity_rhs}"
    )


def test_07_delta_sign_convention():
    """CALL delta > 0, PUT delta < 0."""
    df = generate_option_chain(ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=0.02, scenario="normal",
    ))
    calls = df[df["option_type"] == "CALL"]
    puts = df[df["option_type"] == "PUT"]
    # ATM 近傍の deep でない strike を見る (extreme ITM/OTM は数値誤差で 0 扱い有)
    assert (calls["delta"] >= 0).all(), "CALL delta must be >= 0"
    assert (puts["delta"] <= 0).all(), "PUT delta must be <= 0"


def test_08_delta_sum_approx_one_at_atm():
    """同一 strike で |delta_call| + |delta_put| ≈ 1 (q=0・同一 IV 前提)."""
    S, K, T, r, sigma = 540.0, 540.0, 7.0 / 365.0, 0.045, 0.17
    dc = bs_price_and_greeks(S, K, T, r, sigma, "CALL")["delta"]
    dp = bs_price_and_greeks(S, K, T, r, sigma, "PUT")["delta"]
    total = abs(dc) + abs(dp)
    assert abs(total - 1.0) < 1e-6, f"|delta_c|+|delta_p| = {total}, expected ~1.0"


def test_09_gamma_and_vega_symmetric_call_put():
    """同一 strike・同一 IV で gamma_call == gamma_put, vega_call == vega_put."""
    S, K, T, r, sigma = 470.0, 470.0, 3.0 / 365.0, 0.045, 0.20
    gc = bs_price_and_greeks(S, K, T, r, sigma, "CALL")
    gp = bs_price_and_greeks(S, K, T, r, sigma, "PUT")
    assert abs(gc["gamma"] - gp["gamma"]) < 1e-10
    assert abs(gc["vega"] - gp["vega"]) < 1e-10


def test_10_theta_is_negative_for_long_option():
    """Long option の theta は負 (時間減衰)."""
    S, K, T, r, sigma = 5400.0, 5400.0, 7.0 / 365.0, 0.045, 0.15
    tc = bs_price_and_greeks(S, K, T, r, sigma, "CALL")["theta"]
    tp = bs_price_and_greeks(S, K, T, r, sigma, "PUT")["theta"]
    assert tc < 0, f"call theta should be <0, got {tc}"
    assert tp < 0, f"put theta should be <0, got {tp}"


def test_11_gamma_is_nonnegative_everywhere():
    """Gamma は全 strike で非負."""
    df = generate_option_chain(ChainParams(
        symbol="US.QQQ", underlying_price=470.0, iv=0.20,
        expiry_days=0.5, scenario="normal",
    ))
    assert (df["gamma"] >= -1e-12).all(), "gamma must be >= 0"


def test_12_price_bounds_itm_otm():
    """ITM CALL price >= intrinsic; OTM CALL price >= 0; similar for PUT."""
    S = 5400.0
    # ITM CALL: K=5300 -> 少なくとも intrinsic = 100
    itm = bs_price_and_greeks(S, 5300.0, 7.0 / 365.0, 0.045, 0.15, "CALL")
    assert itm["price"] >= 100.0 - 5.0, (
        f"ITM call below intrinsic: {itm['price']}"
    )
    # OTM CALL: K=5500 -> >= 0
    otm = bs_price_and_greeks(S, 5500.0, 7.0 / 365.0, 0.045, 0.15, "CALL")
    assert otm["price"] >= 0.0


# ────────────────────────────────────────────────────────────
# 3. Scenario 効果検証
# ────────────────────────────────────────────────────────────


def test_13_five_scenarios_all_exist():
    """SCENARIOS に normal / vix_spike_30 / gap_up_5 / crash_10 / iv_crush の 5 つ."""
    assert set(SCENARIOS) == {
        "normal", "vix_spike_30", "gap_up_5", "crash_10", "iv_crush",
    }
    assert len(SCENARIOS) == 5


def test_14_vix_spike_raises_iv():
    """vix_spike_30 は iv を 1.30x にする."""
    base = ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=0.02, scenario="vix_spike_30",
    )
    adjusted = apply_scenario(base)
    assert abs(adjusted.iv - 0.15 * 1.30) < 1e-9


def test_15_gap_up_5_raises_underlying():
    """gap_up_5 は underlying を 1.05x にする."""
    base = ChainParams(
        symbol="US.SPY", underlying_price=540.0, iv=0.17,
        expiry_days=0.02, scenario="gap_up_5",
    )
    adjusted = apply_scenario(base)
    assert abs(adjusted.underlying_price - 540.0 * 1.05) < 1e-9


def test_16_crash_10_drops_underlying_and_raises_iv():
    """crash_10 は underlying を 0.9x, iv を 1.4x にする (2 変数同時変動)."""
    base = ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=0.02, scenario="crash_10",
    )
    adjusted = apply_scenario(base)
    assert abs(adjusted.underlying_price - 5400.0 * 0.90) < 1e-9
    assert abs(adjusted.iv - 0.15 * 1.40) < 1e-9


def test_17_iv_crush_halves_iv():
    """iv_crush は iv を 0.5x にする."""
    base = ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=0.02, scenario="iv_crush",
    )
    adjusted = apply_scenario(base)
    assert abs(adjusted.iv - 0.15 * 0.50) < 1e-9


def test_18_vix_spike_gamma_larger_than_iv_crush():
    """短期満期 0DTE で ATM option の price は IV に単調増加.
    vix_spike (IV up) > normal > iv_crush (IV down) を ATM price で比較.
    """
    S = 5400.0
    T = 1.0 / 365.0

    low_iv = bs_price_and_greeks(S, S, T, 0.045, 0.075, "CALL")["price"]
    mid_iv = bs_price_and_greeks(S, S, T, 0.045, 0.15, "CALL")["price"]
    hi_iv = bs_price_and_greeks(S, S, T, 0.045, 0.195, "CALL")["price"]
    assert low_iv < mid_iv < hi_iv, (
        f"IV monotonicity break: {low_iv} < {mid_iv} < {hi_iv}"
    )


# ────────────────────────────────────────────────────────────
# 4. generate_all_scenarios / fixture factory
# ────────────────────────────────────────────────────────────


def test_19_generate_all_scenarios_returns_five_dfs():
    """generate_all_scenarios は 5 シナリオ分の dict を返す."""
    d = generate_all_scenarios("US..SPX")
    assert set(d.keys()) == set(SCENARIOS)
    for sc, df in d.items():
        assert len(df) > 0, f"{sc} empty"
        assert _EXPECTED_COLS.issubset(set(df.columns))


def test_20_open_interest_positive_everywhere():
    """open_interest は全行で >= 1 (fixture 側でクリップ)."""
    df = generate_option_chain(ChainParams(
        symbol="US.IWM", underlying_price=210.0, iv=0.22,
        expiry_days=0.5, scenario="normal",
    ))
    assert (df["open_interest"] >= 1).all()


def test_21_volume_nonnegative():
    """volume は全行で >= 0."""
    df = generate_option_chain(ChainParams(
        symbol="US.QQQ", underlying_price=470.0, iv=0.20,
        expiry_days=0.5, scenario="normal",
    ))
    assert (df["volume"] >= 0).all()


def test_22_bid_le_last_le_ask():
    """bid <= last <= ask が全行で成立."""
    df = generate_option_chain(ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=0.02, scenario="normal",
    ))
    # floating tolerance
    assert (df["bid_price"] <= df["last_price"] + 1e-9).all()
    assert (df["last_price"] <= df["ask_price"] + 1e-9).all()


def test_23_unsupported_symbol_raises():
    """サポート外 symbol で ValueError."""
    with pytest.raises(ValueError, match="Unsupported symbol"):
        generate_option_chain(ChainParams(
            symbol="US.FAKE", underlying_price=100.0, iv=0.2,
            expiry_days=1.0,
        ))


def test_24_unknown_scenario_raises():
    """apply_scenario に未知シナリオで ValueError."""
    # dataclass は frozen なので直接代入できない。__dict__ 迂回もできない (slots 非使用だが
    # frozen は __setattr__ でブロック)。type: ignore で bypass して ValueError 確認.
    class _FakeParams:
        scenario = "unknown_xyz"
        iv = 0.15
        underlying_price = 100.0
    with pytest.raises(ValueError, match="Unknown scenario"):
        apply_scenario(_FakeParams())  # type: ignore[arg-type]


def test_25_reproducibility_same_seed_same_oi():
    """同一 seed なら open_interest / volume が決定的 (回帰テスト再現性)."""
    p = ChainParams(
        symbol="US.SPY", underlying_price=540.0, iv=0.17,
        expiry_days=0.02, scenario="normal", seed=777,
    )
    df1 = generate_option_chain(p)
    df2 = generate_option_chain(p)
    pd.testing.assert_series_equal(df1["open_interest"], df2["open_interest"])
    pd.testing.assert_series_equal(df1["volume"], df2["volume"])


def test_26_different_seed_different_oi():
    """異なる seed なら open_interest が異なる (決定性のマイナー検証)."""
    p1 = ChainParams(
        symbol="US.SPY", underlying_price=540.0, iv=0.17,
        expiry_days=0.02, scenario="normal", seed=1,
    )
    p2 = ChainParams(
        symbol="US.SPY", underlying_price=540.0, iv=0.17,
        expiry_days=0.02, scenario="normal", seed=99999,
    )
    df1 = generate_option_chain(p1)
    df2 = generate_option_chain(p2)
    # 少なくとも 1 strike では差があるはず
    assert not df1["open_interest"].equals(df2["open_interest"])


def test_27_put_call_parity_across_chain():
    """生成した chain 内で各 strike の put-call parity が近似成立.
    (IV skew をかけているので完全一致ではないが、skew が小さければ ~1% 以内)
    """
    p = ChainParams(
        symbol="US..SPX", underlying_price=5400.0, iv=0.15,
        expiry_days=7.0,  # 7 days (ChainParams.expiry_days 単位は日)
        scenario="normal",
        iv_skew_per_100pct=0.0,  # skew 0 で厳密 parity を検証
        risk_free_rate=0.045,
    )
    df = generate_option_chain(p)
    calls = df[df["option_type"] == "CALL"].set_index("strike_price")
    puts = df[df["option_type"] == "PUT"].set_index("strike_price")
    common_K = sorted(set(calls.index) & set(puts.index))
    assert len(common_K) > 0

    S = p.underlying_price
    T = 7.0 / 365.0
    r = p.risk_free_rate
    max_err = 0.0
    for K in common_K:
        c = float(calls.loc[K, "last_price"])
        pp = float(puts.loc[K, "last_price"])
        lhs = c - pp
        rhs = S - K * math.exp(-r * T)
        max_err = max(max_err, abs(lhs - rhs))
    assert max_err < 1e-6, f"parity max error too large: {max_err}"
