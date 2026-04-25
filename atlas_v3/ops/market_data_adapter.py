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
from atlas_v3.ops.vix_estimator import estimate_vix_from_spy_atm, estimate_term_ratio
from atlas_v3.ops.gex_estimator import estimate_gex_from_moomoo
from atlas_v3.ops.realized_volatility import estimate_hv_from_moomoo, estimate_ivr_proxy_from_hv_history

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

# bias 推定閾値 (SPY MA20 比 + VIX/VVIX 構造)
_BIAS_BULL_VIX_MAX = 18.0  # VIX < 18 で bull バイアス候補
_BIAS_BEAR_VIX_MIN = 25.0  # VIX > 25 で bear バイアス候補


class MoomooMarketDataAdapter:
    """moomoo OpenD 経由で MarketEnvironment を構築する adapter.

    実装範囲:
    - vix: SPY ATM straddle IV 推定 (vix_estimator)
    - vrp: VIX - HV (HV = SPY 30 日 history std × sqrt(252) × 100)
    - bias: VIX 帯から推定 (低 VIX → bull / 高 VIX → bear / 中間 → neutral)
    - gex: 0.0 (option chain gamma weight 後段実装)
    - term_ratio: 1.0 (異 DTE IV slope 後段実装)
    - ivr_by_symbol: 252 日 ATM IV percentile 後段実装 (現状 default)
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

    def _estimate_bias(self, vix: float, term_ratio: Optional[float]) -> str:
        """VIX 帯 + term_ratio で bias 推定.

        term_ratio (= 近 DTE IV / 遠 DTE IV) が **明確な signal** の場合のみ採用:
        - < 0.85: contango (短期 IV が低い = 上昇 bias 候補)
        - > 1.05: backwardation (短期 IV が高い = 下降 bias 候補)
        - 0.85-1.05 中間域は VIX 帯のみで判定 (default 1.0 で誤 bear 判定を回避)
        """
        if term_ratio is not None:
            if term_ratio < 0.85 and vix < _BIAS_BEAR_VIX_MIN:
                return "bull"
            if term_ratio > 1.05:
                return "bear"
        # VIX 帯 fallback
        if vix < _BIAS_BULL_VIX_MAX:
            return "bull"
        if vix > _BIAS_BEAR_VIX_MIN:
            return "bear"
        return "neutral"

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

        # 2. VRP = VIX - HV (HV = SPY 30 日 history 年率 std%)
        hv = estimate_hv_from_moomoo(self._quote_ctx, self._underlying_code)
        if hv is not None:
            vrp = vix - hv
            log.info(
                "[MoomooMarketDataAdapter] VRP=%.2f (VIX=%.2f - HV=%.2f)",
                vrp, vix, hv,
            )
        else:
            vrp = _DEFAULT_VRP

        # 3. term_ratio: 異 DTE IV slope (9 日 / 30 日 ATM IV 比)
        term_ratio = estimate_term_ratio(
            self._quote_ctx, self._underlying_code, near_dte=9, far_dte=30,
        )
        if term_ratio is None:
            term_ratio = _DEFAULT_TERM_RATIO

        # 4. bias: VIX 帯 + term_ratio から推定
        bias = self._estimate_bias(vix, term_ratio)

        # 5. ivr_by_symbol: 252 日 HV percentile rank で IVR proxy
        ivr = estimate_ivr_proxy_from_hv_history(self._quote_ctx, self._underlying_code)
        ivr_dict = {"SPY": ivr if ivr is not None else _DEFAULT_IVR_SPY}

        # 6. GEX: option chain gamma × OI × spot² 集計 (BS gamma fallback)
        gex = estimate_gex_from_moomoo(self._quote_ctx, self._underlying_code)
        if gex is None:
            gex = _DEFAULT_GEX

        env = MarketEnvironment(
            vix=vix,
            vrp=vrp,
            gex=gex,
            term_ratio=term_ratio,
            bias=bias,
            ivr_by_symbol=ivr_dict,
        )

        # Cache update
        self._cache_env = env
        self._cache_ts = now
        return env
