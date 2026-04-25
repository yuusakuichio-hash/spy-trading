"""tests/test_market_data_adapter_20260425.py — MoomooMarketDataAdapter 単体テスト"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ===========================================================================
# 1. VIX 取得成功 → MarketEnvironment.vix に反映
# ===========================================================================

class TestAdapterVixSuccess:
    def test_returns_market_environment_with_vix_from_spy_atm(self):
        from atlas_v3.ops.market_data_adapter import MoomooMarketDataAdapter
        from atlas_v3.core.env_observer import MarketEnvironment

        ctx = MagicMock()
        with patch(
            "atlas_v3.ops.market_data_adapter.estimate_vix_from_spy_atm",
            return_value=18.5,
        ):
            adapter = MoomooMarketDataAdapter(ctx)
            env = adapter.get_environment()

        assert isinstance(env, MarketEnvironment)
        assert env.vix == 18.5
        assert env.bias == "neutral"
        assert env.ivr_by_symbol == {"SPY": 40.0}


# ===========================================================================
# 2. VIX 取得失敗 + cache 不在 → default fallback
# ===========================================================================

class TestAdapterVixFailNoCache:
    def test_returns_default_vix_when_estimate_fails_no_cache(self):
        from atlas_v3.ops.market_data_adapter import MoomooMarketDataAdapter

        ctx = MagicMock()
        with patch(
            "atlas_v3.ops.market_data_adapter.estimate_vix_from_spy_atm",
            return_value=None,
        ):
            adapter = MoomooMarketDataAdapter(ctx)
            env = adapter.get_environment()

        # default fallback (cold start)
        assert env.vix == 16.0


# ===========================================================================
# 3. VIX 取得失敗 + cache あり → 前回値継続 (degraded mode)
# ===========================================================================

class TestAdapterVixFailWithCache:
    def test_returns_cached_env_when_estimate_fails_after_success(self):
        from atlas_v3.ops.market_data_adapter import MoomooMarketDataAdapter

        ctx = MagicMock()
        adapter = MoomooMarketDataAdapter(ctx)

        # 1 回目: 成功 → cache 確立
        with patch(
            "atlas_v3.ops.market_data_adapter.estimate_vix_from_spy_atm",
            return_value=20.0,
        ):
            env1 = adapter.get_environment()
        assert env1.vix == 20.0

        # 2 回目: 失敗・cache TTL 超過後 → 前回値継続
        adapter._cache_ts = 0.0  # TTL 強制超過
        with patch(
            "atlas_v3.ops.market_data_adapter.estimate_vix_from_spy_atm",
            return_value=None,
        ):
            env2 = adapter.get_environment()
        assert env2.vix == 20.0  # 前回値継続


# ===========================================================================
# 4. Cache TTL 内 → estimate を再呼出しない
# ===========================================================================

class TestAdapterCache:
    def test_cache_within_ttl_does_not_recall_estimate(self):
        from atlas_v3.ops.market_data_adapter import MoomooMarketDataAdapter

        ctx = MagicMock()
        adapter = MoomooMarketDataAdapter(ctx)

        with patch(
            "atlas_v3.ops.market_data_adapter.estimate_vix_from_spy_atm",
            return_value=22.0,
        ) as mock_est:
            adapter.get_environment()
            adapter.get_environment()
            adapter.get_environment()

        # TTL 内なら estimate は 1 回だけ呼ばれる
        assert mock_est.call_count == 1
