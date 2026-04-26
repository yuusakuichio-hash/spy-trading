"""atlas_v3 StrategySelector 10 戦術の境界値一貫性プロパティテスト (Hypothesis)

テスト対象: atlas_v3/core/strategy_selector.py :: StrategySelector.select()

境界値不変条件:
  EB-01  IVR=0 (最低) → 高 IVR 依存戦術 (cs_sell / ic_sell / strangle_sell / calendar_sell / gamma_scalp) が選択されない
  EB-02  IVR=100 (最高) → 低 IVR 依存戦術 (butterfly) が選択されない
  EB-03  VIX=0 (極小) → high_vix 専用戦術 (straddle_buy) が選択されない
  EB-04  VIX=100 (極大) → low/medium 専用戦術 (cs_sell / ic_sell / orb_1dte) が選択されない
  EB-05  bias=neutral → cs_sell (directional 必須) が選択されない
  EB-06  bias=bull/bear → straddle_buy (non-directional 必須) が選択されない
  EB-07  delta=0 (delta_hedge 不要) → delta_hedge は常に decisions に含まれる (Phase 2 待ち常時追加)
  EB-08  IVR/VIX 全境界で confidence ∈ [0.0, 1.0]
  EB-09  decisions が list 型で各要素が TacticDecision
  EB-10  全 10 戦術名が ALL_TACTICS に含まれるか "delta_hedge" 常時追加のみ許容
  EB-11  term_ratio <= 1.0 → calendar_sell が選択されない
  EB-12  VIX >= 20 かつ IVR < 50 → gamma_scalp が選択されない
  EB-13  VIX < 18 (low) かつ bias=bull/bear → cs_sell が candidates に出てくる
  EB-14  select() は同一 env で複数回呼んでも同じ decisions を返す (deterministic)
  EB-15  decisions は confidence 降順にソートされている
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
from hypothesis import assume, given, settings, HealthCheck
from hypothesis import strategies as st

from atlas_v3.core.strategy_selector import (
    ALL_TACTICS,
    StrategySelector,
    TacticDecision,
    PercentileSelector,
)
from atlas_v3.core.env_observer import MarketEnvironment

# ── 共通ファクトリ ─────────────────────────────────────────────────────────

def _env(
    vix: float = 20.0,
    ivr: float = 50.0,
    bias: str = "neutral",
    vrp: float = 0.0,
    term_ratio: float = 1.0,
    symbol: str = "US.SPY",
) -> tuple[MarketEnvironment, str]:
    env = MarketEnvironment(
        vix=vix,
        vrp=vrp,
        term_ratio=term_ratio,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={symbol: ivr},
    )
    return env, symbol


def _names(decisions: list) -> set[str]:
    return {d.tactic_name for d in decisions}


_SELECTOR = StrategySelector(phase="phase1")

# ── 共通ストラテジー ────────────────────────────────────────────────────────

st_ivr = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
st_vix = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
st_delta = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
st_bias = st.sampled_from(["bull", "bear", "neutral"])
st_term_ratio = st.floats(min_value=0.5, max_value=3.0, allow_nan=False, allow_infinity=False)
st_vrp = st.floats(min_value=0.0, max_value=80.0, allow_nan=False, allow_infinity=False)
st_symbol = st.sampled_from(["US.SPY", "US.QQQ", "US.IWM"])
st_phase = st.sampled_from(["phase1", "phase2", "phase3", "phase4"])

_COMMON = dict(max_examples=300, suppress_health_check=[HealthCheck.too_slow])


# ── EB-01: IVR=0 → 高 IVR 戦術が選ばれない ─────────────────────────────────

@given(vix=st_vix, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb01_ivr_zero_no_high_iv_tactics(vix, bias, term_ratio, vrp, symbol):
    """EB-01: IVR=0 では高 IVR 依存戦術は candidates に入らない。"""
    env, sym = _env(vix=vix, ivr=0.0, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    high_iv_tactics = {"ic_sell", "strangle_sell", "calendar_sell", "gamma_scalp"}
    selected_high_iv = names & high_iv_tactics
    assert not selected_high_iv, (
        f"EB-01: IVR=0 but high-IV tactics selected: {selected_high_iv} (vix={vix} bias={bias})"
    )


# ── EB-02: IVR=100 → butterfly が選ばれない ────────────────────────────────

@given(vix=st_vix, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb02_ivr_100_no_butterfly(vix, bias, term_ratio, vrp, symbol):
    """EB-02: IVR=100 では butterfly は低 IVR 依存なので選択されない。"""
    env, sym = _env(vix=vix, ivr=100.0, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    assert "butterfly" not in names, (
        f"EB-02: IVR=100 but butterfly selected (vix={vix} bias={bias})"
    )


# ── EB-03: VIX=0 → straddle_buy が選ばれない ───────────────────────────────

@given(ivr=st_ivr, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb03_vix_zero_no_straddle(ivr, bias, term_ratio, vrp, symbol):
    """EB-03: VIX=0 (low regime) → straddle_buy は high VIX 専用なので出ない。"""
    env, sym = _env(vix=0.0, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    assert "straddle_buy" not in names, (
        f"EB-03: VIX=0 but straddle_buy selected (ivr={ivr} bias={bias})"
    )


# ── EB-04: VIX=100 → low/medium 専用戦術が選ばれない ──────────────────────

@given(ivr=st_ivr, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb04_vix_100_no_low_medium_tactics(ivr, term_ratio, vrp, symbol):
    """EB-04: VIX=100 (high regime) → cs_sell / ic_sell / orb_1dte は出ない。"""
    env, sym = _env(vix=100.0, ivr=ivr, bias="bull", term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    low_medium_only = {"cs_sell", "ic_sell", "orb_1dte"}
    blocked = names & low_medium_only
    assert not blocked, (
        f"EB-04: VIX=100 but low/medium tactics selected: {blocked} (ivr={ivr})"
    )


# ── EB-05: bias=neutral → cs_sell が選ばれない ─────────────────────────────

@given(vix=st_vix, ivr=st_ivr, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb05_neutral_no_cs_sell(vix, ivr, term_ratio, vrp, symbol):
    """EB-05: bias=neutral → cs_sell は directional 必須なので選択されない。"""
    env, sym = _env(vix=vix, ivr=ivr, bias="neutral", term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    assert "cs_sell" not in names, (
        f"EB-05: bias=neutral but cs_sell selected (vix={vix} ivr={ivr})"
    )


# ── EB-06: directional → straddle_buy が選ばれない ─────────────────────────

@given(
    vix=st.floats(min_value=28.1, max_value=100.0, allow_nan=False),  # high regime
    ivr=st_ivr,
    term_ratio=st_term_ratio,
    vrp=st_vrp,
    symbol=st_symbol,
    bias=st.sampled_from(["bull", "bear"]),
)
@settings(**_COMMON)
def test_eb06_directional_high_vix_no_straddle(vix, ivr, term_ratio, vrp, symbol, bias):
    """EB-06: directional bias + high VIX → straddle_buy (non-directional 依存) は出ない。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    assert "straddle_buy" not in names, (
        f"EB-06: bias={bias} high_vix but straddle_buy selected (vix={vix} ivr={ivr})"
    )


# ── EB-07: delta_hedge は常に decisions に含まれる (Phase 2 待ち) ───────────

@given(vix=st_vix, ivr=st_ivr, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb07_delta_hedge_always_present(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-07: delta_hedge は全環境で decisions に含まれる (常時追加仕様)。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    assert "delta_hedge" in names, (
        f"EB-07: delta_hedge missing (vix={vix} ivr={ivr} bias={bias})"
    )


# ── EB-08: confidence ∈ [0.0, 1.0] ──────────────────────────────────────────

@given(vix=st_vix, ivr=st_ivr, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb08_confidence_in_unit_interval(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-08: 全境界値で confidence ∈ [0.0, 1.0]。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    for d in _SELECTOR.select(env, sym):
        assert 0.0 <= d.confidence <= 1.0, (
            f"EB-08: confidence={d.confidence} out of [0,1] tactic={d.tactic_name}"
        )


# ── EB-09: decisions が list[TacticDecision] ─────────────────────────────────

@given(vix=st_vix, ivr=st_ivr, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb09_return_type_list_of_tactic_decision(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-09: select() が list を返し各要素が TacticDecision。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    result = _SELECTOR.select(env, sym)
    assert isinstance(result, list), f"EB-09: not a list: {type(result)}"
    for item in result:
        assert isinstance(item, TacticDecision), f"EB-09: item type={type(item)}"


# ── EB-10: 全戦術名が ALL_TACTICS 内 ────────────────────────────────────────

@given(vix=st_vix, ivr=st_ivr, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb10_all_tactic_names_in_registry(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-10: decisions 内の全戦術名が ALL_TACTICS に含まれる。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    for d in _SELECTOR.select(env, sym):
        assert d.tactic_name in ALL_TACTICS, (
            f"EB-10: unknown tactic={d.tactic_name!r} not in ALL_TACTICS"
        )


# ── EB-11: term_ratio <= 1.0 → calendar_sell が選ばれない ──────────────────

@given(
    vix=st_vix,
    ivr=st.floats(min_value=50.0, max_value=100.0, allow_nan=False),  # IVR 高 (高 IV 条件を満たす)
    bias=st_bias,
    vrp=st_vrp,
    symbol=st_symbol,
    term_ratio=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)
@settings(**_COMMON)
def test_eb11_term_ratio_lte1_no_calendar(vix, ivr, bias, vrp, symbol, term_ratio):
    """EB-11: term_ratio <= 1.0 → calendar_sell は contango 必須なので選択されない。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, vrp=vrp, symbol=symbol, term_ratio=term_ratio)
    names = _names(_SELECTOR.select(env, sym))
    assert "calendar_sell" not in names, (
        f"EB-11: term_ratio={term_ratio} <= 1 but calendar_sell selected (vix={vix} ivr={ivr})"
    )


# ── EB-12: VIX >= 20 かつ IVR < 50 → gamma_scalp が選ばれない ─────────────

@given(
    vix=st.floats(min_value=20.0, max_value=100.0, allow_nan=False),
    ivr=st.floats(min_value=0.0, max_value=49.9, allow_nan=False),
    bias=st_bias,
    term_ratio=st_term_ratio,
    vrp=st_vrp,
    symbol=st_symbol,
)
@settings(**_COMMON)
def test_eb12_vix_gte20_ivr_lt50_no_gamma(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-12: IVR < 50 → gamma_scalp の IVR>=50 条件を満たさないので選択されない。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    assert "gamma_scalp" not in names, (
        f"EB-12: IVR={ivr}<50 but gamma_scalp selected (vix={vix})"
    )


# ── EB-13: VIX < 18 (low) + directional → cs_sell が candidates に現れる ───

@given(
    vix=st.floats(min_value=0.0, max_value=17.9, allow_nan=False),
    ivr=st_ivr,
    bias=st.sampled_from(["bull", "bear"]),
    term_ratio=st_term_ratio,
    vrp=st_vrp,
    symbol=st_symbol,
)
@settings(**_COMMON)
def test_eb13_low_vix_directional_cs_sell_selected(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-13: low VIX + directional bias → cs_sell が decisions に含まれる。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names = _names(_SELECTOR.select(env, sym))
    assert "cs_sell" in names, (
        f"EB-13: VIX={vix}<18 bias={bias} but cs_sell NOT selected (ivr={ivr})"
    )


# ── EB-14: Determinism — 同一 env で複数回呼んで同じ結果 ───────────────────

@given(vix=st_vix, ivr=st_ivr, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb14_select_is_deterministic(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-14: 同一 env で 3 回 select() を呼んでも decisions が一致する。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    names1 = _names(_SELECTOR.select(env, sym))
    names2 = _names(_SELECTOR.select(env, sym))
    names3 = _names(_SELECTOR.select(env, sym))
    assert names1 == names2 == names3, (
        f"EB-14: non-deterministic: {names1} vs {names2} vs {names3}"
    )


# ── EB-15: decisions は confidence 降順 ──────────────────────────────────────

@given(vix=st_vix, ivr=st_ivr, bias=st_bias, term_ratio=st_term_ratio, vrp=st_vrp, symbol=st_symbol)
@settings(**_COMMON)
def test_eb15_decisions_sorted_by_confidence_desc(vix, ivr, bias, term_ratio, vrp, symbol):
    """EB-15: decisions は confidence 降順にソートされている。"""
    env, sym = _env(vix=vix, ivr=ivr, bias=bias, term_ratio=term_ratio, vrp=vrp, symbol=symbol)
    decisions = _SELECTOR.select(env, sym)
    confidences = [d.confidence for d in decisions]
    assert confidences == sorted(confidences, reverse=True), (
        f"EB-15: not sorted desc: {confidences}"
    )


# ── Phase 別 PercentileSelector 境界値 ────────────────────────────────────

@given(
    phase=st_phase,
    vix=st_vix,
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_percentile_selector_returns_unit_interval(phase, vix):
    """PercentileSelector は必ず [0.0, 1.0] の percentile を返す。"""
    ps = PercentileSelector()
    result = ps.select("ivr", phase, vix)
    assert 0.0 <= result <= 1.0, (
        f"PercentileSelector out of [0,1]: {result} (phase={phase} vix={vix})"
    )
