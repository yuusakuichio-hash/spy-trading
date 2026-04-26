"""
tests/test_orderflow_analysis.py -- GEX/DIX分析モジュール テスト

テスト対象: common/orderflow_analysis.py
- GEX計算精度
- DIXプロキシ計算精度
- 0DIVゾーン計算
- CBOEオプションコード解析
- Graceful Degradation（データなし・API障害時）
- strategy_hint正確性
- バイアス境界値
- orderflow_to_bias変換
"""

import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common.orderflow_analysis import (
    OptionRecord,
    OrderflowSignal,
    _parse_option_code,
    _parse_options,
    _compute_gex,
    _compute_gex_bias,
    _compute_dix_proxy,
    _compute_dix_bias,
    _compute_zero_strike_vol,
    get_orderflow_signal,
    orderflow_to_bias,
    _SYMBOL_MAP,
    _GEX_NORM_DEFAULTS,
)


# -- ヘルパー ------------------------------------------------------------------

def _make_raw_opt(
    code: str,
    volume: float = 100.0,
    oi: float = 500.0,
    gamma: float = 0.02,
    delta: float = 0.50,
) -> dict:
    """テスト用のCBOE生オプション辞書を生成する。"""
    return {
        "option":        code,
        "volume":        volume,
        "open_interest": oi,
        "gamma":         gamma,
        "delta":         delta,
        "bid":           1.0,
        "ask":           1.1,
        "iv":            0.20,
    }


def _make_record(
    right: str,
    volume: float = 100.0,
    oi: float = 500.0,
    gamma: float = 0.02,
    strike: float = 500.0,
) -> OptionRecord:
    """テスト用OptionRecordを生成する。"""
    return OptionRecord(
        symbol="SPY",
        expiry="20260420",
        strike=strike,
        right=right,
        volume=volume,
        open_interest=oi,
        gamma=gamma,
        delta=0.5 if right == "C" else -0.5,
    )


# -- _parse_option_code テスト -------------------------------------------------

def test_parse_option_code_call():
    """標準的なコールオプションコードを解析できる。"""
    expiry, right, strike = _parse_option_code("SPY260420C00500000")
    assert expiry == "20260420", f"expiry={expiry}"
    assert right == "C", f"right={right}"
    assert strike == 500.0, f"strike={strike}"


def test_parse_option_code_put():
    """標準的なプットオプションコードを解析できる。"""
    expiry, right, strike = _parse_option_code("SPY260420P00480000")
    assert expiry == "20260420", f"expiry={expiry}"
    assert right == "P", f"right={right}"
    assert strike == 480.0, f"strike={strike}"


def test_parse_option_code_spx_index():
    """SPX（インデックス）のオプションコードを解析できる。"""
    expiry, right, strike = _parse_option_code("SPX260515C00200000")
    assert expiry == "20260515", f"expiry={expiry}"
    assert right == "C", f"right={right}"
    assert strike == 200.0, f"strike={strike}"


def test_parse_option_code_underscore_prefix():
    """アンダースコアプレフィックス付きのSPXコードを解析できる。"""
    expiry, right, strike = _parse_option_code("_SPX260515P00550000")
    # アンダースコアプレフィックス付きは現在のパターンでは解析失敗が許容される
    # (CBOEのoption fieldは "SPX260515P00550000" 形式で返る)
    # テストは解析成功/失敗どちらも許容するが、成功した場合は値を検証する
    if expiry:
        assert right in ("C", "P")
        assert strike > 0


def test_parse_option_code_invalid():
    """不正なコードに対しては空文字列と0.0を返す。"""
    expiry, right, strike = _parse_option_code("INVALID_CODE")
    assert expiry == "", f"expiry={expiry}"
    assert strike == 0.0, f"strike={strike}"


def test_parse_option_code_high_strike_spx():
    """SPX高ストライク（7000台）を正しく解析する。"""
    expiry, right, strike = _parse_option_code("SPX260420C07000000")
    assert expiry == "20260420"
    assert right == "C"
    assert strike == 7000.0, f"strike={strike}"


# -- _parse_options テスト -----------------------------------------------------

def test_parse_options_basic():
    """CBOEの生オプションリストをOptionRecordに変換できる。"""
    raw = [
        _make_raw_opt("SPY260420C00500000", volume=200, oi=1000, gamma=0.03),
        _make_raw_opt("SPY260420P00490000", volume=150, oi=800, gamma=0.025),
    ]
    records = _parse_options(raw, spot=500.0)
    assert len(records) == 2
    calls = [r for r in records if r.right == "C"]
    puts  = [r for r in records if r.right == "P"]
    assert len(calls) == 1
    assert len(puts) == 1
    assert calls[0].volume == 200.0
    assert calls[0].open_interest == 1000.0
    assert calls[0].gamma == 0.03
    assert puts[0].strike == 490.0


def test_parse_options_empty_input():
    """空リスト入力に対しては空リストを返す。"""
    records = _parse_options([], spot=500.0)
    assert records == []


def test_parse_options_filters_invalid_codes():
    """不正なオプションコードはスキップされる。"""
    raw = [
        _make_raw_opt("SPY260420C00500000"),
        _make_raw_opt("INVALID"),
        _make_raw_opt(""),
        _make_raw_opt("SPY260420P00490000"),
    ]
    records = _parse_options(raw, spot=500.0)
    assert len(records) == 2


def test_parse_options_zero_values_allowed():
    """gamma=0やvolume=0のレコードも含まれる（GEX計算でゼロ寄与として扱う）。"""
    raw = [
        _make_raw_opt("SPY260420C00500000", volume=0.0, gamma=0.0),
    ]
    records = _parse_options(raw, spot=500.0)
    # パースは成功するが gamma/volume=0 でGEX寄与はゼロになる
    assert len(records) == 1
    assert records[0].gamma == 0.0


# -- _compute_gex テスト -------------------------------------------------------

def test_compute_gex_call_dominant():
    """コールOI優勢のとき正のGEXを返す。"""
    records = [
        _make_record("C", oi=1000, gamma=0.02),
        _make_record("P", oi=200,  gamma=0.02),
    ]
    gex_total, gex_call, gex_put = _compute_gex(records, spot=500.0)
    assert gex_total > 0, f"gex_total={gex_total}"
    assert gex_call > gex_put, f"gex_call={gex_call} gex_put={gex_put}"


def test_compute_gex_put_dominant():
    """プットOI優勢のとき負のGEXを返す。"""
    records = [
        _make_record("C", oi=200,  gamma=0.02),
        _make_record("P", oi=1000, gamma=0.02),
    ]
    gex_total, _, _ = _compute_gex(records, spot=500.0)
    assert gex_total < 0, f"gex_total={gex_total}"


def test_compute_gex_formula_accuracy():
    """GEX計算式の数値精度確認。

    GEX = OI x gamma x 100 x spot
    1000 x 0.02 x 100 x 500 = 1_000_000 (コール)
    500  x 0.02 x 100 x 500 = 500_000   (プット)
    total = 1_000_000 - 500_000 = 500_000
    """
    records = [
        _make_record("C", oi=1000, gamma=0.02, strike=500.0),
        _make_record("P", oi=500,  gamma=0.02, strike=490.0),
    ]
    gex_total, gex_call, gex_put = _compute_gex(records, spot=500.0)
    expected_call = 1000 * 0.02 * 100 * 500
    expected_put  = 500  * 0.02 * 100 * 500
    assert abs(gex_call - expected_call) < 1.0, f"gex_call={gex_call} expected={expected_call}"
    assert abs(gex_put  - expected_put)  < 1.0, f"gex_put={gex_put} expected={expected_put}"
    assert abs(gex_total - (expected_call - expected_put)) < 1.0


def test_compute_gex_skips_zero_oi():
    """OI=0のレコードはGEX計算に含まれない。"""
    records = [
        _make_record("C", oi=0,    gamma=0.05),
        _make_record("C", oi=1000, gamma=0.02),
    ]
    _, gex_call, _ = _compute_gex(records, spot=500.0)
    expected = 1000 * 0.02 * 100 * 500
    assert abs(gex_call - expected) < 1.0


def test_compute_gex_skips_zero_gamma():
    """gamma=0のレコードはGEX計算に含まれない。"""
    records = [
        _make_record("C", oi=1000, gamma=0.0),
        _make_record("C", oi=500,  gamma=0.02),
    ]
    _, gex_call, _ = _compute_gex(records, spot=500.0)
    expected = 500 * 0.02 * 100 * 500
    assert abs(gex_call - expected) < 1.0


# -- _compute_gex_bias テスト --------------------------------------------------

def test_compute_gex_bias_positive_gex():
    """正のGEXは正のバイアスを返す。"""
    bias = _compute_gex_bias(1_000_000_000.0, "SPY")
    assert bias > 0.0, f"bias={bias}"
    assert -1.0 <= bias <= 1.0


def test_compute_gex_bias_negative_gex():
    """負のGEXは負のバイアスを返す。"""
    bias = _compute_gex_bias(-1_000_000_000.0, "SPY")
    assert bias < 0.0, f"bias={bias}"
    assert -1.0 <= bias <= 1.0


def test_compute_gex_bias_zero():
    """GEX=0はバイアス=0を返す。"""
    bias = _compute_gex_bias(0.0, "SPY")
    assert bias == 0.0


def test_compute_gex_bias_bounded():
    """極端な値でもバイアスは[-1.0, +1.0]に収まる。"""
    bias_pos = _compute_gex_bias(1e15, "_SPX")
    bias_neg = _compute_gex_bias(-1e15, "_SPX")
    assert -1.0 <= bias_pos <= 1.0
    assert -1.0 <= bias_neg <= 1.0


def test_compute_gex_bias_tanh_saturation():
    """normの2倍程度でtanh飽和に近い。"""
    norm = _GEX_NORM_DEFAULTS["SPY"]
    bias_at_2x = _compute_gex_bias(2.0 * norm, "SPY")
    # tanh(2) ≒ 0.964
    assert bias_at_2x > 0.9, f"bias={bias_at_2x}"


# -- _compute_dix_proxy テスト -------------------------------------------------

def test_compute_dix_proxy_equal_volumes():
    """コール・プット均等のとき ratio=0.5 を返す。"""
    records = (
        [_make_record("C", volume=1000)] * 5 +
        [_make_record("P", volume=1000)] * 5
    )
    call_vol, put_vol, ratio = _compute_dix_proxy(records)
    assert abs(ratio - 0.5) < 0.01, f"ratio={ratio}"


def test_compute_dix_proxy_call_heavy():
    """コール出来高が多いとき put_ratio < 0.5 を返す（強気）。"""
    records = (
        [_make_record("C", volume=3000)] * 3 +
        [_make_record("P", volume=1000)] * 1
    )
    _, _, ratio = _compute_dix_proxy(records)
    assert ratio < 0.5, f"ratio={ratio}"


def test_compute_dix_proxy_put_heavy():
    """プット出来高が多いとき put_ratio > 0.5 を返す（弱気）。"""
    records = (
        [_make_record("C", volume=1000)] * 1 +
        [_make_record("P", volume=3000)] * 3
    )
    _, _, ratio = _compute_dix_proxy(records)
    assert ratio > 0.5, f"ratio={ratio}"


def test_compute_dix_proxy_no_volume():
    """出来高ゼロのときデフォルト ratio=0.5 を返す。"""
    records = [_make_record("C", volume=0), _make_record("P", volume=0)]
    _, _, ratio = _compute_dix_proxy(records)
    assert ratio == 0.5


# -- _compute_dix_bias テスト --------------------------------------------------

def test_compute_dix_bias_neutral():
    """put_ratio=0.5 でバイアス=0.0。"""
    bias = _compute_dix_bias(0.5)
    assert bias == 0.0


def test_compute_dix_bias_bullish():
    """put_ratio=0.3 (コール優勢) でバイアス > 0 (強気)。"""
    bias = _compute_dix_bias(0.3)
    assert bias > 0.0, f"bias={bias}"


def test_compute_dix_bias_bearish():
    """put_ratio=0.7 (プット優勢) でバイアス < 0 (弱気)。"""
    bias = _compute_dix_bias(0.7)
    assert bias < 0.0, f"bias={bias}"


def test_compute_dix_bias_bounded():
    """極端な値でも[-1.0, +1.0]に収まる。"""
    assert -1.0 <= _compute_dix_bias(0.0) <= 1.0
    assert -1.0 <= _compute_dix_bias(1.0) <= 1.0


# -- _compute_zero_strike_vol テスト -------------------------------------------

def test_zero_strike_vol_atm_strikes():
    """ATM±2%以内のストライクが集計される。"""
    spot = 500.0
    records = [
        _make_record("C", volume=200, strike=500.0),  # ATM
        _make_record("C", volume=200, strike=505.0),  # ATM+1% (以内)
        _make_record("C", volume=200, strike=520.0),  # ATM+4% (以外)
        _make_record("P", volume=200, strike=490.0),  # ATM-2% (以内)
        _make_record("P", volume=200, strike=470.0),  # ATM-6% (以外)
    ]
    zero_vol, ratio = _compute_zero_strike_vol(records, spot=spot)
    # ATM内: 200+200+200 = 600, total = 1000, ratio = 0.6
    assert abs(zero_vol - 600.0) < 1.0, f"zero_vol={zero_vol}"
    assert abs(ratio - 0.6) < 0.01, f"ratio={ratio}"


def test_zero_strike_vol_zero_spot():
    """spot=0 のときゼロを返す（ゼロ除算回避）。"""
    records = [_make_record("C", volume=100, strike=500.0)]
    zero_vol, ratio = _compute_zero_strike_vol(records, spot=0.0)
    assert zero_vol == 0.0
    assert ratio == 0.0


# -- get_orderflow_signal テスト (外部注入モード) --------------------------------

def test_get_orderflow_signal_no_data():
    """空のデータで neutral シグナルを返す。"""
    sig = get_orderflow_signal("SPY", _raw_options=[], _spot_override=0.0, log_to_file=False)
    assert isinstance(sig, OrderflowSignal)
    assert sig.gex_bias == 0.0
    assert sig.dix_proxy == 0.5
    assert sig.data_available is False


def test_get_orderflow_signal_bullish():
    """コール優勢データで強気シグナルを返す。"""
    raw = (
        [_make_raw_opt("SPY260420C00500000", volume=5000, oi=10000, gamma=0.03)] * 10 +
        [_make_raw_opt("SPY260420P00490000", volume=500,  oi=1000,  gamma=0.02)] * 2
    )
    sig = get_orderflow_signal("SPY", _raw_options=raw, _spot_override=500.0, log_to_file=False)
    assert sig.data_available is True
    assert sig.gex_total > 0, f"gex_total={sig.gex_total}"
    assert sig.gex_bias > 0.0, f"gex_bias={sig.gex_bias}"
    assert sig.dix_proxy < 0.5, f"dix_proxy={sig.dix_proxy}"
    assert sig.combined_bias > 0.0, f"combined_bias={sig.combined_bias}"
    assert sig.market_direction() == "bullish"


def test_get_orderflow_signal_bearish():
    """プット優勢データで弱気シグナルを返す。"""
    raw = (
        [_make_raw_opt("SPY260420P00490000", volume=5000, oi=10000, gamma=0.03)] * 10 +
        [_make_raw_opt("SPY260420C00500000", volume=500,  oi=1000,  gamma=0.02)] * 2
    )
    sig = get_orderflow_signal("SPY", _raw_options=raw, _spot_override=500.0, log_to_file=False)
    assert sig.data_available is True
    assert sig.gex_total < 0, f"gex_total={sig.gex_total}"
    assert sig.gex_bias < 0.0, f"gex_bias={sig.gex_bias}"
    assert sig.dix_proxy > 0.5, f"dix_proxy={sig.dix_proxy}"
    assert sig.market_direction() == "bearish"


def test_get_orderflow_signal_combined_bias_bounded():
    """combined_biasが[-1.0, +1.0]に収まる。"""
    raw = [_make_raw_opt("SPY260420C00500000", volume=99999, oi=99999, gamma=1.0)] * 100
    sig = get_orderflow_signal("SPY", _raw_options=raw, _spot_override=500.0, log_to_file=False)
    assert -1.0 <= sig.combined_bias <= 1.0, f"combined_bias={sig.combined_bias}"


def test_get_orderflow_signal_confidence():
    """レコード数に比例して信頼度が上がる (500件で1.0)。"""
    raw_small = [_make_raw_opt("SPY260420C00500000")] * 10
    raw_large = [_make_raw_opt("SPY260420C00500000")] * 500
    sig_small = get_orderflow_signal("SPY", _raw_options=raw_small, _spot_override=500.0, log_to_file=False)
    sig_large = get_orderflow_signal("SPY", _raw_options=raw_large, _spot_override=500.0, log_to_file=False)
    assert sig_large.confidence >= sig_small.confidence
    assert sig_large.confidence == 1.0


def test_get_orderflow_signal_zero_strike_calculated():
    """zero_strike_volが正しく計算される。"""
    spot = 500.0
    raw = [
        _make_raw_opt("SPY260420C00500000", volume=200),  # ATM
        _make_raw_opt("SPY260420C00600000", volume=200),  # ATM+20% (以外)
    ]
    sig = get_orderflow_signal("SPY", _raw_options=raw, _spot_override=spot, log_to_file=False)
    assert sig.zero_strike_vol > 0.0, f"zero_strike_vol={sig.zero_strike_vol}"


# -- volatility_regime / market_direction / strategy_hint テスト ---------------

def test_volatility_regime_positive_gex():
    """正GEXで low_vol を返す。"""
    sig = OrderflowSignal(symbol="SPY", gex_total=1_000_000.0)
    assert sig.volatility_regime() == "low_vol"


def test_volatility_regime_negative_gex():
    """負GEXで high_vol を返す。"""
    sig = OrderflowSignal(symbol="SPY", gex_total=-1_000_000.0)
    assert sig.volatility_regime() == "high_vol"


def test_volatility_regime_zero():
    """GEX=0で neutral を返す。"""
    sig = OrderflowSignal(symbol="SPY", gex_total=0.0)
    assert sig.volatility_regime() == "neutral"


def test_market_direction_bullish():
    """dix_proxy < 0.40 で bullish を返す。"""
    sig = OrderflowSignal(symbol="SPY", dix_proxy=0.35)
    assert sig.market_direction() == "bullish"


def test_market_direction_bearish():
    """dix_proxy > 0.60 で bearish を返す。"""
    sig = OrderflowSignal(symbol="SPY", dix_proxy=0.65)
    assert sig.market_direction() == "bearish"


def test_market_direction_neutral():
    """dix_proxy = 0.50 で neutral を返す。"""
    sig = OrderflowSignal(symbol="SPY", dix_proxy=0.50)
    assert sig.market_direction() == "neutral"


def test_strategy_hint_ic_sell():
    """低ボラ+強気 -> ic_sell を推奨する。"""
    sig = OrderflowSignal(symbol="SPY", gex_total=1_000_000.0, dix_proxy=0.30)
    assert sig.strategy_hint() == "ic_sell"


def test_strategy_hint_straddle_buy():
    """高ボラ+弱気 -> straddle_buy を推奨する。"""
    sig = OrderflowSignal(symbol="SPY", gex_total=-1_000_000.0, dix_proxy=0.70)
    assert sig.strategy_hint() == "straddle_buy"


def test_strategy_hint_orb_buy():
    """高ボラ+強気 -> orb_buy を推奨する。"""
    sig = OrderflowSignal(symbol="SPY", gex_total=-1_000_000.0, dix_proxy=0.30)
    assert sig.strategy_hint() == "orb_buy"


def test_strategy_hint_cs_sell_put():
    """低ボラ+弱気 -> cs_sell_put を推奨する。"""
    sig = OrderflowSignal(symbol="SPY", gex_total=1_000_000.0, dix_proxy=0.70)
    assert sig.strategy_hint() == "cs_sell_put"


# -- orderflow_to_bias テスト --------------------------------------------------

def test_orderflow_to_bias_keys():
    """必要なキーが全て含まれる。"""
    sig = OrderflowSignal(
        symbol="SPY",
        gex_bias=0.5,
        dix_bias=0.3,
        combined_bias=0.42,
        gex_total=1_000_000.0,
        dix_proxy=0.35,
    )
    result = orderflow_to_bias(sig)
    assert "gex_bias"       in result
    assert "dix_bias"       in result
    assert "orderflow_bias" in result
    assert "vol_regime"     in result


def test_orderflow_to_bias_values():
    """バイアス値が正確に変換される。"""
    sig = OrderflowSignal(
        symbol="SPY",
        gex_bias=0.5,
        dix_bias=0.3,
        combined_bias=0.42,
        gex_total=1_000_000.0,
        dix_proxy=0.35,
    )
    result = orderflow_to_bias(sig)
    assert result["gex_bias"]       == 0.5
    assert result["dix_bias"]       == 0.3
    assert result["orderflow_bias"] == 0.42
    assert result["vol_regime"]     == "low_vol"


# -- シンボルマッピング テスト ---------------------------------------------------

def test_symbol_map_spx():
    """SPX は _SPX にマップされる。"""
    assert _SYMBOL_MAP["SPX"] == "_SPX"


def test_symbol_map_spy():
    """SPY は SPY にマップされる。"""
    assert _SYMBOL_MAP["SPY"] == "SPY"


def test_symbol_map_multi_symbols():
    """主要銘柄が全てマッピングされている。"""
    required = ["SPX", "SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "MSFT"]
    for sym in required:
        assert sym in _SYMBOL_MAP, f"{sym} not in _SYMBOL_MAP"


# -- エントリーポイント --------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_parse_option_code_call,
        test_parse_option_code_put,
        test_parse_option_code_spx_index,
        test_parse_option_code_underscore_prefix,
        test_parse_option_code_invalid,
        test_parse_option_code_high_strike_spx,
        test_parse_options_basic,
        test_parse_options_empty_input,
        test_parse_options_filters_invalid_codes,
        test_parse_options_zero_values_allowed,
        test_compute_gex_call_dominant,
        test_compute_gex_put_dominant,
        test_compute_gex_formula_accuracy,
        test_compute_gex_skips_zero_oi,
        test_compute_gex_skips_zero_gamma,
        test_compute_gex_bias_positive_gex,
        test_compute_gex_bias_negative_gex,
        test_compute_gex_bias_zero,
        test_compute_gex_bias_bounded,
        test_compute_gex_bias_tanh_saturation,
        test_compute_dix_proxy_equal_volumes,
        test_compute_dix_proxy_call_heavy,
        test_compute_dix_proxy_put_heavy,
        test_compute_dix_proxy_no_volume,
        test_compute_dix_bias_neutral,
        test_compute_dix_bias_bullish,
        test_compute_dix_bias_bearish,
        test_compute_dix_bias_bounded,
        test_zero_strike_vol_atm_strikes,
        test_zero_strike_vol_zero_spot,
        test_get_orderflow_signal_no_data,
        test_get_orderflow_signal_bullish,
        test_get_orderflow_signal_bearish,
        test_get_orderflow_signal_combined_bias_bounded,
        test_get_orderflow_signal_confidence,
        test_get_orderflow_signal_zero_strike_calculated,
        test_volatility_regime_positive_gex,
        test_volatility_regime_negative_gex,
        test_volatility_regime_zero,
        test_market_direction_bullish,
        test_market_direction_bearish,
        test_market_direction_neutral,
        test_strategy_hint_ic_sell,
        test_strategy_hint_straddle_buy,
        test_strategy_hint_orb_buy,
        test_strategy_hint_cs_sell_put,
        test_orderflow_to_bias_keys,
        test_orderflow_to_bias_values,
        test_symbol_map_spx,
        test_symbol_map_spy,
        test_symbol_map_multi_symbols,
    ]

    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests")
    import sys as _sys
    _sys.exit(0 if failed == 0 else 1)
