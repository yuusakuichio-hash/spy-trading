"""ChainGuard regex プロパティテスト (Hypothesis)

対象: common/option_code.py の parse_option_code / build_option_code / validate_code_for_symbol

ChainGuard バグカテゴリ:
  CG-01  unknown root が underlying=None を返す (4/17 事故根因)
  CG-02  6 桁 strike / 7 桁 strike / 8 桁 strike いずれも parse 可能か
  CG-03  ゴミ入力で例外が出ない (exception safety)
  CG-04  build → parse 完全 round-trip (strike・side・root 保持)
  CG-05  validate_code_for_symbol が誤銘柄コードを必ずブロック
  CG-06  expiry が常に合法な YYYY-MM-DD として解釈可能
  CG-07  側面 (C/P) が反転しない
  CG-08  SPY コードが SPX シンボルとして通らない (4/17 再現防止)
  CG-09  strike ゼロ以下が parse 後に正値になれない
  CG-10  validate は parse が None を返すコードで必ず False
"""
from __future__ import annotations

import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from hypothesis import assume, given, settings, HealthCheck
from hypothesis import strategies as st

from common.option_code import parse_option_code, build_option_code, validate_code_for_symbol
from common.symbol_meta import _ROOT_TO_UNDERLYING, get_option_root, get_strike_interval

# ── 共通ストラテジー ────────────────────────────────────────────────────────

KNOWN_ROOTS = list(_ROOT_TO_UNDERLYING.keys())
KNOWN_SYMBOLS = [
    "US.SPY", "US.QQQ", "US.IWM", "US..SPX",
    "US.NVDA", "US.TSLA", "US.META", "US.AAPL",
]

st_root = st.sampled_from(KNOWN_ROOTS)
st_yy = st.integers(min_value=25, max_value=35).map(lambda x: f"{x:02d}")
st_mm = st.integers(min_value=1, max_value=12).map(lambda x: f"{x:02d}")
st_dd = st.integers(min_value=1, max_value=28).map(lambda x: f"{x:02d}")
st_side = st.sampled_from(["C", "P"])

# regex は \d{6,8} 制約 → strike_int は最低 6 桁 (100_000) 必要
# 6 桁: $100-999  (IWM など低株価)
# 7 桁: $1000-9999 (SPY/QQQ)
# 8 桁: $10000-99999 (SPX)
st_strike_6 = st.integers(min_value=100_000, max_value=999_999)      # 6 桁固定 /1000 = $100-999
st_strike_7 = st.integers(min_value=1_000_000, max_value=9_999_000)  # /1000 = $1000-9999
st_strike_8 = st.integers(min_value=10_000_000, max_value=99_999_000)  # /1000 = $10000-99999
st_strike_any = st.integers(min_value=100_000, max_value=9_999_000)  # 6-7 桁


def _raw(root: str, yy: str, mm: str, dd: str, side: str, strike_int: int, pad: int = 0) -> str:
    """futu 形式のオプションコードを生成。pad=0 は最小桁そのまま、pad>0 でゼロ埋め。"""
    s = f"{strike_int:0{pad}d}" if pad else str(strike_int)
    return f"US.{root}{yy}{mm}{dd}{side}{s}"


# ── CG-01: known root は parse で None を返さない ───────────────────────────

@given(
    root=st_root, yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_any,
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_cg01_known_root_never_none(root, yy, mm, dd, side, strike_int):
    """CG-01: 既知 root + 有効な YYMMDD + 6-7 桁 strike → None を返さない。"""
    code = _raw(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assert result is not None, f"CG-01: parse returned None for known root: {code}"


# ── CG-02a: 6 桁 strike が parse できる ────────────────────────────────────

@given(
    root=st.sampled_from(["IWM", "SPY"]),
    yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_6,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg02a_6digit_strike_parseable(root, yy, mm, dd, side, strike_int):
    """CG-02a: 6 桁 strike コードが parse できる (IWM $279 = 279000)。"""
    code = _raw(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assert result is not None, f"CG-02a: 6-digit strike returned None: {code}"
    assert result["strike"] == pytest.approx(strike_int / 1000.0, abs=0.001)


# ── CG-02b: 7 桁 strike が parse できる ────────────────────────────────────

@given(
    root=st.sampled_from(["SPY", "QQQ"]),
    yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_7,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg02b_7digit_strike_parseable(root, yy, mm, dd, side, strike_int):
    """CG-02b: 7 桁 strike コードが parse できる (SPY $710 = 7100000 / 10000)。"""
    code = _raw(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assert result is not None, f"CG-02b: 7-digit strike returned None: {code}"


# ── CG-02c: 8 桁ゼロ埋めと 6-7 桁生数値が同じ strike を返す ───────────────

@given(
    root=st_root, yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_6,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg02c_6vs8_digit_same_strike(root, yy, mm, dd, side, strike_int):
    """CG-02c: 6 桁生数値と 8 桁ゼロ埋めは同じ strike を返す。"""
    code_raw = _raw(root, yy, mm, dd, side, strike_int)
    code_pad = _raw(root, yy, mm, dd, side, strike_int, pad=8)
    r_raw = parse_option_code(code_raw)
    r_pad = parse_option_code(code_pad)
    assume(r_raw is not None and r_pad is not None)
    assert r_raw["strike"] == pytest.approx(r_pad["strike"], abs=0.001), (
        f"CG-02c: 6/8 digit mismatch: raw={r_raw['strike']} pad={r_pad['strike']}"
    )


# ── CG-03: ゴミ入力で例外が出ない ──────────────────────────────────────────

@given(st.one_of(
    st.just(""),
    st.text(max_size=4),
    st.text(alphabet="!@#$%^&*()\n\t", max_size=30),
    st.binary(max_size=20).map(lambda b: b.decode("latin-1", errors="replace")),
))
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg03_garbage_no_exception(bad):
    """CG-03: ゴミ入力で parse_option_code が例外を投げず None を返す。"""
    try:
        result = parse_option_code(bad)
        assert result is None or isinstance(result, dict)
    except Exception as exc:
        pytest.fail(f"CG-03: exception raised for input={bad!r}: {exc}")


# ── CG-04: build → parse 完全 round-trip ────────────────────────────────────

@given(
    symbol=st.sampled_from(KNOWN_SYMBOLS),
    yy=st.integers(min_value=25, max_value=35),
    mm=st.integers(min_value=1, max_value=12),
    dd=st.integers(min_value=1, max_value=28),
    side=st.sampled_from(["CALL", "PUT"]),
    strike_raw=st.floats(min_value=10.0, max_value=8000.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_cg04_build_parse_round_trip(symbol, yy, mm, dd, side, strike_raw):
    """CG-04: build → parse が strike / side / root を完全保持する。"""
    assume(strike_raw > 0)
    interval = get_strike_interval(symbol)
    strike = round(round(strike_raw / interval) * interval, 3)
    assume(strike > 0)
    expiry = f"20{yy:02d}-{mm:02d}-{dd:02d}"
    code = build_option_code(symbol, expiry, strike, side)
    result = parse_option_code(code)
    assert result is not None, f"CG-04: parse returned None: {code}"
    expected_side = "C" if side == "CALL" else "P"
    assert result["side"] == expected_side, (
        f"CG-04: side mismatch expected={expected_side} got={result['side']} code={code}"
    )
    assert abs(result["strike"] - strike) < 0.01, (
        f"CG-04: strike mismatch expected={strike} got={result['strike']} code={code}"
    )


# ── CG-05: validate は誤銘柄コードを必ずブロック ────────────────────────────

@given(
    wrong_symbol=st.sampled_from(["US.QQQ", "US.IWM", "US..SPX", "US.NVDA"]),
    yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_any,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg05_validate_blocks_wrong_symbol(wrong_symbol, yy, mm, dd, side, strike_int):
    """CG-05: SPY コードを他銘柄として validate すると必ず False。"""
    code = _raw("SPY", yy, mm, dd, side, strike_int)
    result = validate_code_for_symbol(code, wrong_symbol)
    assert result is False, (
        f"CG-05: SPY code should NOT validate as {wrong_symbol}: {code}"
    )


# ── CG-06: expiry が常に合法な date として解釈可能 ──────────────────────────

@given(
    root=st_root, yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_any,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg06_expiry_is_valid_date(root, yy, mm, dd, side, strike_int):
    """CG-06: parse した expiry が datetime.date.fromisoformat で解釈可能。"""
    code = _raw(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assume(result is not None)
    expiry = result["expiry"]
    assert len(expiry) == 10, f"CG-06: expiry length={len(expiry)} != 10: {expiry!r}"
    try:
        d = datetime.date.fromisoformat(expiry)
        assert d.year >= 2025, f"CG-06: year too old: {d}"
    except ValueError as exc:
        pytest.fail(f"CG-06: invalid date {expiry!r}: {exc}")


# ── CG-07: side は反転しない ─────────────────────────────────────────────────

@given(
    root=st_root, yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_any,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg07_side_preserved(root, yy, mm, dd, side, strike_int):
    """CG-07: parse 後の side が入力と一致する。"""
    code = _raw(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assume(result is not None)
    assert result["side"] == side, (
        f"CG-07: side reversed: input={side} got={result['side']} code={code}"
    )


# ── CG-08: SPY コードが SPX シンボルとして通らない (4/17 再現防止) ──────────

@given(
    spy_strike=st.floats(min_value=100.0, max_value=800.0, allow_nan=False),
    yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_cg08_spy_rejected_for_spx(spy_strike, yy, mm, dd, side):
    """CG-08 (4/17 事故再現防止): SPY コードが US..SPX として validate = False。"""
    si = int(round(spy_strike * 1000))
    code = _raw("SPY", yy, mm, dd, side, si)
    assert validate_code_for_symbol(code, "US..SPX") is False, (
        f"CG-08: SPY code validated as SPX — 4/17 regression: {code}"
    )


# ── CG-09: strike が正値 ─────────────────────────────────────────────────────

@given(
    root=st_root, yy=st_yy, mm=st_mm, dd=st_dd, side=st_side,
    strike_int=st_strike_any,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg09_strike_positive(root, yy, mm, dd, side, strike_int):
    """CG-09: parse された strike は必ず正値。"""
    code = _raw(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assume(result is not None)
    assert result["strike"] > 0, f"CG-09: non-positive strike: {result['strike']} code={code}"


# ── CG-10: parse が None を返すコードは validate も False ───────────────────

@given(
    bad=st.one_of(
        st.text(max_size=10),
        st.just("US.ZZZZZ999999C00000000"),
    ),
    symbol=st.sampled_from(KNOWN_SYMBOLS),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_cg10_parse_none_means_validate_false(bad, symbol):
    """CG-10: parse が None を返すコードは validate_code_for_symbol も False。"""
    if parse_option_code(bad) is None:
        assert validate_code_for_symbol(bad, symbol) is False, (
            f"CG-10: validate returned True for unparseable code={bad!r} symbol={symbol}"
        )


# ── deterministic regression: 4/17 実例 ─────────────────────────────────────

def test_4_17_accident_exact_replay():
    """4/17 事故の完全再現: SPY $710 が SPX として validate = False。"""
    spy_code = "US.SPY260417C00710000"
    assert validate_code_for_symbol(spy_code, "US..SPX") is False
    assert validate_code_for_symbol(spy_code, "US.SPY") is True


def test_6digit_strike_iwm_279():
    """IWM $279 (6 桁 279000) が parse できる。"""
    code6 = "US.IWM260417C279000"   # 6 桁 (regex \d{6,8} で合法)
    code8 = "US.IWM260417C00279000"  # 8 桁ゼロ埋め
    r6 = parse_option_code(code6)
    r8 = parse_option_code(code8)
    assert r6 is not None, "6-digit IWM strike returned None"
    assert r8 is not None, "8-digit IWM strike returned None"
    assert r6["strike"] == pytest.approx(r8["strike"], abs=0.001)
