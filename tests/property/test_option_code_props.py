"""Property-based tests for common/option_code.py — parse_option_code

ChainGuard 級バグターゲット:
  - Strike が 6/7/8 桁のどの形式でも正しく parse できるか
  - parse→build の往復が恒等写像か (round-trip invariant)
  - 未知 root を渡したとき underlying=None になるか (不明 root を通過させない)
  - 不正入力で None が返り例外が出ないか (exception safety)
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import re
import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from common.option_code import parse_option_code, build_option_code, validate_code_for_symbol
from common.symbol_meta import _ROOT_TO_UNDERLYING, SYMBOL_META

# ── Strategies ─────────────────────────────────────────────────────────────────

# 既知のオプションコード root
KNOWN_ROOTS = list(_ROOT_TO_UNDERLYING.keys())  # SPY, QQQ, IWM, SPXW, SPX, NVDA...

# strike 範囲: 10 ~ 9999.999 (int * 1000 で 6-8 桁になる範囲)
# 6 桁: 10000 ~ 999999 (strike 10.0 ~ 999.999)
# 7 桁: 1000000 ~ 9999999 (strike 1000.0 ~ 9999.999)
# 8 桁: 10000000 ~ 99999999 (strike 10000.0 ~ 99999.999)
# futu の build_option_code は {:08d} で常に 8 桁ゼロ埋めするため
# parse 側の正規表現が 6-8 桁可変なことをテストする

st_root = st.sampled_from(KNOWN_ROOTS)
st_yy = st.integers(min_value=25, max_value=35).map(lambda x: f"{x:02d}")
st_mm = st.integers(min_value=1, max_value=12).map(lambda x: f"{x:02d}")
st_dd = st.integers(min_value=1, max_value=28).map(lambda x: f"{x:02d}")
st_side = st.sampled_from(["C", "P"])

# Strike × 1000 で 6 桁以上になる値 (strike 10 ~ 9999)
st_strike_int = st.integers(min_value=10_000, max_value=9_999_000)

# 既知銘柄の futu symbol → strike reasonable range
SYMBOL_STRIKE_RANGES: dict[str, tuple[float, float]] = {
    "US.SPY":   (100.0,  800.0),
    "US..SPX":  (1000.0, 9000.0),
    "US.QQQ":   (100.0,  700.0),
    "US.IWM":   (50.0,   400.0),
    "US.NVDA":  (10.0,   2000.0),
    "US.TSLA":  (10.0,   2000.0),
    "US.META":  (50.0,   2000.0),
    "US.AMZN":  (50.0,   400.0),
    "US.GOOGL": (50.0,   400.0),
    "US.AAPL":  (50.0,   400.0),
    "US.MSFT":  (50.0,   600.0),
}


def _build_raw_code(root: str, yy: str, mm: str, dd: str, side: str, strike_int: int) -> str:
    """テスト用に raw futu オプションコードを組み立てる (8 桁固定)。"""
    return f"US.{root}{yy}{mm}{dd}{side}{strike_int:08d}"


# ── Property 1: parse は known root に対して None を返さない ──────────────────

@given(
    root=st_root,
    yy=st_yy,
    mm=st_mm,
    dd=st_dd,
    side=st_side,
    strike_int=st_strike_int,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_parse_known_root_never_none(root, yy, mm, dd, side, strike_int):
    """既知 root + 有効な日付形式 + 8 桁 strike → parse は None を返さない。"""
    code = _build_raw_code(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assert result is not None, f"parse returned None for valid code: {code}"


# ── Property 2: strike の round-trip invariant ────────────────────────────────

@given(
    root=st_root,
    yy=st_yy,
    mm=st_mm,
    dd=st_dd,
    side=st_side,
    strike_int=st_strike_int,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_parse_strike_round_trip(root, yy, mm, dd, side, strike_int):
    """parse した strike * 1000 が元の strike_int と一致する。"""
    code = _build_raw_code(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assume(result is not None)
    recovered = int(round(result["strike"] * 1000))
    assert recovered == strike_int, (
        f"strike round-trip failed: original={strike_int} recovered={recovered} code={code}"
    )


# ── Property 3: reasonable strike range ───────────────────────────────────────

@given(
    symbol=st.sampled_from(list(SYMBOL_STRIKE_RANGES.keys())),
    yy=st_yy,
    mm=st_mm,
    dd=st_dd,
    side=st_side,
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_parse_strike_reasonable_range(symbol, yy, mm, dd, side):
    """銘柄固有の reasonable strike range が build→parse で維持される。"""
    from common.symbol_meta import get_option_root
    lo, hi = SYMBOL_STRIKE_RANGES[symbol]
    # reasonable range 内のランダム strike を生成 (pytest random seed 固定のため中間値)
    import random
    strike = round(random.uniform(lo, hi), 1)
    expiry = f"20{yy}-{mm}-{dd}"
    opt_type = "CALL" if side == "C" else "PUT"
    code = build_option_code(symbol, expiry, strike, opt_type)
    result = parse_option_code(code)
    assert result is not None, f"parse returned None for built code: {code}"
    assert lo * 0.5 <= result["strike"] <= hi * 2.0, (
        f"strike {result['strike']} out of reasonable range [{lo}, {hi}] for {symbol}"
    )


# ── Property 4: unknown root → underlying は None ─────────────────────────────

@given(
    unknown_root=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=3, max_size=8)
    .filter(lambda r: r not in _ROOT_TO_UNDERLYING),
    yy=st_yy,
    mm=st_mm,
    dd=st_dd,
    side=st_side,
    strike_int=st_strike_int,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_unknown_root_underlying_is_none(unknown_root, yy, mm, dd, side, strike_int):
    """未知 root のコードは parse 成功でも underlying=None となる。"""
    code = _build_raw_code(unknown_root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    if result is not None:
        # 未知 root なら underlying=None が期待値
        assert result["underlying"] is None, (
            f"unknown root '{unknown_root}' should yield underlying=None, got {result['underlying']}"
        )


# ── Property 5: validate_code_for_symbol は unknown root を必ずブロック ───────

@given(
    unknown_root=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=3, max_size=8)
    .filter(lambda r: r not in _ROOT_TO_UNDERLYING),
    expected=st.sampled_from(list(SYMBOL_STRIKE_RANGES.keys())),
    yy=st_yy,
    mm=st_mm,
    dd=st_dd,
    side=st_side,
    strike_int=st_strike_int,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_validate_unknown_root_always_blocked(
    unknown_root, expected, yy, mm, dd, side, strike_int
):
    """未知 root のコードは validate_code_for_symbol が必ず False を返す。"""
    code = _build_raw_code(unknown_root, yy, mm, dd, side, strike_int)
    result = validate_code_for_symbol(code, expected)
    assert result is False, (
        f"validate should block unknown root '{unknown_root}' but returned True for {expected}"
    )


# ── Property 6: 4/17 事故シナリオ — SPY strike が SPX コードとして通らない ──

@given(
    spy_strike=st.floats(min_value=100.0, max_value=800.0),
    yy=st_yy,
    mm=st_mm,
    dd=st_dd,
    side=st_side,
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_spy_strike_rejected_for_spx(spy_strike, yy, mm, dd, side):
    """SPY コード (root=SPY) を SPX のコードとして validate するのは False。
    4/17 事故の再現防止テスト。
    """
    strike_int = int(round(spy_strike * 1000))
    code = _build_raw_code("SPY", yy, mm, dd, side, strike_int)
    result = validate_code_for_symbol(code, "US..SPX")
    assert result is False, (
        f"SPY code should NOT validate as US..SPX: {code}"
    )


# ── Property 7: parse は空文字列・None 相当で例外を出さない ──────────────────

@given(st.one_of(
    st.just(""),
    st.text(max_size=5),
    st.text(alphabet="!@#$%^&*()", max_size=20),
))
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_parse_garbage_input_no_exception(bad_input):
    """ゴミ入力で parse_option_code が例外を出さずに None を返す。"""
    try:
        result = parse_option_code(bad_input)
        assert result is None or isinstance(result, dict)
    except Exception as e:
        pytest.fail(f"parse_option_code raised exception for input={bad_input!r}: {e}")


# ── Property 8: expiry の YYMMDD → YYYY-MM-DD 変換が常に合法 ─────────────────

@given(
    root=st_root,
    yy=st_yy,
    mm=st.integers(min_value=1, max_value=12).map(lambda x: f"{x:02d}"),
    dd=st.integers(min_value=1, max_value=28).map(lambda x: f"{x:02d}"),
    side=st_side,
    strike_int=st_strike_int,
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_parse_expiry_format_valid(root, yy, mm, dd, side, strike_int):
    """parse した expiry が YYYY-MM-DD 形式で実際に datetime.date として解釈可能。"""
    import datetime
    code = _build_raw_code(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assume(result is not None)
    expiry = result["expiry"]
    assert len(expiry) == 10, f"expiry length != 10: {expiry!r}"
    # datetime.date として parse 可能かチェック
    try:
        d = datetime.date.fromisoformat(expiry)
        assert d.year >= 2025
    except ValueError as e:
        pytest.fail(f"expiry={expiry!r} is not a valid date: {e}")


# ── Property 9: side は常に "C" か "P" ───────────────────────────────────────

@given(
    root=st_root,
    yy=st_yy,
    mm=st_mm,
    dd=st_dd,
    side=st_side,
    strike_int=st_strike_int,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_parse_side_always_c_or_p(root, yy, mm, dd, side, strike_int):
    """parse 結果の side は必ず 'C' または 'P'。"""
    code = _build_raw_code(root, yy, mm, dd, side, strike_int)
    result = parse_option_code(code)
    assume(result is not None)
    assert result["side"] in ("C", "P"), f"side={result['side']!r} is not C or P"


# ── Property 10: 6 桁 strike (IWM $279 相当) が 8 桁ゼロ埋めと同じ結果 ─────

def test_parse_6digit_strike_same_as_8digit():
    """
    実際の futu レスポンスで観測された 6 桁 strike (IWM $279 = 279000 / 1000)。
    _CODE_PATTERN が 6-8 桁可変なので 6 桁でも parse できること。
    Bug: もし pattern が固定 8 桁なら 6 桁コードが None を返すサイレントバグになる。
    """
    # IWM $279 → strike_int = 279000 (6 桁)
    code_6 = "US.IWM260417C279000"
    # IWM $279 → strike_int = 00279000 (8 桁ゼロ埋め)
    code_8 = "US.IWM260417C00279000"

    result_6 = parse_option_code(code_6)
    result_8 = parse_option_code(code_8)

    assert result_6 is not None, "6-digit strike code returned None (ChainGuard-level bug)"
    assert result_8 is not None, "8-digit strike code returned None"
    assert result_6["strike"] == result_8["strike"], (
        f"6-digit and 8-digit give different strikes: {result_6['strike']} vs {result_8['strike']}"
    )


# ── Property 11: build → parse の完全 round-trip ─────────────────────────────

@given(
    symbol=st.sampled_from([s for s in SYMBOL_STRIKE_RANGES.keys()]),
    yy=st.integers(min_value=25, max_value=35),
    mm=st.integers(min_value=1, max_value=12),
    dd=st.integers(min_value=1, max_value=28),
    side=st.sampled_from(["CALL", "PUT"]),
    strike_raw=st.floats(min_value=10.0, max_value=9000.0),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_build_parse_round_trip(symbol, yy, mm, dd, side, strike_raw):
    """build_option_code → parse_option_code が strike/side/root を保持する。"""
    assume(strike_raw > 0)
    # strike を strike_interval に align
    from common.symbol_meta import get_strike_interval
    interval = get_strike_interval(symbol)
    strike = round(round(strike_raw / interval) * interval, 3)
    assume(strike > 0)

    expiry = f"20{yy:02d}-{mm:02d}-{dd:02d}"
    code = build_option_code(symbol, expiry, strike, side)
    result = parse_option_code(code)

    assert result is not None, f"parse returned None for built code: {code}"
    expected_side = "C" if side == "CALL" else "P"
    assert result["side"] == expected_side, (
        f"side mismatch: expected={expected_side} got={result['side']} code={code}"
    )
    assert abs(result["strike"] - strike) < 0.01, (
        f"strike mismatch: expected={strike} got={result['strike']} code={code}"
    )
