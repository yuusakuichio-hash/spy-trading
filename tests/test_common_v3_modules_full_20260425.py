"""tests/test_common_v3_modules_full_20260425.py — 空モジュール本実装の単体テスト"""
from __future__ import annotations

import datetime

import pytest


# ===========================================================================
# atlas_v3.broker
# ===========================================================================

class TestBroker:
    def test_build_broker_paper_returns_moomoo_paper(self):
        from atlas_v3.broker import build_broker
        from unittest.mock import MagicMock
        broker = build_broker("paper", trade_ctx=MagicMock(), paper_acc_id=1173421)
        assert broker is not None
        assert hasattr(broker, "place_order")

    def test_build_broker_paper_returns_none_without_acc_id(self):
        from atlas_v3.broker import build_broker
        from unittest.mock import MagicMock
        assert build_broker("paper", trade_ctx=MagicMock(), paper_acc_id=None) is None

    def test_build_broker_dry_returns_none(self):
        from atlas_v3.broker import build_broker
        assert build_broker("dry") is None

    def test_build_broker_live_returns_none_safety(self):
        """live broker 未実装で safety で None 返却."""
        from atlas_v3.broker import build_broker
        assert build_broker("live") is None


# ===========================================================================
# common_v3.position
# ===========================================================================

class TestPosition:
    def test_position_snapshot_long_short_property(self):
        from common_v3.position import PositionSnapshot
        long_p = PositionSnapshot(symbol="US.SPY", qty=10)
        short_p = PositionSnapshot(symbol="US.SPY", qty=-5)
        assert long_p.is_long
        assert not long_p.is_short
        assert short_p.is_short
        assert not short_p.is_long

    def test_aggregate_positions_combines_lists(self):
        from common_v3.position import PositionSnapshot, aggregate_positions
        a = [PositionSnapshot(symbol="US.SPY", qty=1)]
        b = [PositionSnapshot(symbol="US.QQQ", qty=2)]
        result = aggregate_positions(a, b)
        assert len(result) == 2

    def test_find_naked_shorts_detects_short_only(self):
        from common_v3.position import PositionSnapshot, find_naked_shorts
        positions = [
            PositionSnapshot(symbol="US.SPY", qty=-1),  # naked short
            PositionSnapshot(symbol="US.QQQ", qty=1),
            PositionSnapshot(symbol="US.QQQ", qty=-1),  # hedged
        ]
        naked = find_naked_shorts(positions)
        assert len(naked) == 1
        assert naked[0].symbol == "US.SPY"


# ===========================================================================
# common_v3.auth
# ===========================================================================

class TestAuth:
    def test_get_credential_reads_from_env(self, monkeypatch):
        from common_v3.auth import get_credential, reset_credentials_cache
        monkeypatch.setenv("MOOMOO_APP_ID", "test_id_123")
        reset_credentials_cache()
        assert get_credential("moomoo_app_id") == "test_id_123"
        reset_credentials_cache()  # cleanup

    def test_unknown_credential_returns_default(self):
        from common_v3.auth import get_credential, reset_credentials_cache
        reset_credentials_cache()
        assert get_credential("unknown_field", default="fallback") == "fallback"


# ===========================================================================
# common_v3.llm
# ===========================================================================

class TestLLM:
    def test_default_model_for_anthropic(self):
        from common_v3.llm import LLMClient
        c = LLMClient(provider="anthropic")
        assert "claude" in c._model.lower()

    def test_default_model_for_gemini(self):
        from common_v3.llm import LLMClient
        c = LLMClient(provider="gemini")
        assert "gemini" in c._model.lower()

    def test_unsupported_provider_raises(self):
        from common_v3.llm import LLMClient
        c = LLMClient(provider="unknown_xyz")
        with pytest.raises(ValueError):
            c.complete("test")


# ===========================================================================
# common_v3.spec_drift
# ===========================================================================

class TestSpecDrift:
    def test_no_checks_returns_empty(self):
        from common_v3.spec_drift import SpecDriftChecker
        checker = SpecDriftChecker()
        assert checker.check() == []

    def test_drift_detected_when_spec_differs(self, tmp_path):
        from common_v3.spec_drift import SpecDriftChecker
        spec_yaml = tmp_path / "spec.yaml"
        spec_yaml.write_text("vix_max: 30.0\n")

        checker = SpecDriftChecker()
        checker.add_check(
            spec_path=str(spec_yaml),
            spec_field="vix_max",
            impl_path="atlas_v3/...",
            impl_value=25.0,  # 仕様 30 と乖離
            severity="warning",
        )
        findings = checker.check()
        assert len(findings) == 1
        assert findings[0].spec_value == 30.0
        assert findings[0].impl_value == 25.0
        assert "vix_max" in findings[0].message
