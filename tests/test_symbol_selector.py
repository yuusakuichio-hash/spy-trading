"""
tests/test_symbol_selector.py — 銘柄選択エンジン テスト (15件以上)

テスト方針:
  - 固定閾値がコード内に存在しないことを確認
  - スコアリングの方向性 (高IVR→credit_spreadで高スコア等) を検証
  - ユニバースが変化してもランキングが相対的に正しいことを確認
  - 除外ロジック、エッジケース、戦術別ウェイトを網羅
"""

import math
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.symbol_selector import (
    SymbolMetrics,
    SymbolScore,
    score_symbols,
    select_symbols,
    get_default_universe,
    get_tactic_names,
    _normalize_ivr,
    _normalize_volume,
    _normalize_liquidity,
    _normalize_vix_corr,
    _normalize_gap,
    _compute_raw_scores,
    _weighted_score,
    _TACTIC_WEIGHTS,
)


# ── フィクスチャ ──────────────────────────────────────────────────────────────

def make_metrics(
    symbol: str = "SPY",
    ivr: float = 50.0,
    vol_spike: float = 1.0,
    gap: float = 0.01,
    spread_pct: float = 0.001,
    vix_corr: float = 0.7,
    near_earnings: bool = False,
    hist_gaps: list = None,
) -> SymbolMetrics:
    return SymbolMetrics(
        symbol=symbol,
        ivr=ivr,
        volume_spike_ratio=vol_spike,
        gap_abs_pct=gap,
        bid_ask_spread_pct=spread_pct,
        vix_correlation=vix_corr,
        near_earnings=near_earnings,
        hist_gaps=hist_gaps or [0.01] * 10,
    )


def make_high_ivr() -> SymbolMetrics:
    return make_metrics("HIGH_IVR", ivr=90.0, vol_spike=1.2, gap=0.005)


def make_low_ivr() -> SymbolMetrics:
    return make_metrics("LOW_IVR", ivr=10.0, vol_spike=0.8, gap=0.005)


def make_high_vol() -> SymbolMetrics:
    return make_metrics("HIGH_VOL", ivr=50.0, vol_spike=5.0, gap=0.04)


def make_low_vol() -> SymbolMetrics:
    return make_metrics("LOW_VOL", ivr=50.0, vol_spike=0.5, gap=0.004)


def make_liquid() -> SymbolMetrics:
    return make_metrics("LIQUID", spread_pct=0.0005, ivr=50.0)


def make_illiquid() -> SymbolMetrics:
    return make_metrics("ILLIQUID", spread_pct=0.05, ivr=50.0)


# ── Test 1: デフォルトユニバースが空でない ───────────────────────────────────

def test_default_universe_nonempty():
    universe = get_default_universe()
    assert len(universe) > 0
    assert "SPY" in universe


# ── Test 2: サポート戦術名が存在する ────────────────────────────────────────

def test_tactic_names():
    names = get_tactic_names()
    assert "credit_spread" in names
    assert "straddle" in names
    assert "butterfly" in names
    assert "iron_condor" in names


# ── Test 3: 空リストでクラッシュしない ──────────────────────────────────────

def test_empty_metrics_list():
    result = score_symbols([])
    assert result == []

    result2 = select_symbols([])
    assert result2 == []


# ── Test 4: 単一銘柄でスコアが返る ──────────────────────────────────────────

def test_single_symbol():
    m = make_metrics("SPY", ivr=60.0)
    result = score_symbols([m])
    assert len(result) == 1
    assert result[0].symbol == "SPY"
    assert 0.0 <= result[0].score <= 1.0


# ── Test 5: credit_spread で高IVRが高スコア ─────────────────────────────────

def test_credit_spread_high_ivr_wins():
    high = make_high_ivr()
    low = make_low_ivr()
    results = score_symbols([high, low], tactic="credit_spread")
    # high IVR should score higher than low IVR for credit_spread
    high_result = next(r for r in results if r.symbol == "HIGH_IVR")
    low_result  = next(r for r in results if r.symbol == "LOW_IVR")
    assert high_result.score > low_result.score, (
        f"HIGH_IVR({high_result.score:.4f}) should > LOW_IVR({low_result.score:.4f})"
    )


# ── Test 6: straddle で高volspike・大gapが高スコア ──────────────────────────

def test_straddle_high_vol_wins():
    high = make_high_vol()
    low = make_low_vol()
    results = score_symbols([high, low], tactic="straddle")
    high_result = next(r for r in results if r.symbol == "HIGH_VOL")
    low_result  = next(r for r in results if r.symbol == "LOW_VOL")
    assert high_result.score > low_result.score, (
        f"straddle: HIGH_VOL({high_result.score:.4f}) should > LOW_VOL({low_result.score:.4f})"
    )


# ── Test 7: butterfly で低IVRが高スコア ─────────────────────────────────────

def test_butterfly_low_ivr_wins():
    high = make_high_ivr()
    low = make_low_ivr()
    results = score_symbols([high, low], tactic="butterfly")
    high_result = next(r for r in results if r.symbol == "HIGH_IVR")
    low_result  = next(r for r in results if r.symbol == "LOW_IVR")
    assert low_result.score > high_result.score, (
        f"butterfly: LOW_IVR({low_result.score:.4f}) should > HIGH_IVR({high_result.score:.4f})"
    )


# ── Test 8: 高流動性銘柄がcredit_spreadで優位 ───────────────────────────────

def test_liquidity_matters_in_credit_spread():
    liquid   = make_liquid()
    illiquid = make_illiquid()
    results  = score_symbols([liquid, illiquid], tactic="credit_spread")
    liq_score   = next(r for r in results if r.symbol == "LIQUID").score
    illiq_score = next(r for r in results if r.symbol == "ILLIQUID").score
    assert liq_score > illiq_score, (
        f"credit_spread: LIQUID({liq_score:.4f}) should > ILLIQUID({illiq_score:.4f})"
    )


# ── Test 9: 決算近傍銘柄が除外される ────────────────────────────────────────

def test_earnings_exclusion():
    normal   = make_metrics("NORMAL", near_earnings=False)
    earnings = make_metrics("EARNINGS", near_earnings=True)
    results  = score_symbols([normal, earnings], earnings_exclude=True)
    excl = [r for r in results if r.excluded]
    active = [r for r in results if not r.excluded]
    assert len(excl) == 1
    assert excl[0].symbol == "EARNINGS"
    assert excl[0].exclude_reason == "near_earnings"
    assert len(active) == 1
    assert active[0].symbol == "NORMAL"


# ── Test 10: earnings_exclude=False で決算銘柄も含む ────────────────────────

def test_earnings_not_excluded_when_disabled():
    normal   = make_metrics("NORMAL", near_earnings=False)
    earnings = make_metrics("EARNINGS", near_earnings=True)
    results  = score_symbols([normal, earnings], earnings_exclude=False)
    excluded = [r for r in results if r.excluded]
    assert len(excluded) == 0
    assert len(results) == 2


# ── Test 11: select_symbols が top_n 件を返す ───────────────────────────────

def test_select_top_n():
    metrics = [make_metrics(f"SYM{i}", ivr=float(i * 10)) for i in range(1, 8)]
    result = select_symbols(metrics, tactic="credit_spread", top_n=3)
    assert len(result) == 3
    # すべてスコア順
    for i in range(len(result) - 1):
        assert result[i].score >= result[i + 1].score


# ── Test 12: select_symbols top_n=0 で全件返す ──────────────────────────────

def test_select_top_n_zero_returns_all():
    metrics = [make_metrics(f"SYM{i}") for i in range(5)]
    result = select_symbols(metrics, tactic="credit_spread", top_n=0)
    assert len(result) == 5


# ── Test 13: raw_scoresが0〜1の範囲 ─────────────────────────────────────────

def test_raw_scores_range():
    metrics = [
        make_metrics("A", ivr=100.0, vol_spike=10.0, gap=0.1, spread_pct=0.001),
        make_metrics("B", ivr=0.0,   vol_spike=0.1,  gap=0.0, spread_pct=0.1),
    ]
    results = score_symbols(metrics)
    for r in results:
        if not r.excluded:
            for key, val in r.raw_scores.items():
                assert 0.0 <= val <= 1.0, (
                    f"{r.symbol}.{key}={val:.4f} outside [0,1]"
                )


# ── Test 14: Noneデータでもクラッシュしない ─────────────────────────────────

def test_none_metrics_handled():
    m = SymbolMetrics(
        symbol="NONE_SYM",
        ivr=None,
        volume_spike_ratio=None,
        gap_abs_pct=None,
        bid_ask_spread_pct=None,
        vix_correlation=None,
        near_earnings=False,
        hist_gaps=[],
    )
    result = score_symbols([m])
    assert len(result) == 1
    assert 0.0 <= result[0].score <= 1.0


# ── Test 15: スコアが対称的 (全Noneは0.5付近) ───────────────────────────────

def test_all_none_metrics_neutral_score():
    m = SymbolMetrics(symbol="NEUTRAL")
    result = score_symbols([m])
    assert len(result) == 1
    # 全データNone時は各指標0.5→合計スコアも0.5付近になるはず
    assert 0.3 <= result[0].score <= 0.7, (
        f"Expected neutral score near 0.5, got {result[0].score:.4f}"
    )


# ── Test 16: _normalize_ivr がユニバース分布を正しく使う ────────────────────

def test_normalize_ivr_uses_universe():
    # 最高IVR → 1.0, 最低IVR → 0.0
    ivrs = [10.0, 30.0, 50.0, 70.0, 90.0]
    assert _normalize_ivr(90.0, ivrs) == pytest.approx(1.0, abs=1e-9)
    assert _normalize_ivr(10.0, ivrs) == pytest.approx(0.0, abs=1e-9)
    # 中間値
    mid = _normalize_ivr(50.0, ivrs)
    assert 0.4 < mid < 0.6


# ── Test 17: _normalize_liquidity が反転 (spread小ほど高スコア) ─────────────

def test_normalize_liquidity_inverted():
    spreads = [0.001, 0.005, 0.01, 0.02, 0.05]
    tight = _normalize_liquidity(0.001, spreads)
    wide  = _normalize_liquidity(0.05,  spreads)
    assert tight > wide, f"tight({tight:.4f}) should > wide({wide:.4f})"


# ── Test 18: _normalize_vix_corr が絶対値を使う ─────────────────────────────

def test_normalize_vix_corr_absolute():
    pos = _normalize_vix_corr(0.8)
    neg = _normalize_vix_corr(-0.8)
    assert pos == pytest.approx(neg, abs=1e-9)
    assert _normalize_vix_corr(None) == 0.5


# ── Test 19: 未知の戦術名がクラッシュせずデフォルトを使う ──────────────────

def test_unknown_tactic_fallback():
    metrics = [make_metrics("SPY"), make_metrics("QQQ")]
    result = score_symbols(metrics, tactic="nonexistent_tactic")
    assert len(result) == 2
    for r in result:
        assert 0.0 <= r.score <= 1.0


# ── Test 20: スコアの降順ソートが保証される ─────────────────────────────────

def test_descending_order():
    metrics = [
        make_metrics("A", ivr=80.0),
        make_metrics("B", ivr=20.0),
        make_metrics("C", ivr=50.0),
    ]
    results = score_symbols(metrics, tactic="credit_spread")
    scores = [r.score for r in results if not r.excluded]
    assert scores == sorted(scores, reverse=True), (
        f"Expected descending order, got {scores}"
    )


# ── Test 21: iron_condor のウェイトプロファイル確認 ──────────────────────────

def test_iron_condor_weights_exist():
    assert "iron_condor" in _TACTIC_WEIGHTS
    weights = _TACTIC_WEIGHTS["iron_condor"]
    # 流動性ウェイトは credit_spread より大きい
    assert weights["liquidity"] >= _TACTIC_WEIGHTS["credit_spread"]["liquidity"]


# ── Test 22: hist_gaps が空でもクラッシュしない ──────────────────────────────

def test_empty_hist_gaps():
    m = make_metrics("SPY", gap=0.02, hist_gaps=[])
    result = score_symbols([m])
    assert len(result) == 1
    assert not math.isnan(result[0].score)


# ── Test 23: 全銘柄near_earningsのときは空リスト返す ────────────────────────

def test_all_earnings_excluded():
    metrics = [
        make_metrics("A", near_earnings=True),
        make_metrics("B", near_earnings=True),
    ]
    result = select_symbols(metrics, earnings_exclude=True)
    # excludedは除外されてselectでは0件
    assert len(result) == 0


# ── Test 24: SymbolScore の repr が壊れない ──────────────────────────────────

def test_symbol_score_repr():
    m = make_metrics("SPY")
    ss = SymbolScore(symbol="SPY", score=0.75, raw_scores={}, metrics=m)
    r = repr(ss)
    assert "SPY" in r
    assert "0.75" in r
