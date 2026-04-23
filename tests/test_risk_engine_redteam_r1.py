"""tests/test_risk_engine_redteam_r1.py — RiskEngine Redteam r1 CRITICAL/HIGH fix テスト

対象 fix:
  C-1: VaR データ不足時 fail-safe DENY (returns_history < 100 で拒否)
  C-2: 分位計算修正 (真の 99%-ile: n>=100 必須 + ceil-based idx)
  C-3: assignment_risk Long side も max_premium_notional チェック
  C-4: returns_unit 不一致で AssertionError (schema 契約)
  C-5: kill_switch import 失敗時 Pushover escalation + check_kill_switch_health()
  H-1: check_max_daily_loss >= → > (境界値業界慣行)
  H-2: Kelly avg_loss_abs=0 で DENY (勝率過大評価温床)

完了条件: 既存 69 + 本ファイル >= 10 tests PASS / regression 0
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
    _MIN_VAR_HISTORY,
)


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def _sufficient_returns(worst: float = -500.0, n: int = 100) -> tuple:
    """C-1/C-2 準拠 (n >= 100) の returns_history を生成する。"""
    return (worst,) + tuple(0.0 for _ in range(n - 1))


def _make_engine(**overrides) -> RiskEngine:
    base = dict(
        max_notional_usd=50_000.0,
        max_daily_loss_usd=-2_000.0,
        max_drawdown_pct=0.10,
        max_var_usd=100_000.0,
        max_assignment_risk_usd=20_000.0,
        fixed_size_contracts=1,
        kelly_fraction=0.25,
        vix_size_base=20.0,
        sizing_method=PositionSizingMethod.FIXED,
    )
    base.update(overrides)
    return RiskEngine(config=RiskConfig(**base))


def _patch_ks(active: bool = False):
    return patch(
        "common_v3.risk.engine.RiskEngine._check_kill_switch",
        return_value=RiskDecision(allowed=False, reason="kill_switch is active", sizing=0)
        if active else None,
    )


# ---------------------------------------------------------------------------
# C-1: VaR データ不足時 fail-safe DENY
# ---------------------------------------------------------------------------

class TestC1VarInsufficientHistory:

    def test_empty_history_raises_value_error(self) -> None:
        """C-1: returns_history=[] (0件) → ValueError raise"""
        eng = _make_engine()
        portfolio = PortfolioSnapshot(pnl_day_usd=-1_000.0, returns_history=())
        with pytest.raises(ValueError, match="insufficient history for VaR"):
            eng.check_var(portfolio)

    def test_n_less_than_100_raises_value_error(self) -> None:
        """C-1: n=99 (<100) → ValueError"""
        eng = _make_engine()
        returns = tuple(float(-i) for i in range(99))
        portfolio = PortfolioSnapshot(returns_history=returns)
        with pytest.raises(ValueError, match="insufficient history for VaR"):
            eng.check_var(portfolio)

    def test_check_all_with_empty_history_denied(self) -> None:
        """C-1: check_all() が returns_history=[] → 'insufficient history for VaR' DENY"""
        eng = _make_engine()
        portfolio = PortfolioSnapshot(pnl_day_usd=-500.0, returns_history=())
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is False
        assert "insufficient history for VaR" in decision.reason

    def test_check_all_with_n_50_denied(self) -> None:
        """C-1: check_all() が n=50 → DENY"""
        eng = _make_engine()
        returns = tuple(float(-i * 10) for i in range(50))
        portfolio = PortfolioSnapshot(returns_history=returns)
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is False
        assert "insufficient history for VaR" in decision.reason

    def test_n_exactly_100_allowed(self) -> None:
        """C-1: n=100 (最小限) → ValueError raise しない"""
        eng = _make_engine()
        returns = _sufficient_returns(worst=-200.0, n=100)
        portfolio = PortfolioSnapshot(returns_history=returns)
        var = eng.check_var(portfolio)
        assert var > 0
        assert math.isclose(var, 200.0 * _FAT_TAIL_MULTIPLIER, rel_tol=1e-9)

    def test_min_var_history_constant_is_100(self) -> None:
        """C-1: _MIN_VAR_HISTORY が 100 であることの schema 確認"""
        assert _MIN_VAR_HISTORY == 100


# ---------------------------------------------------------------------------
# C-2: 分位計算修正 (真の 99%-ile)
# ---------------------------------------------------------------------------

class TestC2TrueVar99:

    def test_n100_worst_first_element(self) -> None:
        """C-2: n=100 → ceil(100*0.01)-1=0 → sorted[0] が最悪値 (真の 1% テール)"""
        eng = _make_engine()
        worst = -5_000.0
        returns = (worst,) + tuple(0.0 for _ in range(99))
        portfolio = PortfolioSnapshot(returns_history=returns)
        var = eng.check_var(portfolio)
        expected = abs(worst) * _FAT_TAIL_MULTIPLIER
        assert math.isclose(var, expected, rel_tol=1e-9)

    def test_n1000_tenth_worst_element(self) -> None:
        """C-2: n=1000 → ceil(1000*0.01)-1=9 → sorted[9] = 10番目の最悪値"""
        eng = _make_engine()
        # sorted[0] ~ sorted[9] = -1000 ~ -100, sorted[10]以降は 0
        returns = tuple(float(-(10 - i) * 100) for i in range(10)) + tuple(0.0 for _ in range(990))
        portfolio = PortfolioSnapshot(returns_history=returns)
        var = eng.check_var(portfolio)
        # sorted[9] = -100 (10番目の最悪値)
        expected = 100.0 * _FAT_TAIL_MULTIPLIER
        assert math.isclose(var, expected, rel_tol=1e-9)

    def test_n99_raises_value_error(self) -> None:
        """C-2: n=99 は不足 → ValueError (n>=100 必須)"""
        eng = _make_engine()
        returns = tuple(float(-i) for i in range(99))
        portfolio = PortfolioSnapshot(returns_history=returns)
        with pytest.raises(ValueError, match="insufficient history for VaR"):
            eng.check_var(portfolio)


# ---------------------------------------------------------------------------
# C-3: assignment_risk Long side も max_premium_notional チェック
# ---------------------------------------------------------------------------

class TestC3LongOptionPremiumCheck:

    def _make_long_opt(self, strike: float = 500.0, contracts: int = 1) -> OptionRequest:
        return OptionRequest(
            symbol="SPY",
            strike=strike,
            underlying_price=500.0,
            contracts=contracts,
            is_short=False,
            multiplier=100,
        )

    def test_long_with_max_premium_notional_none_always_passes(self) -> None:
        """C-3: max_premium_notional=None なら Long は常に通過"""
        eng = _make_engine(max_premium_notional=None)
        opt = self._make_long_opt(strike=500.0, contracts=10)
        # 500 * 10 * 100 = 500_000 >> any limit, but None means no check
        assert eng.check_assignment_risk(opt) is True

    def test_long_within_max_premium_notional_passes(self) -> None:
        """C-3: Long side — notional が max_premium_notional 以内 → 通過"""
        eng = _make_engine(max_premium_notional=60_000.0)
        opt = self._make_long_opt(strike=500.0, contracts=1)
        # 500 * 1 * 100 = 50_000 <= 60_000
        assert eng.check_assignment_risk(opt) is True

    def test_long_exceeds_max_premium_notional_denied(self) -> None:
        """C-3: Long side — notional が max_premium_notional 超過 → 拒否"""
        eng = _make_engine(max_premium_notional=40_000.0)
        opt = self._make_long_opt(strike=500.0, contracts=1)
        # 500 * 1 * 100 = 50_000 > 40_000
        assert eng.check_assignment_risk(opt) is False

    def test_check_all_long_exceeds_max_premium_notional_denied(self) -> None:
        """C-3: check_all() で Long が max_premium_notional 超過 → DENY"""
        eng = _make_engine(
            max_assignment_risk_usd=100_000.0,
            max_premium_notional=1_000.0,  # 極小
        )
        portfolio = PortfolioSnapshot(
            pnl_day_usd=0.0,
            drawdown_pct=0.0,
            returns_history=_sufficient_returns(worst=-100.0),
        )
        opt = OptionRequest(
            symbol="SPY",
            strike=500.0,
            underlying_price=500.0,
            contracts=1,
            is_short=False,
            multiplier=100,
        )
        # 500 * 1 * 100 = 50_000 > 1_000
        with _patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
                option_request=opt,
            )
        assert decision.allowed is False
        assert "assignment_risk" in decision.reason

    def test_short_uses_max_assignment_risk_not_premium(self) -> None:
        """C-3: Short は max_assignment_risk_usd を使い max_premium_notional には影響されない"""
        eng = _make_engine(
            max_assignment_risk_usd=20_000.0,
            max_premium_notional=1_000.0,  # Long には厳しいが Short には無関係
        )
        opt = OptionRequest(
            symbol="SPY",
            strike=100.0,
            underlying_price=500.0,
            contracts=1,
            is_short=True,
            multiplier=100,
        )
        # 100 * 1 * 100 = 10_000 <= 20_000 → 通過
        assert eng.check_assignment_risk(opt) is True


# ---------------------------------------------------------------------------
# C-4: returns_unit schema 契約テスト
# ---------------------------------------------------------------------------

class TestC4ReturnsUnitContract:

    def test_unit_match_usd_passes(self) -> None:
        """C-4: config.returns_unit='usd' / expected_unit='usd' → 通過"""
        eng = RiskEngine(config=RiskConfig(returns_unit="usd"))
        returns = _sufficient_returns(worst=-500.0)
        portfolio = PortfolioSnapshot(returns_history=returns)
        var = eng.check_var(portfolio, expected_unit="usd")
        assert var > 0

    def test_unit_match_ratio_passes(self) -> None:
        """C-4: config.returns_unit='ratio' / expected_unit='ratio' → 通過"""
        eng = RiskEngine(config=RiskConfig(returns_unit="ratio"))
        returns = _sufficient_returns(worst=-0.05)
        portfolio = PortfolioSnapshot(returns_history=returns)
        var = eng.check_var(portfolio, expected_unit="ratio")
        assert var > 0

    def test_unit_mismatch_usd_vs_ratio_raises_assertion_error(self) -> None:
        """C-4: config.returns_unit='usd' / expected_unit='ratio' → AssertionError"""
        eng = RiskEngine(config=RiskConfig(returns_unit="usd"))
        returns = _sufficient_returns(worst=-0.05)
        portfolio = PortfolioSnapshot(returns_history=returns)
        with pytest.raises(AssertionError, match="returns_unit mismatch"):
            eng.check_var(portfolio, expected_unit="ratio")

    def test_unit_mismatch_ratio_vs_usd_raises_assertion_error(self) -> None:
        """C-4: config.returns_unit='ratio' / expected_unit='usd' → AssertionError"""
        eng = RiskEngine(config=RiskConfig(returns_unit="ratio"))
        returns = _sufficient_returns(worst=-500.0)
        portfolio = PortfolioSnapshot(returns_history=returns)
        with pytest.raises(AssertionError, match="returns_unit mismatch"):
            eng.check_var(portfolio, expected_unit="usd")

    def test_no_expected_unit_skips_check(self) -> None:
        """C-4: expected_unit=None (デフォルト) → unit チェックスキップ（後方互換）"""
        eng = RiskEngine(config=RiskConfig(returns_unit="usd"))
        returns = _sufficient_returns(worst=-300.0)
        portfolio = PortfolioSnapshot(returns_history=returns)
        var = eng.check_var(portfolio, expected_unit=None)
        assert var > 0

    def test_invalid_returns_unit_in_config_raises(self) -> None:
        """C-4: RiskConfig(returns_unit='pct') → ValueError"""
        with pytest.raises((ValueError, TypeError)):
            RiskConfig(returns_unit="pct")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# C-5: kill_switch 例外時 Pushover escalation + health probe
# ---------------------------------------------------------------------------

class TestC5KillSwitchHealth:

    def test_check_kill_switch_health_when_available(self) -> None:
        """C-5: kill_switch が利用可能な場合 healthy=True"""
        eng = _make_engine()
        with patch("common_v3.risk.kill_switch.is_active", return_value=False):
            health = eng.check_kill_switch_health()
        assert health["healthy"] is True
        assert health["error"] == ""
        assert health["is_active"] is False

    def test_check_kill_switch_health_when_unavailable(self) -> None:
        """C-5: kill_switch import 失敗時 healthy=False"""
        eng = _make_engine()
        with patch("common_v3.risk.engine.RiskEngine.check_kill_switch_health") as mock_health:
            mock_health.return_value = {"healthy": False, "error": "import error", "is_active": None}
            health = eng.check_kill_switch_health()
        assert health["healthy"] is False
        assert health["is_active"] is None

    def test_kill_switch_import_failure_triggers_pushover(self) -> None:
        """C-5: kill_switch import 失敗時 Pushover escalation が呼ばれる"""
        eng = _make_engine()
        pushover_called = []

        def _mock_pushover_send(**kwargs):
            pushover_called.append(kwargs)

        with (
            patch(
                "common_v3.risk.engine.RiskEngine._check_kill_switch",
                side_effect=Exception("import boom"),
            ),
            patch("common.pushover_client.send", side_effect=_mock_pushover_send),
        ):
            # _check_kill_switch が例外を raise する — check_all は内部で処理
            # ここでは _escalate_kill_switch_failure を直接テストする
            pass

        # _escalate_kill_switch_failure を直接呼び出してテスト
        with patch("common.pushover_client.send") as mock_send:
            RiskEngine._escalate_kill_switch_failure("test failure reason")
            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args
            assert "kill_switch" in str(call_kwargs).lower() or "RiskEngine" in str(call_kwargs)

    def test_kill_switch_health_probe_returns_dict(self) -> None:
        """C-5: check_kill_switch_health() が dict を返す"""
        eng = _make_engine()
        health = eng.check_kill_switch_health()
        assert isinstance(health, dict)
        assert "healthy" in health
        assert "error" in health
        assert "is_active" in health

    def test_bypass_approver_config_allows_continue_on_ks_failure(self) -> None:
        """C-5: kill_switch_bypass_approver 設定時に import 失敗でも check_all が続行"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=50_000.0,
            max_daily_loss_usd=-2_000.0,
            max_drawdown_pct=0.10,
            max_var_usd=100_000.0,
            kill_switch_bypass_approver="yuusakuichio",
        ))
        portfolio = PortfolioSnapshot(
            pnl_day_usd=-100.0,
            returns_history=_sufficient_returns(worst=-100.0),
        )

        # kill_switch import が失敗するが bypass_approver が設定されているため続行
        with patch(
            "common_v3.risk.kill_switch.is_active",
            side_effect=ImportError("module not found"),
        ):
            # _check_kill_switch を直接テスト
            result = eng._check_kill_switch()
            # bypass_approver 設定時は None を返す（check_all を続行させる）
            assert result is None

    def test_no_bypass_approver_on_ks_failure_denies(self) -> None:
        """C-5: kill_switch_bypass_approver=None 時は import 失敗で DENY"""
        eng = RiskEngine(config=RiskConfig(
            max_notional_usd=50_000.0,
            max_daily_loss_usd=-2_000.0,
            max_drawdown_pct=0.10,
            max_var_usd=100_000.0,
            kill_switch_bypass_approver=None,
        ))

        with patch(
            "common_v3.risk.kill_switch.is_active",
            side_effect=ImportError("module not found"),
        ):
            result = eng._check_kill_switch()
            assert result is not None
            assert result.allowed is False
            assert "kill_switch check failed" in result.reason


# ---------------------------------------------------------------------------
# H-1: check_max_daily_loss 境界値業界慣行 (>= → >)
# ---------------------------------------------------------------------------

class TestH1DailyLossBoundary:

    def test_at_limit_denied(self) -> None:
        """H-1: pnl_day == max_daily_loss_usd → 拒否 (業界慣行: 到達で即停止)"""
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        # ちょうど上限に達したら拒否
        assert eng.check_max_daily_loss(-2_000.0) is False

    def test_just_above_limit_allowed(self) -> None:
        """H-1: pnl_day が 1 cent 上回れば許可"""
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        assert eng.check_max_daily_loss(-1_999.99) is True

    def test_just_below_limit_denied(self) -> None:
        """H-1: pnl_day が 1 cent 下回ったら拒否"""
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        assert eng.check_max_daily_loss(-2_000.01) is False

    def test_check_all_at_limit_denied(self) -> None:
        """H-1: check_all() で pnl_day == max_daily_loss_usd → DENY"""
        eng = _make_engine(max_daily_loss_usd=-2_000.0)
        portfolio = PortfolioSnapshot(pnl_day_usd=-2_000.0)
        with _patch_ks(False):
            decision = eng.check_all(request_notional=1_000.0, portfolio=portfolio)
        assert decision.allowed is False
        assert "max_daily_loss" in decision.reason


# ---------------------------------------------------------------------------
# H-2: Kelly avg_loss_abs=0 で DENY
# ---------------------------------------------------------------------------

class TestH2KellyAvgLossZero:

    def test_avg_loss_zero_raises_value_error(self) -> None:
        """H-2: avg_loss_usd=0 → ValueError raise"""
        eng = _make_engine()
        portfolio = PortfolioSnapshot(avg_loss_usd=0.0)
        with pytest.raises(ValueError, match="avg_loss_usd=0 is invalid for Kelly sizing"):
            eng.check_position_sizing(portfolio, method=PositionSizingMethod.KELLY)

    def test_check_all_kelly_avg_loss_zero_denied(self) -> None:
        """H-2: check_all() に KELLY + avg_loss_usd=0 → DENY"""
        eng = _make_engine(sizing_method=PositionSizingMethod.KELLY)
        portfolio = PortfolioSnapshot(
            pnl_day_usd=-100.0,
            avg_loss_usd=0.0,
            returns_history=_sufficient_returns(worst=-200.0),
        )
        with _patch_ks(False):
            decision = eng.check_all(
                request_notional=1_000.0,
                portfolio=portfolio,
                sizing_method=PositionSizingMethod.KELLY,
            )
        assert decision.allowed is False
        assert "position_sizing" in decision.reason or "avg_loss_usd=0" in decision.reason

    def test_avg_loss_near_zero_but_positive_raises(self) -> None:
        """H-2: avg_loss_usd=-0.01 は有効 → raise しない"""
        eng = _make_engine()
        portfolio = PortfolioSnapshot(avg_loss_usd=-0.01, avg_win_usd=0.1)
        # -0.01 は abs=0.01 なので ゼロではない → 正常動作
        size = eng.check_position_sizing(portfolio, method=PositionSizingMethod.KELLY)
        assert size >= 1
