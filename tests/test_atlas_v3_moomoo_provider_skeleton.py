"""atlas_v3/ops/moomoo_provider.py スケルトン状態の interface 検証。

Sprint 2 Day 2 実装前のスケルトン。本ファイルは interface 契約を担保する。
実装完了時はこのテストを実動作テストに拡張する（AST inspection ではない）。
"""
from __future__ import annotations

import pytest


class TestMoomooProviderSkeleton:
    """スケルトン段階の interface 保証。"""

    def test_module_importable(self):
        """module が import できる（futu SDK 不在でも OK・遅延 import）。"""
        import atlas_v3.ops.moomoo_provider as mod  # noqa: F401

    def test_class_exists(self):
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        assert MoomooMetricProvider is not None

    def test_exception_exists(self):
        from atlas_v3.ops.moomoo_provider import MoomooProviderNotImplementedError
        assert issubclass(MoomooProviderNotImplementedError, NotImplementedError)

    def test_instantiation_does_not_raise(self):
        """skeleton なので __init__ は例外なしで動く。"""
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        provider = MoomooMetricProvider()
        assert provider is not None

    def test_exception_can_be_instantiated_with_message(self):
        """C-017 本実装後の更新: skeleton の Sprint 2 prepend は廃止。
        例外は通常の NotImplementedError として message を保持するのみ。"""
        from atlas_v3.ops.moomoo_provider import MoomooProviderNotImplementedError
        exc = MoomooProviderNotImplementedError("test message")
        assert "test message" in str(exc)

    def test_has_yfinance_compatible_interface(self):
        """YFinanceMetricProvider と同じ method name を持つ（interface 契約）。"""
        from atlas_v3.ops.moomoo_provider import MoomooMetricProvider
        assert hasattr(MoomooMetricProvider, "get_metrics")
        assert callable(MoomooMetricProvider.get_metrics)
