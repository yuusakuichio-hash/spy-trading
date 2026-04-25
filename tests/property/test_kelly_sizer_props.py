"""Property-based tests for common/kelly_sizer.py

ChainGuard 級バグターゲット:
  - calc_kelly の出力が [0, max_kelly_fraction] に収まるか
  - win_rate 境界値 (0, 1) で暴走しないか
  - rr_ratio=0 で暴走しないか
  - fail-closed plan_id (空文字/DEPRECATED) は必ず Kelly=0
  - get_size_pct が [0.0, 1.0] に収まるか
  - 期待値負 (full_kelly < 0) のとき必ず 0.0 を返すか
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import math
import pytest
from hypothesis import given, settings, assume, HealthCheck
from hypothesis import strategies as st

from common.kelly_sizer import KellySizer, calc_plan_kelly, _DEFAULT_PROFILES

# ── Strategy ──────────────────────────────────────────────────────────────────

VALID_PLAN_IDS = list(_DEFAULT_PROFILES.keys())

st_win_rate_valid = st.floats(
    min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False
)
st_rr_valid = st.floats(
    min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False
)

# ── Property 1: calc_kelly の出力範囲 [0, max_kelly_fraction] ─────────────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    win_rate=st_win_rate_valid,
    rr_ratio=st_rr_valid,
    half_kelly=st.booleans(),
)
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
def test_calc_kelly_within_range(plan_id, win_rate, rr_ratio, half_kelly):
    """calc_kelly の出力が [0.0, max_kelly_fraction] に収まる。"""
    sizer = KellySizer(plan_id, yaml_override={})
    result = sizer.calc_kelly(win_rate=win_rate, rr_ratio=rr_ratio, half_kelly=half_kelly)
    max_k = sizer.get_profile().max_kelly_fraction
    assert 0.0 <= result <= max_k, (
        f"kelly={result} out of [0, {max_k}] for plan={plan_id}, "
        f"win_rate={win_rate:.3f}, rr={rr_ratio:.3f}"
    )


# ── Property 2: win_rate 境界値で例外が出ず 0.10 フォールバックが返る ─────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    win_rate=st.one_of(
        st.just(0.0),
        st.just(1.0),
        st.floats(max_value=-0.001, allow_nan=False, allow_infinity=False),
        st.floats(min_value=1.001, max_value=100.0, allow_nan=False, allow_infinity=False),
    ),
    rr_ratio=st_rr_valid,
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_boundary_win_rate_no_exception(plan_id, win_rate, rr_ratio):
    """win_rate <= 0 or >= 1 のとき例外が出ず 0.10 フォールバック (または 0.0) が返る。"""
    sizer = KellySizer(plan_id, yaml_override={})
    try:
        result = sizer.calc_kelly(win_rate=win_rate, rr_ratio=rr_ratio)
        # フォールバックは 0.10 か 0.0 のどちらか
        assert result >= 0.0, f"negative kelly: {result}"
        assert result <= 1.0, f"kelly > 1.0: {result}"
    except Exception as e:
        pytest.fail(
            f"calc_kelly raised exception for win_rate={win_rate}: {e}"
        )


# ── Property 3: rr_ratio=0 以下で例外が出ず 0.10 フォールバック ──────────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    rr_ratio=st.one_of(
        st.just(0.0),
        st.floats(max_value=-0.001, allow_nan=False, allow_infinity=False),
    ),
    win_rate=st_win_rate_valid,
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_invalid_rr_no_exception(plan_id, rr_ratio, win_rate):
    """rr_ratio <= 0 のとき例外が出ず安全な値が返る。"""
    sizer = KellySizer(plan_id, yaml_override={})
    try:
        result = sizer.calc_kelly(win_rate=win_rate, rr_ratio=rr_ratio)
        assert 0.0 <= result <= 1.0, f"result={result} out of [0, 1]"
    except Exception as e:
        pytest.fail(f"calc_kelly raised for rr_ratio={rr_ratio}: {e}")


# ── Property 4: fail-closed plan_id は Kelly=0 ───────────────────────────────

@given(
    fail_closed=st.one_of(
        st.just(""),
        st.just("core_50k"),
        st.text(min_size=1, max_size=30)
        .filter(lambda s: s not in _DEFAULT_PROFILES and s not in ("", "core_50k")),
    ),
    win_rate=st_win_rate_valid,
    rr_ratio=st_rr_valid,
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_fail_closed_returns_zero(fail_closed, win_rate, rr_ratio):
    """空文字・DEPRECATED・未知 plan_id は fail-closed で Kelly=0.0。"""
    sizer = KellySizer(fail_closed, yaml_override={})
    result = sizer.calc_kelly(win_rate=win_rate, rr_ratio=rr_ratio)
    assert result == 0.0, (
        f"fail-closed plan_id={fail_closed!r} should return 0.0, got {result}"
    )


# ── Property 5: 期待値負 (full_kelly <= 0) のとき必ず 0.0 ────────────────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    # full_kelly = (b*p - q) / b <= 0 <=> b*p <= q = 1-p <=> p <= 1/(1+b)
    # win_rate が十分低ければ full_kelly < 0 になる
    win_rate=st.floats(min_value=0.01, max_value=0.30, allow_nan=False, allow_infinity=False),
    rr_ratio=st.floats(min_value=2.0, max_value=10.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_negative_expected_value_returns_zero(plan_id, win_rate, rr_ratio):
    """full_kelly <= 0 (期待値負) のとき calc_kelly は 0.0 を返す。"""
    p = win_rate
    b = rr_ratio
    full_kelly = (b * p - (1 - p)) / b
    assume(full_kelly <= 0)

    sizer = KellySizer(plan_id, yaml_override={})
    result = sizer.calc_kelly(win_rate=win_rate, rr_ratio=rr_ratio)
    assert result == 0.0, (
        f"negative full_kelly={full_kelly:.4f} should give 0.0, "
        f"got {result} (win_rate={win_rate:.3f}, rr={rr_ratio:.3f}, plan={plan_id})"
    )


# ── Property 6: get_size_pct の出力が [0.0, 1.0] ─────────────────────────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    kelly_frac=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
    daily_pnl=st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False, allow_infinity=False),
    daily_target=st.floats(min_value=0.0, max_value=100_000.0, allow_nan=False, allow_infinity=False),
    hft_count=st.integers(min_value=0, max_value=300),
    hft_limit=st.integers(min_value=1, max_value=500),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_get_size_pct_range(plan_id, kelly_frac, daily_pnl, daily_target, hft_count, hft_limit):
    """get_size_pct の出力が常に [0.0, 1.0]。"""
    sizer = KellySizer(plan_id, yaml_override={})
    result = sizer.get_size_pct(
        kelly_fraction=kelly_frac,
        daily_pnl=daily_pnl,
        daily_target=daily_target,
        hft_count_today=hft_count,
        hft_limit=hft_limit,
    )
    assert 0.0 <= result <= 1.0, (
        f"get_size_pct={result} out of [0.0, 1.0] for plan={plan_id}"
    )


# ── Property 7: half_kelly=True は full_kelly=False より常に <= ───────────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    win_rate=st.floats(min_value=0.5, max_value=0.99, allow_nan=False, allow_infinity=False),
    rr_ratio=st.floats(min_value=0.5, max_value=5.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
def test_half_kelly_leq_full_kelly(plan_id, win_rate, rr_ratio):
    """half_kelly=True の結果は full_kelly=False の結果以下 (または等しい)。"""
    sizer = KellySizer(plan_id, yaml_override={})
    half = sizer.calc_kelly(win_rate=win_rate, rr_ratio=rr_ratio, half_kelly=True)
    full = sizer.calc_kelly(win_rate=win_rate, rr_ratio=rr_ratio, half_kelly=False)
    assert half <= full + 1e-9, (
        f"half_kelly={half} should be <= full_kelly={full} "
        f"(win_rate={win_rate:.3f}, rr={rr_ratio:.3f}, plan={plan_id})"
    )


# ── Property 8: win_rate が上がると Kelly が単調増加する (同 rr_ratio) ─────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    rr_ratio=st.floats(min_value=0.5, max_value=5.0, allow_nan=False, allow_infinity=False),
    win_rate_lo=st.floats(min_value=0.40, max_value=0.60, allow_nan=False, allow_infinity=False),
    delta=st.floats(min_value=0.05, max_value=0.35, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
def test_kelly_monotone_in_win_rate(plan_id, rr_ratio, win_rate_lo, delta):
    """win_rate が増加すると Kelly が減少しない (単調増加 invariant)。
    ただし max_kelly_fraction による clamp で等しくなることはある。
    """
    win_rate_hi = win_rate_lo + delta
    assume(win_rate_hi < 1.0)

    sizer = KellySizer(plan_id, yaml_override={})
    k_lo = sizer.calc_kelly(win_rate=win_rate_lo, rr_ratio=rr_ratio)
    k_hi = sizer.calc_kelly(win_rate=win_rate_hi, rr_ratio=rr_ratio)

    assert k_hi >= k_lo - 1e-9, (
        f"kelly should be monotone in win_rate: "
        f"k({win_rate_lo:.3f})={k_lo:.4f} > k({win_rate_hi:.3f})={k_hi:.4f} "
        f"(rr={rr_ratio:.3f}, plan={plan_id})"
    )


# ── Property 9: fail-closed plan で get_size_pct=0.0 ─────────────────────────

def test_fail_closed_get_size_pct_zero():
    """fail-closed plan_id は get_size_pct も 0.0 を返す。
    Bug チェック: fail-closed でも get_size_pct が非ゼロを返すと誤発注につながる。
    """
    sizer = KellySizer("", yaml_override={})
    result = sizer.get_size_pct(kelly_fraction=0.25)
    assert result == 0.0, (
        f"fail-closed get_size_pct should return 0.0, got {result}"
    )


# ── Property 10: NaN/Inf の入力で例外が出ない ────────────────────────────────

@given(
    plan_id=st.sampled_from(VALID_PLAN_IDS),
    win_rate=st.one_of(
        st.just(float("nan")),
        st.just(float("inf")),
        st.just(float("-inf")),
    ),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_nan_inf_win_rate_no_exception(plan_id, win_rate):
    """NaN/Inf の win_rate で例外が出ない。"""
    sizer = KellySizer(plan_id, yaml_override={})
    try:
        result = sizer.calc_kelly(win_rate=win_rate, rr_ratio=1.3)
        # 結果は数値であること
        assert not math.isnan(result), f"result is NaN for win_rate={win_rate}"
        assert not math.isinf(result), f"result is Inf for win_rate={win_rate}"
    except Exception as e:
        pytest.fail(f"calc_kelly raised for win_rate={win_rate}: {e}")
