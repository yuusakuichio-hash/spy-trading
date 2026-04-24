"""tests/test_pdt_wiring_10tactics_20260425.py — 新 10 戦術 PDTGuard wiring テスト

テスト対象:
  atlas_v3/bots/engines/broken_wing_butterfly.py  (place_order)
  atlas_v3/bots/engines/iron_fly.py               (place_order)
  atlas_v3/bots/engines/ratio_spread.py            (place_order)
  atlas_v3/bots/engines/short_strangle_0dte.py    (build_order)
  atlas_v3/bots/engines/earnings_straddle_buy.py  (build_order)
  atlas_v3/bots/engines/pmcc.py                    (build_orders)
  atlas_v3/bots/engines/jade_lizard.py            (build_orders)
  atlas_v3/bots/engines/weekly_gamma_scalp.py     (build_orders)
  atlas_v3/bots/engines/diagonal_spread.py        (build_order)
  atlas_v3/bots/engines/vix_tail_hedge.py         (build_order)

各戦術 3 シナリオ × 10 戦術 = 30 件 基本テスト
+ 共通 6 件（PDTBlockedError 型確認・reason 文字列・paper_mode フラグ）= 合計 36 件

シナリオ:
  [paper]  paper_mode=True  → PDTBlockedError を raise しない（allow）
  [block]  live + capital < $25K + rolling 3 件超 → PDTBlockedError を raise する
  [high]   live + capital >= $25K + rolling 3 件 → PDTBlockedError を raise しない（allow）

各テストは独立 PDTTracker（pytest tmp_path）を使用しファイルシステムを汚染しない。
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zoneinfo
_ET = zoneinfo.ZoneInfo("America/New_York")

from common.pdt_tracker import PDTTracker, PDT_THRESHOLD_USD
from atlas_v3.bots.engines.pdt_guard import PDTBlockedError, PDTGuard


# ---------------------------------------------------------------------------
# ヘルパー: n_trades 件を記録した PDTTracker を返す
# ---------------------------------------------------------------------------

_BASE_DATE = datetime.date(2026, 4, 21)  # 月曜営業日


def _et(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, 0, tzinfo=_ET)


def _make_tracker(n_trades: int, tmp_path: Path) -> PDTTracker:
    tracker = PDTTracker(data_file=tmp_path / "pdt.jsonl")
    entry = _et(_BASE_DATE.year, _BASE_DATE.month, _BASE_DATE.day, 10, 0)
    exit_ = _et(_BASE_DATE.year, _BASE_DATE.month, _BASE_DATE.day, 14, 0)
    for _ in range(n_trades):
        tracker.record_round_trip(
            symbol="US.SPY",
            entry_time=entry,
            exit_time=exit_,
            strategy="TEST",
            exit_type="manual_close",
        )
    return tracker


def _empty_tracker(tmp_path: Path) -> PDTTracker:
    return PDTTracker(data_file=tmp_path / "pdt.jsonl")


# ---------------------------------------------------------------------------
# Fixture: モック EnvEnvironment
# ---------------------------------------------------------------------------

from atlas_v3.core.env_observer import MarketEnvironment


def _env(symbol: str = "SPY", ivr: float = 70.0, vix: float = 18.0) -> MarketEnvironment:
    return MarketEnvironment(
        vix=vix,
        vrp=100.0,
        gex=0.0,
        term_ratio=1.1,
        bias="bull",
        ivr_by_symbol={symbol: ivr},
    )


# ===========================================================================
# 経路 A — broken_wing_butterfly / iron_fly / ratio_spread (place_order)
# ===========================================================================

class TestBrokenWingButterflyPDTWiring:
    """broken_wing_butterfly.place_order() の PDT wiring テスト。"""

    def _engine_and_decision(self, paper_mode: bool = True):
        from atlas_v3.bots.engines.broken_wing_butterfly import (
            BrokenWingButterflyConfig,
            BrokenWingButterflyEngine,
            BWBEntryDecision,
            BWBLeg,
        )
        cfg = BrokenWingButterflyConfig(paper_mode=paper_mode)
        engine = BrokenWingButterflyEngine(config=cfg)
        legs = (
            BWBLeg(label="long_call_lower", strike=500.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="short_call_body", strike=505.0, option_type="call", side="sell", quantity=2),
            BWBLeg(label="long_call_upper", strike=515.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="asymmetric_offset", strike=495.0, option_type="call", side="buy", quantity=1),
        )
        decision = BWBEntryDecision(
            should_enter=True,
            symbol="US.SPY",
            legs=legs,
            atm_strike=500.0,
            net_credit=1.5,
            quantity=1,
            idempotency_key="test_key_bwb",
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """BWB-01: paper_mode=True → PDTBlockedError を raise しない"""
        engine, decision = self._engine_and_decision(paper_mode=True)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            # paper_mode=True なら raise しないこと
            order_id = engine.place_order(decision, capital_usd=5000.0)
        assert order_id  # 発注 ID が返ること

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """BWB-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.place_order(decision, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """BWB-03: live + high capital ($25K以上) + rolling 3 → allow"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            order_id = engine.place_order(decision, capital_usd=PDT_THRESHOLD_USD)
        assert order_id


class TestIronFlyPDTWiring:
    """iron_fly.place_order() の PDT wiring テスト。"""

    def _engine_and_decision(self):
        from atlas_v3.bots.engines.iron_fly import (
            IronFlyConfig,
            IronFlyEngine,
            IronFlyEntryDecision,
            IronFlyLeg,
        )
        engine = IronFlyEngine(config=IronFlyConfig())
        legs = (
            IronFlyLeg(strike=500.0, option_type="call", side="sell", quantity=1),
            IronFlyLeg(strike=500.0, option_type="put", side="sell", quantity=1),
            IronFlyLeg(strike=505.0, option_type="call", side="buy", quantity=1),
            IronFlyLeg(strike=495.0, option_type="put", side="buy", quantity=1),
        )
        decision = IronFlyEntryDecision(
            should_enter=True,
            symbol="US.SPY",
            legs=legs,
            atm_strike=500.0,
            max_credit=2.0,
            quantity=1,
            idempotency_key="test_key_iron_fly",
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """IronFly-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.iron_fly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            order_id = engine.place_order(decision, paper_mode=True, capital_usd=5000.0)
        assert order_id

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """IronFly-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.iron_fly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.place_order(decision, paper_mode=False, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """IronFly-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.iron_fly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            order_id = engine.place_order(
                decision, paper_mode=False, capital_usd=PDT_THRESHOLD_USD
            )
        assert order_id


class TestRatioSpreadPDTWiring:
    """ratio_spread.place_order() の PDT wiring テスト。"""

    def _engine_and_decision(self, paper_mode: bool = True):
        from atlas_v3.bots.engines.ratio_spread import (
            RatioSpreadConfig,
            RatioSpreadEngine,
            RatioSpreadEntryDecision,
            RatioSpreadLeg,
        )
        cfg = RatioSpreadConfig(paper_mode=paper_mode)
        engine = RatioSpreadEngine(config=cfg)
        legs = (
            RatioSpreadLeg(label="long_atm_call", side="buy", strike=500.0, quantity=1),
            RatioSpreadLeg(label="short_otm_call_1", side="sell", strike=505.0, quantity=1),
            RatioSpreadLeg(label="short_otm_call_2", side="sell", strike=505.0, quantity=1),
        )
        decision = RatioSpreadEntryDecision(
            should_enter=True,
            symbol="US.SPY",
            legs=legs,
            atm_strike=500.0,
            net_credit=1.0,
            quantity=1,
            idempotency_key="test_key_ratio",
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """Ratio-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision(paper_mode=True)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.ratio_spread.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            order_id, _ = engine.place_order(decision, capital_usd=5000.0)
        assert order_id

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """Ratio-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.ratio_spread.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.place_order(decision, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """Ratio-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.ratio_spread.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            order_id, _ = engine.place_order(decision, capital_usd=PDT_THRESHOLD_USD)
        assert order_id


# ===========================================================================
# 経路 B — short_strangle_0dte / earnings_straddle_buy / pmcc /
#           jade_lizard / weekly_gamma_scalp / diagonal_spread / vix_tail_hedge
#           (build_order / build_orders)
# ===========================================================================

class TestShortStrangle0DTEPDTWiring:
    """short_strangle_0dte.build_order() の PDT wiring テスト。"""

    def _engine_and_decision(self):
        from atlas_v3.bots.engines.short_strangle_0dte import (
            ShortStrangle0DTEConfig,
            ShortStrangle0DTEEngine,
            StrangleEntryDecision,
        )
        engine = ShortStrangle0DTEEngine(config=ShortStrangle0DTEConfig())
        decision = StrangleEntryDecision(
            should_enter=True,
            symbol="US.SPY",
            call_strike=505.0,
            put_strike=495.0,
            call_delta=0.12,
            put_delta=0.12,
            call_credit=0.8,
            put_credit=0.8,
            quantity=1,
            expiry_date="2026-04-21",
            idempotency_key="test_key_ss0dte",
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """SS0DTE-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.short_strangle_0dte.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(decision, paper_mode=True, capital_usd=5000.0)
        assert req is not None

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """SS0DTE-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.short_strangle_0dte.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.build_order(decision, paper_mode=False, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """SS0DTE-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.short_strangle_0dte.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(
                decision, paper_mode=False, capital_usd=PDT_THRESHOLD_USD
            )
        assert req is not None


class TestEarningsStraddleBuyPDTWiring:
    """earnings_straddle_buy.build_order() の PDT wiring テスト。"""

    def _engine_and_decision(self):
        from atlas_v3.bots.engines.earnings_straddle_buy import (
            EarningsStraddleBuyTactic,
            StraddleBuyConfig,
            StraddleBuyEntryDecision,
        )
        engine = EarningsStraddleBuyTactic(config=StraddleBuyConfig())
        decision = StraddleBuyEntryDecision(
            should_enter=True,
            symbol="US.NVDA",
            earnings_date="2026-04-22",
            ivr=45.0,
            idempotency_key="test_key_esb",
            quantity_call=1,
            quantity_put=1,
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """ESB-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.earnings_straddle_buy.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(decision, leg="call", paper_mode=True, capital_usd=5000.0)
        assert req is not None

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """ESB-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.earnings_straddle_buy.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.build_order(decision, leg="call", paper_mode=False, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """ESB-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.earnings_straddle_buy.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(
                decision, leg="put", paper_mode=False, capital_usd=PDT_THRESHOLD_USD
            )
        assert req is not None


class TestPMCCPDTWiring:
    """pmcc.build_orders() の PDT wiring テスト。"""

    def _engine_and_decision(self, paper_mode: bool = True):
        from atlas_v3.bots.engines.pmcc import (
            PMCCConfig,
            PMCCEntryDecision,
            PMCCLeg,
            PMCCTactic,
        )
        cfg = PMCCConfig(paper_mode=paper_mode)
        engine = PMCCTactic(config=cfg)
        legs = (
            PMCCLeg(label="long_call", side="buy", delta=0.82, dte_target=90, quantity=1),
            PMCCLeg(label="short_call", side="sell", delta=0.30, dte_target=7, quantity=1),
        )
        decision = PMCCEntryDecision(
            should_enter=True,
            symbol="US.SPY",
            legs=legs,
            net_debit=5.0,
            quantity=1,
            idempotency_key="test_key_pmcc",
            ivr=45.0,
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """PMCC-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision(paper_mode=True)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.pmcc.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            orders = engine.build_orders(decision, capital_usd=5000.0)
        assert len(orders) == 2

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """PMCC-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.pmcc.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.build_orders(decision, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """PMCC-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.pmcc.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            orders = engine.build_orders(decision, capital_usd=PDT_THRESHOLD_USD)
        assert len(orders) == 2


class TestJadeLizardPDTWiring:
    """jade_lizard.build_orders() の PDT wiring テスト。"""

    def _engine_and_decision(self, paper_mode: bool = True):
        from atlas_v3.bots.engines.jade_lizard import (
            JadeLizardConfig,
            JadeLizardEntryDecision,
            JadeLizardLeg,
            JadeLizardTactic,
        )
        cfg = JadeLizardConfig(paper_mode=paper_mode)
        engine = JadeLizardTactic(config=cfg)
        legs = (
            JadeLizardLeg(label="short_put", side="sell", strike=490.0, delta=0.175, credit=0.5),
            JadeLizardLeg(label="short_call", side="sell", strike=510.0, delta=0.225, credit=0.4),
            JadeLizardLeg(label="long_call", side="buy", strike=515.0, delta=0.1, credit=-0.15),
        )
        decision = JadeLizardEntryDecision(
            should_enter=True,
            symbol="US.SPY",
            legs=legs,
            total_credit=75.0,
            no_risk_upside=True,
            quantity=1,
            idempotency_key="test_key_jl",
            ivr=65.0,
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """JL-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision(paper_mode=True)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.jade_lizard.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            orders = engine.build_orders(decision, capital_usd=5000.0)
        assert len(orders) == 3

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """JL-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.jade_lizard.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.build_orders(decision, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """JL-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.jade_lizard.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            orders = engine.build_orders(decision, capital_usd=PDT_THRESHOLD_USD)
        assert len(orders) == 3


class TestWeeklyGammaScalpPDTWiring:
    """weekly_gamma_scalp.build_orders() の PDT wiring テスト。"""

    def _engine_and_entry(self, paper_mode: bool = True):
        from datetime import date
        from atlas_v3.bots.engines.weekly_gamma_scalp import (
            WeeklyGammaScalpConfig,
            WeeklyGammaScalpEntry,
            WeeklyGammaScalpTactic,
            WeeklyStraddleLeg,
        )
        cfg = WeeklyGammaScalpConfig(paper_mode=paper_mode)
        engine = WeeklyGammaScalpTactic(config=cfg)
        call_leg = WeeklyStraddleLeg(
            option_type="call", strike=500.0,
            expiry=date(2026, 4, 25), ask=5.0, quantity=1,
        )
        put_leg = WeeklyStraddleLeg(
            option_type="put", strike=500.0,
            expiry=date(2026, 4, 25), ask=5.0, quantity=1,
        )
        entry = WeeklyGammaScalpEntry(
            should_enter=True,
            symbol="SPY",
            legs=(call_leg, put_leg),
            total_cost=1000.0,
            underlying_price=500.0,
            weekly_expiry=date(2026, 4, 25),
            idempotency_key="test_key_wgs",
            ivr=30.0,
        )
        return engine, entry

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """WGS-01: paper_mode=True → allow"""
        engine, entry = self._engine_and_entry(paper_mode=True)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.weekly_gamma_scalp.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            orders = engine.build_orders(entry, capital_usd=5000.0)
        assert len(orders) == 2

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """WGS-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, entry = self._engine_and_entry(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.weekly_gamma_scalp.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.build_orders(entry, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """WGS-03: live + high capital + rolling 3 → allow"""
        engine, entry = self._engine_and_entry(paper_mode=False)
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.weekly_gamma_scalp.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            orders = engine.build_orders(entry, capital_usd=PDT_THRESHOLD_USD)
        assert len(orders) == 2


class TestDiagonalSpreadPDTWiring:
    """diagonal_spread.build_order() の PDT wiring テスト。"""

    def _engine_and_decision(self):
        from atlas_v3.bots.engines.diagonal_spread import (
            DiagonalSpreadConfig,
            DiagonalSpreadEntryDecision,
            DiagonalSpreadTactic,
        )
        engine = DiagonalSpreadTactic(config=DiagonalSpreadConfig())
        decision = DiagonalSpreadEntryDecision(
            should_enter=True,
            symbol="US.SPY",
            short_delta_target=0.25,
            short_dte_target=10,
            long_dte_target=45,
            quantity=1,
            idempotency_key="test_key_diag",
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """Diag-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.diagonal_spread.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(decision, paper_mode=True, capital_usd=5000.0)
        assert req is not None

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """Diag-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.diagonal_spread.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.build_order(decision, paper_mode=False, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """Diag-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.diagonal_spread.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(
                decision, paper_mode=False, capital_usd=PDT_THRESHOLD_USD
            )
        assert req is not None


class TestVixTailHedgePDTWiring:
    """vix_tail_hedge.build_order() の PDT wiring テスト。"""

    def _engine_and_decision(self):
        from atlas_v3.bots.engines.vix_tail_hedge import (
            VixTailHedgeConfig,
            VixTailHedgeEngine,
            VixTailHedgeEntryDecision,
        )
        engine = VixTailHedgeEngine(config=VixTailHedgeConfig())
        decision = VixTailHedgeEntryDecision(
            should_enter=True,
            symbol="VIX",
            delta_target=0.125,
            dte_target=45,
            quantity=1,
            estimated_premium=0.5,
            idempotency_key="test_key_vix",
        )
        return engine, decision

    def test_paper_mode_allows(self, tmp_path: Path) -> None:
        """VixHedge-01: paper_mode=True → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.vix_tail_hedge.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(decision, paper_mode=True, capital_usd=5000.0)
        assert req is not None

    def test_live_low_capital_blocked(self, tmp_path: Path) -> None:
        """VixHedge-02: live + low capital + rolling 3 → PDTBlockedError"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.vix_tail_hedge.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError):
                engine.build_order(decision, paper_mode=False, capital_usd=5000.0)

    def test_live_high_capital_allows(self, tmp_path: Path) -> None:
        """VixHedge-03: live + high capital + rolling 3 → allow"""
        engine, decision = self._engine_and_decision()
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.vix_tail_hedge.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            req = engine.build_order(
                decision, paper_mode=False, capital_usd=PDT_THRESHOLD_USD
            )
        assert req is not None


# ===========================================================================
# 共通テスト — PDTBlockedError の型・メッセージ確認
# ===========================================================================

class TestPDTBlockedErrorCommon:
    """PDTBlockedError 共通検証テスト。"""

    def test_pdt_blocked_error_is_runtime_error(self) -> None:
        """Common-01: PDTBlockedError は RuntimeError のサブクラス"""
        err = PDTBlockedError("test reason")
        assert isinstance(err, RuntimeError)

    def test_pdt_blocked_error_message_contains_reason(self, tmp_path: Path) -> None:
        """Common-02: PDTBlockedError のメッセージに reason が含まれる"""
        from atlas_v3.bots.engines.broken_wing_butterfly import (
            BrokenWingButterflyConfig,
            BrokenWingButterflyEngine,
            BWBEntryDecision,
            BWBLeg,
        )
        cfg = BrokenWingButterflyConfig(paper_mode=False)
        engine = BrokenWingButterflyEngine(config=cfg)
        legs = (
            BWBLeg(label="long_call_lower", strike=500.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="short_call_body", strike=505.0, option_type="call", side="sell", quantity=2),
            BWBLeg(label="long_call_upper", strike=515.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="asymmetric_offset", strike=495.0, option_type="call", side="buy", quantity=1),
        )
        decision = BWBEntryDecision(
            should_enter=True, symbol="US.SPY", legs=legs,
            atm_strike=500.0, net_credit=1.5, quantity=1,
            idempotency_key="common_test_key",
        )
        tracker = _make_tracker(3, tmp_path)
        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            with pytest.raises(PDTBlockedError) as exc_info:
                engine.place_order(decision, capital_usd=5000.0)
        assert "PDT blocked" in str(exc_info.value)

    def test_pdt_blocked_error_not_raised_when_rolling_2(self, tmp_path: Path) -> None:
        """Common-03: rolling=2（上限未到達）→ PDTBlockedError を raise しない"""
        from atlas_v3.bots.engines.broken_wing_butterfly import (
            BrokenWingButterflyConfig,
            BrokenWingButterflyEngine,
            BWBEntryDecision,
            BWBLeg,
        )
        cfg = BrokenWingButterflyConfig(paper_mode=False)
        engine = BrokenWingButterflyEngine(config=cfg)
        legs = (
            BWBLeg(label="long_call_lower", strike=500.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="short_call_body", strike=505.0, option_type="call", side="sell", quantity=2),
            BWBLeg(label="long_call_upper", strike=515.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="asymmetric_offset", strike=495.0, option_type="call", side="buy", quantity=1),
        )
        decision = BWBEntryDecision(
            should_enter=True, symbol="US.SPY", legs=legs,
            atm_strike=500.0, net_credit=1.5, quantity=1,
            idempotency_key="common_test_key2",
        )
        # rolling=2 → remaining=1 → allowed=True
        tracker = _make_tracker(2, tmp_path)
        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.PDTGuard",
            lambda **kw: PDTGuard(tracker=tracker, **kw),
        ):
            order_id = engine.place_order(decision, capital_usd=5000.0)
        assert order_id

    def test_pdt_check_result_passed_paper_mode_flag_correctly(self, tmp_path: Path) -> None:
        """Common-04: PDTGuard に paper_mode=True が渡されていること確認（BWB）"""
        from atlas_v3.bots.engines.broken_wing_butterfly import (
            BrokenWingButterflyConfig,
            BrokenWingButterflyEngine,
            BWBEntryDecision,
            BWBLeg,
        )
        cfg = BrokenWingButterflyConfig(paper_mode=True)
        engine = BrokenWingButterflyEngine(config=cfg)
        legs = (
            BWBLeg(label="long_call_lower", strike=500.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="short_call_body", strike=505.0, option_type="call", side="sell", quantity=2),
            BWBLeg(label="long_call_upper", strike=515.0, option_type="call", side="buy", quantity=1),
            BWBLeg(label="asymmetric_offset", strike=495.0, option_type="call", side="buy", quantity=1),
        )
        decision = BWBEntryDecision(
            should_enter=True, symbol="US.SPY", legs=legs,
            atm_strike=500.0, net_credit=1.5, quantity=1,
            idempotency_key="common_test_key3",
        )
        created_guards: list[PDTGuard] = []

        class CapturingGuardFactory:
            def __init__(self, **kw: Any) -> None:
                self._guard = PDTGuard(tracker=_make_tracker(3, tmp_path), **kw)
                created_guards.append(self._guard)

            def check_can_trade(self, symbol: str, trade_date: Any = None):
                return self._guard.check_can_trade(symbol, trade_date)

        with patch(
            "atlas_v3.bots.engines.broken_wing_butterfly.PDTGuard",
            CapturingGuardFactory,
        ):
            engine.place_order(decision, capital_usd=5000.0)

        assert len(created_guards) == 1
        # paper_mode=True が guard に渡されていること
        assert created_guards[0]._paper_mode is True

    def test_pdt_blocked_error_inherits_correctly(self) -> None:
        """Common-05: PDTBlockedError は catch(RuntimeError) で補足できる"""
        with pytest.raises(RuntimeError):
            raise PDTBlockedError("test")

    def test_pdt_guard_wiring_all_10_tactics_importable(self) -> None:
        """Common-06: 10 戦術すべて import 可能で PDTBlockedError をエクスポート"""
        from atlas_v3.bots.engines.broken_wing_butterfly import BrokenWingButterflyEngine  # noqa: F401
        from atlas_v3.bots.engines.iron_fly import IronFlyEngine  # noqa: F401
        from atlas_v3.bots.engines.ratio_spread import RatioSpreadEngine  # noqa: F401
        from atlas_v3.bots.engines.short_strangle_0dte import ShortStrangle0DTEEngine  # noqa: F401
        from atlas_v3.bots.engines.earnings_straddle_buy import EarningsStraddleBuyTactic  # noqa: F401
        from atlas_v3.bots.engines.pmcc import PMCCTactic  # noqa: F401
        from atlas_v3.bots.engines.jade_lizard import JadeLizardTactic  # noqa: F401
        from atlas_v3.bots.engines.weekly_gamma_scalp import WeeklyGammaScalpTactic  # noqa: F401
        from atlas_v3.bots.engines.diagonal_spread import DiagonalSpreadTactic  # noqa: F401
        from atlas_v3.bots.engines.vix_tail_hedge import VixTailHedgeEngine  # noqa: F401
        from atlas_v3.bots.engines.pdt_guard import PDTBlockedError as _E  # noqa: F401
        assert issubclass(_E, RuntimeError)
