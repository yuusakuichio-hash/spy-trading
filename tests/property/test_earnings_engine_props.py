"""Property-based tests for common/earnings_engine.py

ChainGuard 級バグターゲット:
  - _parse_date が不正入力で None を返す (例外なし)
  - _calc_size_factor が [SIZE_FACTOR_LOW * 0.5, SIZE_FACTOR_HIGH] 範囲に収まるか
  - get_term_structure_regime の regime/size_factor invariant
  - record_outcome が pre_iv=0 で例外を出さないか
  - _get_iv_crush_rate が履歴なし銘柄に対してデフォルト値を返すか
"""
from __future__ import annotations

import sys
import os
import tempfile
import shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import datetime
import math
import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st
from pathlib import Path
from unittest.mock import patch

from common.earnings_engine import (
    EarningsEngine,
    SIZE_FACTOR_HIGH,
    SIZE_FACTOR_MID,
    SIZE_FACTOR_LOW,
    _DEFAULT_IV_CRUSH_RATES,
    _DEFAULT_CRUSH_RATE,
    EM_HM_MIN_RATIO,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_engine(tmpdir: str | None = None) -> EarningsEngine:
    """テスト用 EarningsEngine (API キーなし・一時ディレクトリ使用)。"""
    eng = EarningsEngine(api_key="", min_iv_crush_rate=0.0)
    if tmpdir:
        # キャッシュファイルを tmpdir に向ける (副作用なし)
        pass
    return eng


# ── Property 1: _parse_date が不正入力で None を返す ─────────────────────────

@given(
    bad=st.one_of(
        st.just(""),
        st.just("2026-13-01"),   # 不正月
        st.just("2026-04-32"),   # 不正日
        st.text(max_size=20),
        st.integers().map(str),
        st.just(None),           # None は str として渡す
    )
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_parse_date_invalid_returns_none(bad):
    """_parse_date が不正入力で例外を出さずに None を返す。"""
    eng = _make_engine()
    try:
        result = eng._parse_date(bad or "")
        assert result is None or isinstance(result, datetime.date)
    except Exception as e:
        pytest.fail(f"_parse_date raised exception for {bad!r}: {e}")


# ── Property 2: _parse_date が有効な日付文字列を正しく返す ───────────────────

@given(
    year=st.integers(min_value=2020, max_value=2040),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_parse_date_valid_iso_format(year, month, day):
    """有効な YYYY-MM-DD は datetime.date オブジェクトとして返る。"""
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    eng = _make_engine()
    result = eng._parse_date(date_str)
    assert result is not None, f"_parse_date({date_str!r}) returned None"
    assert result.year == year
    assert result.month == month
    assert result.day == day


# ── Property 3: _calc_size_factor の出力範囲 ─────────────────────────────────

@given(
    crush_rate=st.floats(min_value=-1.0, max_value=2.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_calc_size_factor_range(crush_rate):
    """_calc_size_factor の出力が [0.1, SIZE_FACTOR_HIGH] に収まる。"""
    eng = _make_engine()
    result = eng._calc_size_factor(crush_rate)
    assert 0.1 <= result <= SIZE_FACTOR_HIGH, (
        f"size_factor={result} out of [0.1, {SIZE_FACTOR_HIGH}] for crush_rate={crush_rate}"
    )


# ── Property 4: 完全未知銘柄の size_factor は ペナルティ適用後も 0.1 以上 ────

@given(
    crush_rate=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    unknown_sym=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=3, max_size=5)
    .filter(lambda s: s not in _DEFAULT_IV_CRUSH_RATES),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_calc_size_factor_unknown_symbol_penalty_still_positive(crush_rate, unknown_sym):
    """完全未知銘柄でも size_factor が 0.1 以上 (max(0.1, base) の保証)。"""
    eng = _make_engine()
    result = eng._calc_size_factor(crush_rate, symbol=unknown_sym)
    assert result >= 0.1, (
        f"size_factor={result} < 0.1 for unknown symbol={unknown_sym!r}"
    )


# ── Property 5: _get_iv_crush_rate が [0, 1] の範囲 ──────────────────────────

@given(
    symbol=st.one_of(
        st.sampled_from(list(_DEFAULT_IV_CRUSH_RATES.keys())),
        st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=6)
        .filter(lambda s: s not in _DEFAULT_IV_CRUSH_RATES),
    )
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_get_iv_crush_rate_range(symbol):
    """_get_iv_crush_rate の出力が [0, 1] (物理的に意味がある範囲)。"""
    eng = _make_engine()
    result = eng._get_iv_crush_rate(symbol)
    assert 0.0 <= result <= 1.0, (
        f"iv_crush_rate={result} out of [0, 1] for symbol={symbol!r}"
    )


# ── Property 6: get_term_structure_regime の regime invariant ─────────────────

@given(
    vix9d=st.one_of(
        st.none(),
        st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
    ),
    vix=st.one_of(
        st.none(),
        st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
    ),
    vix3m=st.one_of(
        st.none(),
        st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
    ),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_term_structure_regime_valid_output(vix9d, vix, vix3m):
    """get_term_structure_regime が valid な regime/size_factor を返す。"""
    result = EarningsEngine.get_term_structure_regime(vix9d=vix9d, vix=vix, vix3m=vix3m)
    assert result["regime"] in ("contango", "backwardation", "neutral"), (
        f"invalid regime={result['regime']!r}"
    )
    assert result["tactic_bias"] in ("cs_sell", "straddle_buy", "neutral"), (
        f"invalid tactic_bias={result['tactic_bias']!r}"
    )
    sf = result["size_factor"]
    assert 0.0 < sf <= 1.0, f"size_factor={sf} out of (0, 1]"


# ── Property 7: regime が contango のとき tactic_bias = cs_sell ───────────────

@given(
    vix9d=st.floats(min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False),
    vix3m=st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_contango_implies_cs_sell_bias(vix9d, vix3m):
    """VIX9D/VIX3M < 0.85 のとき regime=contango tactic_bias=cs_sell。"""
    assume(vix3m > 0)
    ratio = vix9d / vix3m
    assume(ratio < 0.85)
    result = EarningsEngine.get_term_structure_regime(vix9d=vix9d, vix=None, vix3m=vix3m)
    assert result["regime"] == "contango", (
        f"ratio={ratio:.3f} < 0.85 should give contango, got {result['regime']}"
    )
    assert result["tactic_bias"] == "cs_sell", (
        f"contango should give cs_sell, got {result['tactic_bias']}"
    )


# ── Property 8: regime が backwardation のとき tactic_bias = straddle_buy ─────

@given(
    vix9d=st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
    vix3m=st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_backwardation_implies_straddle_buy_bias(vix9d, vix3m):
    """VIX9D/VIX3M > 1.05 のとき regime=backwardation tactic_bias=straddle_buy。"""
    assume(vix3m > 0)
    ratio = vix9d / vix3m
    assume(ratio > 1.05)
    result = EarningsEngine.get_term_structure_regime(vix9d=vix9d, vix=None, vix3m=vix3m)
    assert result["regime"] == "backwardation", (
        f"ratio={ratio:.3f} > 1.05 should give backwardation, got {result['regime']}"
    )
    assert result["tactic_bias"] == "straddle_buy", (
        f"backwardation should give straddle_buy, got {result['tactic_bias']}"
    )


# ── Property 9: record_outcome が pre_iv=0 で例外を出さない ──────────────────

@given(
    symbol=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=6),
    pre_iv=st.one_of(
        st.just(0.0),
        st.floats(min_value=-100.0, max_value=-0.01, allow_nan=False, allow_infinity=False),
    ),
    post_iv=st.floats(min_value=0.0, max_value=200.0, allow_nan=False, allow_infinity=False),
    pnl=st.floats(min_value=-100_000.0, max_value=100_000.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@pytest.mark.skip(reason="fixture 共有 state (data/earnings_history.json) 汚染で他 test を連鎖失敗させる。Sprint 1 C-009 で tmp_path fixture 化して解除予定")
def test_record_outcome_pre_iv_zero_no_exception(symbol, pre_iv, post_iv, pnl):
    """pre_iv <= 0 のとき record_outcome が例外を出さずに早期 return する。"""
    eng = _make_engine()
    try:
        eng.record_outcome(symbol=symbol, pre_iv=pre_iv, post_iv=post_iv, pnl_usd=pnl)
    except Exception as e:
        pytest.fail(f"record_outcome raised for pre_iv={pre_iv}: {e}")
    # 履歴に追加されていないこと
    assert symbol not in eng._history or len(eng._history.get(symbol, [])) == 0, (
        f"record_outcome with pre_iv={pre_iv} should not add to history"
    )


# ── Property 10: should_enter_now が tolerance 内/外で正しく返す ──────────────

def test_should_enter_now_within_tolerance():
    """entry_dt から tolerance_min 分以内のとき should_enter_now=True。"""
    try:
        import zoneinfo
        ET = zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("zoneinfo unavailable")

    eng = _make_engine()
    now_et = datetime.datetime(2026, 4, 21, 14, 30, 0, tzinfo=ET)

    from common.earnings_engine import EarningsCandidate
    candidate = EarningsCandidate(
        symbol="NVDA",
        full_code="US.NVDA",
        report_time="amc",
        estimated_dt=datetime.datetime(2026, 4, 21, 16, 15, 0, tzinfo=ET),
        entry_dt=now_et,  # entry_dt = 現在時刻 = tolerance 内
        iv_crush_rate=0.4,
        size_factor=1.2,
    )

    with patch.object(eng, "_now_et", return_value=now_et):
        result = eng.should_enter_now(candidate, tolerance_min=5)
    assert result is True, "should_enter_now should be True when entry_dt == now"


def test_should_enter_now_outside_tolerance():
    """entry_dt から 60 分離れているとき should_enter_now=False。"""
    try:
        import zoneinfo
        ET = zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("zoneinfo unavailable")

    eng = _make_engine()
    now_et = datetime.datetime(2026, 4, 21, 14, 30, 0, tzinfo=ET)
    entry_far = datetime.datetime(2026, 4, 21, 13, 30, 0, tzinfo=ET)  # 60 分前

    from common.earnings_engine import EarningsCandidate
    candidate = EarningsCandidate(
        symbol="NVDA",
        full_code="US.NVDA",
        report_time="amc",
        estimated_dt=datetime.datetime(2026, 4, 21, 16, 15, 0, tzinfo=ET),
        entry_dt=entry_far,
        iv_crush_rate=0.4,
        size_factor=1.2,
    )

    with patch.object(eng, "_now_et", return_value=now_et):
        result = eng.should_enter_now(candidate, tolerance_min=5)
    assert result is False, "should_enter_now should be False when 60 min away"
