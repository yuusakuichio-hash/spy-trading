"""tests/test_atlas_v3_risk_20260425.py — RiskAggregator 単体テスト"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestRiskAggregator:
    def test_no_guards_returns_allowed(self):
        from atlas_v3.risk import RiskAggregator
        agg = RiskAggregator()
        res = agg.check_all()
        assert res.allowed is True
        assert res.blocking_guards == ()
        assert res.size_factor == 1.0

    def test_all_passing_guards_allowed(self):
        from atlas_v3.risk import RiskAggregator, DrawdownTracker, ConsecutiveLossGuard
        dd = DrawdownTracker(initial_equity=10000.0)
        cl = ConsecutiveLossGuard()
        agg = RiskAggregator(drawdown=dd, consecutive=cl)
        res = agg.check_all(equity=10000.0)
        assert res.allowed is True
        assert res.size_factor == 1.0

    def test_consecutive_loss_halt_blocks(self):
        from atlas_v3.risk import RiskAggregator, ConsecutiveLossGuard
        cl = ConsecutiveLossGuard()
        agg = RiskAggregator(consecutive=cl)
        # 5 連敗 (record_loss state 経由) → halt
        for _ in range(5):
            agg.record_loss()
        res = agg.check_all()
        assert res.allowed is False
        assert "consecutive_loss" in res.blocking_guards
        assert res.halt is True
