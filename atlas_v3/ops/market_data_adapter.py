"""atlas_v3/ops/market_data_adapter.py — moomoo provider → MarketEnvironment 変換 adapter

Why
---
AtlasEngine は MarketDataClient (`get_environment() -> MarketEnvironment`) を期待するが、
既存 MoomooMetricProvider は `get_metrics() -> dict` で監視メトリクスを返すだけで
MarketEnvironment 用 field (vix / vrp / gex / term_ratio / bias / ivr_by_symbol) を持たない。

このモジュールは moomoo OpenD 経由で MarketEnvironment を構築する責務を持つ:
- VIX: SPY ATM straddle IV から推定 (vix_estimator.estimate_vix_from_spy_atm)
- VRP: VIX - HV (HV は SPY history から計算)
- GEX: 後段実装 (現状 0.0)
- term_ratio: 後段実装 (現状 1.0・近 expiry vs 遠 expiry IV slope で代替予定)
- bias: 後段実装 (現状 "neutral")
- ivr_by_symbol: 後段実装 (現状 空 dict)

設計
----
- MoomooMarketDataAdapter(quote_ctx) で初期化
- get_environment() で都度 moomoo OpenD に query
- 失敗時は前回値 (cache) を返却・cache 不在時は安全な default

精度
----
VIX は SPY ATM straddle IV (誤差 0.5-1.0 ポイント・リアルタイム) で
公式 VIX feed (15 分遅延 yfinance) よりタイムリー性で勝る。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from atlas_v3.core.env_observer import MarketEnvironment
from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm

log = logging.getLogger(__name__)

# 安全な default (paper 開始時の cold start 用)
_DEFAULT_VIX = 16.0
_DEFAULT_VRP = 0.0
_DEFAULT_GEX = 0.0
_DEFAULT_TERM_RATIO = 1.0
_DEFAULT_BIAS = "neutral"
_DEFAULT_IVR_SPY = 40.0

# Cache TTL (60s tick interval を考慮)
_CACHE_TTL_SEC = 30.0


class MoomooMarketDataAdapter:
    """moomoo OpenD 経由で MarketEnvironment を構築する adapter.

    Phase 1 (本実装): VIX のみ実値・他 field は default
    Phase 2 (後段): VRP / bias / ivr_by_symbol を SPY history + option chain で計算
    """

    def __init__(
        self,
        quote_ctx,
        underlying_code: str = "US.SPY",
    ) -> None:
        self._quote_ctx = quote_ctx
        self._underlying_code = underlying_code
        self._cache_ts = 0.0
        self._cache_env: Optional[MarketEnvironment] = None

    def get_environment(self) -> MarketEnvironment:
        """MarketEnvironment を取得する (MarketDataClient Protocol 実装)."""
        now = time.monotonic()

        # Cache hit (TTL 内)
        if self._cache_env is not None and (now - self._cache_ts) < _CACHE_TTL_SEC:
            return self._cache_env

        # 1. VIX: SPY ATM straddle IV から推定
        vix = estimate_vix_from_spy_atm(self._quote_ctx, self._underlying_code)
        if vix is None:
            log.warning("[MoomooMarketDataAdapter] VIX 推定失敗・前回値 or default 使用")
            if self._cache_env is not None:
                return self._cache_env  # 前回値継続 (degraded mode)
            vix = _DEFAULT_VIX

        # 2. VRP / GEX / term_ratio / bias / ivr_by_symbol: 現状 default
        # (β-2 後段で SPY history HV / option chain gamma weight / IV slope / IV rank 実装)
        env = MarketEnvironment(
            vix=vix,
            vrp=_DEFAULT_VRP,
            gex=_DEFAULT_GEX,
            term_ratio=_DEFAULT_TERM_RATIO,
            bias=_DEFAULT_BIAS,
            ivr_by_symbol={"SPY": _DEFAULT_IVR_SPY},
        )

        # Cache update
        self._cache_env = env
        self._cache_ts = now
        return env
