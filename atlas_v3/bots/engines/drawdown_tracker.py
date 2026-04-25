"""atlas_v3/bots/engines/drawdown_tracker.py — DD追跡 (peak ratio) (L04)

設計思想
--------
Ray Dalio "Principles" 2017 ch.5: 5% DD 到達でポジションサイズ削減。
Millennium LP Letter 2023: 5% pod DD で shutdown。
atlas_v3 における DD 可視化と段階的 size 調整の物理ベース。

ソース一次情報
--------------
- Dalio "Principles" (2017) ch.5 vol-target + DD-aware sizing
- Millennium LP letter 2023 (top100_traders_gap_analysis S06/D03)
- research_atlas_trader_gap_v2.md G-NEW6 項

実装 (atlas_v3 namespace・spy_bot.py 書換禁止)
----------------------------------------------
- peak_equity: 起動時の初期値から追跡
- current_dd_pct: (peak - current) / peak * 100
- size_factor: DD 閾値に応じて段階的低減
  - < 3%: 1.0
  - 3-5%: 0.75
  - 5-8%: 0.5
  - > 8%: 0.25
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# DD 段階別 size_factor (Dalio/Millennium 基準)
_DD_LEVELS: list[tuple[float, float]] = [
    (3.0, 1.0),   # < 3%: フルサイズ
    (5.0, 0.75),  # 3-5%: 25% 削減
    (8.0, 0.50),  # 5-8%: 50% 削減
    (float("inf"), 0.25),  # > 8%: 75% 削減
]


@dataclass(frozen=True)
class DrawdownSnapshot:
    """DD スナップショット DTO。"""
    peak_equity: float
    current_equity: float
    dd_usd: float
    dd_pct: float
    size_factor: float
    level_label: str

    @property
    def drawdown_pct(self) -> float:
        """dd_pct のエイリアス (test API)."""
        return self.dd_pct

    @property
    def dd_level(self) -> str:
        """level_label のエイリアス (test API)."""
        return self.level_label


class DrawdownTracker:
    """ピーク資産比 DD を追跡してサイズ係数を動的に返す。

    例::
        tracker = DrawdownTracker(initial_equity=100_000.0)
        tracker.update(98_000.0)   # -2%: size_factor=1.0
        tracker.update(94_500.0)   # -5.5%: size_factor=0.5
        snap = tracker.snapshot()
    """

    def __init__(self, initial_equity: float) -> None:
        assert initial_equity > 0, f"initial_equity={initial_equity} must be > 0"
        self._peak: float = initial_equity
        self._current: float = initial_equity

    # ------------------------------------------------------------------
    # 状態更新
    # ------------------------------------------------------------------

    def update(self, current_equity: float) -> DrawdownSnapshot:
        """現在の資産残高を記録してスナップショットを返す。

        peak は常に高値を記録し続ける (trailing peak)。
        """
        assert not math.isnan(current_equity), "current_equity is NaN"
        assert not math.isinf(current_equity), "current_equity is inf"
        assert current_equity >= 0, f"current_equity={current_equity} < 0"

        self._current = current_equity
        if current_equity > self._peak:
            self._peak = current_equity
            log.debug("drawdown_tracker: new peak=%.2f", self._peak)

        return self.snapshot()

    # ------------------------------------------------------------------
    # スナップショット
    # ------------------------------------------------------------------

    def snapshot(self) -> DrawdownSnapshot:
        """現在の DD 状態スナップショットを返す (状態変更なし)。"""
        dd_usd = self._peak - self._current
        dd_pct = (dd_usd / self._peak * 100.0) if self._peak > 0 else 0.0

        size_factor, label = self._resolve_factor(dd_pct)

        return DrawdownSnapshot(
            peak_equity=self._peak,
            current_equity=self._current,
            dd_usd=dd_usd,
            dd_pct=dd_pct,
            size_factor=size_factor,
            level_label=label,
        )

    # ------------------------------------------------------------------
    # 内部ヘルパー
    # ------------------------------------------------------------------

    def _resolve_factor(self, dd_pct: float) -> tuple[float, str]:
        prev_threshold = 0.0
        for threshold, factor in _DD_LEVELS:
            if dd_pct < threshold:
                label = (
                    f"DD={dd_pct:.2f}% (< {threshold:.1f}%): size_factor={factor:.2f}"
                )
                return factor, label
            prev_threshold = threshold
        # 最後の段階
        factor = _DD_LEVELS[-1][1]
        return factor, f"DD={dd_pct:.2f}%: size_factor={factor:.2f}"

    # ------------------------------------------------------------------
    # アクセサ
    # ------------------------------------------------------------------

    @property
    def peak_equity(self) -> float:
        return self._peak

    @property
    def current_equity(self) -> float:
        return self._current

    @property
    def current_dd_pct(self) -> float:
        """現在の DD %。"""
        return (self._peak - self._current) / self._peak * 100.0 if self._peak > 0 else 0.0
