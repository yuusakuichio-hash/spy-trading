"""tests/test_atlas_v3_strategy_selector.py — StrategySelector テスト（Sprint 1-B Phase B）

仕様: atlas_spec_v3_20260422.md B4

カバレッジ要件:
- PercentileSelector: phase × VIX_regime マッピング
- PercentileSelector: 未定義 phase は ValueError
- PercentileSelector: VIX 分類（low / medium / high）
- StrategySelector: gamma_scalp が IVR>=50 + VIX>=20 + VRP<VIX で選択される
- StrategySelector: cs_sell が 低VIX + directional で選択される
- StrategySelector: ic_sell が 中VIX + 高IVR で選択される
- StrategySelector: butterfly が 低IVR で選択される
- StrategySelector: 結果は TacticDecision リスト
- StrategySelector: confidence 降順ソート
- StrategySelector: delta_hedge は常時候補
- TacticDecision: frozen dataclass（不変）
"""
from __future__ import annotations

import pytest

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.core.strategy_selector import (
    ALL_TACTICS,
    TACTIC_TYPE_MAP,
    PercentileSelector,
    StrategySelector,
    TacticDecision,
)


# ---------------------------------------------------------------------------
# テスト用ヘルパー
# ---------------------------------------------------------------------------

def _env(
    vix: float,
    bias: str = "neutral",
    ivr_spy: float = 50.0,
    vrp: float = 10.0,
    term_ratio: float = 1.0,
) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        bias=bias,  # type: ignore[arg-type]
        ivr_by_symbol={"SPY": ivr_spy},
        vrp=vrp,
        term_ratio=term_ratio,
    )


# ---------------------------------------------------------------------------
# PercentileSelector テスト
# ---------------------------------------------------------------------------

class TestPercentileSelector:

    def test_phase1_low_vix_returns_conservative_percentile(self) -> None:
        """phase1 + VIX<18 → conservative (0.30)"""
        ps = PercentileSelector()
        pct = ps.select("ivr", "phase1", vix=15.0)
        assert pct == pytest.approx(0.30)

    def test_phase4_low_vix_returns_aggressive_percentile(self) -> None:
        """phase4 + VIX<18 → aggressive (0.70)"""
        ps = PercentileSelector()
        pct = ps.select("ivr", "phase4", vix=15.0)
        assert pct == pytest.approx(0.70)

    def test_phase1_high_vix_returns_most_conservative(self) -> None:
        """phase1 + VIX>=28 → most conservative (0.20)"""
        ps = PercentileSelector()
        pct = ps.select("ivr", "phase1", vix=30.0)
        assert pct == pytest.approx(0.20)

    def test_unknown_phase_raises_value_error(self) -> None:
        """未定義 phase は ValueError"""
        ps = PercentileSelector()
        with pytest.raises(ValueError, match="unknown phase"):
            ps.select("ivr", "phase99", vix=20.0)

    def test_vix_classify_low(self) -> None:
        """VIX < 18.0 → low"""
        assert PercentileSelector._classify_vix(17.9) == "low"

    def test_vix_classify_medium(self) -> None:
        """18.0 <= VIX < 28.0 → medium"""
        assert PercentileSelector._classify_vix(20.0) == "medium"
        assert PercentileSelector._classify_vix(27.9) == "medium"

    def test_vix_classify_high(self) -> None:
        """VIX >= 28.0 → high"""
        assert PercentileSelector._classify_vix(28.0) == "high"
        assert PercentileSelector._classify_vix(40.0) == "high"

    def test_percentile_is_between_0_and_1(self) -> None:
        """全フェーズ × 全VIX領域で percentile が 0-1 の範囲"""
        ps = PercentileSelector()
        for phase in ("phase1", "phase2", "phase3", "phase4"):
            for vix in (10.0, 22.0, 35.0):
                pct = ps.select("vix", phase, vix)
                assert 0.0 <= pct <= 1.0, f"phase={phase} vix={vix} pct={pct}"

    def test_higher_phase_means_higher_percentile(self) -> None:
        """フェーズが上がるほど percentile が高くなる（攻め）"""
        ps = PercentileSelector()
        vix = 15.0
        pcts = [ps.select("ivr", f"phase{i}", vix) for i in range(1, 5)]
        assert pcts == sorted(pcts), f"期待値昇順: {pcts}"


# ---------------------------------------------------------------------------
# StrategySelector テスト
# ---------------------------------------------------------------------------

class TestStrategySelector:

    def test_select_returns_list_of_tactic_decisions(self) -> None:
        """select() は TacticDecision のリストを返す"""
        ss = StrategySelector(phase="phase1")
        decisions = ss.select(_env(vix=16.0, ivr_spy=40.0), "SPY")
        assert isinstance(decisions, list)
        assert all(isinstance(d, TacticDecision) for d in decisions)

    def test_gamma_scalp_selected_in_gamma_env(self) -> None:
        """IVR>=50 / VIX>=20 / VRP<VIX → gamma_scalp が選択される"""
        ss = StrategySelector(phase="phase1")
        # vrp=5 < vix=25 → RV<IV 近似成立
        env = _env(vix=25.0, ivr_spy=55.0, vrp=5.0)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "gamma_scalp" in names

    def test_gamma_scalp_not_selected_when_vix_low(self) -> None:
        """VIX<20 の場合 gamma_scalp は選択されない"""
        ss = StrategySelector(phase="phase1")
        env = _env(vix=15.0, ivr_spy=60.0, vrp=5.0)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "gamma_scalp" not in names

    def test_cs_sell_selected_in_low_vix_directional(self) -> None:
        """低VIX + bull bias → cs_sell が選択される"""
        ss = StrategySelector(phase="phase1")
        env = _env(vix=14.0, bias="bull", ivr_spy=35.0)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "cs_sell" in names

    def test_cs_sell_not_selected_when_neutral(self) -> None:
        """bias=neutral の場合 cs_sell は選択されない"""
        ss = StrategySelector(phase="phase1")
        env = _env(vix=14.0, bias="neutral", ivr_spy=35.0)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "cs_sell" not in names

    def test_ic_sell_selected_in_medium_vix_high_ivr(self) -> None:
        """中VIX + IVR高 → ic_sell が選択される"""
        ss = StrategySelector(phase="phase1")
        # phase1 + medium VIX → ivr_threshold = 0.25 * 100 = 25
        # ivr=60 > 25 → is_high_iv=True
        env = _env(vix=22.0, ivr_spy=60.0)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "ic_sell" in names

    def test_butterfly_selected_when_low_ivr(self) -> None:
        """IVR < threshold → butterfly が選択される"""
        ss = StrategySelector(phase="phase1")
        # phase1 + low VIX → ivr_threshold = 0.30 * 100 = 30
        # ivr=15 < 30 → is_high_iv=False → butterfly 候補
        env = _env(vix=14.0, ivr_spy=15.0)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "butterfly" in names

    def test_delta_hedge_always_in_candidates(self) -> None:
        """delta_hedge は常時候補として含まれる"""
        ss = StrategySelector(phase="phase1")
        for vix in (12.0, 22.0, 35.0):
            decisions = ss.select(_env(vix=vix), "SPY")
            names = [d.tactic_name for d in decisions]
            assert "delta_hedge" in names, f"vix={vix} のとき delta_hedge がない"

    def test_decisions_sorted_by_confidence_descending(self) -> None:
        """decisions は confidence 降順でソートされている"""
        ss = StrategySelector(phase="phase1")
        decisions = ss.select(_env(vix=22.0, ivr_spy=60.0, bias="bull"), "SPY")
        confidences = [d.confidence for d in decisions]
        assert confidences == sorted(confidences, reverse=True), f"{confidences}"

    def test_each_decision_has_valid_tactic_name(self) -> None:
        """全 TacticDecision の tactic_name が ALL_TACTICS に含まれる"""
        ss = StrategySelector(phase="phase2")
        decisions = ss.select(_env(vix=20.0, ivr_spy=55.0, vrp=5.0, bias="bear"), "QQQ")
        for d in decisions:
            assert d.tactic_name in ALL_TACTICS, f"不明な tactic_name: {d.tactic_name}"

    def test_tactic_decision_is_frozen(self) -> None:
        """TacticDecision は frozen dataclass（書き換え不可）"""
        d = TacticDecision(
            tactic_name="cs_sell",
            symbol="SPY",
            confidence=0.70,
            reason="test",
        )
        with pytest.raises((AttributeError, TypeError)):
            d.tactic_name = "ic_sell"  # type: ignore[misc]

    def test_confidence_within_0_1(self) -> None:
        """全 TacticDecision の confidence が 0-1 の範囲"""
        ss = StrategySelector(phase="phase3")
        envs = [
            _env(vix=12.0, ivr_spy=20.0),
            _env(vix=22.0, ivr_spy=60.0),
            _env(vix=35.0, ivr_spy=80.0),
        ]
        for env in envs:
            for d in ss.select(env, "SPY"):
                assert 0.0 <= d.confidence <= 1.0, (
                    f"confidence={d.confidence} out of range for {d.tactic_name}"
                )

    def test_straddle_buy_selected_in_high_vix_neutral(self) -> None:
        """高VIX + neutral → straddle_buy が選択される"""
        ss = StrategySelector(phase="phase1")
        env = _env(vix=32.0, bias="neutral", ivr_spy=70.0)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "straddle_buy" in names

    def test_calendar_sell_selected_when_contango(self) -> None:
        """IVR高 + term_ratio>1.0 → calendar_sell が選択される"""
        ss = StrategySelector(phase="phase1")
        # phase1 + medium VIX → ivr_threshold=25 → ivr=60 > 25 → is_high_iv=True
        env = _env(vix=22.0, ivr_spy=60.0, term_ratio=1.2)
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "calendar_sell" in names

    def test_orb_1dte_selected_directional_non_high_vix(self) -> None:
        """方向性あり + non-high VIX → orb_1dte が選択される"""
        ss = StrategySelector(phase="phase1")
        env = _env(vix=20.0, bias="bear")
        decisions = ss.select(env, "SPY")
        names = [d.tactic_name for d in decisions]
        assert "orb_1dte" in names

    def test_reason_is_nonempty_string(self) -> None:
        """全 TacticDecision の reason が空でない文字列"""
        ss = StrategySelector(phase="phase2")
        decisions = ss.select(_env(vix=20.0, ivr_spy=55.0), "SPY")
        for d in decisions:
            assert isinstance(d.reason, str) and len(d.reason) > 0


# ---------------------------------------------------------------------------
# TACTIC_TYPE_MAP 整合テスト
# ---------------------------------------------------------------------------

def test_tactic_type_map_covers_all_tactics() -> None:
    """TACTIC_TYPE_MAP が ALL_TACTICS を全て網羅している"""
    for tactic in ALL_TACTICS:
        assert tactic in TACTIC_TYPE_MAP, f"{tactic} が TACTIC_TYPE_MAP にない"


def test_gamma_scalp_is_hybrid_in_type_map() -> None:
    """gamma_scalp は Type D (hybrid) — R2 修正確認"""
    assert TACTIC_TYPE_MAP["gamma_scalp"] == "hybrid"


def test_delta_hedge_is_portfolio_reactive() -> None:
    """delta_hedge は Type B (portfolio_reactive)"""
    assert TACTIC_TYPE_MAP["delta_hedge"] == "portfolio_reactive"


def test_orb_1dte_is_state_carrying() -> None:
    """orb_1dte は Type C (state_carrying)"""
    assert TACTIC_TYPE_MAP["orb_1dte"] == "state_carrying"
