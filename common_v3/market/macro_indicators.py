"""common_v3/market/macro_indicators.py — マクロ市場指標取得 (L06/L07/L08)

設計思想
--------
優秀トレーダーが毎朝確認するマクロ指標を無料 API (Yahoo Finance) で取得:
- ^TNX  : 10年 Treasury 利回り (リスクオン/オフ判断)
- ^PCCE : Put/Call Ratio Equity (センチメント指標)
- ^SKEW : SKEW 指数 (テールリスク市場認識)

ソース一次情報
--------------
- L06: research_atlas_trader_gap_v2.md G-NEW3: "債券市場(^TNX)の監視"
- L07: research_atlas_trader_gap_v2.md G-NEW4: "Put/Call ratio (CBOE 無料)"
- L08: research_atlas_trader_gap_v2.md G-NEW11: "SKEW 指数 (Yahoo ^SKEW 無料)"
- Yahoo Finance 無料 API (regularMarketPrice) で全件取得可能

実装 (common_v3 namespace・common/ 書換禁止)
--------------------------------------------
- MacroIndicators.fetch() → MacroSnapshot
- yfinance 経由・fallback は None
- キャッシュ TTL: 10 分 (60 分以内の再利用)
"""
from __future__ import annotations

import datetime
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# Yahoo Finance シンボル
_TNX_SYMBOL: str = "^TNX"    # 10年 Treasury 利回り
_PCE_SYMBOL: str = "^PCCE"   # Put/Call Ratio Equity (CBOE)
_SKEW_SYMBOL: str = "^SKEW"  # SKEW Index

_CACHE_TTL_SEC: int = 600  # 10 分キャッシュ


@dataclass
class MacroSnapshot:
    """マクロ指標スナップショット DTO。

    fields (本名):
        fetched_at: Unix timestamp
        tnx_yield_pct: 10年利回り (%)
        put_call_ratio: Put/Call Ratio
        skew_index: SKEW Index
        source: データソース名 ("yfinance" / "fallback" / "mock" 等)

    aliases (test/外部 API 互換):
        tnx_yield ↔ tnx_yield_pct
        pc_ratio_equity ↔ put_call_ratio
    """
    fetched_at: float
    tnx_yield_pct: Optional[float] = None
    put_call_ratio: Optional[float] = None
    skew_index: Optional[float] = None
    source: str = "yfinance"
    tnx_yield: Optional[float] = None
    pc_ratio_equity: Optional[float] = None

    def __post_init__(self) -> None:
        # 双方向同期: どちらの名前で作っても両方揃う
        if self.tnx_yield is None and self.tnx_yield_pct is not None:
            self.tnx_yield = self.tnx_yield_pct
        elif self.tnx_yield_pct is None and self.tnx_yield is not None:
            self.tnx_yield_pct = self.tnx_yield
        if self.pc_ratio_equity is None and self.put_call_ratio is not None:
            self.pc_ratio_equity = self.put_call_ratio
        elif self.put_call_ratio is None and self.pc_ratio_equity is not None:
            self.put_call_ratio = self.pc_ratio_equity

    # 解釈補助
    @property
    def tnx_risk_signal(self) -> str:
        """TNX の方向シグナル。"high_yield" / "elevated_yield" / "normal" / "unknown"。"""
        if self.tnx_yield_pct is None:
            return "unknown"
        if self.tnx_yield_pct > 5.0:
            return "high_yield"
        if self.tnx_yield_pct > 4.5:
            return "elevated_yield"
        return "normal"

    @property
    def pc_ratio_signal(self) -> str:
        """Put/Call Ratio のセンチメント解釈。
        "extreme_bearish" / "bearish" / "neutral" / "bullish" / "unknown"。"""
        if self.put_call_ratio is None:
            return "unknown"
        if self.put_call_ratio > 1.2:
            return "extreme_bearish"
        if self.put_call_ratio > 0.9:
            return "bearish"
        if self.put_call_ratio < 0.6:
            return "bullish"
        return "neutral"

    @property
    def skew_signal(self) -> str:
        """SKEW 指数の解釈。"elevated_tail_risk" / "normal" / "low_tail_risk" / "unknown"。"""
        if self.skew_index is None:
            return "unknown"
        if self.skew_index > 140:
            return "elevated_tail_risk"
        if self.skew_index < 115:
            return "low_tail_risk"
        return "normal"

    def is_fresh(self, ttl_sec: int = _CACHE_TTL_SEC) -> bool:
        """TTL 内に取得されたデータかを返す。"""
        return (time.time() - self.fetched_at) < ttl_sec


class MacroIndicators:
    """Yahoo Finance 経由でマクロ指標を取得するクライアント。

    例::
        mi = MacroIndicators()
        snap = mi.fetch()
        print(snap.tnx_yield_pct)  # e.g. 4.32
        print(snap.tnx_risk_signal)  # "neutral"
    """

    def __init__(self) -> None:
        self._cache: Optional[MacroSnapshot] = None

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def fetch(self, use_cache: bool = True) -> MacroSnapshot:
        """マクロ指標を取得してスナップショットを返す。

        Parameters
        ----------
        use_cache: True=キャッシュ TTL 内なら再利用
        """
        if use_cache and self._cache is not None and self._cache.is_fresh():
            log.debug("macro_indicators: using cached snapshot")
            return self._cache

        # _fetch_yfinance が失敗 (yfinance 不在 / network) なら fallback snapshot
        try:
            snap = self._fetch_yfinance()
        except Exception as exc:
            log.warning("macro_indicators: yfinance fetch failed (%s) → fallback", exc)
            snap = MacroSnapshot(
                fetched_at=time.time(),
                tnx_yield_pct=None,
                put_call_ratio=None,
                skew_index=None,
                source="fallback",
            )
        self._cache = snap
        return snap

    # ------------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------------

    def _fetch_yfinance(self) -> MacroSnapshot:
        """Yahoo Finance から新規取得 (test patch target)。"""
        tnx = self._fetch_price(_TNX_SYMBOL)
        pce = self._fetch_price(_PCE_SYMBOL)
        skew = self._fetch_price(_SKEW_SYMBOL)

        snap = MacroSnapshot(
            fetched_at=time.time(),
            tnx_yield_pct=tnx,
            put_call_ratio=pce,
            skew_index=skew,
            source="yfinance",
        )
        log.info(
            "macro_indicators fetched: TNX=%.3f PCR=%.3f SKEW=%.1f",
            tnx or 0.0,
            pce or 0.0,
            skew or 0.0,
        )
        return snap

    @staticmethod
    def _fetch_price(symbol: str) -> Optional[float]:
        """Yahoo Finance から価格を取得。失敗時は None を返す (silent fallback)。"""
        try:
            import yfinance as yf  # type: ignore
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            price = getattr(info, "last_price", None)
            if price is not None and not math.isnan(float(price)):
                return float(price)
            # fast_info が None の場合は history で取得
            hist = ticker.history(period="1d", interval="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
            return None
        except Exception as exc:
            log.warning("macro_indicators: failed to fetch %s: %s", symbol, exc)
            return None
