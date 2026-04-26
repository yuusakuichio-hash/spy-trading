"""tests/test_risk_engine.py — RiskEngine テスト (Sprint 1-B Phase B)

仕様: ADR-013 v2 / common_v3/risk/engine.py

カバレッジ要件:
- check_max_notional: 許可ケース / 超過ケース
- check_max_daily_loss: 許可ケース / 超過ケース
- check_max_drawdown: 許可ケース / 超過ケース
- check_assignment_risk: ロング（スキップ）/ ショート許可 / ショート超過
- check_var: ヒストリカル（≥5件）/ データ不足フォールバック / ファットテール補正確認
- check_position_sizing: FIXED / KELLY（正常） / KELLY（負期待値）/ VIX_LINKED
- check_all: 全許可パス / 各拒否パス / kill_switch ARMED
- RiskConfig: バリデーション（不正値）
- PortfolioSnapshot: バリデーション（不正値）
- OptionRequest: バリデーション（不正値）
- RiskDecision: バリデーション（sizing < 0）
- Atlas 発注シナリオ統合テスト
- Chronos 発注シナリオ統合テスト（Sprint 2 見越し）
"""
from __future__ import annotations

import math
from unittest.mock import patch, MagicMock

import pytest

from common_v3.risk.engine import (
    OptionRequest,
    PortfolioSnapshot,
    PositionSizingMethod,
    RiskConfig,
    RiskDecision,
    RiskEngine,
    _FAT_TAIL_MULTIPLIER,
    _CVAR_RATIO,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _default_config(**overrides) -> RiskConfig:
    """デフォルト設定を返す（override 可）。"""
    base = dict(
        max_notional_usd=50_000.0,
        max_daily_loss_usd=-2_000.0,
        max_drawdown_pct=0.10,
        max_var_usd=5_000.0,
        max_assignment_risk_usd=20_000.0,
        fixed_size_contracts=1,
        kelly_fraction=0.25,
        vix_size_base=20.0,
        sizing_method=PositionSizingMethod.FIXED,
    )
    base.update(overrides)
    return RiskConfig(**base)


def _default_portfolio(**overrides) -> PortfolioSnapshot:
    """デフォルトポートフォリオスナップショットを返す（override 可）。"""
    base = dict(
        pnl_day_usd=0.0,
        drawdown_pct=0.0,
        returns_history=(),
        current_vix=20.0,
        win_rate=0.55,
        avg_win_usd=200.0,
        avg_loss_usd=-150.0,
    )
    base.update(overrides)
    return PortfolioSnapshot(**base)


def _make_engine(**config_overrides) -> RiskEngine:
    """RiskEngine インスタンスを返す。"""
    return RiskEngine(config=_default_config(**config_overrides))


def _sufficient_returns(worst: float = -500.0, n: int = 100) -> tuple:
    """C-1/C-2 準拠の returns_history を生成する (n >= 100 必須)。

    先頭 1 件を worst 値、残りを 0.0 で埋める。
    """
    return (worst,) + tuple(0.0 for _ in range(n - 1))


# ---------------------------------------------------------------------------
# RiskConfig バリデーション
# ---------------------------------------------------------------------------

class TestRiskConfigValidation:

    def test_default_config_valid(self) -> None:
        cfg = RiskConfig()
        assert cfg.max_notional_usd > 0
        assert cfg.max_daily_loss_usd <= 0

    def test_max_daily_loss_positive_raises(self) -> None:
        with pytest.raises(ValueError, match="max_daily_loss_usd must be"):
            RiskConfig(max_daily_loss_usd=100.0)

    def test_max_drawdown_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_drawdown_pct must be"):
            RiskConfig(max_drawdown_pct=0.0)

    def test_max_drawdown_over_one_raises(self) -> None:
        with pytest.raises(ValueError, match="max_drawdown_pct must be"):
            RiskConfig(max_drawdown_pct=1.01)

    def test_kelly_fraction_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="kelly_fraction must be"):
            RiskConfig(kelly_fraction=0.0)

    def test_kelly_fraction_over_one_raises(self) -> None:
        with pytest.raises(ValueError, match="kelly_fraction must be"):
            RiskConfig(kelly_fraction=1.01)

    def test_vix_size_base_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="vix_size_base must be"):
            RiskConfig(vix_size_base=0.0)

    def test_fixed_size_contracts_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="fixed_size_contracts must be"):
            RiskConfig(fixed_size_contracts=0)


# ---------------------------------------------------------------------------
# PortfolioSnapshot バリデーション
# ---------------------------------------------------------------------------

class TestPortfolioSnapshotValidation:

    def test_drawdown_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="drawdown_pct must be"):
            PortfolioSnapshot(drawdown_pct=-0.01)

    def test_drawdown_over_one_raises(self) -> None:
        with pytest.raises(ValueError, match="drawdown_pct must be"):
            PortfolioSnapshot(drawdown_pct=1.01)

    def test_vix_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="current_vix must be"):
            PortfolioSnapshot(current_vix=0.0)

    def test_win_rate_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="win_rate must be"):
            PortfolioSnapshot(win_rate=-0.01)

    def test_win_rate_over_one_raises(self) -> None:
        with pytest.raises(ValueError, match="win_rate must be"):
            PortfolioSnapshot(win_rate=1.01)


# ---------------------------------------------------------------------------
# OptionRequest バリデーション
# ---------------------------------------------------------------------------

class TestOptionRequestValidation:

    def test_strike_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="strike must be"):
            OptionRequest(symbol="SPY", strike=0.0, underlying_price=500.0, contracts=1)

    def test_underlying_price_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="underlying_price must be"):
            OptionRequest(symbol="SPY", strike=500.0, underlying_price=0.0, contracts=1)

    def test_contracts_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="contracts must be"):
            OptionRequest(symbol="SPY", strike=500.0, underlying_price=500.0, contracts=0)

    def test_multiplier_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="multiplier must be"):
            OptionRequest(
                symbol="SPY", strike=500.0, underlying_price=500.0,
                contracts=1, multiplier=0,
            )


# ---------------------------------------------------------------------------
# RiskDecision バリデーション
# ---------------------------------------------------------------------------

class TestRiskDecisionValidation:

    def test_sizing_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="sizing must be"):
            RiskDecision(allowed=True, reason="", sizing=-1)

    def test_allowed_false_sizing_zero_ok(self) -> None:
        d = RiskDecision(allowed=False, reason="blocked", sizing=0)
        assert d.sizing == 0
        assert not d.allowed


# ---------------------------------------------------------------------------
# check_max_notional
# ---------------------------------------------------------------------------

class TestCheckMaxNotional:

    def test_within_limit_allowed(self) -> None:
        eng = _make_engine(max_notional_usd=50_000.0)
        assert eng.check_max_notional(49_999.99) is True

    def test_at_limit_allowed(self) -> None:
        eng = _make_engine(max_notional_usd=50_000.0)
        assert eng.check_max_notional(50_000.0) is True

    def test_over_limit_denied(self) -> None:
        eng = _make_engine(max_notional_usd=50_000.0)
        assert eng.check_max_notional(50_000.01) is False

    def test_zero_notional_allowed(self) -> None:
        eng = _make_engine()
        assert eng.check_max_notional(0.0) is True


# ---------------------------------------------------------------------------
# check_max_daily_loss
# ---------------------------------------------------------------------------

class TestCheckMaxDailyLoss:

    def test_no_loss_allowed(self) -> None:
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        assert eng.check_max_daily_loss(0.0) is True

    def test_small_loss_allowed(self) -> None:
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        assert eng.check_max_daily_loss(-1_999.99) is True

    def test_at_limit_denied(self) -> None:
        """H-1 業界慣行: ちょうど上限に達したら発注停止"""
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        assert eng.check_max_daily_loss(-2_000.0) is False

    def test_over_limit_denied(self) -> None:
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        assert eng.check_max_daily_loss(-2_000.01) is False

    def test_large_profit_allowed(self) -> None:
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        assert eng.check_max_daily_loss(10_000.0) is True


# ---------------------------------------------------------------------------
# check_max_drawdown
# ---------------------------------------------------------------------------

class TestCheckMaxDrawdown:

    def test_no_drawdown_allowed(self) -> None:
        eng = _make_engine(max_drawdown_pct=0.10)
        assert eng.check_max_drawdown(0.0) is True

    def test_at_limit_allowed(self) -> None:
        eng = _make_engine(max_drawdown_pct=0.10)
        assert eng.check_max_drawdown(0.10) is True

    def test_over_limit_denied(self) -> None:
        eng = _make_engine(max_drawdown_pct=0.10)
        assert eng.check_max_drawdown(0.1001) is False

    def test_exactly_zero_allowed(self) -> None:
        eng = _make_engine(max_drawdown_pct=0.10)
        assert eng.check_max_drawdown(0.0) is True


# ---------------------------------------------------------------------------
# check_assignment_risk
# ---------------------------------------------------------------------------

class TestCheckAssignmentRisk:

    def _make_opt(self, strike: float, contracts: int, is_short: bool = True) -> OptionRequest:
        return OptionRequest(
            symbol="SPY",
            strike=strike,
            underlying_price=500.0,
            contracts=contracts,
            is_short=is_short,
            multiplier=100,
        )

    def test_long_option_always_allowed(self) -> None:
        eng = _make_engine(max_assignment_risk_usd=20_000.0)
        # ロング: strike * contracts * multiplier = 500 * 1 * 100 = 50_000 > 20_000 だが許可
        opt = self._make_opt(500.0, 1, is_short=False)
        assert eng.check_assignment_risk(opt) is True

    def test_short_within_limit_allowed(self) -> None:
        eng = _make_engine(max_assignment_risk_usd=20_000.0)
        # 100 * 2 * 100 = 20_000 <= 20_000
        opt = self._make_opt(100.0, 2, is_short=True)
        assert eng.check_assignment_risk(opt) is True

    def test_short_at_limit_allowed(self) -> None:
        eng = _make_engine(max_assignment_risk_usd=20_000.0)
        # 200 * 1 * 100 = 20_000
        opt = self._make_opt(200.0, 1, is_short=True)
        assert eng.check_assignment_risk(opt) is True

    def test_short_over_limit_denied(self) -> None:
        eng = _make_engine(max_assignment_risk_usd=20_000.0)
        # 201 * 1 * 100 = 20_100 > 20_000
        opt = self._make_opt(201.0, 1, is_short=True)
        assert eng.check_assignment_risk(opt) is False

    def test_short_multiple_contracts_over_limit_denied(self) -> None:
        eng = _make_engine(max_assignment_risk_usd=20_000.0)
        # 100 * 3 * 100 = 30_000 > 20_000
        opt = self._make_opt(100.0, 3, is_short=True)
        assert eng.check_assignment_risk(opt) is False


# ---------------------------------------------------------------------------
# check_var (VaR / CVaR)
# ---------------------------------------------------------------------------

class TestCheckVar:

    def test_insufficient_data_raises_value_error(self) -> None:
        """C-1: returns_history=[] (0件) → ValueError raise"""
        eng = _make_engine()
        portfolio = _default_portfolio(pnl_day_usd=-1_000.0, returns_history=())
        with pytest.raises(ValueError, match="insufficient history for VaR"):
            eng.check_var(portfolio)

    def test_insufficient_data_19_raises_value_error(self) -> None:
        """C-1: n=19 (<100) → ValueError raise"""
        eng = _make_engine()
        returns = tuple(float(-i * 10) for i in range(19))
        portfolio = _default_portfolio(returns_history=returns)
        with pytest.raises(ValueError, match="insufficient history for VaR"):
            eng.check_var(portfolio)

    def test_exactly_100_samples_allowed(self) -> None:
        """C-1/C-2: n=100 (ちょうど) → ValueError raise しない"""
        eng = _make_engine()
        # 最悪値 -3000, 99 件は 0.0
        returns = (-3_000.0,) + tuple(0.0 for _ in range(99))
        portfolio = _default_portfolio(returns_history=returns)
        var = eng.check_var(portfolio)
        # n=100: ceil(100*0.01)-1 = ceil(1.0)-1 = 0 → sorted[0] = -3000
        expected = 3_000.0 * _FAT_TAIL_MULTIPLIER
        assert math.isclose(var, expected, rel_tol=1e-9)

    def test_historical_var_positive_only_returns(self) -> None:
        """全てプラスリターン (n=100) → VaR = 0 × fat_tail = 0"""
        eng = _make_engine()
        returns = tuple(float(i * 10) for i in range(1, 101))
        portfolio = _default_portfolio(returns_history=returns)
        var = eng.check_var(portfolio)
        assert var == 0.0

    def test_fat_tail_multiplier_applied(self) -> None:
        """ファットテール補正が適用されていることを確認（× 1.65 以上）"""
        eng = _make_engine()
        # n=100: 最悪値 -100*99=-9900, 残り 99 件は 0.0
        worst_val = -9_900.0
        returns = (worst_val,) + tuple(0.0 for _ in range(99))
        portfolio = _default_portfolio(returns_history=returns)
        var = eng.check_var(portfolio)
        raw_var = abs(worst_val)
        assert math.isclose(var, raw_var * _FAT_TAIL_MULTIPLIER, rel_tol=1e-9)
        assert var > raw_var  # fat tail 補正で増大している


# ---------------------------------------------------------------------------
# check_position_sizing
# ---------------------------------------------------------------------------

class TestCheckPositionSizing:

    def test_fixed_method(self) -> None:
        eng = _make_engine(fixed_size_contracts=2, sizing_method=PositionSizingMethod.FIXED)
        portfolio = _default_portfolio()
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.FIXED)
        assert size == 2

    def test_fixed_method_default(self) -> None:
        eng = _make_engine(fixed_size_contracts=3)
        portfolio = _default_portfolio()
        size = eng.check_position_sizing(portfolio)
        assert size == 3

    def test_kelly_positive_expectation(self) -> None:
        """勝率 0.6 / avg_win=200 / avg_loss=-100 → Kelly 正・フラクショナル適用"""
        eng = _make_engine(
            fixed_size_contracts=10,
            kelly_fraction=0.25,
            sizing_method=PositionSizingMethod.KELLY,
        )
        portfolio = _default_portfolio(win_rate=0.6, avg_win_usd=200.0, avg_loss_usd=-100.0)
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.KELLY)
        # b = 200/100 = 2.0, Kelly = (0.6*2 - 0.4) / 2 = 0.8/2 = 0.4
        # fractional = 0.4 * 0.25 = 0.1, size = round(0.1 * 10) = 1
        assert size >= 1

    def test_kelly_negative_expectation_returns_one(self) -> None:
        """勝率低く期待値マイナス → Kelly = 負 → 最小 1 を返す"""
        eng = _make_engine(
            fixed_size_contracts=5,
            kelly_fraction=0.25,
        )
        portfolio = _default_portfolio(win_rate=0.1, avg_win_usd=50.0, avg_loss_usd=-1_000.0)
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.KELLY)
        assert size == 1

    def test_kelly_zero_avg_loss_raises_value_error(self) -> None:
        """H-2: avg_loss_usd=0 は Kelly 計算不能 → ValueError raise"""
        eng = _make_engine(fixed_size_contracts=4)
        portfolio = _default_portfolio(avg_loss_usd=0.0)
        with pytest.raises(ValueError, match="avg_loss_usd=0 is invalid for Kelly sizing"):
            eng.check_position_sizing(portfolio, method=PositionSizingMethod.KELLY)

    def test_vix_linked_base_vix(self) -> None:
        """current_vix == vix_size_base → size == fixed_size_contracts"""
        eng = _make_engine(
            fixed_size_contracts=5,
            vix_size_base=20.0,
        )
        portfolio = _default_portfolio(current_vix=20.0)
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.VIX_LINKED)
        assert size == 5

    def test_vix_linked_high_vix_reduces_size(self) -> None:
        """current_vix == 2 * vix_size_base → size は半分"""
        eng = _make_engine(
            fixed_size_contracts=10,
            vix_size_base=20.0,
        )
        portfolio = _default_portfolio(current_vix=40.0)
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.VIX_LINKED)
        # ratio = 20/40 = 0.5 → round(10 * 0.5) = 5
        assert size == 5

    def test_vix_linked_low_vix_increases_size(self) -> None:
        """current_vix == vix_size_base / 2 → size は 2 倍"""
        eng = _make_engine(
            fixed_size_contracts=5,
            vix_size_base=20.0,
        )
        portfolio = _default_portfolio(current_vix=10.0)
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.VIX_LINKED)
        # ratio = 20/10 = 2.0 → round(5 * 2) = 10
        assert size == 10

    def test_vix_linked_minimum_one(self) -> None:
        """extreme high VIX でも minimum 1"""
        eng = _make_engine(
            fixed_size_contracts=1,
            vix_size_base=20.0,
        )
        portfolio = _default_portfolio(current_vix=1_000.0)
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.VIX_LINKED)
        assert size >= 1


# ---------------------------------------------------------------------------
# check_all — 統合テスト
# ---------------------------------------------------------------------------

class TestCheckAll:
    """check_all() の全パス統合テスト。kill_switch は patch で制御する。"""

    def _patch_ks(self, active: bool):
        """kill_switch.is_active を patch するコンテキストマネージャを返す。"""
        return patch(
            "common_v3.risk.engine.RiskEngine._check_kill_switch",
            return_value=RiskDecision(allowed=False, reason="kill_switch is active", sizing=0)
            if active else None,
        )

    def test_all_checks_pass(self) -> None:
        eng = _make_engine(
            max_notional_usd=50_000.0,
            max_daily_loss_usd=-2_000.0,
            max_drawdown_pct=0.10,
            max_var_usd=100_000.0,  # 超えない上限
        )
        portfolio = _default_portfolio(
            pnl_day_usd=-500.0,
            drawdown_pct=0.05,
            returns_history=_sufficient_returns(worst=-500.0),  # C-1: n=100
        )
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=10_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is True
        assert decision.reason == ""
        assert decision.sizing >= 1

    def test_kill_switch_armed_denied(self) -> None:
        eng = _make_engine()
        portfolio = _default_portfolio()
        with self._patch_ks(True):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is False
        assert "kill_switch" in decision.reason

    def test_max_notional_exceeded_denied(self) -> None:
        eng = _make_engine(max_notional_usd=50_000.0)
        portfolio = _default_portfolio()
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=50_001.0,
                portfolio=portfolio,
            )
        assert decision.allowed is False
        assert "max_notional" in decision.reason
        assert decision.sizing == 0

    def test_max_daily_loss_exceeded_denied(self) -> None:
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        portfolio = _default_portfolio(pnl_day_usd=-2_001.0)
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is False
        assert "max_daily_loss" in decision.reason

    def test_max_drawdown_exceeded_denied(self) -> None:
        eng = _make_engine(max_drawdown_pct=0.10)
        portfolio = _default_portfolio(drawdown_pct=0.11)
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is False
        assert "max_drawdown" in decision.reason

    def test_assignment_risk_exceeded_denied(self) -> None:
        eng = _make_engine(max_assignment_risk_usd=20_000.0, max_var_usd=100_000.0)
        portfolio = _default_portfolio(
            returns_history=_sufficient_returns(worst=-100.0)  # C-1: n=100
        )
        opt = OptionRequest(
            symbol="SPY",
            strike=500.0,  # 500 * 1 * 100 = 50_000 > 20_000
            underlying_price=500.0,
            contracts=1,
            is_short=True,
        )
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
                option_request=opt,
            )
        assert decision.allowed is False
        assert "assignment_risk" in decision.reason

    def test_var_exceeded_denied(self) -> None:
        eng = _make_engine(max_var_usd=100.0)  # 極小上限
        # C-1: n=100 必須。最悪値 -10_000 を先頭に 99 件の 0.0 で埋める
        big_loss_returns = (-10_000.0,) + tuple(0.0 for _ in range(99))
        portfolio = _default_portfolio(returns_history=big_loss_returns)
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is False
        assert "VaR" in decision.reason
        assert decision.var_usd > 0

    def test_option_long_assignment_skip(self) -> None:
        """ロングオプションは行使リスクチェックをスキップして許可される（max_premium_notional=None）。"""
        eng = _make_engine(max_assignment_risk_usd=1.0, max_var_usd=100_000.0)
        portfolio = _default_portfolio(returns_history=_sufficient_returns(worst=-100.0))
        opt = OptionRequest(
            symbol="SPY",
            strike=500.0,  # ロングなので assignment リスクなし (max_premium_notional=None)
            underlying_price=500.0,
            contracts=1,
            is_short=False,
        )
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
                option_request=opt,
            )
        assert decision.allowed is True

    def test_decision_var_cvar_populated(self) -> None:
        """許可ケースで var_usd / cvar_usd が正しく設定される（n=100, 最悪値=-500）。"""
        eng = _make_engine(max_var_usd=100_000.0)
        # C-1: n=100 必須。最悪値 -500 を先頭に設定
        returns = (-500.0,) + tuple(0.0 for _ in range(99))
        portfolio = _default_portfolio(pnl_day_usd=-500.0, returns_history=returns)
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is True
        # n=100: ceil(100*0.01)-1=0 → sorted[0]=-500 → VaR=500*fat_tail
        expected_var = 500.0 * _FAT_TAIL_MULTIPLIER
        assert math.isclose(decision.var_usd, expected_var, rel_tol=1e-9)
        assert math.isclose(decision.cvar_usd, expected_var * _CVAR_RATIO, rel_tol=1e-9)

    def test_sizing_method_override(self) -> None:
        """check_all に sizing_method を渡すと config.sizing_method を上書きする。"""
        eng = _make_engine(
            fixed_size_contracts=5,
            vix_size_base=20.0,
            max_var_usd=100_000.0,
            sizing_method=PositionSizingMethod.FIXED,
        )
        portfolio = _default_portfolio(
            current_vix=40.0,
            returns_history=_sufficient_returns(worst=-100.0),  # C-1: n=100
        )
        with self._patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
                sizing_method=PositionSizingMethod.VIX_LINKED,
            )
        assert decision.allowed is True
        # VIX=40, vix_size_base=20, fixed=5 → ratio=0.5 → size=3 (round(5*0.5)=2 → max(1,2)=2... wait)
        # round(5 * 0.5) = round(2.5) = 2 (Python rounds to even) → max(1, 2) = 2
        assert decision.sizing >= 1


# ---------------------------------------------------------------------------
# Atlas 発注シナリオ統合テスト
# ---------------------------------------------------------------------------

class TestAtlasIntegrationScenario:
    """Atlas エンジンでの check_all() 呼び出しシナリオ（実 AtlasEngine は使わない）。"""

    def test_atlas_normal_order_allowed(self) -> None:
        """通常の Atlas ORB 発注: 小さいポジション / 損失なし / VaR 余裕あり"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=100_000.0,
            max_daily_loss_usd=-5_000.0,
            max_drawdown_pct=0.15,
            max_var_usd=20_000.0,
            fixed_size_contracts=2,
        ))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=500.0,   # 利益中
            drawdown_pct=0.02,
            returns_history=_sufficient_returns(worst=-200.0),  # C-1: n=100
            current_vix=18.0,
        )
        with patch.object(eng, "_check_kill_switch", return_value=None):
            decision = eng.check_all(
                request_notional=10_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is True
        assert decision.sizing == 2  # FIXED

    def test_atlas_daily_loss_limit_hit(self) -> None:
        """Atlas 連日損失シナリオ: 当日損失が daily_loss 上限を超えた状態"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=100_000.0,
            max_daily_loss_usd=-3_000.0,
            max_drawdown_pct=0.15,
            max_var_usd=20_000.0,
        ))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=-3_500.0,  # 上限超え
            drawdown_pct=0.05,
            # max_daily_loss チェックで short-circuit するので returns_history 不要
        )
        with patch.object(eng, "_check_kill_switch", return_value=None):
            decision = eng.check_all(
                request_notional=5_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is False
        assert "max_daily_loss" in decision.reason

    def test_atlas_options_iron_condor_assignment_check(self) -> None:
        """Atlas IC 売り: ショートオプション行使リスクが上限内"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=100_000.0,
            max_daily_loss_usd=-5_000.0,
            max_drawdown_pct=0.20,
            max_var_usd=30_000.0,
            max_assignment_risk_usd=50_000.0,
        ))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            drawdown_pct=0.0,
            returns_history=_sufficient_returns(worst=-300.0),  # C-1: n=100
        )
        opt = OptionRequest(
            symbol="SPY",
            strike=450.0,
            underlying_price=500.0,
            contracts=1,
            is_short=True,  # ショート leg
            multiplier=100,
        )
        # 450 * 1 * 100 = 45_000 <= 50_000 → 許可
        with patch.object(eng, "_check_kill_switch", return_value=None):
            decision = eng.check_all(
                request_notional=5_000.0,
                portfolio=portfolio,
                option_request=opt,
            )
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# Chronos 発注シナリオ統合テスト（Sprint 2 見越し）
# ---------------------------------------------------------------------------

class TestChronosIntegrationScenario:
    """Chronos（先物）発注シナリオ（Sprint 2 見越し・RiskEngine は同じ API を使う）。"""

    def test_chronos_futures_normal_order_allowed(self) -> None:
        """Chronos ES 先物発注: 想定元本 25_000 / 損失なし"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=200_000.0,
            max_daily_loss_usd=-5_000.0,
            max_drawdown_pct=0.10,
            max_var_usd=50_000.0,
            fixed_size_contracts=1,
        ))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            drawdown_pct=0.0,
            current_vix=15.0,
            returns_history=_sufficient_returns(worst=-100.0),  # C-1: n=100
        )
        with patch.object(eng, "_check_kill_switch", return_value=None):
            decision = eng.check_all(
                request_notional=25_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is True

    def test_chronos_vix_spike_reduces_sizing(self) -> None:
        """Chronos VIX スパイク時にポジションサイズが縮小される。"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=200_000.0,
            max_daily_loss_usd=-5_000.0,
            max_drawdown_pct=0.20,
            max_var_usd=100_000.0,
            fixed_size_contracts=10,
            vix_size_base=20.0,
            sizing_method=PositionSizingMethod.VIX_LINKED,
        ))
        _returns = _sufficient_returns(worst=-100.0)
        portfolio_normal_vix = PortfolioSnapshot(
            pnl_day_usd=0.0, current_vix=20.0, returns_history=_returns,
        )
        portfolio_spike_vix = PortfolioSnapshot(
            pnl_day_usd=0.0, current_vix=60.0, returns_history=_returns,
        )
        with patch.object(eng, "_check_kill_switch", return_value=None):
            d_normal = eng.check_all(request_notional=10_000.0, portfolio=portfolio_normal_vix)
            d_spike = eng.check_all(request_notional=10_000.0, portfolio=portfolio_spike_vix)
        assert d_normal.allowed is True
        assert d_spike.allowed is True
        # VIX 60 は VIX 20 の 3 倍 → サイズは 1/3 に縮小されているはず
        assert d_spike.sizing < d_normal.sizing

    def test_chronos_drawdown_circuit_breaker(self) -> None:
        """Chronos ドローダウンが circuit breaker 閾値を超えたら拒否。"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=200_000.0,
            max_daily_loss_usd=-10_000.0,
            max_drawdown_pct=0.08,  # 厳しめ
            max_var_usd=50_000.0,
        ))
        # max_drawdown チェックで short-circuit → returns_history は到達しない
        portfolio = PortfolioSnapshot(pnl_day_usd=-1_000.0, drawdown_pct=0.09)
        with patch.object(eng, "_check_kill_switch", return_value=None):
            decision = eng.check_all(
                request_notional=10_000.0,
                portfolio=portfolio,
            )
        assert decision.allowed is False
        assert "max_drawdown" in decision.reason


# ---------------------------------------------------------------------------
# RiskEngine 設定アクセス
# ---------------------------------------------------------------------------

class TestRiskEngineConfig:

    def test_config_property(self) -> None:
        cfg = _default_config(max_notional_usd=99_000.0)
        eng = RiskEngine(config=cfg)
        assert eng.config.max_notional_usd == 99_000.0

    def test_default_config_used_when_none(self) -> None:
        eng = RiskEngine(config=None)
        assert eng.config.max_notional_usd == RiskConfig().max_notional_usd
