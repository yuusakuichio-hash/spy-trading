"""atlas_v3/ops/moomoo_provider.py 本実装テスト（mock ベース・実試行形式）。

Sprint 2 C-017 本実装検証。実 paper 接続 smoke test はゆうさくさん戻り後に別途実施。
mock で business logic / error handling / retry / AuthenticationError を検証。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from atlas_v3.ops.moomoo_provider import (
    AuthenticationError,
    MoomooMetricProvider,
    MoomooProviderNotImplementedError,
)


@pytest.fixture
def mocked_futu():
    """futu SDK が未 import でもテスト可能にする共通 fixture。"""
    with patch("atlas_v3.ops.moomoo_provider.FUTU_AVAILABLE", True), \
         patch("atlas_v3.ops.moomoo_provider.RET_OK", 0), \
         patch("atlas_v3.ops.moomoo_provider.TrdEnv") as mock_trdenv, \
         patch("atlas_v3.ops.moomoo_provider.TrdMarket") as mock_trdmkt, \
         patch("atlas_v3.ops.moomoo_provider.SecurityFirm") as mock_sec, \
         patch("atlas_v3.ops.moomoo_provider.time.sleep"):
        mock_trdenv.SIMULATE = "SIMULATE"
        mock_trdmkt.US = "US"
        mock_sec.FUTUJP = "FUTUJP"
        yield


class TestFuttuUnavailableGuard:
    """futu SDK 未インストール時の guard 動作。"""

    def test_raises_when_futu_unavailable(self):
        with patch("atlas_v3.ops.moomoo_provider.FUTU_AVAILABLE", False):
            provider = MoomooMetricProvider()
            with pytest.raises(MoomooProviderNotImplementedError):
                provider._ensure_connected()


class TestAuthenticationError:
    """セッション期限切れ検知（ADR-014 Decision 2）。"""

    def test_auth_error_on_unlock_failure(self, mocked_futu):
        mock_ctx_class = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.unlock_trade.return_value = (-1, None)
        mock_ctx_class.return_value = mock_ctx

        with patch("atlas_v3.ops.moomoo_provider.OpenSecTradeContext", mock_ctx_class):
            provider = MoomooMetricProvider(trade_password="dummy")
            with pytest.raises(AuthenticationError, match="unlock_trade failed"):
                provider._ensure_connected()

    def test_smoke_test_detects_401_in_get_acc_list(self, mocked_futu):
        mock_ctx = MagicMock()
        mock_ctx.get_acc_list.return_value = (-1, "401 Unauthorized")

        provider = MoomooMetricProvider()
        provider._trade_ctx = mock_ctx
        with pytest.raises(AuthenticationError, match="get_acc_list failed"):
            provider.smoke_test()

    def test_get_metrics_401_becomes_auth_error(self, mocked_futu):
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (-1, "401 unauth")

        provider = MoomooMetricProvider()
        provider._trade_ctx = mock_ctx
        with pytest.raises(AuthenticationError):
            provider.get_metrics()


class TestGetMetricsBusinessLogic:
    """get_metrics の pnl / drawdown 算出ロジック。"""

    def _make_mock_ctx(self, total_assets=100000.0, realized_pl=500.0, unrealized_pl=-200.0):
        import pandas as pd
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (
            0,
            pd.DataFrame([{
                "total_assets": total_assets,
                "realized_pl": realized_pl,
                "unrealized_pl": unrealized_pl,
            }]),
        )
        return mock_ctx

    def test_pnl_day_usd_sum_of_realized_unrealized(self, mocked_futu):
        mock_ctx = self._make_mock_ctx(realized_pl=500.0, unrealized_pl=-200.0)
        provider = MoomooMetricProvider()
        provider._trade_ctx = mock_ctx
        metrics = provider.get_metrics()
        assert metrics["pnl_day_usd"] == pytest.approx(300.0)

    def test_drawdown_computed_from_high_water_mark(self, mocked_futu):
        provider = MoomooMetricProvider()
        provider._trade_ctx = self._make_mock_ctx(total_assets=100000.0)
        m1 = provider.get_metrics()
        assert m1["drawdown_pct"] == pytest.approx(0.0)

        provider._trade_ctx = self._make_mock_ctx(total_assets=95000.0)
        m2 = provider.get_metrics()
        assert m2["drawdown_pct"] == pytest.approx(0.05)

    def test_latency_ms_is_non_negative(self, mocked_futu):
        mock_ctx = self._make_mock_ctx()
        provider = MoomooMetricProvider()
        provider._trade_ctx = mock_ctx
        metrics = provider.get_metrics()
        assert metrics["latency_ms"] >= 0

    def test_dict_has_required_keys(self, mocked_futu):
        mock_ctx = self._make_mock_ctx()
        provider = MoomooMetricProvider()
        provider._trade_ctx = mock_ctx
        metrics = provider.get_metrics()
        assert set(metrics.keys()) >= {"pnl_day_usd", "drawdown_pct", "latency_ms"}


class TestRetryLogic:
    """retry_max 回までリトライして最終失敗時 RuntimeError。"""

    def test_retries_then_raises_runtime_error(self, mocked_futu):
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.side_effect = RuntimeError("transient")

        provider = MoomooMetricProvider(retry_max=2)
        provider._trade_ctx = mock_ctx
        with pytest.raises(RuntimeError, match="after 2 attempts"):
            provider.get_metrics()
        assert mock_ctx.accinfo_query.call_count == 2

    def test_auth_error_does_not_retry(self, mocked_futu):
        mock_ctx = MagicMock()
        mock_ctx.accinfo_query.return_value = (-1, "401 unauth")

        provider = MoomooMetricProvider(retry_max=3)
        provider._trade_ctx = mock_ctx
        with pytest.raises(AuthenticationError):
            provider.get_metrics()
        assert mock_ctx.accinfo_query.call_count == 1


class TestInterface:
    """YFinanceMetricProvider と同一 interface 確認。"""

    def test_get_metrics_callable(self):
        assert callable(MoomooMetricProvider.get_metrics)

    def test_smoke_test_callable(self):
        assert callable(MoomooMetricProvider.smoke_test)

    def test_close_callable(self):
        assert callable(MoomooMetricProvider.close)
