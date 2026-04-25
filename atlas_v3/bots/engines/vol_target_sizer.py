"""atlas_v3/bots/engines/vol_target_sizer.py — Vol-Target サイジング (L10)

設計思想
--------
AQR "Understanding the Volatility-Targeted Strategy" (2012 SSRN):
ポートフォリオのリアライズドボラティリティを目標水準 (10-15%) に
維持するようポジションサイズを調整すると Sharpe +0.15 改善。

Dalio "All Weather" 内部メモ (1996): リスクパリティは vol-target が基盤。

ソース一次情報
--------------
- AQR paper "Volatility-Targeted Strategies" (SSRN 2012)
- Dalio "Principles" 2017 ch.5 risk parity
- top100_traders_gap_analysis_20260422.md S02 項
- Moreira-Muir JF 2017 DOI: 10.1111/jofi.12513

実装 (atlas_v3 namespace・spy_bot.py 書換禁止)
----------------------------------------------
- target_vol: 年率ボラ目標 (default 0.12 = 12%)
- realized_vol: N 日 daily return の std × sqrt(252)
- size_factor = min(1.0, target_vol / realized_vol)
- realized_vol が目標を超えたらサイズ縮小
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence

log = logging.getLogger(__name__)

# AQR 推奨目標ボラティリティ (年率)
DEFAULT_TARGET_VOL: float = 0.12   # 12%
DEFAULT_LOOKBACK_DAYS: int = 20    # 20 日 realized vol
ANNUALIZATION_FACTOR: float = math.sqrt(252)


@dataclass(frozen=True)
class VolTargetResult:
    """Vol-Target サイズ調整結果 DTO。"""
    size_factor: float
    realized_vol_ann: float    # 年率換算実現ボラ
    target_vol: float          # 目標ボラ
    daily_returns: int         # 使用したデータ点数
    reason: str
    source: str = "vol_target_sizer"  # 結果生成元 (test/外部 API 互換タグ)


class VolTargetSizer:
    """リアライズドボラから目標ボラ達成に必要なサイズ係数を算出する。

    例::
        sizer = VolTargetSizer(target_vol=0.12)
        # 20 日分の daily return (小数) を渡す
        daily_rets = [0.001, -0.005, 0.002, ...]
        result = sizer.compute(daily_rets)
        # result.size_factor = target_vol / realized_vol (cap 1.0)
    """

    def __init__(
        self,
        target_vol: float = DEFAULT_TARGET_VOL,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> None:
        assert 0 < target_vol < 1.0, f"target_vol={target_vol} out of (0, 1)"
        assert lookback_days >= 5, f"lookback_days={lookback_days} too small"
        self._target_vol = target_vol
        self._lookback = lookback_days

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def compute(self, daily_returns: Sequence[float]) -> VolTargetResult:
        """daily return 列から vol-target size factor を返す。

        Parameters
        ----------
        daily_returns: 日次リターン (小数) のリスト。0.01 = +1%。
        """
        if not daily_returns:
            return VolTargetResult(
                size_factor=1.0,
                realized_vol_ann=0.0,
                target_vol=self._target_vol,
                daily_returns=0,
                reason="no return data: size_factor=1.0 (no adjustment)",
            )

        # 直近 lookback 日分のみ使用
        recent = list(daily_returns)[-self._lookback:]
        n = len(recent)

        if n < 2:
            return VolTargetResult(
                size_factor=1.0,
                realized_vol_ann=0.0,
                target_vol=self._target_vol,
                daily_returns=n,
                reason=f"insufficient data n={n}: size_factor=1.0",
            )

        # 実現ボラ (日次 std × sqrt(252))
        # NaN を含むと statistics.stdev() が AttributeError を出すので事前除外。
        clean = [r for r in recent if not math.isnan(r)]
        if len(clean) < 2:
            return VolTargetResult(
                size_factor=1.0,
                realized_vol_ann=0.0,
                target_vol=self._target_vol,
                daily_returns=len(clean),
                reason="insufficient finite returns after NaN filter: size_factor=1.0",
            )
        daily_std = statistics.stdev(clean)
        realized_vol_ann = daily_std * ANNUALIZATION_FACTOR

        if realized_vol_ann <= 0.0 or math.isnan(realized_vol_ann):
            return VolTargetResult(
                size_factor=1.0,
                realized_vol_ann=0.0,
                target_vol=self._target_vol,
                daily_returns=n,
                reason=f"realized_vol=0 or NaN: size_factor=1.0",
            )

        # AQR 式: min(1.0, target / realized)
        raw_factor = self._target_vol / realized_vol_ann
        size_factor = min(1.0, raw_factor)

        return VolTargetResult(
            size_factor=size_factor,
            realized_vol_ann=realized_vol_ann,
            target_vol=self._target_vol,
            daily_returns=n,
            reason=(
                f"realized_vol_ann={realized_vol_ann:.4f} "
                f"target={self._target_vol:.4f}: "
                f"size_factor={size_factor:.3f}"
            ),
        )
